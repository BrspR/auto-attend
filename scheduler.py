import asyncio
import logging
import time
from datetime import datetime, timedelta

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import (
    CLASSES, TIMEZONE, MAX_STAY_MINUTES, FIRST_TRY_MINUTES,
    RETRY_INTERVAL_MINUTES, TIMEOUT_GROWTH, MAX_TIMEOUT_SCALE,
)
from lms_main import LMSMain
from lms_nima import LMSNima
import notify
import state
import commands

logger = logging.getLogger(__name__)

# APScheduler day_of_week: 0=Mon,1=Tue,2=Wed,3=Thu,4=Fri,5=Sat,6=Sun
_DAY_MAP = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}


async def _attempt(lms_main: LMSMain, lms_nima: LMSNima, cls: dict, hold_until_ts: float,
                   timeout_scale: float = 1.0) -> bool:
    name = cls["name"]
    lms_type = cls["lms"]
    lms = lms_nima if lms_type == "nima" else lms_main

    # Slower network on later attempts → give the browser more time.
    lms.timeout_scale = timeout_scale
    success = await lms.attend_class(name, cls["keywords"])

    if success:
        now_hm = datetime.now(tz=TIMEZONE).strftime("%H:%M")
        state.set_status(name, "attended", now_hm)
        logger.info(f"[{name}] Attendance confirmed — holding until {datetime.fromtimestamp(hold_until_ts, tz=TIMEZONE).strftime('%H:%M')}")
        asyncio.create_task(notify.send_async(f"✅ حاضری ثبت شد: {name} ({now_hm})"))
        if lms_type == "nima":
            asyncio.create_task(lms_nima.attend_and_hold(name, cls["keywords"], hold_until_ts))
        else:
            asyncio.create_task(lms_main.attend_and_hold(name, cls["keywords"], None, hold_until_ts))

    return success


async def attend_job(lms_main: LMSMain, lms_nima: LMSNima, cls: dict,
                     initial_wait_minutes: int = FIRST_TRY_MINUTES):
    """
    Attend a class: wait initial_wait_minutes, then retry every RETRY_INTERVAL_MINUTES
    until it succeeds or the class ends. Each failed attempt grows the page
    timeouts by TIMEOUT_GROWTH (capped at MAX_TIMEOUT_SCALE) to ride out slow network.
    """
    name = cls["name"]
    now = datetime.now(tz=TIMEZONE)

    # Parse class end time to compute hold_until and the retry deadline
    end_h, end_m = map(int, cls["end"].split(":"))
    class_end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    max_hold_until = now + timedelta(minutes=MAX_STAY_MINUTES + FIRST_TRY_MINUTES + 1)
    hold_until = min(class_end, max_hold_until)
    hold_until_ts = hold_until.timestamp()

    state.set_status(name, "pending")
    if initial_wait_minutes > 0:
        logger.info(f"[{name}] Class job triggered at {now.strftime('%H:%M:%S')} — first attempt in {initial_wait_minutes} min")
        await asyncio.sleep(initial_wait_minutes * 60)
    else:
        logger.info(f"[{name}] Class job triggered at {now.strftime('%H:%M:%S')} — attempting now")

    attempt = 0
    scale = 1.0
    while True:
        attempt += 1
        logger.info(f"[{name}] Attempt #{attempt} (timeout x{scale:.2f})...")
        if await _attempt(lms_main, lms_nima, cls, hold_until_ts, timeout_scale=scale):
            return

        if datetime.now(tz=TIMEZONE) >= class_end:
            state.set_status(name, "failed", f"{attempt} tries")
            logger.warning(f"[{name}] Class ended — gave up after {attempt} failed attempt(s)")
            asyncio.create_task(notify.send_async(f"❌ حاضری ثبت نشد: {name} — بعد از {attempt} تلاش (استاد جلسه رو شروع نکرد؟)"))
            return

        scale = min(scale * TIMEOUT_GROWTH, MAX_TIMEOUT_SCALE)
        logger.info(f"[{name}] Attempt #{attempt} failed — retrying in {RETRY_INTERVAL_MINUTES} min "
                    f"(next timeout x{scale:.2f})")
        await asyncio.sleep(RETRY_INTERVAL_MINUTES * 60)


async def morning_plan():
    """Reset today's status and send the day's class plan (single-user mode)."""
    state.reset_day()
    # In single-user mode, build a simple summary from config.CLASSES
    from config import CLASSES
    now = datetime.now(tz=TIMEZONE)
    _DAY = {0: "دوشنبه", 1: "سه‌شنبه", 2: "چهارشنبه", 3: "پنجشنبه", 4: "جمعه", 5: "شنبه", 6: "یک‌شنبه"}
    today_classes = [c for c in CLASSES if now.weekday() in c["days"]]
    if not today_classes:
        text = f"📋 امروز ({_DAY[now.weekday()]}) کلاسی نداری."
    else:
        lines = [f"📋 کلاس‌های امروز ({_DAY[now.weekday()]}):"]
        for c in sorted(today_classes, key=lambda c: c["start"]):
            st = state.get_status(c["name"])
            icon = {"attended": "✅", "failed": "❌", "pending": "⏳"}.get((st or {}).get("state", ""), "•")
            lines.append(f"{icon} {c['start']}–{c['end']}  {c['name']}")
        text = "\n".join(lines)
    logger.info("[summary] morning plan sent")
    await notify.send_async(text)


async def evening_summary_job():
    from config import CLASSES
    today_classes = [c for c in CLASSES if datetime.now(tz=TIMEZONE).weekday() in c["days"]]
    if not today_classes:
        return
    done = sum(1 for c in today_classes if (state.get_status(c["name"]) or {}).get("state") == "attended")
    lines = [f"🌙 خلاصه امروز: {done}/{len(today_classes)} حاضری"]
    _ICON = {"attended": "✅", "failed": "❌", "pending": "⏳"}
    for c in today_classes:
        st = state.get_status(c["name"])
        icon = _ICON.get((st or {}).get("state", ""), "•")
        lines.append(f"{icon} {c['name']}")
    text = "\n".join(lines)
    logger.info("[summary] evening summary sent")
    await notify.send_async(text)


async def _startup_login_health(lms_main: LMSMain, lms_nima: LMSNima):
    """Quick login probe at startup — alert if credentials are broken."""
    try:
        ctx, page = await lms_main._new_page()
        ok_main = await lms_main._login_page(page)
        await ctx.close()
    except Exception:
        ok_main = False
    try:
        ctx, page = await lms_nima._new_page()
        ok_nima = await lms_nima._login_page(page)
        await ctx.close()
    except Exception:
        ok_nima = False
    if not ok_main or not ok_nima:
        broken = ", ".join(n for n, ok in (("فراروم", ok_main), ("نیما", ok_nima)) if not ok)
        await notify.send_async(f"⚠️ ورود ناموفق به {broken} — یوزر/رمز را در .env چک کن")
    else:
        logger.info("[health] startup login check: both OK")


def _is_class_in_progress(cls: dict) -> tuple[bool, float]:
    """Returns (in_progress, seconds_since_start)."""
    now = datetime.now(tz=TIMEZONE)
    if now.weekday() not in cls["days"]:
        return False, 0

    start_h, start_m = map(int, cls["start"].split(":"))
    end_h, end_m = map(int, cls["end"].split(":"))
    class_start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    class_end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)

    if class_start <= now <= class_end:
        return True, (now - class_start).total_seconds()
    return False, 0


async def run_scheduler(username: str, password: str):
    lms_main = LMSMain(username, password)
    lms_nima = LMSNima(username, password)

    await lms_main.start()
    await lms_nima.start()

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)

    for cls in CLASSES:
        start_h, start_m = map(int, cls["start"].split(":"))
        days_str = ",".join(_DAY_MAP[d] for d in cls["days"])

        scheduler.add_job(
            attend_job,
            CronTrigger(day_of_week=days_str, hour=start_h, minute=start_m, timezone=TIMEZONE),
            args=[lms_main, lms_nima, cls],
            id=cls["name"],
            name=cls["name"],
            misfire_grace_time=60,
        )
        logger.info(f"Scheduled '{cls['name']}' on [{days_str}] at {cls['start']}")

    # Daily summaries
    scheduler.add_job(morning_plan, CronTrigger(hour=7, minute=0, timezone=TIMEZONE),
                      id="__morning_plan", misfire_grace_time=3600)
    scheduler.add_job(evening_summary_job, CronTrigger(hour=22, minute=0, timezone=TIMEZONE),
                      id="__evening_summary", misfire_grace_time=3600)

    # Two-way command bot (token-gated) + startup login health check
    asyncio.create_task(commands.run_poller())
    asyncio.create_task(_startup_login_health(lms_main, lms_nima))

    # On startup: if any class is already in progress, start attempting immediately
    # (no initial wait) — the retry loop handles "session not live yet".
    logger.info("Checking for in-progress classes on startup...")
    for cls in CLASSES:
        in_progress, elapsed_secs = _is_class_in_progress(cls)
        if in_progress:
            logger.info(f"[{cls['name']}] Already in progress ({elapsed_secs/60:.0f} min elapsed) — starting attempts now")
            asyncio.create_task(attend_job(lms_main, lms_nima, cls, initial_wait_minutes=0))

    scheduler.start()
    logger.info("Scheduler running. Press Ctrl+C to stop.\n")
    asyncio.create_task(notify.send_async(f"🟢 ربات حاضری روشن شد — {len(CLASSES)} کلاس زیر نظر"))

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down...")
        scheduler.shutdown()
        await lms_main.stop()
        await lms_nima.stop()
