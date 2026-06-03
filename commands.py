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


# ---- multi-user onboarding (serve mode) ----
_onboarding: dict = {}  # chat_id -> {"step": "username"|"password", "username": str}


async def _verify_login(username: str, password: str) -> bool:
    from lms_main import LMSMain
    lms = LMSMain(username, password)
    try:
        await lms.start()
        ctx, page = await lms._new_page()
        ok = await lms._login_page(page)
        await ctx.close()
        return ok
    except Exception:
        return False
    finally:
        try:
            await lms.stop()
        except Exception:
            pass


async def _dispatch_serve(text, chat, msg_id, on_register, on_stop) -> str | None:
    import users
    t = text.strip()
    cid = str(chat)

    if users.is_registered(cid):
        low = t.lower()
        if low.startswith("/stop"):
            users.set_enabled(cid, False)
            if on_stop:
                on_stop(cid)
            return "⏹️ حاضری خودکارت متوقف شد. برای روشن‌کردن دوباره: /start"
        if low.startswith("/start") or low.startswith("/resume"):
            users.set_enabled(cid, True)
            if on_register:
                on_register(cid)
            return "▶️ دوباره روشن شد."
        if low.startswith("/status"):
            u = users.get_user(cid)
            return "🟢 ثبت‌نام شدی. حاضری خودکار " + ("روشن است." if u.get("enabled") else "خاموش است.")
        if low.startswith("/help"):
            return "دستورها: /status (وضعیت) /stop (توقف) /start (روشن)"
        return "دستورها: /status /stop /start"

    if cid in _onboarding:
        st = _onboarding[cid]
        if st["step"] == "username":
            st["username"] = t
            st["step"] = "password"
            return "🔑 حالا رمز سامانه‌ت رو بفرست. (بعد از بررسی، پیام رمزت پاک می‌شه)"
        if st["step"] == "password":
            username, password = st["username"], t
            if msg_id:
                notify.delete_message(cid, msg_id)  # wipe the password from chat
            ok = await _verify_login(username, password)
            if ok:
                users.add_user(cid, username, password)
                _onboarding.pop(cid, None)
                if on_register:
                    on_register(cid)
                return "✅ ثبت شدی! حاضری خودکار روشن شد. سر هر کلاس برات می‌زنم. 🟢"
            st["step"] = "username"
            return "❌ ورود نشد. یوزرت رو دوباره بفرست."

    # brand-new chat — require an invite token
    if t.lower().startswith("/start"):
        parts = t.split()
        token = parts[1] if len(parts) > 1 else ""
        if not token:
            return "برای فعال‌سازی کد دعوت لازمه:  /start <کد>"
        if users.count() >= users.MAX_USERS:
            return "ظرفیت ربات پره فعلاً 🙏"
        if users.invite_open(token):
            users.redeem_invite(token, cid)
            _onboarding[cid] = {"step": "username"}
            return "👋 خوش اومدی! یوزر سامانه‌ت (LMS) رو بفرست."
        return "این کد معتبر نیست یا قبلاً استفاده شده."
    return "سلام 👋 برای شروع کد دعوتت رو بفرست:  /start <کد>"


async def run_poller(serve_mode: bool = False, on_register=None, on_stop=None):
    """Poll loop: read commands and reply to the sender. In serve_mode it also
    runs invite-gated onboarding for new users."""
    if not notify._token():
        logger.info("[commands] no Bale token — command poller disabled")
        return
    loop = asyncio.get_event_loop()
    backlog = await loop.run_in_executor(None, notify.get_updates, None, 0)
    offset = (backlog[-1]["update_id"] + 1) if backlog else None
    logger.info(f"[commands] command poller started (serve_mode={serve_mode})")
    while True:
        try:
            updates = await loop.run_in_executor(None, notify.get_updates, offset, 0)
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                text = msg.get("text") or ""
                chat = (msg.get("chat") or {}).get("id")
                msg_id = msg.get("message_id")
                if not text or chat is None:
                    continue
                if serve_mode:
                    reply = await _dispatch_serve(text, chat, msg_id, on_register, on_stop)
                else:
                    reply = _handle(text)
                if reply:
                    await loop.run_in_executor(None, notify.send, reply, chat)
        except Exception as e:
            logger.warning(f"[commands] poll error: {e}")
        await asyncio.sleep(3)
