# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""The local web server: who may talk to it, what it returns, how it handles bad requests."""
import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from redactor import server


@pytest.fixture(scope="module")
def port():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def request(port, method, path, body=None, token=True, host=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": host or f"127.0.0.1:{port}", **(headers or {})}
    if token:
        h["X-Token"] = server.TOKEN
    data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
    if data is not None:
        h["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=h)
    r = conn.getresponse()
    payload = r.read()
    conn.close()
    return r, payload


# ---------------------------------------------------------------- access control

def test_page_carries_token_and_security_headers(port):
    r, body = request(port, "GET", "/", token=False)
    assert r.status == 200
    assert server.TOKEN.encode() in body
    assert "frame-ancestors 'none'" in r.getheader("Content-Security-Policy")
    assert r.getheader("X-Frame-Options") == "DENY"
    assert r.getheader("X-Content-Type-Options") == "nosniff"
    assert r.getheader("Referrer-Policy") == "no-referrer"


@pytest.mark.parametrize("host", ["evil.example:80", "127.0.0.1", "127.0.0.1.evil.example", "localhost:1"])
def test_other_host_names_are_refused(port, host):
    """A web page re-pointed at 127.0.0.1 (DNS rebinding) arrives with its own name as Host."""
    r, _ = request(port, "GET", "/", host=host)
    assert r.status == 403


def test_localhost_name_is_accepted(port):
    r, _ = request(port, "GET", "/", host=f"localhost:{port}", token=False)
    assert r.status == 200


def test_api_needs_the_token(port):
    assert request(port, "GET", "/api/projects", token=False)[0].status == 403
    assert request(port, "GET", "/api/projects?token=wrong", token=False)[0].status == 403
    r, body = request(port, "GET", "/api/projects")
    assert r.status == 200 and isinstance(json.loads(body), list)
    assert request(port, "GET", f"/api/projects?token={server.TOKEN}", token=False)[0].status == 200


def test_only_listed_static_files_are_served(port):
    r, _ = request(port, "GET", "/static/app.js", token=False)
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/javascript")
    assert request(port, "GET", "/static/index.html", token=False)[0].status == 403
    assert request(port, "GET", "/static/server.py")[0].status == 404
    assert request(port, "GET", "/static/../server.py")[0].status == 404
    assert request(port, "GET", "/static/..%5Cserver.py")[0].status == 404


# ---------------------------------------------------------------- bad requests

def test_bad_requests_get_clear_errors(port, project):
    base = f"/api/projects/{project.id}"
    assert request(port, "GET", "/api/nothing")[0].status == 404
    assert request(port, "GET", "/api/projects/..%2F..%2Fwindows")[0].status == 404
    r, body = request(port, "POST", f"{base}/regions", b"{not json")
    assert r.status == 400 and "JSON" in json.loads(body)["error"]
    assert request(port, "POST", f"{base}/regions", b"[1, 2]")[0].status == 400
    assert request(port, "POST", f"{base}/regions", {"start": 0, "end": 5, "keys": {}})[0].status == 400
    assert request(port, "POST", f"{base}/style", {"mode": "sparkles", "shape": "rect"})[0].status == 400
    assert request(port, "POST", f"{base}/cancel", {"job": "everything"})[0].status == 400
    assert request(port, "GET", f"{base}/boxes?start=a&end=b")[0].status == 400


def test_oversized_body_is_refused_unread(port, project):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest("POST", f"/api/projects/{project.id}/regions", skip_host=True)
    conn.putheader("Host", f"127.0.0.1:{port}")
    conn.putheader("X-Token", server.TOKEN)
    conn.putheader("Content-Length", str(50_000_000))
    conn.endheaders()
    r = conn.getresponse()
    assert r.status == 413
    conn.close()


def test_valid_region_is_saved(port, project):
    r, body = request(port, "POST", f"/api/projects/{project.id}/regions",
                      {"start": 0, "end": 99, "keys": {"0": [10, 10, 100, 100]}, "label": "<b>Plate</b>"})
    assert r.status == 200
    region = json.loads(body)
    assert region["id"] == 1 and region["label"] == "<b>Plate</b>"  # stored as text; the page shows it as text
    r, _ = request(port, "DELETE", f"/api/projects/{project.id}/regions/1")
    assert r.status == 200 and project.data["regions"] == []


def test_export_refused_until_analysis_is_done(port, project):
    project.update(status="analyzing")
    r, _ = request(port, "POST", f"/api/projects/{project.id}/export", {"mode": "blur", "shape": "ellipse"})
    assert r.status == 409


# ---------------------------------------------------------------- the preview video

def test_video_supports_range_requests(port, project):
    (project.dir / "proxy.mp4").write_bytes(bytes(range(256)) * 4)  # 1024 bytes
    url = f"/media/{project.id}/proxy.mp4"
    r, body = request(port, "GET", url)
    assert r.status == 200 and len(body) == 1024 and r.getheader("Accept-Ranges") == "bytes"
    r, body = request(port, "GET", url, headers={"Range": "bytes=10-19"})
    assert r.status == 206 and body == bytes(range(10, 20))
    assert r.getheader("Content-Range") == "bytes 10-19/1024"
    r, body = request(port, "GET", url, headers={"Range": "bytes=-4"})
    assert r.status == 206 and body == bytes([252, 253, 254, 255])
    r, _ = request(port, "GET", url, headers={"Range": "bytes=5000-"})
    assert r.status == 416
    assert request(port, "GET", url, token=False)[0].status == 403
    assert request(port, "GET", f"/media/{project.id}/project.json")[0].status == 404
    assert request(port, "GET", f"/media/{project.id}/thumbs/..%2Fproject.json")[0].status == 404
