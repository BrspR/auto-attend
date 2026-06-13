"""
Encrypted per-user credential store + invite tokens for the shared attendance bot.

Each user's data lives in its own file  user_data/<chat_id>.json, isolated from
every other user. The worker for user A only ever reads/writes user A's file —
no shared mutable state between users.

Invite-token redemption tracking stays in users.json (invites key only).

Migration: on first import, any users nested inside a legacy users.json are
moved into user_data/ automatically.

Passwords are encrypted at rest with a dependency-free stdlib construction:
scrypt KDF → HMAC-SHA256 keystream (counter mode) → encrypt-then-MAC. The master
key lives in a separate 0600 file (.users_key) outside the repo.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

INVITES_FILE = BASE / "users.json"   # invite redemption tracking only
USER_DIR     = BASE / "user_data"    # one JSON file per registered user
KEY_FILE     = BASE / ".users_key"
MAX_USERS    = 15


# ── encryption (stdlib only) ──────────────────────────────────────────────────

def _master() -> bytes:
    if KEY_FILE.exists():
        return KEY_FILE.read_bytes()
    k = secrets.token_bytes(32)
    KEY_FILE.write_bytes(k)
    os.chmod(KEY_FILE, 0o600)
    return k


def _keystream(enc_key: bytes, nonce: bytes, n: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < n:
        out += hmac.new(enc_key, nonce + counter.to_bytes(8, "big"), "sha256").digest()
        counter += 1
    return out[:n]


def encrypt(plaintext: str) -> str:
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(16)
    dk = hashlib.scrypt(_master(), salt=salt, n=2**14, r=8, p=1, dklen=64)
    enc_key, mac_key = dk[:32], dk[32:]
    data = plaintext.encode()
    ct = bytes(a ^ b for a, b in zip(data, _keystream(enc_key, nonce, len(data))))
    tag = hmac.new(mac_key, salt + nonce + ct, "sha256").digest()
    return base64.b64encode(salt + nonce + tag + ct).decode()


def decrypt(token: str) -> str:
    raw = base64.b64decode(token)
    salt, nonce, tag, ct = raw[:16], raw[16:32], raw[32:64], raw[64:]
    dk = hashlib.scrypt(_master(), salt=salt, n=2**14, r=8, p=1, dklen=64)
    enc_key, mac_key = dk[:32], dk[32:]
    if not hmac.compare_digest(tag, hmac.new(mac_key, salt + nonce + ct, "sha256").digest()):
        raise ValueError("tampered or wrong key")
    return bytes(a ^ b for a, b in zip(ct, _keystream(enc_key, nonce, len(ct)))).decode()


# ── per-user file I/O ─────────────────────────────────────────────────────────

def _user_file(chat_id) -> Path:
    USER_DIR.mkdir(exist_ok=True)
    return USER_DIR / f"{str(chat_id)}.json"


def _load_user(chat_id) -> dict | None:
    p = _user_file(chat_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _save_user(chat_id, data: dict):
    USER_DIR.mkdir(exist_ok=True)
    p = _user_file(chat_id)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


# ── invite file I/O ───────────────────────────────────────────────────────────

def _load_invites() -> dict:
    if INVITES_FILE.exists():
        raw = json.loads(INVITES_FILE.read_text(encoding="utf-8"))
        return raw.get("invites", {})
    return {}


def _save_invites(invites: dict):
    tmp = INVITES_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"invites": invites}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, INVITES_FILE)


# ── one-time migration from legacy single-file format ────────────────────────

def _migrate_legacy():
    if not INVITES_FILE.exists():
        return
    raw = json.loads(INVITES_FILE.read_text(encoding="utf-8"))
    legacy_users = raw.get("users", {})
    if not legacy_users:
        return
    USER_DIR.mkdir(exist_ok=True)
    for cid, u in legacy_users.items():
        if not _user_file(cid).exists():
            _save_user(cid, u)
    _save_invites(raw.get("invites", {}))


_migrate_legacy()


# ── invite tokens loaded from .env ───────────────────────────────────────────

_env_tokens = os.getenv("INVITE_TOKENS", "")
VALID_TOKENS = frozenset([t.strip() for t in _env_tokens.split(",") if t.strip()])


def gen_invites(n: int = 0) -> list:
    return sorted(VALID_TOKENS)


def invite_open(token: str) -> bool:
    if token not in VALID_TOKENS:
        return False
    inv = _load_invites().get(token)
    return not (inv and inv.get("used_by") is not None)


def redeem_invite(token: str, chat_id) -> bool:
    if token not in VALID_TOKENS:
        return False
    invites = _load_invites()
    inv = invites.get(token, {})
    if inv.get("used_by") is not None:
        return False
    invites[token] = {"used_by": str(chat_id), "created": time.time()}
    _save_invites(invites)
    return True


# ── user CRUD ─────────────────────────────────────────────────────────────────

def count() -> int:
    if not USER_DIR.exists():
        return 0
    return sum(1 for f in USER_DIR.glob("*.json") if not f.name.endswith(".tmp"))


def add_user(chat_id, username: str, password: str, invite_token: str = "") -> bool:
    cid = str(chat_id)
    if not _user_file(cid).exists() and count() >= MAX_USERS:
        return False
    _save_user(cid, {
        "chat_id": cid,
        "invite_token": invite_token,
        "username": username,
        "password_enc": encrypt(password),
        "enabled": True,
        "joined": time.time(),
        "classes": [],
        "setup_done": False,
    })
    return True


def get_user(chat_id) -> dict | None:
    return _load_user(chat_id)


def get_credentials(chat_id):
    u = get_user(chat_id)
    if not u:
        return None
    return u["username"], decrypt(u["password_enc"])


def get_classes(chat_id) -> list:
    u = get_user(chat_id)
    return (u or {}).get("classes", [])


def set_classes(chat_id, classes: list):
    u = _load_user(chat_id)
    if u:
        u["classes"] = classes
        u["setup_done"] = True
        _save_user(chat_id, u)


def set_schedule(chat_id, class_url: str, days: list, start: str | None, end: str | None):
    """Patch schedule fields onto one class entry — called after scraping."""
    u = _load_user(chat_id)
    if not u:
        return
    for cls in u.get("classes", []):
        if cls.get("url") == class_url:
            cls["days"] = days
            cls["start"] = start
            cls["end"] = end
            break
    _save_user(chat_id, u)


def is_setup_done(chat_id) -> bool:
    u = get_user(chat_id)
    return bool((u or {}).get("setup_done"))


def all_users() -> dict:
    """Return {chat_id: user_data} for every file in user_data/."""
    result = {}
    if not USER_DIR.exists():
        return result
    for f in USER_DIR.glob("*.json"):
        if f.name.endswith(".tmp"):
            continue
        try:
            result[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return result


def is_registered(chat_id) -> bool:
    return _user_file(chat_id).exists()


def set_enabled(chat_id, on: bool):
    u = _load_user(chat_id)
    if u:
        u["enabled"] = bool(on)
        _save_user(chat_id, u)


def remove_user(chat_id):
    p = _user_file(chat_id)
    if p.exists():
        p.unlink()
