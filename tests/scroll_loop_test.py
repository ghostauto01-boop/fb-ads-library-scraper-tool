"""
Mock-Playwright tests for the scraper's scroll loop.

Verifies the loop:
  1. Loads N batches correctly when N are available
  2. Caps at target_pages
  3. Exits early on stall (no new content)
  4. Returns correct card counts

Run: python3 tests/scroll_loop_test.py
"""
import sys
import types
from unittest.mock import MagicMock

# --- Mock external deps so we can import scraper.py without installing them
sys.modules["playwright"] = types.ModuleType("playwright")
fake_pw_sync = types.ModuleType("playwright.sync_api")
fake_pw_sync.sync_playwright = lambda: MagicMock()
fake_pw_sync.TimeoutError = type("PWTimeout", (Exception,), {})
sys.modules["playwright.sync_api"] = fake_pw_sync

fake_bs4 = types.ModuleType("bs4")


class _FakeSoup:
    def __init__(self, html, parser):
        self.html = html

    def select(self, sel):
        return []


fake_bs4.BeautifulSoup = _FakeSoup
sys.modules["bs4"] = fake_bs4

import importlib.util

spec = importlib.util.spec_from_file_location("scraper", "scraper.py")
scraper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scraper)


# --- Fake page
class FakePage:
    def __init__(self, total_batches):
        self.batches_left = total_batches
        self.cards = [{"text": f"init {i}"} for i in range(12)]

    def evaluate(self, *a, **kw):
        return 1

    def mouse_wheel(self, *a, **kw):
        pass

    def wait_for_timeout(self, ms):
        pass  # no real sleep in tests

    def locator(self, sel):
        # The new scraper scopes load-more clicks to the ads-library main
        # area. The fake page emulates that structure: a single
        # [role='main'] element containing all the cards AND the load-more
        # button, so the scoped selectors work.
        return FakeLocator(self, sel)

    def content(self):
        h = "<html><body>"
        h += "<div role='main'>"
        for c in self.cards:
            h += f"<div data-testid='ad-library-card'>{c['text']}</div>"
        # Render a load-more button inside the main area
        if self.batches_left > 0:
            h += "<div role='button'>See more</div>"
        h += "</div></body></html>"
        return h

    def screenshot(self, **kw):
        pass

    def goto(self, *a, **kw):
        pass


class FakeLocator:
    def __init__(self, page, sel):
        self.page = page
        self.sel = sel

    def count(self):
        # Scope to [role='main'] for load-more; cards work anywhere.
        if "ad-library-card" in self.sel:
            return len(self.page.cards)
        if any(s in self.sel for s in ["Load more", "See more", "Show more"]):
            # Only return positive if the selector includes a main-area scope
            # AND we still have a load-more button to click
            if ("[role='main']" in self.sel or "[data-pagelet=" in self.sel):
                return 1 if self.page.batches_left > 0 else 0
            return 0
        # The main-area wrapper itself
        if "[role='main']" in self.sel or "[data-pagelet" in self.sel:
            return 1
        return 0

    def nth(self, i):
        return self

    def is_visible(self, timeout=None):
        return self.count() > 0

    def inner_text(self, timeout=None):
        return self.page.cards[-1]["text"] if self.page.cards else ""

    def click(self, timeout=None):
        # If this locator is a scoped load-more click and there's budget, click it
        if self.page.batches_left <= 0:
            return False
        self.page.batches_left -= 1
        n = len(self.page.cards)
        for j in range(12):
            self.page.cards.append({"text": f"new {n + j}"})
        return True

    def scroll_into_view_if_needed(self, timeout=None):
        pass


# --- Helpers: skip network/wait calls in tests
scraper._scroll_inner_container = lambda p, log: None
scraper._wait_for_settle = lambda p, log: None
scraper._inject_legibility_css = lambda p: None


def run_loop(page, target_pages=30, max_iter=200):
    page_advances = 0
    stall = 0
    i = 0
    last_count = scraper._count_cards(page)
    last_sig = scraper._last_card_signature(page)
    while page_advances < target_pages and i < max_iter:
        i += 1
        scraper._try_load_more(page, lambda m: None)
        scraper._scroll_inner_container(page, lambda m: None)
        scraper._wait_for_settle(page, lambda m: None)
        new_count = scraper._count_cards(page)
        new_sig = scraper._last_card_signature(page)
        if new_count > last_count or (new_sig and new_sig != last_sig):
            page_advances += 1
            last_count = new_count
            if new_sig:
                last_sig = new_sig
            stall = 0
        else:
            stall += 1
            if stall >= scraper.STALL_LIMIT:
                break
    return page_advances, i, len(page.cards)


passed = 0


def ok(name):
    global passed
    passed += 1
    print("✓", name)


# A: 5 batches available, target 30 → should load all 5 then exit
page = FakePage(total_batches=5)
adv, it, n = run_loop(page, target_pages=30)
assert adv == 5, f"expected 5 advances, got {adv}"
assert n == 12 + 60, f"expected 72 cards, got {n}"
ok(f"5/5 batches loaded (cards={n})")

# B: 100 batches available, target 30 → should cap at 30
page = FakePage(total_batches=100)
adv, it, n = run_loop(page, target_pages=30)
assert adv == 30, f"expected cap at 30, got {adv}"
ok(f"caps at target_pages=30 (cards={n})")

# C: 0 batches, target 30 → 0 advances, exits on stall
page = FakePage(total_batches=0)
adv, it, n = run_loop(page, target_pages=30)
assert adv == 0, f"expected 0, got {adv}"
assert n == 12, f"expected 12, got {n}"
assert it <= scraper.STALL_LIMIT, f"expected exit by stall limit, got {it} iters"
ok(f"0 batches → 0 advances, exits in {it} iters (stall limit {scraper.STALL_LIMIT})")

# D: 1 batch
page = FakePage(total_batches=1)
adv, it, n = run_loop(page, target_pages=30)
assert adv == 1
assert n == 24
ok("1 batch loaded then stalls")

# E: signatures detect new content even if count is the same (replacement)
# (e.g., FB "rotates" cards on scroll but the count may stay constant)
class ReplacingPage(FakePage):
    def __init__(self):
        super().__init__(total_batches=0)
        self.rotated = 0
    def locator(self, sel):
        # When asked for cards, mutate the last card's text on every call
        # to simulate "content updated" without changing the count
        if "ad-library-card" in sel and self.rotated < 5:
            self.rotated += 1
            self.cards[-1] = {"text": f"rotated {self.rotated}"}
        return super().locator(sel)

page = ReplacingPage()
adv, it, n = run_loop(page, target_pages=10, max_iter=20)
# Signature should detect changes; we expect up to 5 advances
assert adv >= 1, f"expected at least 1 advance from signature change, got {adv}"
ok(f"signature changes count as advances (advances={adv})")

print(f"\n✅ All {passed} scroll-loop tests passed.")
