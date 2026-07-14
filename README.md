# OSINT Tools

This repo hosts OSINT and web-intelligence utilities.

## FB Ads Library Scraper

Located in this repo's root, this is a Flask + Playwright tool that:

- Takes any Facebook Ads Library URL
- Auto-scrolls up to 100 pages in headless Chromium
- Saves a full-page PNG screenshot, the rendered HTML, and extracted ads JSON
- Exposes a live log terminal via a JSON API

### Run locally

```bash
pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium
python app.py     # http://localhost:5000
```

### Run the scraper from the CLI

```bash
python scraper.py "https://web.facebook.com/ads/library/?q=women%20fashion" 30
```

Outputs go to `static/outputs/<job_id>/`:
- `final_30_pages.png` — full-page screenshot
- `full_page.html` — raw rendered HTML
- `ads_data.json` — extracted ad cards
- `log.txt` — run log
- `result.json` — job summary

### Deploy to Render

The repo includes `render.yaml` and a `Dockerfile` (Playwright + Chromium pre-baked).

1. In Render: **New → Web Service → Connect repo**.
2. Render reads `render.yaml` and provisions a Docker web service.
3. Health check is `/health`.

### GitHub Actions

The repo also ships a `Scrape Ad Library (Paste Link)` workflow under `.github/workflows/scrape.yml`. Run it from the **Actions** tab without deploying anything; results land in the run's artifacts for 30 days.
