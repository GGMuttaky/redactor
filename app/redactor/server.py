# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""Local HTTP server: the review page, a JSON API and the preview video.

Bound to 127.0.0.1 only. Browsers let any website send requests to localhost, so:
  - every API and media request must carry the per-run token that is embedded in
    the page (other sites cannot read the page, so they cannot learn it);
  - the Host header must be exactly this server (defeats DNS rebinding);
  - responses forbid framing and carry a strict content security policy, so another
    site cannot embed the app and trick clicks on it.
"""
import json
import logging
import logging.handlers
import os
import re
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, analyze, media, render
from .detect import gpu_available
from .project import SENSITIVITY, Project, home

log = logging.getLogger("redactor")

WEB = Path(__file__).resolve().parent / "web"
STATIC = {"app.js": "text/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8"}
TOKEN = secrets.token_urlsafe(24)
BUILD = f"{__version__}-{secrets.token_hex(4)}"  # new per start: browsers refetch scripts after an update
MAX_BODY = 1_000_000
BOXES_WINDOW = 5000  # most frames of boxes one request may ask for

SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                                "media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                                "form-action 'none'"),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
}

VIDEO_TYPES = "*.mp4 *.mov *.m4v *.mkv *.avi *.mts *.m2ts *.webm *.wmv *.mpg *.mpeg"
PICK_SCRIPT = f"""
import tkinter as tk
from tkinter import filedialog
root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)
path = filedialog.askopenfilename(title='Choose a video to redact',
    filetypes=[('Video files', '{VIDEO_TYPES}'), ('All files', '*.*')])
print(path or '')
"""


class HttpError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _project(pid):
    try:
        return Project.get(pid)
    except KeyError:
        raise HttpError(404, "No such project.") from None


def _busy(p):
    job = p.data.get("export_job") or {}
    return p.data.get("status") in ("queued", "analyzing"), job.get("status") in ("queued", "running")


class Handler(BaseHTTPRequestHandler):
    server_version = f"Redactor/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # requests are not logged: media URLs carry the token
        pass

    # ---------------------------------------------------------------- plumbing

    def _headers(self, code, ctype, length, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        self._headers(code, ctype, len(body), {"Cache-Control": "no-store", **(extra or {})})
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, code=200):
        self._send(code, json.dumps(data).encode("utf-8"))

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise HttpError(400, "Bad Content-Length.") from None
        if n < 0:
            raise HttpError(400, "Bad Content-Length.")
        if n > MAX_BODY:
            raise HttpError(413, "Request too large.")
        if not n:
            return {}
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise HttpError(400, "The request was not valid JSON.") from None
        if not isinstance(body, dict):
            raise HttpError(400, "The request must be a JSON object.")
        return body

    def _host_ok(self):
        # Exactly this server: a hostile domain re-pointed at 127.0.0.1 (DNS rebinding)
        # arrives with its own name in the Host header.
        port = self.server.server_address[1]
        return (self.headers.get("Host") or "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _token_ok(self, query):
        token = self.headers.get("X-Token") or (query.get("token") or [""])[0]
        return secrets.compare_digest(token.encode("utf-8"), TOKEN.encode("utf-8"))

    def _file(self, path: Path, ctype):
        """Serve a file, with single-range support (the video element seeks with ranges)."""
        if not path.is_file():
            raise HttpError(404, "Not found.")
        size = path.stat().st_size
        start, end, partial = 0, size - 1, False
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if m and (m.group(1) or m.group(2)) and size:
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end:
                return self._headers(416, ctype, 0, {"Content-Range": f"bytes */{size}"})
            partial = True
        length = max(0, end - start + 1)
        extra = {"Accept-Ranges": "bytes", "Cache-Control": "no-cache"}
        if partial:
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
        self._headers(206 if partial else 200, ctype, length, extra)
        if self.command == "HEAD" or not length:
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

    def _dispatch(self, route):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        parts = [p for p in url.path.split("/") if p]
        try:
            if not self._host_ok():
                raise HttpError(403, "Forbidden.")
            if self.command in ("GET", "HEAD") and url.path == "/":
                page = ((WEB / "index.html").read_text(encoding="utf-8")
                        .replace("__TOKEN__", TOKEN).replace("__BUILD__", BUILD))
                return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            if self.command in ("GET", "HEAD") and len(parts) == 2 and parts[0] == "static" and parts[1] in STATIC:
                return self._file(WEB / parts[1], STATIC[parts[1]])
            if not self._token_ok(query):
                raise HttpError(403, "Forbidden.")
            return route(parts, query)
        except HttpError as e:
            self._json({"error": str(e)}, e.code)
        except (ValueError, FileNotFoundError) as e:  # bad input: say what was wrong
            self._json({"error": str(e)}, 400)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass
        except Exception:
            log.exception("%s %s failed", self.command, url.path)
            self._json({"error": "Something went wrong inside Redactor; details are in redactor.log."}, 500)

    def do_HEAD(self):
        self._dispatch(self._get)

    def do_GET(self):
        self._dispatch(self._get)

    def do_POST(self):
        self._dispatch(self._post)

    def do_DELETE(self):
        self._dispatch(self._delete)

    # ---------------------------------------------------------------- routes

    def _get(self, parts, query):
        if parts[:1] == ["media"] and len(parts) >= 3:
            p = _project(parts[1])
            if parts[2:] == ["proxy.mp4"]:
                return self._file(p.dir / "proxy.mp4", "video/mp4")
            if len(parts) == 4 and parts[2] == "thumbs" and re.fullmatch(r"\d+\.jpg", parts[3]):
                return self._file(p.dir / "thumbs" / parts[3], "image/jpeg")
            raise HttpError(404, "Not found.")
        if parts == ["api", "status"]:
            problem = media.tools_problem()
            ok = problem is None
            return self._json({"version": __version__, "ffmpeg": ok, "ffprobe": ok, "ffmpeg_problem": problem,
                               "home": str(home()), "gpu": gpu_available(),
                               "encoder": media.pick_encoder()[1] if ok else None})
        if parts == ["api", "projects"]:
            return self._json(Project.list_all())
        if parts[:2] == ["api", "projects"] and len(parts) in (3, 4):
            p = _project(parts[2])
            sub = parts[3] if len(parts) == 4 else ""
            if sub == "":
                return self._send(200, p.to_json().encode("utf-8"))
            if sub == "tracks":
                return self._json({"tracks": p.track_summary, "disabled": p.data["disabled_tracks"]})
            if sub == "times":
                return self._json(p.times)
            if sub == "density":
                return self._json(p.density())
            if sub == "boxes":
                try:
                    start = max(0, int(query.get("start", ["0"])[0]))
                    end = min(int(query.get("end", ["0"])[0]), start + BOXES_WINDOW)
                except ValueError:
                    raise HttpError(400, "start and end must be frame numbers.") from None
                return self._json(p.window(start, end))
        raise HttpError(404, "Not found.")

    def _post(self, parts, query):
        body = self._body()
        if parts == ["api", "pick"]:
            return self._json({"path": _pick_file()})
        if parts == ["api", "projects"]:
            p = Project.create(body.get("path", ""), body.get("sensitivity", "normal"), body.get("stride", 1))
            if p.data["status"] in ("new", "error", "cancelled"):
                _queue_analysis(p)
            return self._send(200, p.to_json().encode("utf-8"))
        if parts[:2] != ["api", "projects"] or len(parts) != 4:
            raise HttpError(404, "Not found.")
        p = _project(parts[2])
        analysing, exporting = _busy(p)
        sub = parts[3]
        if sub == "analyze":
            if analysing:
                raise HttpError(409, "This video is already being analysed.")
            if exporting:
                raise HttpError(409, "Wait for the export to finish, then analyse again.")
            sens = body.get("sensitivity", p.data["settings"]["sensitivity"])
            stride = body.get("stride", p.data["settings"]["stride"])
            if sens not in SENSITIVITY or stride not in (1, 2):
                raise HttpError(400, "Unknown sensitivity or speed.")
            with p.lock:
                p.data["settings"] = {"sensitivity": sens, "conf": SENSITIVITY[sens], "stride": stride}
                p.data["disabled_tracks"] = []
            _queue_analysis(p)
            return self._send(200, p.to_json().encode("utf-8"))
        if sub == "cancel":
            which = body.get("job")
            if which not in ("analysis", "export"):
                raise HttpError(400, "Say which job to stop: analysis or export.")
            (p.cancel_analysis if which == "analysis" else p.cancel_export).set()
            return self._json({"ok": True})
        if analysing:
            raise HttpError(409, "Analysis is not finished.")
        if sub == "tracks":
            ids = body.get("ids", [])
            if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
                raise HttpError(400, "ids must be a list of face numbers.")
            p.set_tracks_enabled(ids, bool(body.get("enabled")))
            return self._json({"disabled": p.data["disabled_tracks"]})
        if sub == "regions":
            return self._json(p.upsert_region(body))
        if sub == "style":
            p.set_style(body.get("mode"), body.get("shape"))
            return self._json(p.data["style"])
        if sub == "export":
            if p.data["status"] != "ready":
                raise HttpError(409, "Analysis is not finished.")
            if exporting:
                raise HttpError(409, "An export is already running.")
            mode, shape = body.get("mode"), body.get("shape")
            p.set_style(mode, shape)  # validates
            snap = p.snapshot()  # exactly what the user sees now; the export and its report both use it
            p.cancel_export = threading.Event()
            p.update(export_job={"status": "queued", "progress": 0, "stage": "Waiting"})
            analyze.submit(render.export, p, mode, shape, snap)
            return self._json({"ok": True})
        if sub in ("reveal", "open"):
            target = str(body.get("path", ""))
            _launch(p, target, reveal=(sub == "reveal"))
            return self._json({"ok": True})
        raise HttpError(404, "Not found.")

    def _delete(self, parts, query):
        if parts[:2] == ["api", "projects"] and len(parts) == 3:
            _project(parts[2])
            try:
                Project.delete(parts[2])
            except RuntimeError as e:
                raise HttpError(409, str(e)) from None
            return self._json({"ok": True})
        if parts[:2] == ["api", "projects"] and len(parts) == 5 and parts[3] == "regions":
            try:
                rid = int(parts[4])
            except ValueError:
                raise HttpError(400, "Region ids are numbers.") from None
            _project(parts[2]).delete_region(rid)
            return self._json({"ok": True})
        raise HttpError(404, "Not found.")


# ---------------------------------------------------------------- actions


def _queue_analysis(p):
    p.cancel_analysis = threading.Event()  # a stop pressed while queued must count
    p.update(status="queued", error=None, progress={"stage": "Queued", "value": 0.0})
    analyze.submit(analyze.analyze, p)


def _pick_file():
    """Native file dialog, run in a child process (tkinter needs its own main thread)."""
    out = media.run([sys.executable, "-X", "utf8", "-c", PICK_SCRIPT])
    if out.returncode != 0:
        log.warning("file picker failed: %s", out.stderr.strip()[-300:])
        raise HttpError(500, "The file picker could not open. Paste the file's path instead.")
    return out.stdout.strip() or None


def _launch(p, target, reveal):
    """Show an export in Explorer or open it. Only files this app wrote for this project."""
    allowed = set()
    for e in p.data.get("exports", []):
        allowed.update(os.path.normcase(os.path.abspath(x)) for x in (e.get("path"), e.get("report")) if x)
    key = os.path.normcase(os.path.abspath(target)) if target else ""
    if key not in allowed or Path(target).suffix.lower() not in (".mp4", ".html") or not Path(target).is_file():
        raise HttpError(403, "That is not an export of this project, or it has been moved.")
    if reveal:
        subprocess.Popen(["explorer", "/select,", os.path.normpath(target)])
    else:
        os.startfile(target)  # a .mp4 export or .html report, in its default app


# ---------------------------------------------------------------- startup

def _setup_logging():
    root = logging.getLogger("redactor")
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(home() / "redactor.log", maxBytes=1_000_000, backupCount=2,
                                              encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
    root.addHandler(fh)
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    root.addHandler(console)


class _Instance:
    """One Redactor per user: a second launch opens the running one instead of starting another,
    which would fight the first over the same projects."""

    def __init__(self):
        self.lock_path, self.info_path, self.fh = home() / "instance.lock", home() / "instance.json", None

    def acquire(self):
        self.fh = open(self.lock_path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.fh.close()
            self.fh = None
            return False

    def running_url(self):
        try:
            return json.loads(self.info_path.read_text(encoding="utf-8")).get("url")
        except (OSError, ValueError):
            return None

    def publish(self, url):
        self.info_path.write_text(json.dumps({"url": url, "pid": os.getpid()}), encoding="utf-8")


def serve(port=0, open_browser=True):
    _setup_logging()
    instance = _Instance()
    if not instance.acquire():
        url = instance.running_url()
        print(f"Redactor is already running{f' at {url}' if url else ''}.", flush=True)
        if url and open_browser:
            webbrowser.open(url)
        return
    Project.recover_interrupted()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    instance.publish(url)
    # Warm the one-off hardware checks now, so the first page load doesn't wait for them.
    threading.Thread(target=lambda: (gpu_available(), media.tools_ok() and media.pick_encoder()),
                     daemon=True).start()
    log.info("Redactor %s started at %s", __version__, url)
    print(f"Redactor {__version__} running at {url}  (close this window to quit)", flush=True)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
