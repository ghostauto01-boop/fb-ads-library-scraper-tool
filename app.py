"""
Flask app for the FB Ads Library scraper.

Designed for the Render free plan: no persistent disk, single gunicorn
worker, no state surviving a sleep/wake cycle. The scraper is run
inside a Server-Sent Events (SSE) handler so the browser can follow
progress in real time, and the resulting files are downloaded before
the tab is closed.

Routes:
  GET  /                        — the web UI
  POST /api/scrape              — start a job; returns the job_id immediately
  GET  /api/stream/<job_id>     — SSE stream of progress for that job
  GET  /api/job/<job_id>        — one-shot JSON snapshot (in-memory only)
  GET  /api/download/<id>/<f>   — download a file from a finished job
  GET  /api/download-all/<id>   — ZIP all files for a job
  GET  /health                  — health check
"""
import io
import os
import re
import uuid
import json
import time
import queue
import zipfile
import threading
from pathlib import Path
from datetime import datetime, timezone

from flask import Flask, render_template, request, jsonify, Response, stream_with_context, send_from_directory, send_file
from flask_cors import CORS

from scraper import scrape_ads_library, OUTPUT_DIR

app = Flask(__name__)
CORS(app)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ------------------------------------------------------------------
# In-memory state
# ------------------------------------------------------------------
# jobs:  { job_id: {"status": "...", "url": "...", "pages": N,
#                    "logs": [...], "last_log": "...", "files": [...],
#                    "pages_loaded": N, "total_cards": N, "error": "...",
#                    "started_at": ts, "finished_at": ts,
#                    "events": queue.Queue, "thread": Thread} }
#
# Everything is in-memory. On the free Render plan the container sleeps
# after 15 minutes idle and ALL state is wiped. That's fine: the user
# must download their files before closing the tab. The UI makes this
# explicit.
jobs = {}
_jobs_lock = threading.Lock()

_SAFE = re.compile(r"^[a-zA-Z0-9._-]+$")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set_field(job_id: str, **fields) -> None:
    with _jobs_lock:
        rec = jobs.get(job_id)
        if rec is not None:
            rec.update(fields)


def _push_event(job_id: str, kind: str, **payload) -> None:
    """Push a structured event onto the SSE queue for this job."""
    with _jobs_lock:
        rec = jobs.get(job_id)
    if rec is None:
        return
    evt = {"type": kind, "ts": _now_iso(), **payload}
    try:
        rec["events"].put_nowait(evt)
    except queue.Full:
        # Drop oldest, push newest
        try:
            rec["events"].get_nowait()
        except Exception:
            pass
        try:
            rec["events"].put_nowait(evt)
        except Exception:
            pass


def _worker(job_id: str, url: str, pages: int) -> None:
    """Run the scraper in a background thread, streaming events."""
    def log_cb(msg: str) -> None:
        with _jobs_lock:
            rec = jobs.get(job_id)
            if rec is None:
                return
            rec["logs"].append(msg)
            rec["logs"] = rec["logs"][-200:]
            rec["last_log"] = msg
        _push_event(job_id, "log", message=msg)

    try:
        result = scrape_ads_library(
            url, pages, job_id=job_id, log_callback=log_cb
        )
        status = result.get("status", "completed")
        with _jobs_lock:
            rec = jobs.get(job_id)
            if rec is None:
                return
            rec.update(
                {
                    "status": status,
                    "files": result.get("files", []),
                    "pages_loaded": result.get("pages_loaded", 0),
                    "iterations": result.get("iterations", 0),
                    "total_cards": result.get("total_cards", 0),
                    "finished_at": int(time.time()),
                    "error": result.get("error"),
                }
            )
        _push_event(
            job_id,
            "done",
            status=status,
            files=result.get("files", []),
            pages_loaded=result.get("pages_loaded", 0),
            total_cards=result.get("total_cards", 0),
            error=result.get("error"),
        )
    except Exception as e:
        with _jobs_lock:
            rec = jobs.get(job_id)
            if rec is not None:
                rec.update(
                    {
                        "status": "failed",
                        "error": str(e),
                        "finished_at": int(time.time()),
                    }
                )
        _push_event(job_id, "done", status="failed", error=str(e))


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or request.form.get("url") or "").strip()
    try:
        pages = int(data.get("pages") or request.form.get("pages") or 30)
    except (TypeError, ValueError):
        pages = 30
    pages = max(1, min(pages, 100))

    if not url:
        return jsonify({"error": "URL required"}), 400
    if "facebook.com/ads/library" not in url:
        return jsonify({"error": "Valid Ads Library URL required"}), 400

    job_id = str(uuid.uuid4())[:8]
    (OUTPUT_DIR / job_id).mkdir(parents=True, exist_ok=True)
    with _jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "url": url,
            "pages": pages,
            "logs": ["Queued, starting worker..."],
            "last_log": "Queued",
            "files": [],
            "pages_loaded": 0,
            "total_cards": 0,
            "started_at": int(time.time()),
            "finished_at": None,
            "error": None,
            "events": queue.Queue(maxsize=500),
        }
    t = threading.Thread(
        target=_worker, args=(job_id, url, pages), daemon=True
    )
    with _jobs_lock:
        jobs[job_id]["thread"] = t
    t.start()
    return jsonify({"job_id": job_id, "status": "queued"})


@app.route("/api/stream/<job_id>")
def api_stream(job_id: str):
    """Server-Sent Events stream of progress for a job.

    Heartbeat every 2 seconds so the connection survives Render's idle
    proxy timeouts during long operations like the full-page screenshot.
    """
    if not _SAFE.match(job_id):
        return jsonify({"error": "Invalid job id"}), 400
    with _jobs_lock:
        rec = jobs.get(job_id)
    if rec is None:
        return jsonify(
            {"error": "Job not found. The server may have been restarted, or the free Render instance went to sleep. Please start a new scrape."}
        ), 404

    @stream_with_context
    def _gen():
        yield f"event: hello\ndata: {json.dumps({'job_id': job_id, 'status': rec['status']})}\n\n"
        snap = {
            "status": rec["status"],
            "logs": rec.get("logs", [])[-200:],
            "last_log": rec.get("last_log"),
            "files": rec.get("files", []),
            "pages_loaded": rec.get("pages_loaded", 0),
            "total_cards": rec.get("total_cards", 0),
        }
        yield f"event: snapshot\ndata: {json.dumps(snap)}\n\n"
        # Heartbeat every 2s so the proxy doesn't kill the connection
        while True:
            try:
                evt = rec["events"].get(timeout=2.0)
            except queue.Empty:
                yield ": ping\n\n"
                continue
            yield f"event: {evt.get('type', 'message')}\ndata: {json.dumps(evt)}\n\n"
            if evt.get("type") == "done":
                break
        yield ": done\n\n"

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return Response(_gen(), headers=headers)


@app.route("/api/job/<job_id>")
def api_job_status(job_id: str):
    """Plain-JSON status endpoint (kept for compatibility)."""
    if not _SAFE.match(job_id):
        return jsonify({"error": "Invalid job id"}), 400
    with _jobs_lock:
        rec = jobs.get(job_id)
    if rec is None:
        return jsonify(
            {
                "error": "Job not found. The free Render plan wipes server state on idle, so previous jobs are not retrievable. Please run a new scrape.",
                "job_id": job_id,
            }
        ), 404
    job_dir = OUTPUT_DIR / job_id
    files = (
        sorted(p.name for p in job_dir.iterdir() if p.is_file())
        if job_dir.exists()
        else []
    )
    out = {k: v for k, v in rec.items() if k not in ("events", "thread")}
    out["files"] = files
    return jsonify(out)


@app.route("/api/download/<job_id>/<path:filename>")
def download_file(job_id: str, filename: str):
    if not _SAFE.match(job_id) or not _SAFE.match(filename):
        return "Invalid", 400
    job_dir = OUTPUT_DIR / job_id
    if not job_dir.exists():
        return "Job no longer exists on the server (free plan wipes state on idle). Please re-run the scrape.", 410
    target = job_dir / filename
    if not target.exists() or not target.is_file():
        return "File not found", 404
    return send_from_directory(str(job_dir), filename, as_attachment=True)


@app.route("/api/download-all/<job_id>")
def download_all(job_id: str):
    """ZIP all the files for a job and send as a single download."""
    if not _SAFE.match(job_id):
        return jsonify({"error": "Invalid job id"}), 400
    job_dir = OUTPUT_DIR / job_id
    if not job_dir.exists():
        return jsonify(
            {"error": "Job no longer exists on the server (free plan wipes state on idle). Please re-run the scrape."}
        ), 410
    files = [p for p in job_dir.iterdir() if p.is_file() and p.suffix in (".png", ".html", ".json", ".txt")]
    if not files:
        return jsonify({"error": "No files available to download yet."}), 404
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as z:
        for p in files:
            z.write(p, arcname=p.name)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"fb-ads-{job_id}.zip",
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok", "jobs_in_memory": len(jobs)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
