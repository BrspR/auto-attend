"""
Two-way Bale commands + daily summaries.

A background poller reads getUpdates and replies to /status, /today, /log so you
can check the bot from your pocket instead of SSHing. The summary builders are
also used by the morning-plan / evening-summary cron jobs in scheduler.py.
"""
import asyncio
import logging
from datetime import datetime
from pathlib import Path

import config
import notify
import state

logger = logging.getLogger(__name__)

LOG_FILE = Path(__file__).parent / "scheduler.log"
_DAY = {0: "دوشنبه", 1: "سه‌شنبه", 2: "چهارشنبه", 3: "پنجشنبه", 4: "جمعه", 5: "شنبه", 6: "یک‌شنبه"}
_ICON = {"attended": "✅", "failed": "❌", "pending": "⏳"}

HELP = (
    "سلام 👋 دستورها:\n"
    "/status — وضعیت ربات و کلاس بعدی\n"
    "/today — کلاس‌های امروز و حاضری‌ها\n"
    "/log — آخرین خطوط لاگ"
)


def _today_classes() -> list:
    wd = datetime.now(tz=config.TIMEZONE).weekday()
    return sorted((c for c in config.CLASSES if wd in c["days"]), key=lambda c: c["start"])


def build_today() -> str:
    now = datetime.now(tz=config.TIMEZONE)
    classes = _today_classes()
    if not classes:
        return f"📋 امروز ({_DAY[now.weekday()]}) کلاسی نداری."
    lines = [f"📋 کلاس‌های امروز ({_DAY[now.weekday()]}):"]
    for c in classes:
        st = state.get_status(c["name"])
        icon = _ICON.get(st["state"], "•") if st else "•"
        lines.append(f"{icon} {c['start']}–{c['end']}  {c['name']}")
    return "\n".join(lines)


def build_status() -> str:
    now = datetime.now(tz=config.TIMEZONE)
    hm = now.strftime("%H:%M")
    classes = _today_classes()
    upcoming = [c for c in classes if c["start"] > hm]
    done = sum(1 for c in classes if (state.get_status(c["name"]) or {}).get("state") == "attended")
    parts = [f"🟢 ربات روشن است — {hm}"]
    if upcoming:
        nxt = upcoming[0]
        parts.append(f"کلاس بعدی: {nxt['name']} ساعت {nxt['start']}")
    elif classes:
        parts.append("کلاس دیگه‌ای برای امروز نمونده.")
    else:
        parts.append("امروز کلاسی نداری.")
    if classes:
        parts.append(f"امروز: {done}/{len(classes)} حاضری ثبت شده")
    return "\n".join(parts)


def evening_summary() -> str:
    classes = _today_classes()
    if not classes:
        return ""
    done = sum(1 for c in classes if (state.get_status(c["name"]) or {}).get("state") == "attended")
    lines = [f"🌙 خلاصه امروز: {done}/{len(classes)} حاضری"]
    for c in classes:
        st = state.get_status(c["name"])
        icon = _ICON.get(st["state"], "•") if st else "•"
        lines.append(f"{icon} {c['name']}")
    return "\n".join(lines)


def tail_log(n: int = 15) -> str:
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        return "📜 آخرین لاگ:\n" + "\n".join(lines[-n:])
    except Exception as e:
        return f"لاگ در دسترس نیست: {e}"


def _handle(text: str) -> str:
    t = text.strip().lower()
    if t.startswith("/status"):
        return build_status()
    if t.startswith("/today"):
        return build_today()
    if t.startswith("/log"):
        return tail_log()
    return HELP


async def run_poller():
    """Long-ish poll loop: read commands, reply to the sender's chat_id."""
    if not notify._token():
        logger.info("[commands] no Bale token — command poller disabled")
        return
    loop = asyncio.get_event_loop()
    # Skip the backlog so we don't replay old messages on restart.
    backlog = await loop.run_in_executor(None, notify.get_updates, None, 0)
    offset = (backlog[-1]["update_id"] + 1) if backlog else None
    logger.info("[commands] command poller started")
    while True:
        try:
            updates = await loop.run_in_executor(None, notify.get_updates, offset, 0)
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                text = msg.get("text") or ""
                chat = (msg.get("chat") or {}).get("id")
                if not text or chat is None:
                    continue
                reply = _handle(text)
                await loop.run_in_executor(None, notify.send, reply, chat)
        except Exception as e:
            logger.warning(f"[commands] poll error: {e}")
        await asyncio.sleep(3)
