"""
Two-way Bale commands — fully multi-user, token-gated.

Every interaction requires authentication. An unregistered user must:
  1. Send /token <invite_code>   →  redeems the invite, starts onboarding
  2. Send their LMS username
  3. Send their LMS password  (deleted immediately after verification)
  4. Bot discovers their Fararoom classes, lists them numbered
  5. User replies with numbers to pick (e.g. "1 3 5") or "all"
  6. Bot asks if they have Nima classes → user can add names

Once registered + set up, they get:
  /start   — resume their attendance worker
  /stop    — pause their attendance worker
  /status  — their own bot status
  /today   — their own classes today (discovered from LMS)
  /log     — last lines from their personal log
  /classes — show selected classes
  /setup   — redo class selection
  /help    — list commands

Nobody can see another user's data. Unauthed messages are silently
rejected with a "send /token <code>" prompt.
"""
import asyncio
import logging
from datetime import datetime
from pathlib import Path

import config
import notify
import state
import users

logger = logging.getLogger(__name__)

LOG_DIR = Path(__file__).parent / "logs"
_DAY = {0: "دوشنبه", 1: "سه‌شنبه", 2: "چهارشنبه", 3: "پنجشنبه", 4: "جمعه", 5: "شنبه", 6: "یک‌شنبه"}
_ICON = {"attended": "✅", "failed": "❌", "pending": "⏳"}

HELP_UNAUTHED = "سلام 👋 برای استفاده از ربات، اول کد دعوتت رو بفرست:\n/token <کد>"
HELP_AUTHED = (
    "دستورها 📋\n"
    "/status — وضعیت ربات\n"
    "/today — کلاس‌های امروز\n"
    "/log — آخرین لاگ‌ها\n"
    "/classes — کلاس‌های انتخابی\n"
    "/setup — تغییر کلاس‌ها\n"
    "/start — روشن‌کردن حاضری خودکار\n"
    "/stop — خاموش‌کردن حاضری\n"
    "/help — نمایش دستورها"
)

# ---- onboarding + setup state (in-memory, per chat_id) ----
# Possible steps:
#   "username"  → waiting for LMS username
#   "password"  → waiting for LMS password
#   "pick"      → discovered classes listed, waiting for user to pick by number
#   "nima"      → asked if they have Nima classes
#   "nima_names"→ waiting for Nima class names
_onboarding: dict = {}  # chat_id -> {step, username, password, discovered, selected, ...}


def _user_log_file(chat_id: str) -> Path:
    """Each user gets their own log file under logs/."""
    LOG_DIR.mkdir(exist_ok=True)
    return LOG_DIR / f"user_{chat_id}.log"


def append_user_log(chat_id: str, line: str):
    """Append a timestamped line to a user's personal log."""
    ts = datetime.now(tz=config.TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(_user_log_file(chat_id), "a", encoding="utf-8") as f:
            f.write(f"{ts}  {line}\n")
    except Exception:
        pass


def tail_user_log(chat_id: str, n: int = 15) -> str:
    """Return the last N lines of a user's personal log."""
    p = _user_log_file(chat_id)
    if not p.exists():
        return "📜 هنوز لاگی ثبت نشده."
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return "📜 آخرین لاگ:\n" + "\n".join(lines[-n:])
    except Exception as e:
        return f"لاگ در دسترس نیست: {e}"


def build_today(chat_id: str) -> str:
    """Today's classes for this specific user."""
    now = datetime.now(tz=config.TIMEZONE)
    statuses = state.get_user_statuses(chat_id)
    if not statuses:
        # Fall back to showing stored classes
        cls_list = users.get_classes(chat_id)
        if not cls_list:
            return f"📋 امروز ({_DAY[now.weekday()]}) هنوز کلاسی تنظیم نشده. /setup"
        lines = [f"📋 کلاس‌های تو ({_DAY[now.weekday()]}):"]
        for c in cls_list:
            lms_label = "نیما" if c.get("lms") == "nima" else "فراروم"
            lines.append(f"• {c['name']}  ({lms_label})")
        return "\n".join(lines)
    lines = [f"📋 کلاس‌های امروز ({_DAY[now.weekday()]}):"]
    for name, st in statuses.items():
        icon = _ICON.get(st.get("state", ""), "•")
        lines.append(f"{icon}  {name}")
    return "\n".join(lines)


def build_status(chat_id: str) -> str:
    """Status for this specific user."""
    now = datetime.now(tz=config.TIMEZONE)
    hm = now.strftime("%H:%M")
    u = users.get_user(chat_id)
    if not u:
        return "❌ ثبت‌نام نشدی."
    enabled = u.get("enabled", False)
    setup = u.get("setup_done", False)
    cls_list = users.get_classes(chat_id)
    statuses = state.get_user_statuses(chat_id)
    done = sum(1 for s in statuses.values() if s.get("state") == "attended")
    total = len(statuses)
    parts = [f"{'🟢' if enabled else '🔴'} ربات {'روشن' if enabled else 'خاموش'} — {hm}"]
    if not setup:
        parts.append("⚠️ هنوز کلاسی انتخاب نکردی. /setup")
    else:
        parts.append(f"📚 {len(cls_list)} کلاس انتخاب شده")
    if total:
        parts.append(f"امروز: {done}/{total} حاضری ثبت شده")
    return "\n".join(parts)


def build_classes(chat_id: str) -> str:
    """Show the user's selected classes."""
    cls_list = users.get_classes(chat_id)
    if not cls_list:
        return "📚 هنوز کلاسی انتخاب نکردی. /setup"
    lines = ["📚 کلاس‌های انتخابی تو:"]
    for i, c in enumerate(cls_list, 1):
        lms_label = "نیما" if c.get("lms") == "nima" else "فراروم"
        lines.append(f"  {i}. {c['name']}  ({lms_label})")
    lines.append("\nبرای تغییر: /setup")
    return "\n".join(lines)


# ---- login verification for onboarding ----
async def _verify_login(username: str, password: str, cid: str = None) -> bool:
    from lms_main import LMSMain
    
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        if cid and attempt > 1:
            await _send(cid, f"⏳ تلاش قبلی ناموفق بود یا زمانش تموم شد. در حال تلاش مجدد (تلاش {attempt} از {max_attempts})...")
            await asyncio.sleep(4)
        
        lms = LMSMain(username, password)
        try:
            # Scale up timeouts for retries
            lms.timeout_scale = 1.0 + (attempt - 1) * 0.5
            await lms.start()
            ctx, page = await lms._new_page()
            ok = await lms._login_page(page)
            await ctx.close()
            if ok:
                return True
        except Exception as e:
            logger.warning(f"Verify login attempt {attempt} failed: {e}")
        finally:
            try:
                await lms.stop()
            except Exception:
                pass
                
    return False


async def _discover_classes(username: str, password: str) -> list[dict]:
    """Discover all enrolled Fararoom classes for this user."""
    from lms_main import LMSMain
    lms = LMSMain(username, password)
    try:
        await lms.start()
        return await lms.list_all_lessons()
    except Exception as e:
        logger.warning(f"[commands] discover failed: {e}")
        return []
    finally:
        try:
            await lms.stop()
        except Exception:
            pass


# ---- main dispatcher (handles EVERY incoming message) ----
async def dispatch(text: str, chat_id, msg_id, on_register=None, on_stop=None) -> str | None:
    """Route a message from chat_id. Returns reply text or None."""
    t = text.strip()
    cid = str(chat_id)
    low = t.lower()

    # --- onboarding / setup flow (mid-process) ---
    if cid in _onboarding:
        return await _handle_onboarding(cid, t, msg_id, on_register)

    # --- /token command: start authentication (works for everyone) ---
    if low.startswith("/token"):
        parts = t.split()
        token = parts[1] if len(parts) > 1 else ""
        if not token:
            return "برای فعال‌سازی کد دعوت لازمه:\n/token <کد>"
        if users.is_registered(cid):
            return "✅ قبلاً ثبت‌نام شدی! دستورها: /help"
        if users.count() >= users.MAX_USERS:
            return "ظرفیت ربات پره فعلاً 🙏"
        if users.invite_open(token):
            users.redeem_invite(token, cid)
            _onboarding[cid] = {"step": "username"}
            return "👋 خوش اومدی! یوزر سامانه‌ت (LMS) رو بفرست."
        return "❌ این کد معتبر نیست یا قبلاً استفاده شده."

    # --- /start with token (alternative syntax for unauthed) ---
    if low.startswith("/start") and not users.is_registered(cid):
        parts = t.split()
        if len(parts) > 1:
            token = parts[1]
            if users.invite_open(token):
                users.redeem_invite(token, cid)
                _onboarding[cid] = {"step": "username"}
                return "👋 خوش اومدی! یوزر سامانه‌ت (LMS) رو بفرست."
            return "❌ این کد معتبر نیست یا قبلاً استفاده شده."
        return HELP_UNAUTHED

    # --- reject unauthed users for everything else ---
    if not users.is_registered(cid):
        return HELP_UNAUTHED

    # ---- AUTHENTICATED USER COMMANDS ----

    if low.startswith("/setup"):
        # Re-enter class selection (discover + pick)
        creds = users.get_credentials(cid)
        if not creds:
            return "❌ خطا در خواندن اطلاعات — دوباره /token بزن."
        _onboarding[cid] = {"step": "discover", "username": creds[0], "password": creds[1]}
        return await _handle_onboarding(cid, "", msg_id, on_register)

    if low.startswith("/start"):
        if not users.is_setup_done(cid):
            return "⚠️ اول کلاس‌هاتو مشخص کن: /setup"
        users.set_enabled(cid, True)
        if on_register:
            on_register(cid)
        append_user_log(cid, "▶️ حاضری دوباره روشن شد")
        return "▶️ حاضری خودکارت روشن شد."

    if low.startswith("/stop"):
        users.set_enabled(cid, False)
        if on_stop:
            on_stop(cid)
        append_user_log(cid, "⏹️ حاضری متوقف شد")
        return "⏹️ حاضری خودکارت خاموش شد. برای روشن‌کردن: /start"

    if low.startswith("/status"):
        return build_status(cid)

    if low.startswith("/today"):
        return build_today(cid)

    if low.startswith("/log"):
        return tail_user_log(cid)

    if low.startswith("/classes"):
        return build_classes(cid)

    if low.startswith("/help"):
        return HELP_AUTHED

    return HELP_AUTHED


async def _handle_onboarding(cid: str, text: str, msg_id, on_register) -> str:
    """Handle the multi-step onboarding + class selection flow."""
    st = _onboarding[cid]
    step = st["step"]

    # Step 1: username
    if step == "username":
        st["username"] = text
        st["step"] = "password"
        return "🔑 حالا رمز سامانه‌ت رو بفرست. (بعد از بررسی، پیام رمزت پاک می‌شه)"

    # Step 2: password
    if step == "password":
        username, password = st["username"], text
        if msg_id:
            notify.delete_message(cid, msg_id)  # wipe the password from chat
            
        await _send(cid, "🔑 در حال بررسی و تلاش برای ورود... (به دلیل شلوغی سرور دانشگاه، ممکن است تا ۱ دقیقه طول بکشد. لطفاً منتظر بمانید)")
        
        ok = await _verify_login(username, password, cid)
        if ok:
            users.add_user(cid, username, password)
            append_user_log(cid, f"✅ ثبت‌نام شد — یوزر: {username}")
            st["password"] = password
            st["step"] = "discover"
            return await _handle_onboarding(cid, "", msg_id, on_register)
        st["step"] = "username"
        return "❌ ورود نشد. یوزرت رو دوباره بفرست."

    # Step 3: discover Fararoom classes
    if step == "discover":
        username = st.get("username", "")
        password = st.get("password", "")
        if not password:
            creds = users.get_credentials(cid)
            if creds:
                username, password = creds
        await _send(cid, "🔍 در حال پیدا کردن کلاس‌هات از فراروم (lmshome)...")
        discovered = await _discover_classes(username, password)
        st["discovered"] = discovered
        if discovered:
            lines = ["📚 کلاس‌های فراروم (lmshome) تو:\n"]
            for i, c in enumerate(discovered, 1):
                lines.append(f"  {i}. {c['name']}")
            lines.append("\n✏️ شماره کلاس‌هایی که می‌خوای حاضری بزنم رو بفرست.")
            lines.append("مثلاً: 1 3 5")
            lines.append("یا بزن all برای همه.")
            lines.append("یا بزن skip اگه هیچ‌کدوم رو نمی‌خوای.")
            st["step"] = "pick"
            return "\n".join(lines)
        else:
            st["step"] = "nima"
            st["selected"] = []
            return ("⚠️ کلاسی در فراروم پیدا نشد.\n\n"
                    "آیا کلاسی در نیما (lms.aut.ac.ir) داری?\n"
                    "اگه آره، اسم‌شون رو بفرست (هر خط یکی).\n"
                    "اگه نه، بزن skip.")

    # Step 4: pick Fararoom classes by number
    if step == "pick":
        t = text.strip().lower()
        discovered = st.get("discovered", [])
        selected = []

        if t == "skip":
            selected = []
        elif t == "all":
            selected = [{"name": c["name"], "url": c["url"], "lms": "main"} for c in discovered]
        else:
            # Parse numbers like "1 3 5" or "1,3,5" or "1، 3، 5"
            nums_text = text.replace("،", " ").replace(",", " ").split()
            for n in nums_text:
                try:
                    idx = int(n) - 1
                    if 0 <= idx < len(discovered):
                        c = discovered[idx]
                        selected.append({"name": c["name"], "url": c["url"], "lms": "main"})
                except ValueError:
                    pass
            if not selected and nums_text:
                return "❌ شماره درست نبود. دوباره بفرست (مثلاً: 1 3 5) یا all یا skip."

        st["selected"] = selected
        if selected:
            names = "\n".join(f"  ✓ {c['name']}" for c in selected)
            reply = f"✅ {len(selected)} کلاس فراروم انتخاب شد:\n{names}\n\n"
        else:
            reply = ""

        st["step"] = "nima"
        return reply + ("آیا کلاسی در نیما (lms.aut.ac.ir) داری?\n"
                        "اگه آره، اسم‌شون رو بفرست (هر خط یکی).\n"
                        "اگه نه، بزن skip.")

    # Step 5: Nima classes
    if step == "nima":
        t = text.strip().lower()
        selected = st.get("selected", [])

        if t != "skip" and t:
            # Each line is a Nima class name
            for line in text.strip().splitlines():
                name = line.strip()
                if name and name.lower() != "skip":
                    selected.append({"name": name, "url": "", "lms": "nima"})

        if not selected:
            _onboarding.pop(cid, None)
            return ("⚠️ هیچ کلاسی انتخاب نشد.\n"
                    "هر وقت خواستی دوباره تنظیم کنی: /setup")

        # Save and finish
        users.set_classes(cid, selected)
        _onboarding.pop(cid, None)

        main_count = sum(1 for c in selected if c["lms"] == "main")
        nima_count = sum(1 for c in selected if c["lms"] == "nima")
        lines = [f"✅ تنظیم تمام شد! {len(selected)} کلاس ذخیره شد:"]
        for c in selected:
            lms_label = "نیما" if c["lms"] == "nima" else "فراروم"
            lines.append(f"  • {c['name']}  ({lms_label})")
        lines.append(f"\nفراروم: {main_count} | نیما: {nima_count}")
        lines.append("\nبرای شروع حاضری خودکار: /start")
        lines.append("تغییر کلاس‌ها: /setup")

        append_user_log(cid, f"📚 {len(selected)} کلاس تنظیم شد ({main_count} فراروم, {nima_count} نیما)")

        if on_register:
            on_register(cid)

        return "\n".join(lines)

    # Fallback
    _onboarding.pop(cid, None)
    return HELP_UNAUTHED


async def _send(cid: str, text: str):
    """Helper to send a message to a user mid-flow."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, notify.send, text, cid)


# ---- poller ----
async def run_poller(on_register=None, on_stop=None):
    """Poll getUpdates and route every message through `dispatch`."""
    if not notify._token():
        logger.info("[commands] no Bale token — command poller disabled")
        return
    loop = asyncio.get_event_loop()
    backlog = await loop.run_in_executor(None, notify.get_updates, None, 0)
    offset = (backlog[-1]["update_id"] + 1) if backlog else None
    logger.info("[commands] command poller started (token-gated)")
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
                reply = await dispatch(text, chat, msg_id, on_register, on_stop)
                if reply:
                    await loop.run_in_executor(None, notify.send, reply, chat)
        except Exception as e:
            logger.warning(f"[commands] poll error: {e}")
        await asyncio.sleep(3)
