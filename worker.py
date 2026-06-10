"""
Per-user attendance worker for the multi-user (--serve) mode.

Each enabled user gets one UserWorker task. It binds its own Bale chat_id (so every
notification routes to that user via notify's context var), then uses the user's
SELECTED classes (from /setup) to check for live sessions.

Resource model: browsers are on-demand, NOT always-on. A global semaphore caps how
many users check concurrently, a second semaphore caps how many 2-hour presence
holds run at once (attendance is already registered by the join click, so when hold
capacity is full the hold is skipped, not the attendance), and each user's whole
Fararoom sweep shares ONE login/context — so 15 users do not mean 15 open browsers.

PENDING LIVE VERIFICATION: the live-session detection and the whole BBB in-room layer
have never run in a real class yet. Treat first runs as best-effort; the bbb_debug_*
dumps capture the real DOM for fixing selectors.
"""
import asyncio
import logging
import os
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

# Cap concurrent 2h presence-hold browsers across ALL users. Class times cluster
# (everyone's 13:00 starts together), so without a cap 15 users can mean 15+
# Chromiums ≈ OOM on a small VPS. Attendance itself (the join click) is never
# skipped — only the optional stay-in-room presence is, when at capacity.
MAX_CONCURRENT_HOLDS = int(os.getenv("MAX_CONCURRENT_HOLDS", "8"))
_hold_sem = asyncio.Semaphore(MAX_CONCURRENT_HOLDS)


async def _try_acquire_hold() -> bool:
    """Acquire a hold slot without queueing behind a 2-hour wait."""
    try:
        await asyncio.wait_for(_hold_sem.acquire(), timeout=1)
        return True
    except asyncio.TimeoutError:
        return False


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
        self._hold_tasks: set = set()        # live hold tasks, cancelled on /stop

    def shutdown(self):
        """Stop the check loop AND cancel any in-flight presence holds."""
        self.running = False
        for t in list(self._hold_tasks):
            t.cancel()

    def _track(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._hold_tasks.add(task)
        task.add_done_callback(self._hold_tasks.discard)
        return task

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
        # --- Fararoom classes: ONE login/context for the whole sweep ---
        to_check = [
            l for l in self.main_classes
            if self.held.get(l["url"], 0) <= time.time()
        ]
        live: dict[str, bool] = {}
        if to_check:
            async with _check_sem:
                lms = LMSMain(self.username, self.password)
                try:
                    await lms.start()
                    live = await lms.check_live_many(to_check)
                except Exception as e:
                    logger.warning(f"[user {self.chat_id}] fararoom sweep error: {e}")
                finally:
                    await lms.stop()
        for lesson in to_check:
            if live.get(lesson["url"]):
                self._spawn_main_hold(lesson)

        # --- Nima classes ---
        for nima_cls in self.nima_classes:
            nima_name = nima_cls["name"]
            if self.nima_held.get(nima_name, 0) > time.time():
                continue
            try:
                async with _check_sem:
                    nima = LMSNima(self.username, self.password)
                    attended = False
                    try:
                        await nima.start()
                        attended = await nima.attend_class(nima_name, [nima_name])
                    except Exception as e:
                        logger.warning(f"[user {self.chat_id}] nima check {nima_name} error: {e}")
                    if attended:
                        self.nima_held[nima_name] = time.time() + HOLD_SECS
                        state.set_status(nima_name, "attended", chat_id=self.chat_id)
                        append_user_log(self.chat_id, f"✅ حاضری نیما ثبت شد: {nima_name}")
                        await notify.send_async(f"✅ حاضری نیما ثبت شد: {nima_name}")
                        self._track(self._nima_hold(nima, nima_name))  # hold owns the browser now
                    else:
                        await nima.stop()
            except Exception as e:
                logger.warning(f"[user {self.chat_id}] nima {nima_name} error: {e}")

    def _spawn_main_hold(self, lesson: dict):
        name, url = lesson["name"], lesson["url"]
        # Optimistic marker so the next sweep skips this class; CLEARED on failure
        # below so a transient join error retries next cycle instead of silently
        # missing the class for 2 hours.
        self.held[url] = time.time() + HOLD_SECS

        async def _hold():
            lms = LMSMain(self.username, self.password)
            ok = False
            try:
                await lms.start()
                try:
                    ok = await lms.attend_class(name, [], lesson_url=url)
                except Exception as e:
                    logger.warning(f"[user {self.chat_id}] join {name} error: {e}")
                if not ok:
                    self.held.pop(url, None)  # let the next check cycle retry
                    state.set_status(name, "failed", chat_id=self.chat_id)
                    append_user_log(self.chat_id, f"⚠️ ورود به {name} نشد — دوباره تلاش می‌کنم")
                    return
                state.set_status(name, "attended", chat_id=self.chat_id)
                append_user_log(self.chat_id, f"✅ حاضری ثبت شد: {name}")
                await notify.send_async(f"✅ حاضری ثبت شد: {name}")
                # Optional presence hold (polls + خسته نباشید) — capped globally.
                if await _try_acquire_hold():
                    try:
                        await lms.attend_and_hold(name, [], url, time.time() + HOLD_SECS)
                    except Exception as e:
                        logger.warning(f"[user {self.chat_id}] hold {name} error: {e}")
                    finally:
                        _hold_sem.release()
                else:
                    logger.info(f"[user {self.chat_id}] hold capacity full — {name} attended, presence hold skipped")
            finally:
                try:
                    await lms.stop()
                except Exception:
                    pass

        self._track(_hold())

    async def _nima_hold(self, nima: LMSNima, name: str):
        try:
            if await _try_acquire_hold():
                try:
                    await nima.attend_and_hold(name, [name], time.time() + HOLD_SECS)
                except Exception as e:
                    logger.warning(f"[user {self.chat_id}] nima hold {name} error: {e}")
                finally:
                    _hold_sem.release()
            else:
                logger.info(f"[user {self.chat_id}] hold capacity full — {name} attended, presence hold skipped")
        finally:
            try:
                await nima.stop()
            except Exception:
                pass

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
