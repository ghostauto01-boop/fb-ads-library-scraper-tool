"""
Facebook Ads Library scraper.

Loads a public Ads Library search, auto-scrolls until the requested number
of pages have been fetched (or no new content arrives), then captures a
*high-resolution* full-page screenshot plus the rendered HTML and the
extracted ad cards as JSON.

Key behaviours:
  * Real infinite-scroll: scrolls the *inner* scrollable container, waits
    for *new* DOM nodes (count + signature of the last card), and stops
    only after MANY stalled iterations (Facebook is slow to respond).
  * Network-aware: tracks in-flight GraphQL requests via fetch hooks and
    waits for them to settle before counting cards.
  * High-DPI screenshots via `device_scale_factor=2` and a forced,
    legible font via CSS injection so downstream AI/OCR can read the text.
  * Robust load-more detection: tries multiple selector strategies, all
    SCOPED to the ad-library main content area so we don't accidentally
    click "See more" links inside individual ads.
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

VIEWPORT = {"width": 1440, "height": 900}
DEVICE_SCALE_FACTOR = 2  # 2× DPI = 2880-wide PNGs
SCROLL_PAUSE_SEC = 1.8   # time between scroll attempts (Facebook is slow)
STALL_LIMIT = 8          # allow MANY stalled iters before giving up
HARD_MAX_ITERATIONS = 400

PAGE_SETTLE_JS = """
async () => {
  if (document.readyState !== 'complete') {
    await new Promise(r => window.addEventListener('load', r, { once: true }));
  }
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
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
#  Selector strategies
# ---------------------------------------------------------------------------

CARD_SELECTORS = [
    "[data-testid='ad-library-card']",
    "div[role='article'][data-pagelet^='AdLibrarySearchResult']",
    "div.x1yztbdb.x1n2onr6",
    "div[class*='x1yztbdb']",
]

# The Facebook Ads Library renders the ad list inside a specific region. We
# scope every load-more click to that region so we don't accidentally click
# "See more translations" or "See more" links inside individual ad cards.
# The safest scope: anything inside [role='main'] or [data-pagelet*='AdLibrary'].
MAIN_AREA_SELECTOR = (
    "[role='main'], [data-pagelet='AdLibrarySearchResults'], "
    "div[class*='AdLibrarySearchResults'], main"
)


# Build a list of (desc, scope_selector + button_selector) tuples.
# We resolve scope:contains(target) at click time so the locator finds the
# load-more button that lives INSIDE the ads list, not in random side panels.
def _in_main(btn_sel: str) -> str:
    return f"{MAIN_AREA_SELECTOR} {btn_sel}"


LOAD_MORE_SELECTORS = [
    # Scoped to the ad-library main area only
    ("main:aria:Load more",    f"{MAIN_AREA_SELECTOR} [aria-label='Load more']"),
    ("main:aria:See more",     f"{MAIN_AREA_SELECTOR} [aria-label='See more']"),
    ("main:aria:Show more",    f"{MAIN_AREA_SELECTOR} [aria-label='Show more']"),
    ("main:role:Load more",    f"{MAIN_AREA_SELECTOR} div[role='button']:has-text('Load more')"),
    ("main:role:See more",     f"{MAIN_AREA_SELECTOR} div[role='button']:has-text('See more')"),
    ("main:role:Show more",    f"{MAIN_AREA_SELECTOR} div[role='button']:has-text('Show more')"),
    ("main:button:Load more",  f"{MAIN_AREA_SELECTOR} button:has-text('Load more')"),
    ("main:button:See more",   f"{MAIN_AREA_SELECTOR} button:has-text('See more')"),
    # Fallback: any button containing "See more" in the main area
    ("main:any-See more",      f"{MAIN_AREA_SELECTOR} :is(button, div[role='button'], a):has-text('See more')"),
]


# ---------------------------------------------------------------------------
#  Counting + signature
# ---------------------------------------------------------------------------

def _count_cards(page) -> int:
    total = 0
    for sel in CARD_SELECTORS:
        try:
            total += page.locator(sel).count()
        except Exception:
            pass
    return total


def _last_card_signature(page) -> str:
    for sel in CARD_SELECTORS:
        try:
            loc = page.locator(sel)
            n = loc.count()
            if n > 0:
                text = loc.nth(n - 1).inner_text(timeout=2000).strip()
                if text:
                    return _sig_of_text(text)
        except Exception:
            continue
    return ""


# ---------------------------------------------------------------------------
#  Load-more click
# ---------------------------------------------------------------------------

def _try_load_more(page, log) -> bool:
    """Click the load-more button. Returns True if a click succeeded.

    Critical: every selector is scoped to the ads-library main content area
    (see LOAD_MORE_SELECTORS), so we won't accidentally click "See more"
    links inside individual ad cards.
    """
    for desc, sel in LOAD_MORE_SELECTORS:
        try:
            loc = page.locator(sel)
            n = loc.count()
            if n == 0:
                continue
            for i in range(n):
                btn = loc.nth(i)
                try:
                    if not btn.is_visible(timeout=600):
                        continue
                    btn.scroll_into_view_if_needed(timeout=2000)
                    btn.click(timeout=2500)
                    log(f"  clicked {desc} ({n} matches, picked #{i + 1})")
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
#  Scroll the inner container
# ---------------------------------------------------------------------------

def _scroll_inner_container(page, log) -> int:
    """Scroll the deepest, most-contentful scrollable container.
    Returns the number of containers scrolled.
    """
    js = r"""
    () => {
      const candidates = Array.from(document.querySelectorAll('div'))
        .filter(el => {
          const cs = getComputedStyle(el);
          return (cs.overflowY === 'auto' || cs.overflowY === 'scroll')
                 && el.scrollHeight > el.clientHeight + 200;
        })
        .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
      for (const el of candidates.slice(0, 3)) {
        el.scrollTo({ top: el.scrollHeight, behavior: 'instant' });
      }
      window.scrollTo(0, document.body.scrollHeight);
      return candidates.length;
    }
    """
    n = 0
    try:
        n = page.evaluate(js) or 0
        if n:
            log(f"  scrolled {n} inner container(s)")
    except Exception as e:
        log(f"  inner scroll error: {e}")
    try:
        page.mouse.wheel(0, 4000)
    except Exception:
        pass
    return n


# ---------------------------------------------------------------------------
#  Wait for settle (network-aware)
# ---------------------------------------------------------------------------

def _wait_for_settle(page, log) -> None:
    """Wait for the page to actually be done with network + rendering.

    This is the key fix: we install a fetch hook on the first call that
    tracks in-flight requests, and subsequent calls wait for those requests
    to finish before returning. Without this, we count cards before the
    new batch has actually arrived.
    """
    js = r"""
    async () => {
      if (!window.__pendingFetches) {
        window.__pendingFetches = new Set();
        const origFetch = window.fetch.bind(window);
        window.fetch = function(...args) {
          const id = Symbol('fetch');
          window.__pendingFetches.add(id);
          return origFetch(...args).finally(() => window.__pendingFetches.delete(id));
        };
        // Also count XHRs
        const origOpen = XMLHttpRequest.prototype.open;
        const origSend = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.open = function(...a) { this.__id = Symbol('xhr'); return origOpen.apply(this, a); };
        XMLHttpRequest.prototype.send = function(...a) {
          if (this.__id) window.__pendingFetches.add(this.__id);
          this.addEventListener('loadend', () => window.__pendingFetches.delete(this.__id));
          return origSend.apply(this, a);
        };
      }
      if (document.readyState !== 'complete') {
        await new Promise(r => window.addEventListener('load', r, { once: true }));
      }
      // Wait for in-flight fetches to settle
      const start = Date.now();
      while (window.__pendingFetches.size > 0 && Date.now() - start < 10000) {
        await new Promise(r => setTimeout(r, 100));
      }
    }
    """
    try:
        page.evaluate(js)
    except Exception:
        pass
    # Always a small extra pause for layout/paint
    page.wait_for_timeout(int(SCROLL_PAUSE_SEC * 1000))


# ---------------------------------------------------------------------------
#  Legibility CSS
# ---------------------------------------------------------------------------

def _inject_legibility_css(page) -> None:
    css = """
    (() => {
      if (document.getElementById('__scraper_legibility__')) return;
      const style = document.createElement('style');
      style.id = '__scraper_legibility__';
      style.textContent = `
        [data-testid='ad-library-card'] *,
        div[role='article'][data-pagelet^='AdLibrarySearchResult'] *,
        .x1yztbdb, .x1yztbdb * {
          font-size: 1.15em !important;
          line-height: 1.5 !important;
          letter-spacing: 0.01em !important;
          -webkit-font-smoothing: antialiased !important;
          text-rendering: optimizeLegibility !important;
        }
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
#  Wait for the first batch of cards to actually render
# ---------------------------------------------------------------------------

def _wait_for_initial_cards(page, log, max_wait_sec: int = 30) -> int:
    """Wait until at least N cards are visible. Returns the final count."""
    js = """
    async () => {
      const start = Date.now();
      const max = arguments[0] * 1000;
      const selectors = [
        "[data-testid='ad-library-card']",
        "div[role='article'][data-pagelet^='AdLibrarySearchResult']",
        "div[class*='x1yztbdb']"
      ];
      while (Date.now() - start < max) {
        let n = 0;
        for (const s of selectors) {
          n += document.querySelectorAll(s).length;
        }
        if (n >= 5) return n;
        await new Promise(r => setTimeout(r, 250));
      }
      return n;
    }
    """
    # The function above takes a JS arg — Playwright handles it as the second positional
    try:
        result = page.evaluate(
            "(max) => { const start = Date.now(); const selectors = [\"[data-testid='ad-library-card']\", \"div[role='article'][data-pagelet^='AdLibrarySearchResult']\", \"div[class*='x1yztbdb']\"]; let n=0; while (Date.now() - start < max) { n = 0; for (const s of selectors) n += document.querySelectorAll(s).length; if (n >= 5) return n; } return n; }",
            max_wait_sec * 1000,
        )
        log(f"  initial render: {result} cards after wait")
        return int(result or 0)
    except Exception as e:
        log(f"  initial-render wait error: {e}")
        return _count_cards(page)


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
                    "--font-render-hinting=none",
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
            page.wait_for_timeout(5000)

            # -------- Cookie consent --------
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

            # Install the fetch hook (one-time) and inject legibility CSS
            try:
                page.evaluate(
                    """() => {
                      if (!window.__pendingFetches) {
                        window.__pendingFetches = new Set();
                        const origFetch = window.fetch.bind(window);
                        window.fetch = function(...args) {
                          const id = Symbol('fetch');
                          window.__pendingFetches.add(id);
                          return origFetch(...args).finally(() => window.__pendingFetches.delete(id));
                        };
                      }
                    }"""
                )
            except Exception:
                pass
            _inject_legibility_css(page)

            # -------- Wait for the first batch of cards to actually render --------
            initial_cards = _wait_for_initial_cards(page, log, max_wait_sec=30)
            log(f"Initial card count: {initial_cards}")
            last_sig = _last_card_signature(page)
            log(f"Initial last-card sig: {last_sig or '(none)'}")

            # -------- Scroll loop --------
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

                # 1) Try clicking load-more (scoped to the ads library main area)
                clicked = _try_load_more(page, log)

                # 2) Scroll the inner container + window
                _scroll_inner_container(page, log)

                # 3) Wait for network + render to settle
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
            # Stream frequent heartbeats so the SSE connection doesn't time
            # out during this long operation.
            log("Capturing high-DPI full-page screenshot…")
            final_png = job_dir / "final_30_pages.png"
            try:
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(800)
                # Settle while sending heartbeats
                for _ in range(3):
                    try:
                        page.evaluate(PAGE_SETTLE_JS)
                    except Exception:
                        pass
                    page.wait_for_timeout(600)
                    log("  …settling")
                # The actual screenshot
                page.screenshot(path=str(final_png), full_page=True, timeout=120000)
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
