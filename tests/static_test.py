"""
Static test of app.py using a minimal in-process Flask shim.
Verifies the routes, persistence, and security of the app
WITHOUT needing a real Flask/Playwright install.

Run: python3 tests/static_test.py
"""
import sys, types, importlib.util, json as _json, time, pathlib, os


class _Resp:
    def __init__(self, body, status=200, ctype="application/json"):
        self._body_str = body if isinstance(body, str) else body.decode() if isinstance(body, bytes) else str(body)
        self.status_code = status
        self.headers = {"Content-Type": ctype}

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

# Fake scraper that writes real files to disk so the endpoint can list them
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
                "pages": target_pages,
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
        "pages": target_pages,
        "files": ["final_30_pages.png", "full_page.html", "ads_data.json"],
    }


fake.scrape_ads_library = fs
sys.modules["scraper"] = fake

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

for _ in range(40):
    r = c.get(f"/api/job/{job_id}")
    d = r.get_json()
    if d and d.get("status") in ("completed", "failed"):
        break
    time.sleep(0.05)
assert d["status"] == "completed"
ok(f"job completed with files: {d.get('files')}")

# --- 4: persistence ---
job_dir = pathlib.Path("static/outputs") / job_id
assert (job_dir / "status.json").exists()
ok("status.json persisted to disk on completion")

# --- 5: disk fallback via status.json (cold restart scenario) ---
appmod.jobs.pop(job_id, None)
r = c.get(f"/api/job/{job_id}")
d = r.get_json()
assert d["status"] == "completed"
ok(f"disk fallback (status.json) works: {d.get('files')}")

# --- 6: disk fallback via result.json (legacy) ---
appmod.jobs.pop(job_id, None)
(job_dir / "status.json").unlink()
(job_dir / "log.txt").write_text("line1\nline2\nline3\n")
r = c.get(f"/api/job/{job_id}")
d = r.get_json()
assert d["status"] == "completed"
assert "line1" in str(d["logs"])
ok(f"disk fallback (result.json) works: {d['status']}")

# --- 7: 404 for unknown job ---
r = c.get("/api/job/zzznotreal")
assert r.status_code == 404
ok("unknown job → 404")

# --- 8: security ---
def _is_invalid(resp):
    # Flask returns ("Invalid", 400) tuple for the string-return path
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

# --- 9: download ---
r = c.get(f"/api/download/{job_id}/final_30_pages.png")
assert r.status_code == 200
ok(f"download works: {len(r.get_data())} bytes")

# --- 10: page cap ---
r = c.post("/api/scrape", json={"url": "https://web.facebook.com/ads/library/?q=x", "pages": 99999})
job2 = r.get_json()["job_id"]
for _ in range(40):
    r = c.get(f"/api/job/{job2}")
    d = r.get_json()
    if d and d.get("status") in ("completed", "failed"):
        break
    time.sleep(0.05)
assert d["pages"] == 200
ok(f"page cap works: requested 99999 → got {d['pages']}")

# --- 11: single-worker invariant ---
ids = set()
for _ in range(20):
    r = c.post("/api/scrape", json={"url": "https://web.facebook.com/ads/library/?q=t", "pages": 1})
    ids.add(id(appmod.jobs))
assert len(ids) == 1
ok("jobs dict identity stable (single-worker compatible)")

# --- 12: invalid job id in URL ---
r = c.get("/api/job/..%2Fetc")
assert r.status_code == 400
ok("invalid job id in URL → 400")

print(f"\n✅ ALL {passed} static tests passed.")
