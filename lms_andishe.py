import asyncio
import logging
from playwright.async_api import async_playwright, Playwright, Browser, BrowserContext, Page

logger = logging.getLogger(__name__)

BASE_URL = "https://lms.aut.ac.ir"


class LMSAndishe:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self._pw: Playwright | None = None
        self._browser: Browser | None = None

    async def start(self):
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        logger.info("Andishe browser started")

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        logger.info("Andishe browser stopped")

    async def _new_page(self) -> tuple[BrowserContext, Page]:
        ctx = await self._browser.new_context(locale="fa-IR")
        page = await ctx.new_page()
        return ctx, page

    async def _login_page(self, page: Page) -> bool:
        logger.info("Navigating to Andishe LMS...")
        # lms.aut.ac.ir redirects to courses.aut.ac.ir (Moodle) for auth
        await page.goto(BASE_URL, wait_until="networkidle", timeout=20000)
        logger.info(f"Andishe redirected to: {page.url}")

        # If already logged in (back at lms.aut.ac.ir)
        if "lms.aut.ac.ir" in page.url and "login" not in page.url.lower():
            logger.info("Andishe: already logged in")
            return True

        # We're on courses.aut.ac.ir/login — click the SSO button
        try:
            sso_btn = page.locator("a:has-text('ورود با سامانه یکپارچه')")
            if await sso_btn.count() > 0:
                logger.info("Clicking Andishe SSO button...")
                await sso_btn.first.click()
                await page.wait_for_load_state("networkidle", timeout=15000)
                logger.info(f"Andishe SSO page: {page.url}")
            else:
                logger.warning(f"No SSO button found on: {page.url}")

            # Fill credentials on CAS page (accounts.aut.ac.ir)
            await page.wait_for_selector("input[type='text'], input[type='password']", timeout=10000)
            user_input = page.locator("input[type='text'], input[name*='user'], input[id*='user']").first
            pass_input = page.locator("input[type='password']").first
            await user_input.fill(self.username)
            await pass_input.fill(self.password)
            await page.locator("button[type='submit'], input[type='submit']").first.click()
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception as e:
            logger.error(f"Andishe credential fill error: {e}")
            return False

        logger.info(f"After Andishe login URL: {page.url}")
        if "login" not in page.url.lower() or "lms.aut.ac.ir" in page.url:
            logger.info("Andishe login successful")
            return True

        logger.error(f"Andishe login failed. URL: {page.url}")
        return False

    async def attend_class(self, class_name: str) -> bool:
        logger.info(f"[Andishe] Attending '{class_name}'")
        ctx, page = await self._new_page()
        try:
            if not await self._login_page(page):
                return False

            announcements_url = f"{BASE_URL}/users-panel/announcements-list"
            logger.info(f"Navigating to announcements: {announcements_url}")
            await page.goto(announcements_url, wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle", timeout=10000)

            # Look for class session link or join button for Andishe
            join_btn = page.locator(
                "a:has-text('ورود'), "
                "button:has-text('ورود'), "
                "a:has-text('شرکت'), "
                "a:has-text('Join'), "
                "a[href*='join'], "
                "a[href*='attend')"
            ).first

            if await join_btn.count() == 0:
                logger.warning(f"[Andishe] No join button found on announcements page")
                logger.info(f"[Andishe] Page content preview: {(await page.content())[:500]}")
                await ctx.close()
                return False

            await join_btn.click()
            await page.wait_for_load_state("networkidle", timeout=10000)
            logger.info(f"[Andishe] ✓ Joined class! URL: {page.url}")
            return True
        except Exception as e:
            logger.error(f"[Andishe] Error: {e}")
            await ctx.close()
            return False

    async def attend_and_hold(
        self,
        class_name: str,
        hold_until_ts: float,
        keepalive_interval: int = 600,
    ):
        ctx, page = await self._new_page()
        try:
            if not await self._login_page(page):
                return

            announcements_url = f"{BASE_URL}/users-panel/announcements-list"
            await page.goto(announcements_url, wait_until="domcontentloaded")

            import time
            remaining = hold_until_ts - time.time()
            logger.info(f"[Andishe/{class_name}] Holding for {remaining/60:.1f} more minutes...")

            while time.time() < hold_until_ts:
                await asyncio.sleep(min(keepalive_interval, hold_until_ts - time.time()))
                if time.time() < hold_until_ts:
                    try:
                        await page.evaluate("window.scrollBy(0,1)")
                        logger.info(f"[Andishe/{class_name}] Keepalive ({(hold_until_ts - time.time())/60:.1f} min left)")
                    except Exception:
                        logger.warning(f"[Andishe/{class_name}] Keepalive failed")
                        break

            logger.info(f"[Andishe/{class_name}] ✓ Hold complete")
        finally:
            await ctx.close()
