import asyncio
import json
import logging
import time
from pathlib import Path
from playwright.async_api import async_playwright, Playwright, Browser, BrowserContext, Page

logger = logging.getLogger(__name__)

BASE_URL = "https://lms.aut.ac.ir"
CACHE_FILE = Path(__file__).parent / "cache" / "class_urls.json"


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

    async def start(self):
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
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
        return ctx, page

    async def _login_page(self, page: Page) -> bool:
        logger.info("Navigating to Nima LMS...")
        await page.goto(f"{BASE_URL}/users-panel/announcements-list", wait_until="domcontentloaded", timeout=15000)
        logger.info(f"Nima redirected to: {page.url}")

        if "users-panel" in page.url:
            logger.info("Nima: already logged in")
            return True

        try:
            sso_btn = page.locator("a:has-text('ورود با سامانه یکپارچه')")
            if await sso_btn.count() > 0:
                logger.info("Clicking Nima SSO button...")
                await sso_btn.first.click()
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                logger.info(f"Nima CAS page: {page.url}")
            else:
                logger.warning(f"No SSO button found on: {page.url}")
                return False

            await page.wait_for_selector("input[type='password']", timeout=10000)
            user_input = page.locator("input[type='text'], input[name*='user'], input[id*='user']").first
            await user_input.fill(self.username)
            await page.locator("input[type='password']").first.fill(self.password)
            await page.locator("button[type='submit'], input[type='submit']").first.click()
            await page.wait_for_load_state("domcontentloaded", timeout=20000)
            await page.wait_for_timeout(2000)
        except Exception as e:
            logger.error(f"Nima login error: {e}")
            return False

        logger.info(f"After Nima login URL: {page.url}")
        if "users-panel" in page.url or ("lms.aut.ac.ir" in page.url and "login" not in page.url.lower()):
            logger.info("Nima login successful")
            return True

        logger.error(f"Nima login failed. URL: {page.url}")
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

    async def attend_class(self, class_name: str, keywords: list[str]) -> bool:
        """
        Find the active session for this class on the announcements page and click ورود.
        Looks for a join button inside a section that also contains a keyword match
        so it doesn't click the wrong class's button.
        """
        logger.info(f"[Nima] Attending '{class_name}'")
        ctx, page = await self._new_page()
        try:
            if not await self._login_page(page):
                return False

            # First check if there's a cached per-class URL
            cache = _load_cache()
            class_url = cache.get(class_name)
            if class_url:
                logger.info(f"[Nima] Using cached URL: {class_url}")
                await page.goto(class_url, wait_until="domcontentloaded", timeout=12000)
                await page.wait_for_timeout(1000)
            else:
                await page.goto(f"{BASE_URL}/users-panel/announcements-list", wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)

            # Try keyword-scoped join button first (correct class section)
            join_btn = None
            for kw in keywords:
                scoped = page.locator(f"*:has-text('{kw}')").filter(
                    has=page.locator("a:has-text('ورود'), button:has-text('ورود')")
                ).last
                if await scoped.count() > 0:
                    join_btn = scoped.locator("a:has-text('ورود'), button:has-text('ورود')").first
                    logger.info(f"[Nima] Found scoped ورود button near '{kw}'")
                    break

            # Fall back to any ورود / شرکت / Join button on the page
            if join_btn is None or await join_btn.count() == 0:
                logger.warning(f"[Nima] No keyword-scoped button, trying any join button")
                join_btn = page.locator(
                    "a:has-text('ورود'), button:has-text('ورود'), "
                    "a:has-text('شرکت'), a:has-text('Join')"
                ).first

            if await join_btn.count() == 0:
                logger.warning(f"[Nima] No join button found — session not live yet")
                logger.debug(f"[Nima] Page snippet: {(await page.content())[:800]}")
                return False

            await join_btn.click()
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
            await page.wait_for_timeout(1000)
            logger.info(f"[Nima] ✓ Joined '{class_name}'! URL: {page.url}")
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

            cache = _load_cache()
            target_url = cache.get(class_name, f"{BASE_URL}/users-panel/announcements-list")
            await page.goto(target_url, wait_until="domcontentloaded")

            remaining = hold_until_ts - time.time()
            logger.info(f"[Nima/{class_name}] Holding for {remaining/60:.1f} more minutes...")

            while time.time() < hold_until_ts:
                await asyncio.sleep(min(keepalive_interval, hold_until_ts - time.time()))
                if time.time() < hold_until_ts:
                    try:
                        await page.evaluate("window.scrollBy(0,1)")
                        logger.info(f"[Nima/{class_name}] Keepalive ({(hold_until_ts - time.time())/60:.1f} min left)")
                    except Exception:
                        logger.warning(f"[Nima/{class_name}] Keepalive failed")
                        break

            logger.info(f"[Nima/{class_name}] ✓ Hold complete")
        finally:
            await ctx.close()
