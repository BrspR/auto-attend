import asyncio
import json
import logging
import time
from pathlib import Path
from playwright.async_api import async_playwright, Playwright, Browser, BrowserContext, Page

import bbb

logger = logging.getLogger(__name__)

BASE_URL = "https://lms.aut.ac.ir"
CACHE_FILE = Path(__file__).parent / "cache" / "class_urls.json"


def _norm(s: str) -> str:
    """Unify Arabic/Persian yeh (ي→ی) & kaf (ك→ک), turn ZWNJ into a space,
    collapse whitespace — so keyword matching survives the LMS's mixed encoding."""
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


class LMSNima:
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
        from lms_main import CHROMIUM_ARGS
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        logger.info("Nima browser started")

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        logger.info("Nima browser stopped")

    async def _new_page(self) -> tuple[BrowserContext, Page]:
        ctx = await self._browser.new_context(locale="fa-IR")
        page = await ctx.new_page()
        # Scaled defaults so even calls without an explicit timeout grow on retries
        page.set_default_timeout(self._t(30000))
        page.set_default_navigation_timeout(self._t(40000))
        return ctx, page

    async def _looks_authenticated(self, page: Page) -> bool:
        """
        Nima is a single-page app. When NOT logged in, /users-panel/* loads an
        empty shell (URL still contains 'users-panel') and then redirects to
        /login. So a URL substring is a false positive — we must check for
        authenticated-only CONTENT. The announcements panel renders the heading
        'اعلانات' and the schedule-table header 'وضعیت' only when logged in.
        """
        url = page.url.lower()
        if "/login" in url or "courses.aut.ac.ir" in url or "accounts.aut.ac.ir" in url:
            return False
        # The SPA may take a few seconds to render; poll the body text for an
        # authenticated-only marker. 'خروج' (logout) and 'اعلانات' (announcements)
        # only appear once logged in.
        markers = ("اعلانات", "خروج", "جلسات", "وضعیت")
        for _ in range(10):
            if "/login" in page.url.lower():
                return False
            try:
                text = await page.locator("body").inner_text()
            except Exception:
                text = ""
            if any(m in text for m in markers):
                return True
            await page.wait_for_timeout(1000)
        return False

    async def _login_page(self, page: Page) -> bool:
        logger.info("Navigating to Nima LMS...")
        await page.goto(
            f"{BASE_URL}/users-panel/announcements-list",
            wait_until="domcontentloaded", timeout=self._t(30000),
        )
        await page.wait_for_timeout(self._t(3000))  # let the SPA settle / redirect
        logger.info(f"Nima landed on: {page.url}")

        if await self._looks_authenticated(page):
            logger.info("Nima: already logged in")
            return True

        # Real login: /login redirects to the Moodle login page on courses.aut.ac.ir
        # which carries the 'ورود با سامانه یکپارچه' (unified SSO) button.
        logger.info("Nima: not authenticated — logging in via /login → SSO")
        try:
            await page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded", timeout=self._t(30000))
            await page.wait_for_timeout(self._t(2000))

            sso_btn = page.locator(
                "a:has-text('ورود با سامانه یکپارچه'), "
                "button:has-text('ورود با سامانه یکپارچه'), "
                "a:has-text('سامانه یکپارچه')"
            )
            await sso_btn.first.wait_for(timeout=self._t(20000))
            logger.info(f"Nima SSO button found on: {page.url}")
            await sso_btn.first.click()
            await page.wait_for_load_state("domcontentloaded", timeout=self._t(40000))
            await page.wait_for_timeout(self._t(1500))

            # CAS may prompt for credentials, or skip straight back if already SSO'd
            if await page.locator("input[type='password']").count() > 0:
                logger.info(f"Nima CAS login form at: {page.url}")
                user_input = page.locator(
                    "input[type='text'], input[name*='user'], input[id*='user']"
                ).first
                await user_input.fill(self.username)
                await page.locator("input[type='password']").first.fill(self.password)
                await page.locator("button[type='submit'], input[type='submit']").first.click()
                await page.wait_for_load_state("domcontentloaded", timeout=self._t(40000))
                await page.wait_for_timeout(self._t(4000))
        except Exception as e:
            logger.error(f"Nima login error: {e}")
            return False

        # Re-verify by content, never by URL substring
        await page.goto(
            f"{BASE_URL}/users-panel/announcements-list",
            wait_until="domcontentloaded", timeout=self._t(30000),
        )
        await page.wait_for_timeout(self._t(3000))
        if await self._looks_authenticated(page):
            logger.info(f"Nima login successful — URL: {page.url}")
            return True

        logger.error(f"Nima login FAILED — URL: {page.url}")
        return False

    async def discover_all_urls(self, classes: list[dict]) -> dict[str, str]:
        """
        Login once, scan Nima panel pages for course links, match by keywords.
        Nima uses a different structure than Fararoom — we scan multiple panel
        pages to find course-specific URLs and save them to cache.
        """
        found: dict[str, str] = {}
        ctx, page = await self._new_page()
        try:
            if not await self._login_page(page):
                logger.error("Cannot discover Nima URLs — login failed")
                return found

            # Try known panel pages that might list courses
            candidate_pages = [
                f"{BASE_URL}/users-panel/lessons-list",
                f"{BASE_URL}/users-panel/courses-list",
                f"{BASE_URL}/users-panel/announcements-list",
                f"{BASE_URL}/users-panel",
            ]

            all_links: list[dict] = []
            for panel_url in candidate_pages:
                try:
                    await page.goto(panel_url, wait_until="domcontentloaded", timeout=12000)
                    await page.wait_for_timeout(2000)
                    links = await page.evaluate("""
                        () => Array.from(document.querySelectorAll('a[href]')).map(a => ({
                            text: a.innerText.trim(),
                            href: a.getAttribute('href') || ''
                        }))
                    """)
                    all_links.extend(links)
                except Exception:
                    pass

            remaining = {cls["name"]: cls for cls in classes}

            for link in all_links:
                if not remaining:
                    break
                text = link["text"]
                href = link["href"]
                if not href or href.startswith("#") or "javascript" in href:
                    continue

                # Longest-keyword-wins matching
                best_name, best_kw_len = None, 0
                for name, cls in remaining.items():
                    for kw in cls["keywords"]:
                        if kw in text and len(kw) > best_kw_len:
                            best_name, best_kw_len = name, len(kw)

                if best_name is None:
                    continue

                url = href if href.startswith("http") else f"{BASE_URL}{href}"
                found[best_name] = url
                del remaining[best_name]
                logger.info(f"✓ [Nima] {best_name}: {url}")

            if found:
                cache = _load_cache()
                cache.update(found)
                _save_cache(cache)

            # For Nima, attendance works via the announcements page regardless of
            # per-class URL — log a note for any still-unfound classes
            for name in remaining:
                logger.info(
                    f"[Nima] {name}: no direct URL found — "
                    "attendance will use announcements-list (works when session is live)"
                )

            return found
        finally:
            await ctx.close()

    # Match a "join" control: any ورود link/button (the live-session join is
    # labelled with ورود), or a direct BBB link. We deliberately do NOT fall back
    # to a bare green button — the green PrimeNG buttons on this page are 'refresh'
    # icon buttons. The unified-SSO button "ورود با سامانه یکپارچه" only exists on
    # the unauthenticated Moodle page (not here, post-login), but we still drop any
    # control whose own label contains 'سامانه' as a safety net.
    _JOIN_SELECTOR = (
        "a:has-text('ورود'), button:has-text('ورود'), "
        "a[href*='bigbluebutton'], a[href*='/join'], a[href*='blue3'], a[href*='blue']"
    )
    _ROW_TEXT_JS = (
        "(node) => {"
        "  const a = node.closest('tr, li, [class*=card], [class*=row], [class*=item]');"
        "  return a ? a.innerText : (node.parentElement ? node.parentElement.innerText : '');"
        "}"
    )

    async def _find_join_button(self, page: Page, class_name: str, keywords: list[str]):
        """
        Return a locator for this class's ورود control, or None if not live.
        Scoping is done on NORMALIZED text (Arabic/Persian yeh + ZWNJ) so it
        survives the LMS's mixed encoding — the same bug that broke Fararoom.
        """
        controls = page.locator(self._JOIN_SELECTOR)
        n = await controls.count()
        candidates = []  # (locator, normalized enclosing-row text)
        for i in range(n):
            el = controls.nth(i)
            try:
                own = _norm(await el.inner_text())
            except Exception:
                own = ""
            if "سامانه" in own:  # unified-SSO button, not a class join
                continue
            try:
                row = _norm(await el.evaluate(self._ROW_TEXT_JS))
            except Exception:
                row = ""
            candidates.append((el, row))

        if not candidates:
            return None

        kws = [_norm(k) for k in keywords]
        for el, row in candidates:
            if any(k and k in row for k in kws):
                logger.info(f"[Nima] Matched ورود control for '{class_name}' by row text")
                return el

        # No keyword match: only act if exactly one join control is live (one Nima
        # session per time slot). Refuse to guess when several are present.
        if len(candidates) == 1:
            logger.info("[Nima] One ورود control present, no keyword match — using it")
            return candidates[0][0]
        logger.warning(f"[Nima] {len(candidates)} ورود controls, none matched '{class_name}' — refusing to guess")
        return None

    async def _dump_debug(self, page: Page, class_name: str):
        """Save HTML + screenshot so a real live session can be inspected later."""
        try:
            safe = class_name.replace(" ", "_").replace("/", "_")[:30]
            html_path = Path(__file__).parent / f"nima_debug_{safe}.html"
            png_path = Path(__file__).parent / f"nima_debug_{safe}.png"
            html_path.write_text(await page.content(), encoding="utf-8")
            await page.screenshot(path=str(png_path), full_page=True)
            logger.info(f"[Nima] Saved debug dump: {html_path.name} / {png_path.name}")
        except Exception as e:
            logger.warning(f"[Nima] debug dump failed: {e}")

    async def _open_session(self, page: Page, class_name: str, keywords: list[str]):
        """
        Go to the front page (announcements-list shows جلسات فعلی), find this
        class's ورود button and click it. Returns the page to stay on (which may
        be a new tab if ورود opens BBB in a popup), or None if not joinable.
        """
        await page.goto(
            f"{BASE_URL}/users-panel/announcements-list",
            wait_until="domcontentloaded", timeout=self._t(30000),
        )
        await page.wait_for_timeout(self._t(4000))  # let جلسات فعلی render

        btn = await self._find_join_button(page, class_name, keywords)
        if btn is None:
            return None

        pages_before = len(page.context.pages)
        await btn.click()
        await page.wait_for_timeout(self._t(3000))

        if len(page.context.pages) > pages_before:
            new_page = page.context.pages[-1]
            try:
                await new_page.wait_for_load_state("domcontentloaded", timeout=self._t(20000))
            except Exception:
                pass
            logger.info(f"[Nima] ✓ '{class_name}' opened in new tab — URL: {new_page.url}")
            return new_page

        logger.info(f"[Nima] ✓ '{class_name}' ورود clicked — URL: {page.url}")
        return page

    async def attend_class(self, class_name: str, keywords: list[str]) -> bool:
        """Log in, open the front page, click this class's green ورود button."""
        logger.info(f"[Nima] Attending '{class_name}'")
        ctx, page = await self._new_page()
        try:
            if not await self._login_page(page):
                return False

            session_page = await self._open_session(page, class_name, keywords)
            if session_page is None:
                logger.warning(f"[Nima] No live ورود button for '{class_name}' — session not started yet")
                await self._dump_debug(page, class_name)
                return False
            return True
        except Exception as e:
            logger.error(f"[Nima] Error: {e}")
            return False
        finally:
            await ctx.close()

    async def attend_and_hold(
        self,
        class_name: str,
        keywords: list[str],
        hold_until_ts: float,
        keepalive_interval: int = 600,
    ):
        ctx, page = await self._new_page()
        try:
            if not await self._login_page(page):
                return

            # Re-join and stay on the resulting page (BBB tab if it popped one,
            # otherwise the front page) to keep the session alive.
            session_page = await self._open_session(page, class_name, keywords)
            hold_page = session_page or page

            # Stay in the room: keepalive + auto-answer polls + خسته نباشید near end
            await bbb.hold_with_presence(hold_page, class_name, hold_until_ts)
        finally:
            await ctx.close()
