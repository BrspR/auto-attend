"""
Push notifications via Bale (tapi.bale.ai) — the only mainstream bot API reachable
from the Iran server (Telegram / ntfy / Pushover / Gmail-SMTP are all filtered there).

Dependency-free (stdlib urllib) so there's no extra wheel to ship to the server.

Setup:
  1. In the Bale app, talk to @botfather, create a bot, copy its TOKEN.
  2. Put BALE_BOT_TOKEN=<token> in .env, then DM your new bot once (say "hi").
  3. Run `python main.py --notify-test` — it auto-finds your chat_id, sends a test,
     and prints the chat_id to optionally pin as BALE_CHAT_ID in .env.

If BALE_BOT_TOKEN is unset, every call is a silent no-op, so the bot runs fine
without notifications configured.
"""
import asyncio
import contextvars
import json
import logging
import os
import urllib.parse
import urllib.request
import uuid

logger = logging.getLogger(__name__)

BALE_API = "https://tapi.bale.ai"

# Per-task default chat: a user-worker binds its own chat_id so every notify call
# inside that task (and its sub-tasks) routes to that user — no need to thread
# chat_id through attend/bbb. Single-user code never binds it → falls back to env.
_chat_ctx: contextvars.ContextVar = contextvars.ContextVar("bale_chat_id", default=None)


def bind_chat(chat_id):
    """Route all notify calls in the current task (and subtasks) to this chat_id."""
    _chat_ctx.set(str(chat_id) if chat_id is not None else None)


def _token() -> str:
    return os.getenv("BALE_BOT_TOKEN", "").strip()


def _resolve_chat_id(chat_id) -> str | None:
    if chat_id:
        return str(chat_id)
    bound = _chat_ctx.get()
    if bound:
        return bound
    env = os.getenv("BALE_CHAT_ID", "").strip()
    return env or discover_chat_id()


def _api(method: str, params: dict, timeout: int = 15) -> dict:
    token = _token()
    if not token:
        return {}
    url = f"{BALE_API}/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def discover_chat_id() -> str | None:
    """Find the chat_id of whoever last DM'd the bot (via getUpdates)."""
    try:
        res = _api("getUpdates", {"limit": 10}, timeout=15)
    except Exception as e:
        logger.warning(f"[notify] getUpdates failed: {e}")
        return None
    updates = res.get("result", []) if isinstance(res, dict) else []
    for upd in reversed(updates):
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            return str(chat["id"])
    return None


def set_bot_profile(commands=None, description=None, short_description=None) -> dict:
    """Set the bot's command menu + description. Each call is isolated because
    Bale doesn't implement all of these (returns 501) — partial support is fine."""
    if not _token():
        return {}
    out = {}
    calls = [
        ("commands", "setMyCommands", {"commands": json.dumps(commands)} if commands is not None else None),
        ("description", "setMyDescription", {"description": description} if description is not None else None),
        ("short_description", "setMyShortDescription", {"short_description": short_description} if short_description is not None else None),
    ]
    for key, method, params in calls:
        if params is None:
            continue
        try:
            out[key] = _api(method, params)
        except Exception as e:
            out[key] = f"unsupported ({e})"
    return out


def get_updates(offset: int | None = None, timeout: int = 0) -> list:
    """Return raw update objects (for the command poller)."""
    params: dict = {}
    if offset is not None:
        params["offset"] = offset
    if timeout:
        params["timeout"] = timeout
    try:
        res = _api("getUpdates", params, timeout=timeout + 15)
        return res.get("result", []) if isinstance(res, dict) else []
    except Exception as e:
        logger.warning(f"[notify] getUpdates failed: {e}")
        return []


def delete_message(chat_id, message_id) -> bool:
    """Best-effort delete (used to wipe a password message after onboarding)."""
    if not _token():
        return False
    try:
        res = _api("deleteMessage", {"chat_id": str(chat_id), "message_id": str(message_id)})
        return bool(res.get("ok"))
    except Exception:
        return False


def send(text: str, chat_id=None) -> bool:
    """Send a plain-text message. No-op (returns False) if not configured."""
    if not _token():
        return False
    chat_id = _resolve_chat_id(chat_id)
    if not chat_id:
        logger.warning("[notify] token set but no chat_id — DM your bot once, then set BALE_CHAT_ID")
        return False
    try:
        res = _api("sendMessage", {"chat_id": chat_id, "text": text})
        ok = bool(res.get("ok"))
        if not ok:
            logger.warning(f"[notify] Bale returned: {res}")
        return ok
    except Exception as e:
        logger.warning(f"[notify] send failed: {e}")
        return False


def send_photo(path: str, caption: str = "", chat_id=None) -> bool:
    """Upload a photo (e.g. a join screenshot) via multipart. No-op if unconfigured."""
    token = _token()
    if not token:
        return False
    chat_id = _resolve_chat_id(chat_id)
    if not chat_id:
        return False
    try:
        with open(path, "rb") as f:
            img = f.read()
        boundary = "----autoattend" + uuid.uuid4().hex
        parts = []
        for k, v in (("chat_id", str(chat_id)), ("caption", caption)):
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"shot.png\"\r\n".encode()
            + b"Content-Type: image/png\r\n\r\n" + img + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        req = urllib.request.Request(
            f"{BALE_API}/bot{token}/sendPhoto", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req, timeout=40) as resp:
            return bool(json.loads(resp.read().decode()).get("ok"))
    except Exception as e:
        logger.warning(f"[notify] send_photo failed: {e}")
        return False


async def send_async(text: str, chat_id=None) -> bool:
    """Non-blocking send for use inside the asyncio scheduler."""
    if not _token():
        return False
    return await asyncio.get_event_loop().run_in_executor(None, lambda: send(text, chat_id))


async def send_photo_async(path: str, caption: str = "", chat_id=None) -> bool:
    if not _token():
        return False
    return await asyncio.get_event_loop().run_in_executor(None, lambda: send_photo(path, caption, chat_id))
