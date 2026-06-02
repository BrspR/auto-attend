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
        logger.info(f"[{name}] Attendance confirmed — holding until {datetime.fromtimestamp(hold_until_ts, tz=TIMEZONE).strftime('%H:%M')}")
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
            logger.warning(f"[{name}] Class ended — gave up after {attempt} failed attempt(s)")
            return

        scale = min(scale * TIMEOUT_GROWTH, MAX_TIMEOUT_SCALE)
        logger.info(f"[{name}] Attempt #{attempt} failed — retrying in {RETRY_INTERVAL_MINUTES} min "
                    f"(next timeout x{scale:.2f})")
        await asyncio.sleep(RETRY_INTERVAL_MINUTES * 60)


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

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down...")
        scheduler.shutdown()
        await lms_main.stop()
        await lms_nima.stop()
