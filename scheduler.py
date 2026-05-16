import asyncio
import logging
import time
from datetime import datetime, timedelta

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import CLASSES, TIMEZONE, MAX_STAY_MINUTES, FIRST_TRY_MINUTES, SECOND_TRY_MINUTES
from lms_main import LMSMain
from lms_nima import LMSNima

logger = logging.getLogger(__name__)

# APScheduler day_of_week: 0=Mon,1=Tue,2=Wed,3=Thu,4=Fri,5=Sat,6=Sun
_DAY_MAP = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}


async def _attempt(lms_main: LMSMain, lms_nima: LMSNima, cls: dict, hold_until_ts: float) -> bool:
    name = cls["name"]
    lms_type = cls["lms"]

    if lms_type == "nima":
        success = await lms_nima.attend_class(name)
    else:
        success = await lms_main.attend_class(name, cls["keywords"])

    if success:
        logger.info(f"[{name}] Attendance confirmed — holding until {datetime.fromtimestamp(hold_until_ts, tz=TIMEZONE).strftime('%H:%M')}")
        if lms_type == "nima":
            asyncio.create_task(lms_nima.attend_and_hold(name, hold_until_ts))
        else:
            asyncio.create_task(lms_main.attend_and_hold(name, cls["keywords"], None, hold_until_ts))

    return success


async def attend_job(lms_main: LMSMain, lms_nima: LMSNima, cls: dict):
    name = cls["name"]
    now = datetime.now(tz=TIMEZONE)

    # Parse class end time to compute hold_until
    end_h, end_m = map(int, cls["end"].split(":"))
    class_end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    max_hold_until = now + timedelta(minutes=MAX_STAY_MINUTES + FIRST_TRY_MINUTES + 1)
    hold_until = min(class_end, max_hold_until)
    hold_until_ts = hold_until.timestamp()

    logger.info(f"[{name}] Class job triggered at {now.strftime('%H:%M:%S')} — first attempt in {FIRST_TRY_MINUTES} min")

    # Wait T+5 minutes
    await asyncio.sleep(FIRST_TRY_MINUTES * 60)
    logger.info(f"[{name}] First attempt (T+{FIRST_TRY_MINUTES}min)...")
    success = await _attempt(lms_main, lms_nima, cls, hold_until_ts)

    if success:
        return

    # Wait until T+15 and retry
    remaining_wait = (SECOND_TRY_MINUTES - FIRST_TRY_MINUTES) * 60
    logger.info(f"[{name}] First attempt failed. Waiting {remaining_wait//60} more min for second try...")
    await asyncio.sleep(remaining_wait)
    logger.info(f"[{name}] Second attempt (T+{SECOND_TRY_MINUTES}min)...")
    success = await _attempt(lms_main, lms_nima, cls, hold_until_ts)

    if not success:
        logger.warning(f"[{name}] Both attempts failed — attendance not recorded today")


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

    # On startup: if any class is already in progress, attend immediately
    logger.info("Checking for in-progress classes on startup...")
    for cls in CLASSES:
        in_progress, elapsed_secs = _is_class_in_progress(cls)
        if in_progress:
            logger.info(f"[{cls['name']}] Already in progress ({elapsed_secs/60:.0f} min elapsed) — attempting attendance now")
            now = datetime.now(tz=TIMEZONE)
            end_h, end_m = map(int, cls["end"].split(":"))
            class_end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
            max_hold = now + timedelta(minutes=MAX_STAY_MINUTES)
            hold_until_ts = min(class_end, max_hold).timestamp()

            # Attempt immediately without T+5 delay if we're between T+5 and T+15
            if elapsed_secs >= FIRST_TRY_MINUTES * 60:
                asyncio.create_task(_attempt(lms_main, lms_nima, cls, hold_until_ts))
            else:
                # Class just started, run full job logic
                asyncio.create_task(attend_job(lms_main, lms_nima, cls))

    scheduler.start()
    logger.info("Scheduler running. Press Ctrl+C to stop.\n")

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down...")
        scheduler.shutdown()
        await lms_main.stop()
        await lms_nima.stop()
