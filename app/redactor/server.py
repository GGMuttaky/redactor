# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""Local HTTP server: the review page, a JSON API and the preview video.

Bound to 127.0.0.1 only. Every API and media request must carry the per-run token
that is embedded in the page, and the Host header must be this server: without
that, any website open in the same browser could drive the API (it can open files
and read local videos), because browsers let pages send requests to localhost.
"""
import json
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, analyze, media, render
from .detect import gpu_available
from .project import SENSITIVITY, Project, home

WEB = Path(__file__).resolve().parent / "web"
TOKEN = secrets.token_urlsafe(24)
BUILD = f"{__version__}-{secrets.token_hex(4)}"  # new per start: browsers refetch scripts after an update
VIDEO_TYPES = "*.mp4 *.mov *.m4v *.mkv *.avi *.mts *.m2ts *.webm *.wmv *.mpg *.mpeg"

PICK_SCRIPT = f"""
import tkinter as tk
from tkinter import filedialog
root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)
path = filedialog.askopenfilename(title='Choose a video to redact',
    filetypes=[('Video files', '{VIDEO_TYPES}'), ('All files', '*.*')])
print(path or '')
"""


class Handler(BaseHTTPRequestHandler):
    server_version = f"Redactor/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet: the console is for errors
        pass

    # ---------------------------------------------------------------- plumbing

    def _send(self, code, body=b"", ctype="application/json", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, code=200):
        self._send(code, json.dumps(data).encode())

    def _error(self, code, msg):
        self._json({"error": msg}, code)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _host_ok(self):
        # Rejects DNS-rebinding: a hostile domain pointed at 127.0.0.1 arrives with its own Host header.
        return (self.headers.get("Host") or "").rsplit(":", 1)[0] in ("127.0.0.1", "localhost")

    def _allowed(self, query):
        if not self._host_ok():
            return False
        token = self.headers.get("X-Token") or (query.get("token") or [""])[0]
        return secrets.compare_digest(token, TOKEN)

    def _file(self, path: Path, ctype=None):
        """Serve a file with Range support (the video element seeks with ranges)."""
        if not path.is_file():
            return self._error(404, "Not found")
        size = path.stat().st_size
        ctype = ctype or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and (m := re.match(r"bytes=(\d*)-(\d*)", rng)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            elif m.group(2):
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                return self._send(416, headers={"Content-Range": f"bytes */{size}"})
        length = end - start + 1
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")  # an app update must not run against a stale script
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as fh:
            fh.seek(start)
            left = length
            try:
                while left > 0:
                    chunk = fh.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                pass  # the player abandoned this range; normal while scrubbing

    # ---------------------------------------------------------------- routing

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        parts = [p for p in url.path.split("/") if p]
        if not self._host_ok():
            return self._error(403, "Forbidden")
        if url.path == "/":
            page = (WEB / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", TOKEN).replace("__BUILD__", BUILD)
            return self._send(200, page.encode(), "text/html; charset=utf-8")
        if parts[:1] == ["static"] and len(parts) == 2 and re.fullmatch(r"[\w.-]+", parts[1]):
            return self._file(WEB / parts[1])
        if not self._allowed(q):
            return self._error(403, "Forbidden")
        try:
            if parts[:1] == ["media"] and len(parts) >= 3:
                p = Project.get(parts[1])
                if parts[2] == "proxy.mp4":
                    return self._file(p.dir / "proxy.mp4", "video/mp4")
                if parts[2] == "thumbs" and len(parts) == 4 and re.fullmatch(r"\d+\.jpg", parts[3]):
                    return self._file(p.dir / "thumbs" / parts[3], "image/jpeg")
                return self._error(404, "Not found")
            if parts == ["api", "status"]:
                ok = bool(media.find_tool("ffmpeg")) and bool(media.find_tool("ffprobe"))
                return self._json({"version": __version__, "ffmpeg": ok, "ffprobe": ok, "home": str(home()),
                                   "gpu": gpu_available(), "encoder": media.pick_encoder()[1] if ok else None})
            if parts == ["api", "projects"]:
                return self._json(Project.list_all())
            if parts[:2] == ["api", "projects"] and len(parts) >= 3:
                p = Project.get(parts[2])
                sub = parts[3] if len(parts) > 3 else ""
                if sub == "":
                    with p.lock:  # the job thread edits this dict while we serialise it
                        body = json.dumps(p.data).encode()
                    return self._send(200, body)
                if sub == "tracks":
                    return self._json({"tracks": p.track_summary, "disabled": p.data["disabled_tracks"]})
                if sub == "times":
                    return self._json(p.times)
                if sub == "density":
                    return self._json(p.density())
                if sub == "boxes":
                    start = int(q.get("start", ["0"])[0])
                    end = min(int(q.get("end", ["0"])[0]), start + 5000)
                    return self._json(p.window(start, end))
            return self._error(404, "Not found")
        except KeyError:
            return self._error(404, "No such project")

    def do_POST(self):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        if not self._allowed(parse_qs(url.query)):
            return self._error(403, "Forbidden")
        try:
            body = self._body()
            if parts == ["api", "pick"]:
                out = subprocess.run([sys.executable, "-c", PICK_SCRIPT], capture_output=True, text=True,
                                     creationflags=media.NO_WINDOW)
                return self._json({"path": out.stdout.strip() or None})
            if parts == ["api", "projects"]:
                sens = body.get("sensitivity", "normal")
                if sens not in SENSITIVITY:
                    return self._error(400, "Unknown sensitivity")
                p = Project.create(body.get("path", ""), sens, int(body.get("stride", 1)))
                if p.data["status"] in ("new", "error", "cancelled"):
                    p.update(status="queued")
                    analyze.submit(analyze.analyze, p)
                return self._json(p.data)
            if parts[:2] == ["api", "projects"] and len(parts) == 4:
                p = Project.get(parts[2])
                sub = parts[3]
                if sub == "analyze":
                    if p.data["status"] in ("queued", "analyzing"):
                        return self._error(409, "Already analysing")
                    sens = body.get("sensitivity", p.data["settings"]["sensitivity"])
                    p.data["settings"] = {"sensitivity": sens, "conf": SENSITIVITY[sens],
                                          "stride": int(body.get("stride", p.data["settings"]["stride"]))}
                    p.update(status="queued", disabled_tracks=[])
                    analyze.submit(analyze.analyze, p)
                    return self._json(p.data)
                if sub == "cancel":
                    p.cancel = True
                    return self._json({"ok": True})
                if sub == "tracks":
                    p.set_tracks_enabled([int(i) for i in body.get("ids", [])], bool(body.get("enabled")))
                    return self._json({"disabled": p.data["disabled_tracks"]})
                if sub == "regions":
                    return self._json(p.upsert_region(body))
                if sub == "style":
                    p.update(style={"mode": body.get("mode", "blur"), "shape": body.get("shape", "ellipse")})
                    return self._json(p.data["style"])
                if sub == "export":
                    if p.data["status"] != "ready":
                        return self._error(409, "Analysis is not finished")
                    job = p.data.get("export_job") or {}
                    if job.get("status") == "running":
                        return self._error(409, "An export is already running")
                    mode, shape = body.get("mode", "blur"), body.get("shape", "ellipse")
                    if mode not in ("blur", "pixelate", "solid") or shape not in ("ellipse", "rect"):
                        return self._error(400, "Unknown style")
                    p.update(style={"mode": mode, "shape": shape},
                             export_job={"status": "running", "progress": 0, "stage": "Waiting"})
                    analyze.submit(render.export, p, mode, shape)
                    return self._json({"ok": True})
                if sub in ("reveal", "open"):
                    # Only files this app wrote: never an arbitrary path from the request.
                    target = body.get("path", "")
                    allowed = {e.get("path") for e in p.data.get("exports", [])} | \
                              {e.get("report") for e in p.data.get("exports", [])}
                    if target not in allowed or not Path(target).exists():
                        return self._error(403, "Not an export of this project")
                    if sub == "reveal":
                        subprocess.Popen(["explorer", "/select,", os.path.normpath(target)])
                    else:
                        os.startfile(target)  # an .html report or .mp4 export, opened in its default app
                    return self._json({"ok": True})
            return self._error(404, "Not found")
        except KeyError:
            return self._error(404, "No such project")
        except (FileNotFoundError, ValueError) as e:
            return self._error(400, str(e))

    def do_DELETE(self):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        if not self._allowed(parse_qs(url.query)):
            return self._error(403, "Forbidden")
        try:
            if parts[:2] == ["api", "projects"] and len(parts) == 5 and parts[3] == "regions":
                Project.get(parts[2]).delete_region(int(parts[4]))
                return self._json({"ok": True})
            return self._error(404, "Not found")
        except KeyError:
            return self._error(404, "No such project")


def serve(port=0, open_browser=True):
    Project.recover_interrupted()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"Redactor {__version__} running at {url}  (close this window to quit)", flush=True)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
