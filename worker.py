"""
Per-user attendance worker for the multi-user (--serve) mode.

Each enabled user gets one UserWorker task. It binds its own Bale chat_id (so every
notification routes to that user via notify's context var), then uses the user's
SELECTED classes (from /setup) to check for live sessions.

Resource model: browsers are on-demand, NOT always-on. A global semaphore caps how
many users check concurrently, and each user's whole Fararoom sweep shares ONE
login/context. Presence holds (staying in the room for the whole class) are NEVER
skipped or capped — being visibly present in the BBB room for the full class is the
whole point, so the box must simply be sized for the worst-case overlap.

PENDING LIVE VERIFICATION: the live-session detection and the whole BBB in-room layer
have never run in a real class yet. Treat first runs as best-effort; the bbb_debug_*
dumps capture the real DOM for fixing selectors.
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta

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
        """Reload this user's classes from their own user_data/<chat_id>.json."""
        cls_list = users.get_classes(self.chat_id)
        self.main_classes = [
            {
                "name":  c["name"],
                "url":   c["url"],
                "days":  c.get("days"),
                "start": c.get("start"),
                "end":   c.get("end"),
            }
            for c in cls_list if c.get("lms") == "main" and c.get("url")
        ]
        self.nima_classes = [
            {"name": c["name"]}
            for c in cls_list if c.get("lms") == "nima"
        ]

    def _next_half_hour_secs(self) -> float:
        """Seconds until the next :00 or :30 mark in Tehran time (min 60s)."""
        now = datetime.now(tz=config.TIMEZONE)
        if now.minute < 30:
            target = now.replace(minute=30, second=0, microsecond=0)
        else:
            target = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        return max((target - now).total_seconds(), 60)

    def _sleep_until_next(self) -> float:
        """Return seconds to sleep before the next useful check.

        Classes with full schedule: sleep until 2 min before next class window.
        Fallback (unscheduled classes or Nima): sleep until next :00 or :30
        mark in Tehran time — checks happen on the half-hour, not by interval.
        """
        has_all = (
            not self.nima_classes
            and self.main_classes
            and all(c.get("days") and c.get("start") for c in self.main_classes)
        )
        if not has_all:
            return self._next_half_hour_secs()

        now = datetime.now(tz=config.TIMEZONE)

        # Inside a class window → keep checking every 6 min
        for cls in self.main_classes:
            if now.weekday() not in cls["days"]:
                continue
            sh, sm = map(int, cls["start"].split(":"))
            end_str = cls.get("end") or f"{sh+2}:{sm:02d}"
            eh, em = map(int, end_str.split(":"))
            window_start = now.replace(hour=sh, minute=sm, second=0, microsecond=0) - timedelta(minutes=2)
            window_end   = now.replace(hour=eh, minute=em, second=0, microsecond=0) + timedelta(minutes=10)
            if window_start <= now <= window_end:
                return CHECK_INTERVAL

        # Find next upcoming class across the week
        soonest = None
        for cls in self.main_classes:
            sh, sm = map(int, cls["start"].split(":"))
            for offset in range(1, 8):
                candidate_day = (now.weekday() + offset) % 7
                if candidate_day not in cls["days"]:
                    continue
                target = (now + timedelta(days=offset)).replace(
                    hour=sh, minute=sm, second=0, microsecond=0)
                wait = (target - now).total_seconds() - 120
                if soonest is None or wait < soonest:
                    soonest = wait
                break

        half = self._next_half_hour_secs()
        if soonest and soonest > half:
            return min(soonest, 3600)
        return half

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
                    if attended and self.running:
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
                if not self.running:
                    self.held.pop(url, None)
                    return
                state.set_status(name, "attended", chat_id=self.chat_id)
                append_user_log(self.chat_id, f"✅ حاضری ثبت شد: {name}")
                await notify.send_async(f"✅ حاضری ثبت شد: {name}")
                # Presence hold (stay in room + polls + خسته نباشید) — ALWAYS runs;
                # being present for the whole class is non-negotiable.
                try:
                    await lms.attend_and_hold(name, [], url, time.time() + HOLD_SECS)
                except Exception as e:
                    logger.warning(f"[user {self.chat_id}] hold {name} error: {e}")
            finally:
                try:
                    await lms.stop()
                except Exception:
                    pass

        self._track(_hold())

    async def _nima_hold(self, nima: LMSNima, name: str):
        try:
            try:
                await nima.attend_and_hold(name, [name], time.time() + HOLD_SECS)
            except Exception as e:
                logger.warning(f"[user {self.chat_id}] nima hold {name} error: {e}")
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
            # Reload from user_data/<chat_id>.json each cycle so freshly scraped
            # schedules are picked up without restarting the worker.
            self._load_classes()
            now = datetime.now(tz=config.TIMEZONE)
            if DAY_START_HOUR <= now.hour < DAY_END_HOUR:
                try:
                    await self._check_cycle()
                except Exception as e:
                    logger.warning(f"[user {self.chat_id}] check cycle error: {e}")
            await asyncio.sleep(self._sleep_until_next())
