import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright, Playwright, Browser, BrowserContext, Page

import bbb
import config

logger = logging.getLogger(__name__)

CACHE_FILE = Path(__file__).parent / "cache" / "class_urls.json"
BASE_URL = "https://lmshome.aut.ac.ir"


# Launch flags that cut per-browser RAM and avoid the multi-browser /dev/shm
# crash — important when many user workers run headless Chromium at once.
CHROMIUM_ARGS = [
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-sandbox",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--renderer-process-limit=1",
    "--js-flags=--max-old-space-size=256",
]


def _parse_schedule(page_text: str) -> dict | None:
    """Extract schedule from text containing 'زمان برگزاری'.
    Returns {days:[int,...], start:'HH:MM', end:'HH:MM'} or None.
    Python weekday: Mon=0 Tue=1 Wed=2 Thu=3 Fri=4 Sat=5 Sun=6."""
    import re
    idx = page_text.find("زمان برگزاری")
    if idx == -1:
        return None
    snippet = page_text[idx: idx + 150]
    for p, l in zip("۰۱۲۳۴۵۶۷۸۹", "0123456789"):
        snippet = snippet.replace(p, l)
    for z in ("‌", "‍", "‎", "‏"):
        snippet = snippet.replace(z, " ")
    snippet = " ".join(snippet.split())
    # Longer names first so 'شنبه' doesn't absorb 'یکشنبه' etc.
    DAY_MAP = [
        ("یکشنبه", 6), ("دوشنبه", 0), ("سه شنبه", 1),
        ("چهارشنبه", 2), ("پنجشنبه", 3), ("جمعه", 4),
        ("شنبه", 5),
    ]
    days, work = [], snippet
    for name, num in DAY_MAP:
        if name in work:
            days.append(num)
            work = work.replace(name, "", 1)
    times = re.findall(r"\b\d{1,2}:\d{2}\b", snippet)
    if not days and not times:
        return None
    return {
        "days":  sorted(set(days)),
        "start": times[0] if times else None,
        "end":   times[1] if len(times) >= 2 else None,
    }


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
        self._browser = await self._pw.chromium.launch(headless=True, args=CHROMIUM_ARGS)
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
        # Optimize memory/CPU by blocking images, fonts, and media
        async def block_assets(route):
            if route.request.resource_type in ("image", "font", "media"):
                await route.abort()
            else:
                await route.continue_()
        await page.route("**/*", block_assets)

        # Scaled defaults so even calls without an explicit timeout grow on retries
        page.set_default_timeout(self._t(30000))
        page.set_default_navigation_timeout(self._t(40000))
        return ctx, page

    async def _login_once(self, page: Page) -> bool:
        """One SSO login attempt. Returns True only once /panel/ is actually reached."""
        await page.goto(f"{BASE_URL}/panel/home", wait_until="domcontentloaded")

        sso_btn = page.locator("a:has-text('ورود با سامانه یکپارچه')")
        if await sso_btn.count() == 0 and "panel" in page.url:
            logger.info("Already logged in to main LMS")
            return True

        logger.info("Logging into main LMS via SSO...")
        try:
            # Navigate directly to the Moodle SSO redirection page to trigger the redirect chain
            await page.goto("https://courses.aut.ac.ir/login/index.php?authCASattras=CASattras", wait_until="commit", timeout=self._t(30000))

            await page.wait_for_url(re.compile(r".*cas/login.*"), timeout=self._t(45000))
            logger.info(f"SSO page reached: {page.url}")

            await page.wait_for_selector("input[type='password']", timeout=self._t(30000))
            user_input = page.locator("input[type='text'], input[name*='user'], input[id*='user']").first
            await user_input.fill(self.username)
            await page.locator("input[type='password']").first.fill(self.password)
            await page.locator("button[type='submit'], input[type='submit']").first.click(no_wait_after=True)

            # Wait for the redirect to ACTUALLY land on /panel/. Matching
            # "panel|cas/login" races — we are already ON cas/login when we click,
            # so it returns instantly and reports a false FAILED. Wait for panel
            # only; a genuine failure simply times out and the caller retries.
            try:
                await page.wait_for_url(re.compile(r".*panel.*"), timeout=self._t(20000))
            except Exception:
                pass
            await page.wait_for_timeout(self._t(1500))
        except Exception as e:
            logger.error(f"Login error: {e}")
            return False

        success = "panel" in page.url
        logger.info(f"Login {'successful' if success else 'FAILED'} — URL: {page.url}")
        return success

    async def _login_page(self, page: Page, deadline: float | None = None) -> bool:
        """Log in via SSO, retrying until we are in. Each failed attempt multiplies
        the timeout by config.TIMEOUT_GROWTH (1.5x, capped at MAX_TIMEOUT_SCALE).

        deadline: a time.time() value (e.g. the class end). When given, there is
        NO cap on attempts — we keep logging in, growing the timeout 1.5x each
        time, until we are in or the deadline passes. This is the join path:
        "every time you fail to join the BBB, log in again, 1.5x the wait." When
        None (routine 10-min liveness check) we do a single attempt so the sweep
        cannot hang — the next 10-min cycle is the retry.
        """
        attempt = 0
        while True:
            attempt += 1
            if await self._login_once(page):
                self.timeout_scale = 1.0   # reset for subsequent page ops
                return True
            self.timeout_scale = min(
                config.MAX_TIMEOUT_SCALE,
                self.timeout_scale * config.TIMEOUT_GROWTH,
            )
            if deadline is None:
                return False
            if time.time() >= deadline:
                logger.warning(f"[login] gave up after {attempt} attempts — class deadline passed")
                return False
            logger.info(f"[login] attempt {attempt} failed — retrying with timeout x{self.timeout_scale:.2f}")
            await asyncio.sleep(3)

    async def _join_bbb_session(self, page: Page, class_name: str) -> str | None:
        """
        Join a live BBB session from the /panel/myLesson/ page.

        The page shows a "جلسات امروز من" table with columns:
          روز | تاریخ | ساعت شروع | ساعت پایان | مدرس | نوع جلسه | وضعیت جلسه | لینک ورود

        When a session is live ("در حال برگزاری"), the لینک ورود column contains
        a green button labelled "ورود به جلسه بیگبلوباتن".

        The button is a <button type="submit" class="btn btn-success"> inside
        <form action="https://lmshome.aut.ac.ir/join" method="POST"> with hidden
        fields (_token, room_id, service, scheduleNo).  Clicking submit joins BBB.
        """
        # Comprehensive selector covering all known BBB join button variations
        _join_sel = (
            "form[action*='/join'], "
            "button.btn-success, a.btn-success, "
            "button:has-text('ورود به جلسه'), a:has-text('ورود به جلسه'), "
            "button:has-text('بیگبلوباتن'), a:has-text('بیگبلوباتن'), "
            "a[href*='blue3'], a[href*='bigbluebutton'], "
            "button:has-text('ورود'), a:has-text('ورود')"
        )
        # Wait for the join form/button to appear — the session table may load
        # asynchronously via AJAX after domcontentloaded fires.
        try:
            await page.wait_for_selector(_join_sel, timeout=15000)
            logger.info(f"[{class_name}] Join element appeared on page")
        except Exception:
            # Selector didn't appear within 15s — fall through and check anyway
            logger.info(f"[{class_name}] Join selector not found within 15s, checking DOM anyway")
        await page.wait_for_timeout(1000)  # settle time after element appears

        # ---- Strategy 1: form POST to /join (the actual DOM structure) ----
        join_form = page.locator("form[action*='/join']").first
        if await join_form.count() > 0:
            submit = join_form.locator("button[type='submit'], input[type='submit']").first
            if await submit.count() > 0:
                logger.info(f"[{class_name}] Found form[action*/join] with submit button — clicking")
                await submit.click(no_wait_after=True)
                await page.wait_for_load_state("domcontentloaded", timeout=25000)
                logger.info(f"[{class_name}] ✓ Joined via form POST! Now at: {page.url}")
                return page.url

        # ---- Strategy 2: green btn-success button (any tag) ----
        green_btn = page.locator(
            "button.btn-success:has-text('ورود'), a.btn-success:has-text('ورود'), "
            "button:has-text('ورود به جلسه'), a:has-text('ورود به جلسه'), "
            "button:has-text('بیگبلوباتن'), a:has-text('بیگبلوباتن')"
        ).first
        if await green_btn.count() > 0:
            tag = await green_btn.evaluate("el => el.tagName")
            if tag == "A":
                bbb_href = await green_btn.get_attribute("href") or ""
                logger.info(f"[{class_name}] BBB <a> link found: {bbb_href[:120]}")
                target = bbb_href if bbb_href.startswith("http") else f"{BASE_URL}{bbb_href}"
                await page.goto(target, wait_until="domcontentloaded", timeout=25000)
            else:
                logger.info(f"[{class_name}] Green join <button> found — clicking")
                await green_btn.click(no_wait_after=True)
                await page.wait_for_load_state("domcontentloaded", timeout=25000)
            logger.info(f"[{class_name}] ✓ Joined BBB! Now at: {page.url}")
            return page.url

        # ---- Strategy 3: direct BBB href (legacy fallback) ----
        bbb_link = page.locator(
            "a[href*='blue3'], a[href*='bigbluebutton']"
        ).first
        if await bbb_link.count() > 0:
            bbb_href = await bbb_link.get_attribute("href") or ""
            logger.info(f"[{class_name}] Direct BBB link: {bbb_href[:120]}")
            target = bbb_href if bbb_href.startswith("http") else f"{BASE_URL}{bbb_href}"
            await page.goto(target, wait_until="domcontentloaded", timeout=25000)
            logger.info(f"[{class_name}] ✓ Joined BBB! Now at: {page.url}")
            return page.url

        # ---- Strategy 4: check for "در حال برگزاری" status text, then find nearby button ----
        try:
            status_cell = page.locator("text=در حال برگزاری").first
            if await status_cell.count() > 0:
                # Session is live — find the closest button/link in the same table row
                row = status_cell.locator("xpath=ancestor::tr").first
                if await row.count() > 0:
                    row_btn = row.locator("button, a[href]").last
                    if await row_btn.count() > 0:
                        tag = await row_btn.evaluate("el => el.tagName")
                        if tag == "A":
                            href = await row_btn.get_attribute("href") or ""
                            target = href if href.startswith("http") else f"{BASE_URL}{href}"
                            logger.info(f"[{class_name}] Found row button via 'در حال برگزاری' — navigating: {target[:120]}")
                            await page.goto(target, wait_until="domcontentloaded", timeout=25000)
                        else:
                            logger.info(f"[{class_name}] Found row button via 'در حال برگزاری' — clicking")
                            await row_btn.click(no_wait_after=True)
                            await page.wait_for_load_state("domcontentloaded", timeout=25000)
                        logger.info(f"[{class_name}] ✓ Joined BBB via status row! Now at: {page.url}")
                        return page.url
        except Exception as e:
            logger.warning(f"[{class_name}] Strategy 4 (status text) failed: {e}")

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

    async def scrape_class_schedules(self, lessons: list[dict]) -> list[dict]:
        """Visit each myLesson page once (shared login) and extract زمان برگزاری.
        Returns a copy of lessons with days/start/end added where found."""
        result = []
        ctx, page = await self._new_page()
        try:
            if not await self._login_page(page):
                return lessons
            for lesson in lessons:
                updated = dict(lesson)
                if lesson.get("lms") != "main" or not lesson.get("url"):
                    result.append(updated)
                    continue
                try:
                    await page.goto(lesson["url"], wait_until="domcontentloaded",
                                    timeout=self._t(20000))
                    await page.wait_for_timeout(1000)
                    text = await page.evaluate("() => document.body.innerText")
                    sched = _parse_schedule(text)
                    if sched:
                        updated.update(sched)
                        logger.info(
                            f"[{lesson['name']}] schedule scraped: "
                            f"days={sched['days']} {sched['start']}–{sched['end']}"
                        )
                    else:
                        logger.warning(f"[{lesson['name']}] زمان برگزاری not found on page")
                except Exception as e:
                    logger.warning(f"[{lesson['name']}] schedule scrape error: {e}")
                result.append(updated)
        finally:
            await ctx.close()
        return result

    async def check_live_many(self, lessons: list[dict]) -> dict[str, bool]:
        """Check several lessons ({name, url}) with ONE login/context.
        Returns {url: is_live}. Used by multi-user workers — a per-class
        login (check_live) makes a user with 11 classes do 11 SSO logins
        per sweep, which is slow and hammers CAS with redundant logins."""
        out: dict[str, bool] = {}
        if not lessons:
            return out
        ctx, page = await self._new_page()
        try:
            if not await self._login_page(page):
                return out
            for lesson in lessons:
                url = lesson["url"]
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=self._t(30000))
                    _live_sel = (
                        "form[action*='/join'], "
                        "button.btn-success:has-text('ورود'), a.btn-success:has-text('ورود'), "
                        "button:has-text('ورود به جلسه'), a:has-text('ورود به جلسه'), "
                        "button:has-text('بیگبلوباتن'), a:has-text('بیگبلوباتن'), "
                        "a[href*='blue3'], a[href*='bigbluebutton']"
                    )
                    try:
                        await page.wait_for_selector(_live_sel, timeout=8000)
                        out[url] = True
                    except Exception:
                        out[url] = False
                except Exception as e:
                    logger.warning(f"[{lesson.get('name', url)}] live-check error: {e}")
                    out[url] = False
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
            _live_sel = (
                "form[action*='/join'], "
                "button.btn-success:has-text('ورود'), a.btn-success:has-text('ورود'), "
                "button:has-text('ورود به جلسه'), a:has-text('ورود به جلسه'), "
                "button:has-text('بیگبلوباتن'), a:has-text('بیگبلوباتن'), "
                "a[href*='blue3'], a[href*='bigbluebutton']"
            )
            try:
                await page.wait_for_selector(_live_sel, timeout=8000)
                return True
            except Exception:
                return False
        except Exception:
            return False
        finally:
            await ctx.close()

    async def attend_class(self, class_name: str, keywords: list[str], lesson_url: str | None = None,
                           deadline: float | None = None) -> bool:
        """Click the BBB join button to register attendance. Returns True on success.

        Retries up to 4 times with page reloads between attempts.
        deadline (time.time() value, e.g. class end): keep re-logging-in with a
        1.5x-growing timeout until we are in or the deadline passes."""
        cache = _load_cache()
        lesson_url = lesson_url or cache.get(class_name)
        if not lesson_url:
            logger.error(f"[{class_name}] No URL in cache — run --discover first")
            return False

        logger.info(f"[{class_name}] Navigating to lesson page: {lesson_url}")
        MAX_JOIN_ATTEMPTS = 4

        ctx, page = await self._new_page()
        try:
            if not await self._login_page(page, deadline=deadline):
                return False

            for attempt in range(1, MAX_JOIN_ATTEMPTS + 1):
                logger.info(f"[{class_name}] Join attempt {attempt}/{MAX_JOIN_ATTEMPTS}")
                try:
                    await page.goto(lesson_url, wait_until="domcontentloaded", timeout=self._t(30000))
                    await page.wait_for_timeout(1500)  # let AJAX table render
                    bbb_url = await self._join_bbb_session(page, class_name)
                    if bbb_url:
                        logger.info(f"[{class_name}] ✓ Joined on attempt {attempt}/{MAX_JOIN_ATTEMPTS}")
                        return True
                except Exception as e:
                    logger.warning(f"[{class_name}] Join attempt {attempt} error: {e}")

                if attempt < MAX_JOIN_ATTEMPTS:
                    wait_secs = 5 + attempt * 3  # 8s, 11s, 14s between retries
                    logger.info(f"[{class_name}] Button not found — retrying in {wait_secs}s (attempt {attempt}/{MAX_JOIN_ATTEMPTS})")
                    await asyncio.sleep(wait_secs)

            logger.warning(f"[{class_name}] No live BBB session found after {MAX_JOIN_ATTEMPTS} attempts — professor hasn't started yet")
            return False
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
        chat_id: str = None,
    ):
        """Join the BBB session and stay on the page until hold_until_ts.
        
        chat_id: explicit Bale chat ID so notifications route to the correct user.
        Retries up to 4 times with page reloads between attempts.
        """
        cache = _load_cache()
        lesson_url = class_url or cache.get(class_name)
        if not lesson_url:
            logger.error(f"[{class_name}] No URL for hold session")
            return

        logger.info(f"[{class_name}] Navigating to lesson page: {lesson_url}")
        MAX_JOIN_ATTEMPTS = 4
        ctx, page = await self._new_page()
        try:
            if not await self._login_page(page, deadline=hold_until_ts):
                return

            bbb_url = None
            for attempt in range(1, MAX_JOIN_ATTEMPTS + 1):
                logger.info(f"[{class_name}] Hold-join attempt {attempt}/{MAX_JOIN_ATTEMPTS}")
                try:
                    await page.goto(lesson_url, wait_until="domcontentloaded", timeout=self._t(30000))
                    await page.wait_for_timeout(1500)  # let AJAX table render
                    bbb_url = await self._join_bbb_session(page, class_name)
                    if bbb_url:
                        logger.info(f"[{class_name}] ✓ Hold-joined on attempt {attempt}/{MAX_JOIN_ATTEMPTS}")
                        break
                except Exception as e:
                    logger.warning(f"[{class_name}] Hold-join attempt {attempt} error: {e}")

                if attempt < MAX_JOIN_ATTEMPTS:
                    wait_secs = 5 + attempt * 3
                    logger.info(f"[{class_name}] Button not found — retrying in {wait_secs}s (attempt {attempt}/{MAX_JOIN_ATTEMPTS})")
                    await asyncio.sleep(wait_secs)

            if not bbb_url:
                logger.warning(f"[{class_name}] Could not join BBB for hold after {MAX_JOIN_ATTEMPTS} attempts")
                return

            # Stay in the room: keepalive + auto-answer polls + خسته نباشید near end
            await bbb.hold_with_presence(page, class_name, hold_until_ts, chat_id=chat_id)
        finally:
            await ctx.close()
