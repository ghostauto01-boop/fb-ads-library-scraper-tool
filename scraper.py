"""
Facebook Ads Library scraper.

Loads a public Ads Library search, auto-scrolls until the requested number
of pages have been fetched (or no new content arrives), then captures a
*high-resolution* full-page screenshot plus the rendered HTML and the
extracted ad cards as JSON.

Key behaviours:
  * Real infinite-scroll: scrolls the *inner* scrollable container, waits
    for *new* DOM nodes (count + signature of the last card), and stops
    early if no new content has appeared for a few iterations.
  * High-DPI screenshots via `device_scale_factor=2` and a forced,
    legible font via CSS injection so downstream AI/OCR can read the text.
  * Robust load-more detection: tries multiple selector strategies that
    cover the current Facebook Ads Library DOM (button by text, by
    `aria-label`, by `[role=button]`, by `[data-testid]`).
  * Idempotent: writes a `result.json` even on failure.
"""
import os
import re
import time
import json
import uuid
import hashlib
import traceback
from pathlib import Path
from typing import Callable, List, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from bs4 import BeautifulSoup

OUTPUT_DIR = Path("static/outputs")

# ---------------------------------------------------------------------------
#  Tunable constants
# ---------------------------------------------------------------------------

VIEWPORT = {"width": 1440, "height": 900}        # 1440 keeps text readable
DEVICE_SCALE_FACTOR = 2                           # 2× DPI = 2880-wide PNGs
SCROLL_PAUSE_SEC = 1.2                            # time between scroll attempts
STALL_LIMIT = 4                                   # stop after N no-new-content iters
HARD_MAX_ITERATIONS = 250                         # absolute safety cap
PAGE_SETTLE_JS = """
async () => {
  // Wait for any in-flight fetches and any lazy work to settle
  if (document.readyState !== 'complete') {
    await new Promise(r => window.addEventListener('load', r, { once: true }));
  }
  // Trigger IntersectionObserver-based lazy loaders
  window.scrollTo(0, document.body.scrollHeight);
  await new Promise(r => setTimeout(r, 250));
  window.scrollTo(0, document.body.scrollHeight);
  await new Promise(r => setTimeout(r, 250));
}
"""


def _truncate(s, n):
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def _sig_of_text(text: str) -> str:
    """Stable short hash of card text so we can detect 'no new content'."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
#  Selector strategies for ad cards and "load more" buttons
# ---------------------------------------------------------------------------

# The Facebook Ads Library DOM has changed several times. The selectors below
# are listed roughly from most-stable to least-stable. The scraper tries
# them in order and uses the first one that finds elements.
CARD_SELECTORS = [
    "[data-testid='ad-library-card']",
    "div[role='article'][data-pagelet^='AdLibrarySearchResult']",
    "div.x1yztbdb.x1n2onr6",                   # legacy class-based fallback
    "div[class*='x1yztbdb']",                  # broader legacy
]

# Strategies to find a "Load more / See more / Show more" button.
# Each entry: (description, locator) — the locator is tried with .first.click().
LOAD_MORE_SELECTORS = [
    ("aria:Load more",          "[aria-label='Load more']"),
    ("aria:See more",           "[aria-label='See more']"),
    ("aria:Show more",          "[aria-label='Show more']"),
    ("role:button Load more",   "div[role='button']:has-text('Load more')"),
    ("role:button See more",    "div[role='button']:has-text('See more')"),
    ("role:button Show more",   "div[role='button']:has-text('Show more')"),
    ("button:Load more",        "button:has-text('Load more')"),
    ("button:See more",         "button:has-text('See more')"),
    ("button:Show more",        "button:has-text('Show more')"),
    ("a:Load more",             "a:has-text('Load more')"),
    ("a:See more",              "a:has-text('See more')"),
]


def _count_cards(page) -> int:
    """Total ad cards visible in the DOM across all known selectors."""
    total = 0
    for sel in CARD_SELECTORS:
        try:
            total += page.locator(sel).count()
        except Exception:
            pass
    return total


def _last_card_signature(page) -> str:
    """A short stable signature of the *last* visible card. If this doesn't
    change between iterations, we're seeing the same content (stuck)."""
    for sel in CARD_SELECTORS:
        try:
            loc = page.locator(sel)
            n = loc.count()
            if n > 0:
                # Use the innerText of the last card
                text = loc.nth(n - 1).inner_text(timeout=2000).strip()
                if text:
                    return _sig_of_text(text)
        except Exception:
            continue
    return ""


def _try_load_more(page, log) -> bool:
    """Click any visible load-more-like button. Returns True if clicked."""
    for desc, sel in LOAD_MORE_SELECTORS:
        try:
            loc = page.locator(sel)
            n = loc.count()
            for i in range(n):
                btn = loc.nth(i)
                try:
                    if btn.is_visible(timeout=800):
                        btn.click(timeout=2000)
                        log(f"  clicked {desc} ({n} matches, picked #{i+1})")
                        return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


def _scroll_inner_container(page, log) -> None:
    """Scroll the *inner* scrollable container — the one Facebook uses for
    infinite scroll inside the Ads Library pane. We try several candidates
    and always end with a window-level scroll as a fallback."""
    scrolled = False
    # Strategy: find a container that has its own overflow + actual content
    js = r"""
    () => {
      const candidates = Array.from(document.querySelectorAll('div'))
        .filter(el => {
          const cs = getComputedStyle(el);
          return (cs.overflowY === 'auto' || cs.overflowY === 'scroll')
                 && el.scrollHeight > el.clientHeight + 200;
        })
        // Prefer the deepest, most-contentful container
        .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
      for (const el of candidates.slice(0, 3)) {
        el.scrollTo({ top: el.scrollHeight, behavior: 'instant' });
      }
      window.scrollTo(0, document.body.scrollHeight);
      return candidates.length;
    }
    """
    try:
        n = page.evaluate(js)
        if n:
            scrolled = True
            log(f"  scrolled {n} inner container(s)")
    except Exception as e:
        log(f"  inner scroll error: {e}")
    # Mouse wheel as a final nudge (some virtualized lists need it)
    try:
        page.mouse.wheel(0, 4000)
    except Exception:
        pass


def _wait_for_settle(page, log) -> None:
    """Wait for the page to actually be done with network + rendering."""
    try:
        page.evaluate(
            """async () => {
              if (document.readyState !== 'complete') {
                await new Promise(r => window.addEventListener('load', r, { once: true }));
              }
              // Best-effort: wait for any in-flight fetches
              if (window.__pendingFetches) {
                while (window.__pendingFetches.size) {
                  await new Promise(r => setTimeout(r, 50));
                }
              }
            }"""
        )
    except Exception:
        pass
    # A small fixed pause for layout/paint
    page.wait_for_timeout(int(SCROLL_PAUSE_SEC * 1000))


def _inject_legibility_css(page) -> None:
    """Force ad-card text to a readable size for screenshots/OCR."""
    css = """
    (() => {
      const style = document.createElement('style');
      style.id = '__scraper_legibility__';
      style.textContent = `
        /* Make ad text bigger and crisper for the final screenshot */
        [data-testid='ad-library-card'] *,
        div[role='article'][data-pagelet^='AdLibrarySearchResult'] *,
        .x1yztbdb, .x1yztbdb * {
          font-size: 1.15em !important;
          line-height: 1.5 !important;
          letter-spacing: 0.01em !important;
          -webkit-font-smoothing: antialiased !important;
          text-rendering: optimizeLegibility !important;
        }
        /* Hide obvious noise that crowds the screenshot */
        [aria-label='Close'], [data-testid='cookie-policy-banner'],
        [data-testid='fb-pxy-ufb-notification'] { display: none !important; }
      `;
      document.documentElement.appendChild(style);
    })()
    """
    try:
        page.evaluate(css)
    except Exception:
        pass


# ---------------------------------------------------------------------------
#  Main scraper
# ---------------------------------------------------------------------------

def scrape_ads_library(
    url: str,
    target_pages: int = 30,
    job_id: str = None,
    log_callback: Optional[Callable[[str], None]] = None,
):
    if job_id is None:
        job_id = str(uuid.uuid4())[:8]
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    def log(msg: str):
        text = msg if not msg.startswith("URL:") else f"URL: {_truncate(msg[4:].strip(), 220)}"
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
                    "--font-render-hinting=none",  # crisper text at high DPI
                ],
            )
            context = browser.new_context(
                viewport=VIEWPORT,
                device_scale_factor=DEVICE_SCALE_FACTOR,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                timezone_id="America/Los_Angeles",
            )
            page = context.new_page()

            # -------- Load the Ads Library page --------
            log("Loading Ads Library page…")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
            except PWTimeout:
                log("WARN: page.goto timed out, continuing anyway")
            page.wait_for_timeout(7000)

            # -------- Cookie consent (best effort) --------
            try:
                cookie_btn = page.locator(
                    "button:has-text('Allow all cookies'), "
                    "button:has-text('Accept All'), "
                    "button:has-text('Accept all')"
                ).first
                if cookie_btn.is_visible(timeout=3000):
                    cookie_btn.click()
                    page.wait_for_timeout(1200)
            except Exception:
                pass

            # Inject legibility CSS once, after the page has had a moment to render
            _inject_legibility_css(page)
            page.wait_for_timeout(1000)

            # -------- Initial card count --------
            initial_cards = _count_cards(page)
            log(f"Initial card count: {initial_cards}")
            last_sig = _last_card_signature(page)
            log(f"Initial last-card sig: {last_sig or '(none)'}")

            # -------- Scroll loop --------
            #
            # Stop conditions:
            #   * we've made `target_pages` "page-equivalent" advances
            #     (where 1 advance ≈ +12–24 cards, but we count by stable
            #     signature changes instead)
            #   * no new content has arrived for STALL_LIMIT consecutive
            #     iterations
            #   * we've hit HARD_MAX_ITERATIONS
            #
            page_advances = 0
            stall = 0
            i = 0
            last_count = initial_cards

            while page_advances < int(target_pages) and i < HARD_MAX_ITERATIONS:
                i += 1
                log(
                    f"[{i}/{target_pages}] "
                    f"advances={page_advances} cards={last_count} stall={stall}"
                )

                # 1) Try clicking a load-more button (cheap, fast when it works)
                clicked = _try_load_more(page, log)

                # 2) Always also scroll the inner container + window
                _scroll_inner_container(page, log)

                # 3) Wait for the page to settle (network + render)
                _wait_for_settle(page, log)

                # 4) Did anything new show up?
                new_count = _count_cards(page)
                new_sig = _last_card_signature(page)

                if new_count > last_count or (new_sig and new_sig != last_sig):
                    delta = new_count - last_count
                    log(f"  +{delta} new cards (sig changed)")
                    last_count = new_count
                    if new_sig:
                        last_sig = new_sig
                    page_advances += 1
                    stall = 0
                else:
                    stall += 1
                    log(f"  no new content (stall {stall}/{STALL_LIMIT})")
                    if stall >= STALL_LIMIT:
                        log("Stopping: no new content after multiple attempts")
                        break

                if i % 10 == 0:
                    try:
                        page.screenshot(
                            path=str(job_dir / f"progress_{i}_adv.png"),
                            full_page=False,
                        )
                    except Exception:
                        pass

            log(f"Scroll loop done. advances={page_advances} iterations={i} cards={last_count}")

            # -------- High-DPI full-page screenshot --------
            log("Capturing high-DPI full-page screenshot…")
            final_png = job_dir / "final_30_pages.png"
            try:
                # Make sure we are scrolled back to the top so the screenshot
                # starts at the beginning (and any lazy-loaders above the fold
                # have a chance to render)
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(800)
                page.evaluate(PAGE_SETTLE_JS)
                page.wait_for_timeout(800)
                page.screenshot(path=str(final_png), full_page=True)
                size = final_png.stat().st_size if final_png.exists() else 0
                log(f"  final screenshot saved: {final_png.name} ({size:,} bytes)")
            except Exception as e:
                log(f"  final screenshot error: {e}")

            # -------- Save HTML --------
            log("Saving HTML…")
            try:
                html_content = page.content()
                with open(job_dir / "full_page.html", "w", encoding="utf-8") as f:
                    f.write(html_content)
            except Exception as e:
                log(f"html save error: {e}")
                html_content = ""

            # -------- Extract ad cards as JSON --------
            extracted: List[dict] = []
            if html_content:
                log("Extracting ad cards…")
                try:
                    soup = BeautifulSoup(html_content, "lxml")
                    # Deduplicate cards by their text signature
                    seen = set()
                    for sel in CARD_SELECTORS:
                        for idx, card in enumerate(soup.select(sel)):
                            text = card.get_text(separator=" ", strip=True)
                            if len(text) < 50:
                                continue
                            sig = _sig_of_text(text)
                            if sig in seen:
                                continue
                            seen.add(sig)
                            extracted.append(
                                {
                                    "id": len(extracted),
                                    "selector": sel,
                                    "text_preview": text[:1000],
                                    "signature": sig,
                                }
                            )
                            if len(extracted) >= 1000:
                                break
                        if len(extracted) >= 1000:
                            break
                    with open(
                        job_dir / "ads_data.json", "w", encoding="utf-8"
                    ) as f:
                        json.dump(
                            {
                                "job_id": job_id,
                                "url": url,
                                "pages_loaded": page_advances,
                                "iterations": i,
                                "total_found": len(extracted),
                                "ads": extracted,
                            },
                            f,
                            indent=2,
                            ensure_ascii=False,
                        )
                    log(f"  extracted {len(extracted)} unique cards")
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
            "pages_requested": int(target_pages),
            "pages_loaded": page_advances,
            "iterations": i,
            "total_cards": len(extracted),
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
        tb = traceback.format_exc(limit=8)
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
