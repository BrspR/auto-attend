import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright, Playwright, Browser, BrowserContext, Page

logger = logging.getLogger(__name__)

CACHE_FILE = Path(__file__).parent / "cache" / "class_urls.json"
BASE_URL = "https://lmshome.aut.ac.ir"


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())
    return {}


def _save_cache(cache: dict):
    CACHE_FILE.parent.mkdir(exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))


def _meetings_url(lesson_url: str) -> str:
    """Convert /panel/myLesson/COURSE/GROUP/TERM → /panel/getMeetingsLesson/COURSE/GROUP"""
    m = re.search(r"/panel/myLesson/(\d+)/(\w+)/", lesson_url)
    if m:
        return f"{BASE_URL}/panel/getMeetingsLesson/{m.group(1)}/{m.group(2)}"
    return lesson_url


class LMSMain:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self._pw: Playwright | None = None
        self._browser: Browser | None = None

    async def start(self):
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        logger.info("Browser started")

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        logger.info("Browser stopped")

    async def _new_page(self) -> tuple[BrowserContext, Page]:
        ctx = await self._browser.new_context(locale="fa-IR")
        page = await ctx.new_page()
        return ctx, page

    async def _login_page(self, page: Page) -> bool:
        await page.goto(f"{BASE_URL}/panel/home", wait_until="domcontentloaded")

        sso_btn = page.locator("a:has-text('ورود با سامانه یکپارچه')")
        if await sso_btn.count() == 0 and "panel" in page.url:
            logger.info("Already logged in to main LMS")
            return True

        logger.info("Logging into main LMS via SSO...")
        try:
            await sso_btn.first.click()
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            logger.info(f"SSO page: {page.url}")

            await page.wait_for_selector("input[type='password']", timeout=10000)
            user_input = page.locator("input[type='text'], input[name*='user'], input[id*='user']").first
            await user_input.fill(self.username)
            await page.locator("input[type='password']").first.fill(self.password)
            await page.locator("button[type='submit'], input[type='submit']").first.click()
            await page.wait_for_load_state("domcontentloaded", timeout=20000)
            await page.wait_for_timeout(2000)
        except Exception as e:
            logger.error(f"Login error: {e}")
            return False

        success = "panel" in page.url
        logger.info(f"Login {'successful' if success else 'FAILED'} — URL: {page.url}")
        return success

    async def _join_bbb_session(self, page: Page, class_name: str) -> str | None:
        """
        Fararoom session attendance flow:
          1. Sessions page lists each session with a collapsed row
          2. Click the '+' button next to a session row to expand it
          3. Inside the expanded row: a blue 'لینک ورود / ورود به جلسه / BigBlueButton' link
          4. Navigate to that link's href directly (avoids new-tab issues)
          5. Return the BBB URL we landed on, or None if no live session found
        Checks pages last→first so the most recent session is tried first.
        """
        # Detect pagination
        page_links = await page.locator("a.page-link[href*='?page=']").all()
        max_page = 1
        for lnk in page_links:
            href = await lnk.get_attribute("href") or ""
            m = re.search(r"\?page=(\d+)", href)
            if m:
                max_page = max(max_page, int(m.group(1)))

        logger.info(f"[{class_name}] Sessions list: {max_page} page(s), scanning last→first")

        for pg in range(max_page, 0, -1):
            if pg != 1:
                pg_url = page.url.split("?")[0] + f"?page={pg}"
                await page.goto(pg_url, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(500)

            # Find all + expand triggers (accordion toggles) on this page
            expand_btns = page.locator(
                "button:has-text('+')"
                ", [data-toggle='collapse']"
                ", [data-bs-toggle='collapse']"
                ", td button"
            )
            btn_count = await expand_btns.count()
            logger.info(f"[{class_name}] Page {pg}: {btn_count} session row(s) found")

            # Try rows in reverse order (bottom = most recent session)
            for i in range(btn_count - 1, -1, -1):
                btn = expand_btns.nth(i)
                try:
                    await btn.click()
                    await page.wait_for_timeout(800)  # let accordion expand

                    # Look for the blue BBB join link in the now-expanded content
                    join_link = page.locator(
                        "a:has-text('لینک ورود')"
                        ", a:has-text('ورود به جلسه')"
                        ", a:has-text('BigBlueButton')"
                        ", a.btn-primary:has-text('ورود')"
                        ", a[href*='bigbluebutton']"
                        ", a[href*='/join']"
                    ).first

                    if await join_link.count() > 0:
                        bbb_href = await join_link.get_attribute("href") or ""
                        logger.info(f"[{class_name}] BBB link found: {bbb_href[:100]}")

                        # Navigate directly to BBB URL to avoid new-tab handling
                        target = bbb_href if bbb_href.startswith("http") else f"{BASE_URL}{bbb_href}"
                        await page.goto(target, wait_until="domcontentloaded", timeout=25000)
                        logger.info(f"[{class_name}] ✓ Joined BBB! Now at: {page.url}")
                        return page.url

                    # No join link in this row — collapse it and try the next
                    await btn.click()
                    await page.wait_for_timeout(300)

                except Exception as e:
                    logger.debug(f"[{class_name}] Row {i} expand error: {e}")
                    continue

        return None

    async def discover_all_urls(self, classes: list[dict]) -> dict[str, str]:
        """
        Login once, scrape the home page for lesson links, match against each class
        by keywords. Longest-keyword-wins avoids substring collisions.
        Saves results to cache automatically.
        """
        found: dict[str, str] = {}
        ctx, page = await self._new_page()
        try:
            if not await self._login_page(page):
                logger.error("Cannot discover URLs — login failed")
                return found

            await page.goto(f"{BASE_URL}/panel/home", wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(3000)

            all_links: list[dict] = await page.evaluate("""
                () => Array.from(document.querySelectorAll('a[href]')).map(a => ({
                    text: a.innerText.trim(),
                    href: a.getAttribute('href') || ''
                }))
            """)

            remaining = {cls["name"]: cls for cls in classes}

            for link in all_links:
                if not remaining:
                    break
                text = link["text"]
                href = link["href"]
                if not href or href.startswith("#") or "javascript" in href:
                    continue

                # Longest-matching keyword wins
                best_name, best_kw_len = None, 0
                for name, cls in remaining.items():
                    for kw in cls["keywords"]:
                        if kw in text and len(kw) > best_kw_len:
                            best_name, best_kw_len = name, len(kw)

                if best_name is None:
                    continue

                if "/panel/myLesson/" in href:
                    url = href if href.startswith("http") else f"{BASE_URL}{href}"
                else:
                    target = href if href.startswith("http") else f"{BASE_URL}{href}"
                    try:
                        await page.goto(target, wait_until="domcontentloaded", timeout=12000)
                        url = page.url if "/panel/myLesson/" in page.url else None
                    except Exception:
                        url = None
                    finally:
                        await page.goto(f"{BASE_URL}/panel/home", wait_until="domcontentloaded", timeout=15000)
                        await page.wait_for_timeout(800)

                if url:
                    found[best_name] = url
                    del remaining[best_name]
                    logger.info(f"✓ {best_name}: {url}")

            cache = _load_cache()
            cache.update(found)
            _save_cache(cache)

            for name in remaining:
                logger.warning(f"✗ {name}: NOT FOUND — add manually to cache/class_urls.json")

            return found
        finally:
            await ctx.close()

    async def attend_class(self, class_name: str, keywords: list[str]) -> bool:
        """Click the BBB join button to register attendance. Returns True on success."""
        cache = _load_cache()
        lesson_url = cache.get(class_name)
        if not lesson_url:
            logger.error(f"[{class_name}] No URL in cache — run --discover first")
            return False

        meetings_url = _meetings_url(lesson_url)
        logger.info(f"[{class_name}] Checking sessions at: {meetings_url}")

        ctx, page = await self._new_page()
        try:
            if not await self._login_page(page):
                return False

            await page.goto(meetings_url, wait_until="domcontentloaded", timeout=15000)
            bbb_url = await self._join_bbb_session(page, class_name)

            if not bbb_url:
                logger.warning(f"[{class_name}] No live BBB session found — professor hasn't started yet")

            return bbb_url is not None
        except Exception as e:
            logger.error(f"[{class_name}] Error: {e}")
            return False
        finally:
            await ctx.close()

    async def attend_and_hold(
        self,
        class_name: str,
        keywords: list[str],
        class_url: str | None,
        hold_until_ts: float,
        keepalive_interval: int = 600,
    ):
        """Join the BBB session and stay on the page until hold_until_ts."""
        cache = _load_cache()
        lesson_url = class_url or cache.get(class_name)
        if not lesson_url:
            logger.error(f"[{class_name}] No URL for hold session")
            return

        meetings_url = _meetings_url(lesson_url)
        ctx, page = await self._new_page()
        try:
            if not await self._login_page(page):
                return

            await page.goto(meetings_url, wait_until="domcontentloaded", timeout=15000)
            bbb_url = await self._join_bbb_session(page, class_name)

            if not bbb_url:
                logger.warning(f"[{class_name}] Could not join BBB for hold — session may not be live")
                return

            hold_end = datetime.fromtimestamp(hold_until_ts).strftime("%H:%M")
            remaining = hold_until_ts - time.time()
            logger.info(f"[{class_name}] Holding on BBB for {remaining/60:.1f} min until {hold_end}")

            while time.time() < hold_until_ts:
                sleep_secs = min(keepalive_interval, hold_until_ts - time.time())
                if sleep_secs <= 0:
                    break
                await asyncio.sleep(sleep_secs)
                if time.time() < hold_until_ts:
                    try:
                        await page.evaluate("window.scrollBy(0, 1)")
                        logger.info(f"[{class_name}] Keepalive on BBB — {(hold_until_ts - time.time())/60:.1f} min left")
                    except Exception:
                        logger.warning(f"[{class_name}] BBB keepalive failed — page may have closed")
                        break

            logger.info(f"[{class_name}] ✓ Hold complete")
        finally:
            await ctx.close()
