#!/usr/bin/env python3
"""
AUT Auto-Attendance Script
Attends skipped classes automatically via LMS.

Usage:
    python main.py               # run scheduler (needs .env with credentials)
    python main.py --test-login  # test login only
    python main.py --discover    # discover and cache all class URLs
    python main.py --dry-run     # show what would run today, no clicking
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def get_credentials() -> tuple[str, str]:
    username = os.getenv("LMS_USERNAME", "").strip()
    password = os.getenv("LMS_PASSWORD", "").strip()
    if not username or not password:
        logger.error("Missing LMS_USERNAME or LMS_PASSWORD in .env file")
        logger.error("Copy .env.example to .env and fill in your credentials")
        sys.exit(1)
    return username, password


async def cmd_test_login(username: str, password: str):
    from lms_main import LMSMain
    from lms_nima import LMSNima
    from config import CLASSES

    logger.info("=== Testing Main LMS (Fararoom) Login ===")
    lms = LMSMain(username, password)
    await lms.start()
    ctx, page = await lms._new_page()
    main_ok = await lms._login_page(page)
    logger.info(f"Main LMS login: {'SUCCESS' if main_ok else 'FAILED'}")
    await ctx.close()
    await lms.stop()

    logger.info("=== Testing Nima LMS Login ===")
    nima = LMSNima(username, password)
    await nima.start()
    ctx, page = await nima._new_page()
    nima_ok = await nima._login_page(page)
    logger.info(f"Nima LMS login: {'SUCCESS' if nima_ok else 'FAILED'}")
    await ctx.close()
    await nima.stop()

    logger.info("\n=== Your Scheduled Classes ===")
    day_names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
    for cls in CLASSES:
        days = ", ".join(day_names[d] for d in cls["days"])
        lms_label = "Fararoom (main)" if cls["lms"] == "main" else "Nima"
        ok = main_ok if cls["lms"] == "main" else nima_ok
        status = "✓" if ok else "✗ (login failed)"
        logger.info(f"  {status}  {cls['name']}  [{days}]  {cls['start']}–{cls['end']}  ({lms_label})")


async def cmd_discover(username: str, password: str):
    from lms_main import LMSMain
    from config import CLASSES

    lms = LMSMain(username, password)
    await lms.start()

    logger.info("=== Discovering class URLs ===")
    for cls in CLASSES:
        if cls["lms"] == "main":
            url = await lms.discover_class_url(cls["name"], cls["keywords"])
            if url:
                logger.info(f"✓ {cls['name']}: {url}")
            else:
                logger.warning(f"✗ {cls['name']}: NOT FOUND — add manually to cache/class_urls.json")

    await lms.stop()


def cmd_dry_run():
    from config import CLASSES, TIMEZONE
    from scheduler import _is_class_in_progress

    now = datetime.now(tz=TIMEZONE)
    day_names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
    logger.info(f"=== Dry Run — Today: {day_names[now.weekday()]} {now.strftime('%H:%M')} (Tehran) ===\n")

    today_classes = [c for c in CLASSES if now.weekday() in c["days"]]
    if not today_classes:
        logger.info("No classes scheduled for today.")
        return

    for cls in today_classes:
        in_progress, elapsed = _is_class_in_progress(cls)
        status = f"IN PROGRESS ({elapsed/60:.0f} min elapsed)" if in_progress else "not started / ended"
        logger.info(
            f"  {cls['name']}\n"
            f"    Time: {cls['start']} – {cls['end']}\n"
            f"    LMS:  {cls['lms']}\n"
            f"    Status: {status}\n"
            f"    Would attend at: T+5 ({cls['start'].split(':')[0]}:{int(cls['start'].split(':')[1])+5:02d}) "
            f"and retry at T+15\n"
        )


async def main():
    parser = argparse.ArgumentParser(description="AUT Auto-Attendance")
    parser.add_argument("--test-login", action="store_true", help="Test login credentials")
    parser.add_argument("--discover", action="store_true", help="Discover and cache class URLs")
    parser.add_argument("--dry-run", action="store_true", help="Show today's schedule without attending")
    args = parser.parse_args()

    if args.dry_run:
        cmd_dry_run()
        return

    username, password = get_credentials()

    if args.test_login:
        await cmd_test_login(username, password)
        return

    if args.discover:
        await cmd_discover(username, password)
        return

    # Default: run the scheduler
    logger.info("=" * 60)
    logger.info("AUT Auto-Attendance Starting")
    logger.info("=" * 60)
    from scheduler import run_scheduler
    await run_scheduler(username, password)



if __name__ == "__main__":
    asyncio.run(main())
