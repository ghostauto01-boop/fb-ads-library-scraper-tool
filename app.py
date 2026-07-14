from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_cors import CORS
import os
import uuid
import json
import threading
from pathlib import Path
from scraper import scrape_ads_library, OUTPUT_DIR

app = Flask(__name__)
CORS(app)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

jobs = {}


def run_job(job_id, url, pages):
    logs = []

    def log_cb(msg):
        logs.append(msg)
        if job_id in jobs:
            jobs[job_id]["logs"] = logs[-100:]
            jobs[job_id]["last_log"] = msg

    jobs[job_id] = {"status": "running", "logs": ["Starting..."], "url": url, "pages": pages}
    result = scrape_ads_library(url, pages, job_id=job_id, log_callback=log_cb)
    jobs[job_id].update(result)
    jobs[job_id]["status"] = result.get("status", "completed")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    data = request.get_json() or {}
    url = data.get("url") or request.form.get("url")
    pages = int(data.get("pages") or 30)
    if not url:
        return jsonify({"error": "URL required"}), 400
    if "facebook.com/ads/library" not in url:
        return jsonify({"error": "Valid Ads Library URL required"}), 400
    job_id = str(uuid.uuid4())[:8]
    thread = threading.Thread(target=run_job, args=(job_id, url, pages), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id, "status": "started"})


@app.route("/api/job/<job_id>")
def api_job_status(job_id):
    job_dir = OUTPUT_DIR / job_id
    if job_id in jobs:
        files = (
            [f.name for f in job_dir.iterdir() if f.is_file()]
            if job_dir.exists()
            else []
        )
        return jsonify({**jobs[job_id], "files": files})
    result_path = job_dir / "result.json"
    if result_path.exists():
        with open(result_path) as f:
            result = json.load(f)
        log_path = job_dir / "log.txt"
        logs = open(log_path).read().splitlines()[-100:] if log_path.exists() else []
        return jsonify({**result, "logs": logs})
    return jsonify({"error": "Job not found"}), 404


@app.route("/api/download/<job_id>/<path:filename>")
def download_file(job_id, filename):
    if ".." in filename or filename.startswith("/"):
        return "Invalid", 400
    return send_from_directory(OUTPUT_DIR / job_id, filename, as_attachment=True)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
