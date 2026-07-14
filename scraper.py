import os
import time
import json
import uuid
from pathlib import Path
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

OUTPUT_DIR = Path("static/outputs")


def scrape_ads_library(url: str, target_pages: int = 30, job_id: str = None, log_callback=None):
    if job_id is None:
        job_id = str(uuid.uuid4())[:8]
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    def log(msg):
        print(msg)
        if log_callback:
            log_callback(msg)
        with open(job_dir / "log.txt", "a", encoding="utf-8") as f:
            f.write(msg + "\n")

    # Reset log
    with open(job_dir / "log.txt", "w", encoding="utf-8") as f:
        f.write("")

    log(f"Starting job {job_id} URL: {url} Pages: {target_pages}")
    ads_data = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            time.sleep(7)

            # Cookie consent
            try:
                cookie_btn = page.locator(
                    "button:has-text('Allow all cookies'), button:has-text('Accept All')"
                ).first
                if cookie_btn.is_visible(timeout=3000):
                    cookie_btn.click()
            except Exception:
                pass

            for i in range(1, target_pages + 1):
                log(f"[{i}/{target_pages}] Loading more...")
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(1.5)
                for sel in ["button:has-text('See more')", "button:has-text('Show more')"]:
                    try:
                        loc = page.locator(sel)
                        for j in range(min(loc.count(), 3)):
                            btn = loc.nth(j)
                            if btn.is_visible():
                                try:
                                    btn.click(timeout=2000)
                                except Exception:
                                    pass
                    except Exception:
                        pass
                page.mouse.wheel(0, 3000)
                time.sleep(2)
                if i % 10 == 0:
                    page.screenshot(
                        path=str(job_dir / f"progress_{i}_pages.png"), full_page=True
                    )

            final_png = job_dir / "final_30_pages.png"
            page.screenshot(path=str(final_png), full_page=True)
            html_content = page.content()
            with open(job_dir / "full_page.html", "w", encoding="utf-8") as f:
                f.write(html_content)

            try:
                soup = BeautifulSoup(html_content, "lxml")
                cards = soup.select(
                    "div[data-testid='ad-library-card'], div.x1yztbdb"
                )
                extracted = []
                for idx, card in enumerate(cards[:500]):
                    text = card.get_text(separator=" ", strip=True)[:1000]
                    if len(text) > 50:
                        extracted.append({"id": idx, "text_preview": text})
                with open(job_dir / "ads_data.json", "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "job_id": job_id,
                            "url": url,
                            "pages_loaded": target_pages,
                            "total_found": len(extracted),
                            "ads": extracted,
                        },
                        f,
                        indent=2,
                        ensure_ascii=False,
                    )
            except Exception as e:
                log(f"Extraction warning: {e}")

            browser.close()

        result = {
            "job_id": job_id,
            "status": "completed",
            "url": url,
            "pages": target_pages,
            "files": [str(p.relative_to(OUTPUT_DIR)) for p in job_dir.glob("*")],
        }
        with open(job_dir / "result.json", "w") as f:
            json.dump(result, f, indent=2)
        log("Done!")
        return result

    except Exception as e:
        log(f"ERROR: {str(e)}")
        error_result = {
            "job_id": job_id,
            "status": "failed",
            "error": str(e),
            "url": url,
        }
        with open(job_dir / "result.json", "w") as f:
            json.dump(error_result, f, indent=2)
        return error_result


if __name__ == "__main__":
    import sys

    default_url = (
        "https://web.facebook.com/ads/library/?active_status=active&ad_type=all"
        "&country=US&is_targeted_country=false&media_type=all&q=women%20fashion"
        "&search_type=keyword_unordered&sort_data[mode]=total_impressions"
        "&sort_data[direction]=desc"
    )
    test_url = sys.argv[1] if len(sys.argv) > 1 else default_url
    pages = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    scrape_ads_library(test_url, pages)
