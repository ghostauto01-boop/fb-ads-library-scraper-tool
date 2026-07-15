"""
Facebook Ads Library scraper.

Auto-scrolls a public Ads Library search, captures a full-page screenshot,
saves the rendered HTML, and extracts a JSON dump of the ad cards.
"""
import os
import time
import json
import uuid
import traceback
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from bs4 import BeautifulSoup

OUTPUT_DIR = Path("static/outputs")


def _truncate(s, n):
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def scrape_ads_library(
    url: str,
    target_pages: int = 30,
    job_id: str = None,
    log_callback=None,
):
    if job_id is None:
        job_id = str(uuid.uuid4())[:8]
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    def log(msg):
        # Pretty print the URL only on the first line
        text = msg if not msg.startswith("URL:") else f"URL: {_truncate(msg[4:].strip(), 200)}"
        print(text, flush=True)
        if log_callback:
            log_callback(text)
        try:
            with open(job_dir / "log.txt", "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass

    # Reset log
    with open(job_dir / "log.txt", "w", encoding="utf-8") as f:
        f.write("")

    log(f"Starting job {job_id} target_pages={target_pages}")
    log(f"URL: {url}")

    browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                timezone_id="America/Los_Angeles",
            )
            page = context.new_page()
            log("Loading Ads Library page…")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
            except PWTimeout:
                log("WARN: page.goto timed out, continuing anyway")
            time.sleep(7)

            # Cookie consent
            try:
                cookie_btn = page.locator(
                    "button:has-text('Allow all cookies'), "
                    "button:has-text('Accept All'), "
                    "button:has-text('Accept all')"
                ).first
                if cookie_btn.is_visible(timeout=3000):
                    cookie_btn.click()
                    time.sleep(1)
            except Exception:
                pass

            for i in range(1, max(1, int(target_pages)) + 1):
                log(f"[{i}/{target_pages}] Loading more…")
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except Exception as e:
                    log(f"  scroll error: {e}")
                time.sleep(1.5)
                for sel in ["button:has-text('See more')", "button:has-text('Show more')"]:
                    try:
                        loc = page.locator(sel)
                        for j in range(min(loc.count(), 3)):
                            btn = loc.nth(j)
                            try:
                                if btn.is_visible():
                                    btn.click(timeout=2000)
                            except Exception:
                                pass
                    except Exception:
                        pass
                try:
                    page.mouse.wheel(0, 3000)
                except Exception:
                    pass
                time.sleep(2)
                if i % 10 == 0:
                    try:
                        page.screenshot(
                            path=str(job_dir / f"progress_{i}_pages.png"),
                            full_page=True,
                        )
                        log(f"  saved progress_{i}_pages.png")
                    except Exception as e:
                        log(f"  progress screenshot failed: {e}")

            log("Capturing final screenshot…")
            final_png = job_dir / "final_30_pages.png"
            try:
                page.screenshot(path=str(final_png), full_page=True)
            except Exception as e:
                log(f"final screenshot error: {e}")

            log("Saving HTML…")
            try:
                html_content = page.content()
                with open(job_dir / "full_page.html", "w", encoding="utf-8") as f:
                    f.write(html_content)
            except Exception as e:
                log(f"html save error: {e}")
                html_content = ""

            if html_content:
                log("Extracting ad cards…")
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
                    with open(
                        job_dir / "ads_data.json", "w", encoding="utf-8"
                    ) as f:
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
                    log(f"  extracted {len(extracted)} cards")
                except Exception as e:
                    log(f"Extraction warning: {e}")

            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
                browser = None
            except Exception:
                pass

        result = {
            "job_id": job_id,
            "status": "completed",
            "url": url,
            "pages": target_pages,
            "files": [
                str(p.relative_to(OUTPUT_DIR))
                for p in job_dir.glob("*")
                if p.is_file()
            ],
        }
        with open(job_dir / "result.json", "w") as f:
            json.dump(result, f, indent=2)
        log("Done!")
        return result

    except Exception as e:
        tb = traceback.format_exc(limit=5)
        log(f"ERROR: {e}")
        log(tb)
        error_result = {
            "job_id": job_id,
            "status": "failed",
            "error": str(e),
            "url": url,
        }
        with open(job_dir / "result.json", "w") as f:
            json.dump(error_result, f, indent=2)
        return error_result
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


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
