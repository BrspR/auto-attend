import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright, Playwright, Browser, BrowserContext, Page

import bbb

logger = logging.getLogger(__name__)

CACHE_FILE = Path(__file__).parent / "cache" / "class_urls.json"
BASE_URL = "https://lmshome.aut.ac.ir"


def _norm(s: str) -> str:
    """
    Normalize Persian text for keyword matching. The LMS renders class names with
    Arabic yeh (ي) / kaf (ك) and ZWNJ joiners (e.g. 'رياضي', 'تدريس‌يار'), while
    config.py uses Persian yeh (ی) / kaf (ک) and plain spaces — so a raw substring
    test fails on different Unicode codepoints. Unify them and treat ZWNJ as a space.
    """
    if not s:
        return ""
    s = s.replace("ي", "ی").replace("ك", "ک")
    for z in ("‌", "‍", "‎", "‏"):  # ZWNJ/ZWJ + bidi marks
        s = s.replace(z, " ")
    return " ".join(s.split())


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())
    return {}


def _save_cache(cache: dict):
    CACHE_FILE.parent.mkdir(exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))


class LMSMain:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self.timeout_scale = 1.0  # bumped by the scheduler on each failed retry

    def _t(self, ms: int) -> int:
        """Scale a timeout (ms) by the current retry multiplier."""
        return int(ms * self.timeout_scale)

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
        # Scaled defaults so even calls without an explicit timeout grow on retries
        page.set_default_timeout(self._t(30000))
        page.set_default_navigation_timeout(self._t(40000))
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
            await page.wait_for_load_state("domcontentloaded", timeout=self._t(30000))
            logger.info(f"SSO page: {page.url}")

            await page.wait_for_selector("input[type='password']", timeout=self._t(20000))
            user_input = page.locator("input[type='text'], input[name*='user'], input[id*='user']").first
            await user_input.fill(self.username)
            await page.locator("input[type='password']").first.fill(self.password)
            await page.locator("button[type='submit'], input[type='submit']").first.click()
            await page.wait_for_load_state("domcontentloaded", timeout=self._t(40000))
            await page.wait_for_timeout(self._t(3000))
        except Exception as e:
            logger.error(f"Login error: {e}")
            return False

        success = "panel" in page.url
        logger.info(f"Login {'successful' if success else 'FAILED'} — URL: {page.url}")
        return success

    async def _join_bbb_session(self, page: Page, class_name: str) -> str | None:
        """
        Join a live BBB session from the /panel/myLesson/ page.

        The page shows a "جلسات امروز من" table with columns:
          روز | تاریخ | ساعت شروع | ساعت پایان | مدرس | نوع جلسه | وضعیت جلسه | لینک ورود

        When a session is live ("در حال برگزاری"), the لینک ورود column contains
        a green button labelled "ورود به جلسه بیگبلوباتن" whose href is the BBB URL
        (typically starts with blue3...).

        Fallback: submit <form action="https://lmshome.aut.ac.ir/join"> if present.
        """
        await page.wait_for_timeout(1500)

        # Primary: green join button in the لینک ورود column
        join_btn = page.locator(
            "a:has-text('ورود به جلسه')"
            ", a:has-text('بیگبلوباتن')"
            ", a.btn-success"
            ", a[href*='blue3']"
            ", a[href*='bigbluebutton']"
        ).first

        if await join_btn.count() > 0:
            bbb_href = await join_btn.get_attribute("href") or ""
            logger.info(f"[{class_name}] BBB link found: {bbb_href[:120]}")
            target = bbb_href if bbb_href.startswith("http") else f"{BASE_URL}{bbb_href}"
            await page.goto(target, wait_until="domcontentloaded", timeout=25000)
            logger.info(f"[{class_name}] ✓ Joined BBB! Now at: {page.url}")
            return page.url

        # Fallback: form-based join
        join_form = page.locator("form[action*='/join']").first
        if await join_form.count() > 0:
            submit = join_form.locator("button[type='submit'], input[type='submit']").first
            if await submit.count() > 0:
                await submit.click()
                await page.wait_for_load_state("domcontentloaded", timeout=25000)
                logger.info(f"[{class_name}] ✓ Joined via form! Now at: {page.url}")
                return page.url

        logger.warning(f"[{class_name}] No join button or form found — session not live yet")
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

            await page.goto(f"{BASE_URL}/panel/home", wait_until="domcontentloaded", timeout=30000)
            # Lesson links load asynchronously after domcontentloaded. Wait for them
            # to appear instead of a fixed sleep — the home page was intermittently
            # scraped empty (0/9), causing discover to silently find nothing.
            try:
                await page.wait_for_selector("a[href*='myLesson']", timeout=25000)
            except Exception:
                logger.warning("No lesson links appeared within 25s — home scrape may be empty")
            await page.wait_for_timeout(1500)

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

                text_n = _norm(text)

                # Longest-matching keyword wins (compared on normalized text)
                best_name, best_kw_len = None, 0
                for name, cls in remaining.items():
                    for kw in cls["keywords"]:
                        kw_n = _norm(kw)
                        if kw_n in text_n and len(kw_n) > best_kw_len:
                            best_name, best_kw_len = name, len(kw_n)

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

    async def list_all_lessons(self) -> list[dict]:
        """Return [{name, url}] for every enrolled lesson — no keyword matching.
        Used by multi-user workers that don't have a preset class list."""
        out: list[dict] = []
        ctx, page = await self._new_page()
        try:
            if not await self._login_page(page):
                return out
            await page.goto(f"{BASE_URL}/panel/home", wait_until="domcontentloaded", timeout=self._t(30000))
            try:
                await page.wait_for_selector("a[href*='myLesson']", timeout=self._t(25000))
            except Exception:
                pass
            await page.wait_for_timeout(1500)
            links = await page.evaluate("""
                () => Array.from(document.querySelectorAll("a[href*='myLesson']")).map(a => ({
                    name: (a.innerText||'').trim().replace(/\\s+/g,' '),
                    href: a.getAttribute('href') || ''
                }))
            """)
            seen = set()
            for l in links:
                href = l["href"]
                url = href if href.startswith("http") else f"{BASE_URL}{href}"
                if url in seen:
                    continue
                seen.add(url)
                out.append({"name": l["name"] or url, "url": url})
            return out
        finally:
            await ctx.close()

    async def check_live(self, lesson_url: str) -> bool:
        """True if this lesson currently has a live BBB join button (no click)."""
        ctx, page = await self._new_page()
        try:
            if not await self._login_page(page):
                return False
            await page.goto(lesson_url, wait_until="domcontentloaded", timeout=self._t(30000))
            await page.wait_for_timeout(1500)
            btn = page.locator(
                "a:has-text('ورود به جلسه'), a:has-text('بیگبلوباتن'), "
                "a.btn-success, a[href*='blue3'], a[href*='bigbluebutton'], form[action*='/join']"
            )
            return await btn.count() > 0
        except Exception:
            return False
        finally:
            await ctx.close()

    async def attend_class(self, class_name: str, keywords: list[str], lesson_url: str | None = None) -> bool:
        """Click the BBB join button to register attendance. Returns True on success."""
        cache = _load_cache()
        lesson_url = lesson_url or cache.get(class_name)
        if not lesson_url:
            logger.error(f"[{class_name}] No URL in cache — run --discover first")
            return False

        logger.info(f"[{class_name}] Navigating to lesson page: {lesson_url}")

        ctx, page = await self._new_page()
        try:
            if not await self._login_page(page):
                return False

            await page.goto(lesson_url, wait_until="domcontentloaded", timeout=self._t(30000))
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

        logger.info(f"[{class_name}] Navigating to lesson page: {lesson_url}")
        ctx, page = await self._new_page()
        try:
            if not await self._login_page(page):
                return

            await page.goto(lesson_url, wait_until="domcontentloaded", timeout=self._t(30000))
            bbb_url = await self._join_bbb_session(page, class_name)

            if not bbb_url:
                logger.warning(f"[{class_name}] Could not join BBB for hold — session may not be live")
                return

            # Stay in the room: keepalive + auto-answer polls + خسته نباشید near end
            await bbb.hold_with_presence(page, class_name, hold_until_ts)
        finally:
            await ctx.close()
