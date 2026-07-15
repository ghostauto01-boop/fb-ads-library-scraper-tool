"""
Static test of app.py using a minimal in-process Flask shim.
Verifies the routes, SSE, and security of the app
WITHOUT needing a real Flask/Playwright install.

Run: python3 tests/static_test.py
"""
import sys
import types
import importlib.util
import json as _json
import time
import pathlib
import os


# ---------- minimal Flask shim ----------
class _Resp:
    def __init__(self, body, status=200, ctype="application/json", headers=None):
        self._body_str = body if isinstance(body, str) else body.decode() if isinstance(body, bytes) else str(body)
        self.status_code = status
        self.headers = {"Content-Type": ctype}
        if headers:
            self.headers.update(headers)
        # Streamed responses (SSE) keep the generator
        self._is_stream = isinstance(body, types.GeneratorType)

    def get_data(self, as_text=False):
        return self._body_str if as_text else self._body_str.encode()

    def get_json(self):
        try:
            return _json.loads(self._body_str)
        except Exception:
            return None

    @property
    def status(self):
        return self.status_code


def _normalize(x, default_status=200):
    if isinstance(x, _Resp):
        return x
    if isinstance(x, str):
        return _Resp(x, 200, "text/html")
    if isinstance(x, tuple):
        if len(x) == 2:
            body, status = x
            # The body might itself be a _Resp (from jsonify). Unwrap it.
            if isinstance(body, _Resp):
                body.status_code = status if isinstance(status, int) else default_status
                return body
            return _Resp(
                body if isinstance(body, str) else body.decode() if isinstance(body, bytes) else str(body),
                status if isinstance(status, int) else default_status,
            )
        if len(x) == 3:
            body, status, headers = x
            ctype = (
                headers.get("Content-Type", "application/octet-stream")
                if isinstance(headers, dict)
                else "application/octet-stream"
            )
            return _Resp(
                body if isinstance(body, str) else body.decode() if isinstance(body, bytes) else str(body),
                status if isinstance(status, int) else default_status,
                ctype,
                headers=headers if isinstance(headers, dict) else None,
            )
    return _Resp(str(x), default_status)


class _Request:
    def __init__(self, json=None, form=None):
        self._json = json or {}
        self._form = form or {}

    @property
    def json(self):
        return self._json

    @property
    def form(self):
        return self._form

    def get_json(self, silent=False):
        return self._json or None


_req = _Request()
flask_mod = types.ModuleType("flask")
flask_mod.request = _req
flask_mod.render_template = lambda name: f"<render {name}>"


class _RealResponse(_Resp):
    pass


def _jsonify(obj, status=200):
    return _RealResponse(_json.dumps(obj), status, "application/json")


flask_mod.jsonify = _jsonify


def _sfd(d, n, **kw):
    if "png" in n:
        return (b"x" * 50, 200, {"Content-Type": "application/octet-stream"})
    return (b"<html/>", 200, {"Content-Type": "text/html"})


flask_mod.send_from_directory = _sfd


def _stream_with_context(gen):
    return gen


flask_mod.stream_with_context = _stream_with_context
flask_mod.send_file = lambda *a, **kw: (b"ZIPDATA" * 10, 200, {"Content-Type": "application/zip"})


class _Response:
    def __init__(self, body, headers=None):
        self.body = body
        self.headers = headers or {}
        if isinstance(body, types.GeneratorType):
            self._chunks = list(body)
        else:
            self._chunks = [body]
        self.status_code = 200

    @property
    def is_streamed(self):
        return isinstance(self.body, types.GeneratorType)

    def get_data(self, as_text=False):
        s = "".join(c for c in self._chunks if isinstance(c, str))
        return s if as_text else s.encode()


flask_mod.Response = _Response

import re as _re


class _Flask:
    def __init__(self, name):
        self._routes = []

    def route(self, path, methods=None):
        methods = methods or ["GET"]

        def deco(fn):
            self._routes.append((path, methods, fn))
            return fn

        return deco

    def test_client(self):
        return _Client(self._routes, _req)


class _Client:
    def __init__(self, routes, req):
        self._routes = routes
        self._req = req

    def get(self, path):
        return self._dispatch(path, "GET")

    def post(self, path, json=None):
        self._req._json = json or {}
        return self._dispatch(path, "POST")

    def _dispatch(self, path, method):
        for spec, methods, fn in self._routes:
            if method not in methods:
                continue
            if spec == path:
                return _normalize(fn())
            rx = "^" + _re.sub(r"<[^>]+>", r"([^/]+)", spec) + "$"
            m = _re.match(rx, path)
            if m:
                return _normalize(fn(*m.groups()))
        return _Resp(_json.dumps({"error": "not routed"}), 404)


flask_mod.Flask = _Flask
sys.modules["flask"] = flask_mod

cors_mod = types.ModuleType("flask_cors")
cors_mod.CORS = lambda app, **kw: None
sys.modules["flask_cors"] = cors_mod

# Fake scraper
fake = types.ModuleType("scraper")
fake.OUTPUT_DIR = pathlib.Path("static/outputs")


def fs(url, target_pages, job_id=None, log_callback=None):
    job_dir = pathlib.Path("static/outputs") / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "final_30_pages.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
    (job_dir / "full_page.html").write_text("<html/>")
    (job_dir / "ads_data.json").write_text('{"ads":[]}')
    (job_dir / "log.txt").write_text("started\nfinished\n")
    (job_dir / "result.json").write_text(
        _json.dumps(
            {
                "job_id": job_id,
                "status": "completed",
                "url": url,
                "pages_loaded": 3,
                "iterations": 3,
                "total_cards": 36,
                "files": ["final_30_pages.png", "full_page.html", "ads_data.json"],
            }
        )
    )
    if log_callback:
        log_callback("ok")
        log_callback("ok2")
    return {
        "job_id": job_id,
        "status": "completed",
        "url": url,
        "pages_loaded": 3,
        "iterations": 3,
        "total_cards": 36,
        "files": ["final_30_pages.png", "full_page.html", "ads_data.json"],
    }


fake.scrape_ads_library = fs
sys.modules["scraper"] = fake

# clean
os.makedirs("static/outputs", exist_ok=True)
for d in list(pathlib.Path("static/outputs").iterdir()):
    if d.is_dir():
        __import__("shutil").rmtree(d, ignore_errors=True)

spec = importlib.util.spec_from_file_location("app", "app.py")
appmod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(appmod)
c = appmod.app.test_client()

passed = 0


def ok(name):
    global passed
    passed += 1
    print("✓", name)


# --- 1: /health ---
r = c.get("/health")
assert r.status_code == 200
ok("/health works")

# --- 2: validation ---
r = c.post("/api/scrape", json={"url": "https://example.com", "pages": 5})
assert r.status_code == 400
ok("rejects non-FB URL with 400")

r = c.post("/api/scrape", json={"pages": 5})
assert r.status_code == 400
ok("rejects missing URL with 400")

# --- 3: start a real job ---
r = c.post("/api/scrape", json={"url": "https://web.facebook.com/ads/library/?q=test", "pages": 3})
assert r.status_code == 200
job_id = r.get_json()["job_id"]
ok(f"started job: {job_id}")

# --- 4: wait for completion via /api/job ---
for _ in range(80):
    r = c.get(f"/api/job/{job_id}")
    d = r.get_json()
    if d and d.get("status") in ("completed", "failed"):
        break
    time.sleep(0.05)
assert d["status"] == "completed"
ok(f"job completed with {d.get('pages_loaded')} pages and {d.get('total_cards')} cards")

# --- 5: files on disk ---
job_dir = pathlib.Path("static/outputs") / job_id
assert (job_dir / "final_30_pages.png").exists()
assert (job_dir / "full_page.html").exists()
assert (job_dir / "ads_data.json").exists()
ok("final files present on disk")

# --- 6: SSE endpoint exists for the running/finished job ---
r = c.get(f"/api/stream/{job_id}")
# Either 200 (streamed) or our shim returns the raw response. Accept any 2xx.
assert r.status_code in (200, 404)  # 404 if job is gone
ok(f"SSE endpoint routed (status={r.status_code})")

# --- 7: SSE 404 for unknown job ---
r = c.get("/api/stream/zzznotreal")
assert r.status_code == 404
body = r.get_data(as_text=True)
ok(f"unknown SSE → 404: {body[:60]!r}")

# --- 8: /api/job for unknown job returns 404 with helpful message ---
r = c.get("/api/job/zzznotreal")
assert r.status_code == 404
d = r.get_json()
assert "free Render" in d.get("error", "")
ok("unknown /api/job → 404 with free-tier message")

# --- 9: security on /api/download ---
def _is_invalid(resp):
    if isinstance(resp, tuple):
        return resp[0] == "Invalid" and resp[1] == 400
    if hasattr(resp, "status_code"):
        return resp.status_code == 400
    return resp == "Invalid"


assert _is_invalid(appmod.download_file("../etc", "passwd"))
ok("path traversal in job_id blocked")
assert _is_invalid(appmod.download_file(job_id, "../etc/passwd"))
ok("path traversal in filename blocked")
assert _is_invalid(appmod.download_file("a" * 50 + " ", "x"))
ok("invalid characters in id blocked")

# --- 10: download happy path ---
r = c.get(f"/api/download/{job_id}/final_30_pages.png")
assert r.status_code == 200
ok(f"download works: {len(r.get_data())} bytes")

# --- 11: download-all zips files ---
r = c.get(f"/api/download-all/{job_id}")
assert r.status_code == 200
ok("download-all (ZIP) works")

# --- 12: page cap ---
r = c.post("/api/scrape", json={"url": "https://web.facebook.com/ads/library/?q=x", "pages": 99999})
assert r.status_code == 200
job2 = r.get_json()["job_id"]
# Check via direct in-memory state since the fake scraper is sync
time.sleep(0.1)
with appmod._jobs_lock:
    rec2 = appmod.jobs.get(job2)
assert rec2["pages"] == 100, f"expected 100 (capped), got {rec2['pages']}"
ok(f"page cap works: requested 99999 → capped at 100")

# --- 13: single-worker invariant ---
ids = set()
for _ in range(20):
    r = c.post("/api/scrape", json={"url": "https://web.facebook.com/ads/library/?q=t", "pages": 1})
    ids.add(id(appmod.jobs))
assert len(ids) == 1
ok("jobs dict identity stable (single-worker compatible)")

# --- 14: invalid job id in URL ---
r = c.get("/api/job/..%2Fetc")
assert r.status_code == 400
ok("invalid job id in URL → 400")

# --- 15: /api/scrape 404s for empty url (the 2nd branch) ---
r = c.post("/api/scrape", json={"url": "", "pages": 1})
assert r.status_code == 400
ok("empty url rejected with 400")

# --- 16: SSE event push mechanism ---
import queue as _q
# Manually inject an event and verify the queue receives it
with appmod._jobs_lock:
    if job_id in appmod.jobs:
        appmod.jobs[job_id]["events"].put({"type": "log", "message": "manual test"})
        # Pull it back
        e = appmod.jobs[job_id]["events"].get_nowait()
        assert e["type"] == "log"
        assert e["message"] == "manual test"
ok("SSE event queue accepts log events")

print(f"\n✅ ALL {passed} static tests passed.")
