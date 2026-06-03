"""
Per-user attendance worker for the multi-user (--serve) mode.

Each enabled user gets one UserWorker task. It binds its own Bale chat_id (so every
notification routes to that user via notify's context var), discovers their enrolled
classes once, then during the day periodically checks whether any class has a live
session and, if so, joins + holds it.

Resource model: browsers are on-demand, NOT always-on. A global semaphore caps how
many users check concurrently, and each live class gets its own short-lived browser
for the hold — so 15 users do not mean 15 open browsers.

PENDING LIVE VERIFICATION: the live-session detection and the whole BBB in-room layer
have never run in a real class yet. Treat first runs as best-effort; the bbb_debug_*
dumps capture the real DOM for fixing selectors.
"""
import asyncio
import logging
import time
from datetime import datetime

import config
import notify
from lms_main import LMSMain
from lms_nima import LMSNima

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 360          # seconds between live-session checks per user
DAY_START_HOUR = 7
DAY_END_HOUR = 22
HOLD_SECS = config.MAX_STAY_MINUTES * 60

# Cap concurrent check-browsers across ALL users so the box doesn't get swamped.
_check_sem = asyncio.Semaphore(3)


class UserWorker:
    def __init__(self, chat_id, username: str, password: str):
        self.chat_id = str(chat_id)
        self.username = username
        self.password = password
        self.lessons: list[dict] | None = None
        self.held: dict[str, float] = {}     # lesson_url -> hold_until_ts
        self.nima_held_until = 0.0
        self.running = True

    # ---- discovery ----
    async def _discover(self):
        lms = LMSMain(self.username, self.password)
        try:
            await lms.start()
            self.lessons = await lms.list_all_lessons()
            logger.info(f"[user {self.chat_id}] discovered {len(self.lessons)} lessons")
        finally:
            await lms.stop()

    # ---- one Fararoom + Nima sweep ----
    async def _check_cycle(self):
        async with _check_sem:
            lms = LMSMain(self.username, self.password)
            try:
                await lms.start()
                for lesson in (self.lessons or []):
                    url = lesson["url"]
                    if self.held.get(url, 0) > time.time():
                        continue
                    try:
                        if await lms.check_live(url):
                            self._spawn_main_hold(lesson)
                    except Exception as e:
                        logger.warning(f"[user {self.chat_id}] check {url} error: {e}")
            finally:
                await lms.stop()

        # Nima: one shot at the front page (best-effort, single live session)
        if self.nima_held_until <= time.time():
            try:
                async with _check_sem:
                    nima = LMSNima(self.username, self.password)
                    try:
                        await nima.start()
                        if await nima.attend_class("جلسه نیما", []):
                            self.nima_held_until = time.time() + HOLD_SECS
                            await notify.send_async("✅ حاضری نیما برات ثبت شد")
                            asyncio.create_task(self._nima_hold(nima))
                            return  # _nima_hold owns the browser now
                        await nima.stop()
                    except Exception:
                        await nima.stop()
            except Exception as e:
                logger.warning(f"[user {self.chat_id}] nima check error: {e}")

    def _spawn_main_hold(self, lesson: dict):
        name, url = lesson["name"], lesson["url"]
        self.held[url] = time.time() + HOLD_SECS

        async def _hold():
            lms = LMSMain(self.username, self.password)
            try:
                await lms.start()
                if await lms.attend_class(name, [], lesson_url=url):
                    await notify.send_async(f"✅ حاضری ثبت شد: {name}")
                    await lms.attend_and_hold(name, [], url, time.time() + HOLD_SECS)
            except Exception as e:
                logger.warning(f"[user {self.chat_id}] hold {name} error: {e}")
            finally:
                await lms.stop()

        asyncio.create_task(_hold())

    async def _nima_hold(self, nima: LMSNima):
        try:
            await nima.attend_and_hold("جلسه نیما", [], time.time() + HOLD_SECS)
        except Exception as e:
            logger.warning(f"[user {self.chat_id}] nima hold error: {e}")
        finally:
            await nima.stop()

    # ---- main loop ----
    async def run(self):
        notify.bind_chat(self.chat_id)   # all notify in this task → this user
        await notify.send_async("🟢 حاضری خودکار برات روشن شد. سر هر کلاس برات حاضری می‌زنم و خبرت می‌کنم.")
        try:
            await self._discover()
        except Exception as e:
            logger.warning(f"[user {self.chat_id}] discover failed: {e}")
            await notify.send_async("⚠️ نتونستم وارد بشم یا کلاس‌هاتو پیدا کنم — یوزر/رمز رو چک کن.")
            return

        while self.running:
            now = datetime.now(tz=config.TIMEZONE)
            if DAY_START_HOUR <= now.hour < DAY_END_HOUR and self.lessons:
                try:
                    await self._check_cycle()
                except Exception as e:
                    logger.warning(f"[user {self.chat_id}] check cycle error: {e}")
            await asyncio.sleep(CHECK_INTERVAL)
