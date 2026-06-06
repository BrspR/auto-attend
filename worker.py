"""
Per-user attendance worker for the multi-user (--serve) mode.

Each enabled user gets one UserWorker task. It binds its own Bale chat_id (so every
notification routes to that user via notify's context var), then uses the user's
SELECTED classes (from /setup) to check for live sessions.

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
import state
import users
from commands import append_user_log
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
        self.main_classes: list[dict] = []   # [{name, url}] — Fararoom
        self.nima_classes: list[dict] = []   # [{name}] — Nima
        self.held: dict[str, float] = {}     # lesson_url -> hold_until_ts
        self.nima_held: dict[str, float] = {}  # nima_name -> hold_until_ts
        self.running = True

    def _load_classes(self):
        """Load this user's selected classes from users.json."""
        cls_list = users.get_classes(self.chat_id)
        self.main_classes = [
            {"name": c["name"], "url": c["url"]}
            for c in cls_list if c.get("lms") == "main" and c.get("url")
        ]
        self.nima_classes = [
            {"name": c["name"]}
            for c in cls_list if c.get("lms") == "nima"
        ]

    # ---- one Fararoom + Nima sweep ----
    async def _check_cycle(self):
        # --- Fararoom classes ---
        if self.main_classes:
            async with _check_sem:
                lms = LMSMain(self.username, self.password)
                try:
                    await lms.start()
                    for lesson in self.main_classes:
                        url = lesson["url"]
                        name = lesson["name"]
                        if self.held.get(url, 0) > time.time():
                            continue
                        try:
                            if await lms.check_live(url):
                                self._spawn_main_hold(lesson)
                        except Exception as e:
                            logger.warning(f"[user {self.chat_id}] check {name} error: {e}")
                finally:
                    await lms.stop()

        # --- Nima classes ---
        for nima_cls in self.nima_classes:
            nima_name = nima_cls["name"]
            if self.nima_held.get(nima_name, 0) > time.time():
                continue
            try:
                async with _check_sem:
                    nima = LMSNima(self.username, self.password)
                    try:
                        await nima.start()
                        if await nima.attend_class(nima_name, [nima_name]):
                            self.nima_held[nima_name] = time.time() + HOLD_SECS
                            state.set_status(nima_name, "attended", chat_id=self.chat_id)
                            append_user_log(self.chat_id, f"✅ حاضری نیما ثبت شد: {nima_name}")
                            await notify.send_async(f"✅ حاضری نیما ثبت شد: {nima_name}")
                            asyncio.create_task(self._nima_hold(nima, nima_name))
                            continue  # _nima_hold owns the browser now
                        await nima.stop()
                    except Exception:
                        await nima.stop()
            except Exception as e:
                logger.warning(f"[user {self.chat_id}] nima check {nima_name} error: {e}")

    def _spawn_main_hold(self, lesson: dict):
        name, url = lesson["name"], lesson["url"]
        self.held[url] = time.time() + HOLD_SECS

        async def _hold():
            lms = LMSMain(self.username, self.password)
            try:
                await lms.start()
                if await lms.attend_class(name, [], lesson_url=url):
                    state.set_status(name, "attended", chat_id=self.chat_id)
                    append_user_log(self.chat_id, f"✅ حاضری ثبت شد: {name}")
                    await notify.send_async(f"✅ حاضری ثبت شد: {name}")
                    await lms.attend_and_hold(name, [], url, time.time() + HOLD_SECS)
            except Exception as e:
                logger.warning(f"[user {self.chat_id}] hold {name} error: {e}")
                state.set_status(name, "failed", chat_id=self.chat_id)
                append_user_log(self.chat_id, f"❌ خطا در حاضری: {name}")
            finally:
                await lms.stop()

        asyncio.create_task(_hold())

    async def _nima_hold(self, nima: LMSNima, name: str):
        try:
            await nima.attend_and_hold(name, [name], time.time() + HOLD_SECS)
        except Exception as e:
            logger.warning(f"[user {self.chat_id}] nima hold {name} error: {e}")
        finally:
            await nima.stop()

    # ---- main loop ----
    async def run(self):
        notify.bind_chat(self.chat_id)   # all notify in this task → this user

        # Check if setup is done
        if not users.is_setup_done(self.chat_id):
            await notify.send_async("⚠️ هنوز کلاس‌هاتو مشخص نکردی. /setup رو بزن.")
            return

        self._load_classes()
        total = len(self.main_classes) + len(self.nima_classes)
        if total == 0:
            await notify.send_async("⚠️ هیچ کلاسی انتخاب نشده. /setup رو بزن.")
            return

        await notify.send_async(
            f"🟢 حاضری خودکار روشن شد.\n"
            f"📚 {len(self.main_classes)} فراروم + {len(self.nima_classes)} نیما\n"
            f"سر هر کلاس برات حاضری می‌زنم و خبرت می‌کنم."
        )
        append_user_log(self.chat_id, f"🟢 ربات روشن شد — {total} کلاس")

        while self.running:
            now = datetime.now(tz=config.TIMEZONE)
            if DAY_START_HOUR <= now.hour < DAY_END_HOUR:
                try:
                    await self._check_cycle()
                except Exception as e:
                    logger.warning(f"[user {self.chat_id}] check cycle error: {e}")
            await asyncio.sleep(CHECK_INTERVAL)
