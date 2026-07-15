from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_cors import CORS
import os
import re
import uuid
import json
import time
import threading
from pathlib import Path
from scraper import scrape_ads_library, OUTPUT_DIR

app = Flask(__name__)
CORS(app)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# In-memory job state. NOTE: only reliable because gunicorn is started with
# --workers 1. See Dockerfile. We also persist every state change to disk
# (status.json) so a cold restart can still surface a finished job's result.
jobs = {}
_jobs_lock = threading.Lock()


def _persist(job_id):
    """Write the current in-memory state to disk so we survive worker restarts."""
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    with _jobs_lock:
        data = jobs.get(job_id, {})
    # Don't write huge logs to disk; the scraper already writes log.txt
    persist = {k: v for k, v in data.items() if k != "logs"}
    with open(job_dir / "status.json", "w") as f:
        json.dump(persist, f, indent=2)


def _set_status(job_id, **fields):
    with _jobs_lock:
        if job_id not in jobs:
            jobs[job_id] = {}
        jobs[job_id].update(fields)
    _persist(job_id)


def _load_status(job_id):
    """Try to recover job state from disk (for jobs that finished before a restart)."""
    status_path = OUTPUT_DIR / job_id / "status.json"
    if status_path.exists():
        try:
            with open(status_path) as f:
                return json.load(f)
        except Exception:
            return None
    return None


def run_job(job_id, url, pages):
    logs = []
    log_path = OUTPUT_DIR / job_id / "log.txt"

    def log_cb(msg):
        logs.append(msg)
        _set_status(job_id, logs=logs[-100:], last_log=msg)

    _set_status(
        job_id,
        status="running",
        logs=["Starting..."],
        url=url,
        pages=pages,
        started_at=int(time.time()),
    )
    try:
        result = scrape_ads_library(url, pages, job_id=job_id, log_callback=log_cb)
        # Mirror the scraper's final result into the in-memory state so the
        # UI can show real progress numbers as soon as the job finishes.
        _set_status(
            job_id,
            status=result.get("status", "completed"),
            files=result.get("files", []),
            pages_loaded=result.get("pages_loaded", 0),
            iterations=result.get("iterations", 0),
            total_cards=result.get("total_cards", 0),
            finished_at=int(time.time()),
        )
    except Exception as e:
        _set_status(
            job_id,
            status="failed",
            error=str(e),
            finished_at=int(time.time()),
        )


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
    pages = max(1, min(pages, 200))

    if not url:
        return jsonify({"error": "URL required"}), 400
    if "facebook.com/ads/library" not in url:
        return jsonify({"error": "Valid Ads Library URL required"}), 400

    job_id = str(uuid.uuid4())[:8]
    # Pre-create the job dir + initial status so even an immediate poll works
    (OUTPUT_DIR / job_id).mkdir(parents=True, exist_ok=True)
    _set_status(
        job_id,
        status="queued",
        url=url,
        pages=pages,
        logs=["Queued, starting worker..."],
        started_at=int(time.time()),
    )
    thread = threading.Thread(
        target=run_job, args=(job_id, url, pages), daemon=True
    )
    thread.start()
    return jsonify({"job_id": job_id, "status": "queued"})


_SAFE = re.compile(r"^[a-zA-Z0-9._-]+$")


@app.route("/api/job/<job_id>")
def api_job_status(job_id):
    if not _SAFE.match(job_id):
        return jsonify({"error": "Invalid job id"}), 400
    job_dir = OUTPUT_DIR / job_id

    # Live in-memory state first
    with _jobs_lock:
        mem = jobs.get(job_id)

    if mem is not None:
        files = (
            [f.name for f in job_dir.iterdir() if f.is_file()]
            if job_dir.exists()
            else []
        )
        return jsonify({**mem, "files": files})

    # Otherwise: try to recover from disk (job finished before this worker started)
    disk = _load_status(job_id)
    if disk is None and (job_dir / "result.json").exists():
        try:
            with open(job_dir / "result.json") as f:
                disk = json.load(f)
        except Exception:
            disk = None

    if disk is not None:
        log_path = job_dir / "log.txt"
        logs = (
            open(log_path).read().splitlines()[-100:] if log_path.exists() else []
        )
        files = (
            [f.name for f in job_dir.iterdir() if f.is_file()]
            if job_dir.exists()
            else []
        )
        return jsonify({**disk, "logs": logs, "files": files})

    return jsonify({"error": "Job not found", "job_id": job_id}), 404


@app.route("/api/download/<job_id>/<path:filename>")
def download_file(job_id, filename):
    if not _SAFE.match(job_id) or not _SAFE.match(filename):
        return "Invalid", 400
    return send_from_directory(
        OUTPUT_DIR / job_id, filename, as_attachment=True
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok", "jobs_in_memory": len(jobs)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
