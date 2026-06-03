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

# Playwright requires SelectorEventLoop on Windows
import sys
if sys.platform == 'win32':
    import asyncio as _asyncio
    _asyncio.set_event_loop_policy(_asyncio.WindowsSelectorEventLoopPolicy())

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
    from lms_nima import LMSNima
    from config import CLASSES

    main_classes = [cls for cls in CLASSES if cls["lms"] == "main"]
    nima_classes = [cls for cls in CLASSES if cls["lms"] == "nima"]

    lms = LMSMain(username, password)
    await lms.start()
    logger.info("=== Discovering Fararoom class URLs ===")
    main_found = await lms.discover_all_urls(main_classes)
    logger.info(f"Fararoom: {len(main_found)}/{len(main_classes)} URLs found")
    await lms.stop()

    nima = LMSNima(username, password)
    await nima.start()
    logger.info("\n=== Discovering Nima class URLs ===")
    nima_found = await nima.discover_all_urls(nima_classes)
    logger.info(f"Nima: noted {len(nima_found)}/{len(nima_classes)} classes")
    await nima.stop()

    total = len(main_found) + len(nima_found)
    total_classes = len(main_classes) + len(nima_classes)
    logger.info(f"\nTotal: {total}/{total_classes} classes ready")
    if total < total_classes:
        logger.info("For missing Fararoom classes, add URLs manually to cache/class_urls.json")


def cmd_notify_test():
    import notify
    if not notify._token():
        logger.error("BALE_BOT_TOKEN is not set in .env — add it first (see notify.py header)")
        return
    chat_id = os.getenv("BALE_CHAT_ID", "").strip() or notify.discover_chat_id()
    if not chat_id:
        logger.error("No chat_id found. DM your bot once in Bale (say 'hi'), then re-run --notify-test")
        return
    logger.info(f"Your chat_id is: {chat_id}  (optionally pin it as BALE_CHAT_ID in .env)")
    ok = notify.send("✅ تست اعلان — ربات حاضری به Bale وصل شد")
    logger.info("Test notification SENT ✓" if ok else "Test notification FAILED — check token/chat_id")


BOT_COMMANDS = [
    {"command": "start", "description": "فعال‌سازی با کد دعوت"},
    {"command": "status", "description": "وضعیت ربات و کلاس بعدی"},
    {"command": "today", "description": "کلاس‌های امروز"},
    {"command": "log", "description": "آخرین لاگ‌ها"},
    {"command": "stop", "description": "توقف حاضری برای من"},
]
BOT_DESCRIPTION = (
    "ربات حاضری خودکار دانشگاه. با کد دعوت فعالش کن، یوزر و رمز سامانه LMS رو بده، "
    "بعدش خودکار سر هر کلاس برات حاضری می‌زنه و خبرت می‌کنه."
)
BOT_SHORT = "حاضری خودکار کلاس‌های LMS با اعلان در بله."


def cmd_bot_setup():
    import notify
    if not notify._token():
        logger.error("BALE_BOT_TOKEN not set — add it to .env first")
        return
    res = notify.set_bot_profile(BOT_COMMANDS, BOT_DESCRIPTION, BOT_SHORT)
    for k, v in res.items():
        ok = isinstance(v, dict) and v.get("ok")
        logger.info(f"  {k}: {'OK' if ok else v}")
    logger.info("Bot profile updated.")


def cmd_gen_invites(n: int):
    import users
    tokens = users.gen_invites(n)
    logger.info(f"Generated {n} invite token(s) — give one to each friend:")
    for t in tokens:
        logger.info(f"  {t}")
    logger.info("They activate by sending the bot:  /start <token>")


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
        from config import FIRST_TRY_MINUTES, RETRY_INTERVAL_MINUTES
        logger.info(
            f"  {cls['name']}\n"
            f"    Time: {cls['start']} – {cls['end']}\n"
            f"    LMS:  {cls['lms']}\n"
            f"    Status: {status}\n"
            f"    First attempt at T+{FIRST_TRY_MINUTES} min, then retries every "
            f"{RETRY_INTERVAL_MINUTES} min until it works or the class ends\n"
        )


async def main():
    parser = argparse.ArgumentParser(description="AUT Auto-Attendance")
    parser.add_argument("--test-login", action="store_true", help="Test login credentials")
    parser.add_argument("--discover", action="store_true", help="Discover and cache class URLs")
    parser.add_argument("--dry-run", action="store_true", help="Show today's schedule without attending")
    parser.add_argument("--notify-test", action="store_true", help="Send a test Bale notification and print your chat_id")
    parser.add_argument("--bot-setup", action="store_true", help="Set the bot's command menu + description on Bale")
    parser.add_argument("--gen-invites", type=int, metavar="N", help="Generate N invite tokens to hand to friends")
    parser.add_argument("--serve", action="store_true", help="Multi-user mode: onboard friends by invite + run a worker per user")
    args = parser.parse_args()

    if args.dry_run:
        cmd_dry_run()
        return

    if args.notify_test:
        cmd_notify_test()
        return

    if args.bot_setup:
        cmd_bot_setup()
        return

    if args.gen_invites:
        cmd_gen_invites(args.gen_invites)
        return

    if args.serve:
        logger.info("=" * 60)
        logger.info("AUT Auto-Attendance — MULTI-USER mode")
        logger.info("=" * 60)
        from supervisor import run_supervisor
        await run_supervisor()
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
