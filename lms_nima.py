import asyncio
import logging
from playwright.async_api import async_playwright, Playwright, Browser, BrowserContext, Page

logger = logging.getLogger(__name__)

BASE_URL = "https://lms.aut.ac.ir"


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
        # Go to a protected page to trigger the full login redirect chain
        await page.goto(f"{BASE_URL}/users-panel/announcements-list", wait_until="domcontentloaded", timeout=15000)
        logger.info(f"Nima redirected to: {page.url}")

        # Already logged in
        if "users-panel" in page.url:
            logger.info("Nima: already logged in")
            return True

        # Should now be on courses.aut.ac.ir/login — click SSO button
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

            # Fill credentials on CAS page (accounts.aut.ac.ir)
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

    async def attend_class(self, class_name: str) -> bool:
        logger.info(f"[Nima] Attending '{class_name}'")
        ctx, page = await self._new_page()
        try:
            if not await self._login_page(page):
                return False

            announcements_url = f"{BASE_URL}/users-panel/announcements-list"
            logger.info(f"Navigating to announcements: {announcements_url}")
            await page.goto(announcements_url, wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle", timeout=10000)

            join_btn = page.locator(
                "a:has-text('ورود'), "
                "button:has-text('ورود'), "
                "a:has-text('شرکت'), "
                "a:has-text('Join'), "
                "a[href*='join'], "
                "a[href*='attend']"
            ).first

            if await join_btn.count() == 0:
                logger.warning(f"[Nima] No join button found on announcements page")
                logger.info(f"[Nima] Page content preview: {(await page.content())[:500]}")
                return False

            await join_btn.click()
            await page.wait_for_load_state("networkidle", timeout=10000)
            logger.info(f"[Nima] ✓ Joined class! URL: {page.url}")
            return True
        except Exception as e:
            logger.error(f"[Nima] Error: {e}")
            return False
        finally:
            await ctx.close()

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
