# FB Ads Library Scraper

A self-contained Flask + Playwright tool to scrape the public Facebook Ads Library.

- Paste any Ads Library URL, set a page count (5–100), click Start
- Auto-scrolls the actual infinite-scroll container, with proper stall detection
- High-DPI (2×) full-page screenshots designed to be readable by AI/OCR
- Streams progress to the browser via **Server-Sent Events** (no polling, no missed updates)
- One-click ZIP download of all artifacts at the end
- No external state, no database, no disk dependency

## ⚠️ Free-tier constraint (important!)

This app is designed to run on the **Render free plan**, which means:

- **No persistent disk** — the container's filesystem is wiped on every cold start.
- **No cross-session resume** — when the free instance sleeps (~15 min idle), all in-memory state is lost. Previous jobs are not retrievable.
- **Keep this tab open** while the scraper runs. Download your files (the green "Download all" button) before closing the tab or letting the server idle out.

If you need a version with persistent state, deploy the same code to a paid Render plan, Fly.io, or a VM. The code is the same; just remove the free-plan-specific warnings from the UI.

## Run locally

```bash
pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium
python app.py     # http://localhost:5000
```

Run the scraper from the CLI directly:

```bash
python scraper.py "https://web.facebook.com/ads/library/?q=women%20fashion" 30
```

Outputs land in `static/outputs/<job_id>/`:
- `final_30_pages.png` — high-DPI full-page screenshot
- `full_page.html` — raw rendered HTML
- `ads_data.json` — extracted ad cards (deduplicated, signed)
- `log.txt` — run log
- `result.json` — job summary

## API

| Method | Path                          | Purpose                                                                |
|--------|-------------------------------|------------------------------------------------------------------------|
| GET    | `/`                           | Web UI                                                                |
| POST   | `/api/scrape`                 | Start a job — body: `{url, pages}`                                     |
| GET    | `/api/stream/<job_id>`        | SSE stream of progress events for a running job                       |
| GET    | `/api/job/<job_id>`           | One-shot JSON status snapshot (in-memory only)                         |
| GET    | `/api/download/<id>/<file>`   | Download a single file from a finished job                             |
| GET    | `/api/download-all/<id>`      | ZIP and download all files for a job                                  |
| GET    | `/health`                     | Health check                                                          |

## Deploy to Render

1. Sign in to https://dashboard.render.com
2. **New + → Web Service → Public Git Repository** → pick `ghostauto01-boop/fb-ads-library-scraper-tool`
3. Render will read `render.yaml` and provision a Docker web service on the free plan
4. **No disk configuration needed** — `render.yaml` is disk-free for the free plan
5. The first build pulls the Playwright Docker image and installs Chromium (~3–5 min)
6. Once it says **Live**, open the URL

If the deploy fails because `render.yaml` references a disk, delete the disk block from the rendered service's Settings page (Disks section) and re-deploy.

## Tests

```bash
python3 tests/static_test.py        # 19 tests, no external deps
python3 tests/scroll_loop_test.py   # 5 tests with mock Playwright
```
