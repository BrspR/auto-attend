"""
Encrypted multi-user store + invite tokens for the shared attendance bot.

Passwords are encrypted at rest with a dependency-free stdlib construction:
scrypt KDF → HMAC-SHA256 keystream (counter mode) → encrypt-then-MAC. The master
key lives in a separate 0600 file (.users_key) outside the repo.

Honest caveat: the key is co-located with the data, so this protects a leaked or
backed-up users.json, NOT a full server compromise (an attacker who owns the box
gets both). There's no way around that — the bot must decrypt to log in.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

BASE = Path(__file__).parent
USERS_FILE = BASE / "users.json"
KEY_FILE = BASE / ".users_key"
MAX_USERS = 15


# ---------------- encryption (stdlib only) ----------------
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
    dk = hashlib.scrypt(_master(), salt=salt, n=2 ** 14, r=8, p=1, dklen=64)
    enc_key, mac_key = dk[:32], dk[32:]
    data = plaintext.encode()
    ct = bytes(a ^ b for a, b in zip(data, _keystream(enc_key, nonce, len(data))))
    tag = hmac.new(mac_key, salt + nonce + ct, "sha256").digest()
    return base64.b64encode(salt + nonce + tag + ct).decode()


def decrypt(token: str) -> str:
    raw = base64.b64decode(token)
    salt, nonce, tag, ct = raw[:16], raw[16:32], raw[32:64], raw[64:]
    dk = hashlib.scrypt(_master(), salt=salt, n=2 ** 14, r=8, p=1, dklen=64)
    enc_key, mac_key = dk[:32], dk[32:]
    if not hmac.compare_digest(tag, hmac.new(mac_key, salt + nonce + ct, "sha256").digest()):
        raise ValueError("tampered or wrong key")
    return bytes(a ^ b for a, b in zip(ct, _keystream(enc_key, nonce, len(ct)))).decode()


# ---------------- store ----------------
def _load() -> dict:
    if USERS_FILE.exists():
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    return {"invites": {}, "users": {}}


def _save(data: dict):
    USERS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(USERS_FILE, 0o600)


# ---------------- invites ----------------
def gen_invites(n: int) -> list:
    data = _load()
    out = []
    for _ in range(n):
        t = secrets.token_urlsafe(6)
        data["invites"][t] = {"used_by": None, "created": time.time()}
        out.append(t)
    _save(data)
    return out


def invite_open(token: str) -> bool:
    inv = _load()["invites"].get(token)
    return bool(inv) and inv.get("used_by") is None


def redeem_invite(token: str, chat_id) -> bool:
    data = _load()
    inv = data["invites"].get(token)
    if not inv or inv.get("used_by") is not None:
        return False
    inv["used_by"] = str(chat_id)
    _save(data)
    return True


# ---------------- users ----------------
def count() -> int:
    return len(_load()["users"])


def add_user(chat_id, username: str, password: str) -> bool:
    data = _load()
    cid = str(chat_id)
    if len(data["users"]) >= MAX_USERS and cid not in data["users"]:
        return False
    data["users"][cid] = {
        "username": username,
        "password_enc": encrypt(password),
        "enabled": True,
        "joined": time.time(),
    }
    _save(data)
    return True


def get_user(chat_id):
    return _load()["users"].get(str(chat_id))


def get_credentials(chat_id):
    u = get_user(chat_id)
    if not u:
        return None
    return u["username"], decrypt(u["password_enc"])


def all_users() -> dict:
    return _load()["users"]


def is_registered(chat_id) -> bool:
    return str(chat_id) in _load()["users"]


def set_enabled(chat_id, on: bool):
    data = _load()
    cid = str(chat_id)
    if cid in data["users"]:
        data["users"][cid]["enabled"] = bool(on)
        _save(data)


def remove_user(chat_id):
    data = _load()
    data["users"].pop(str(chat_id), None)
    _save(data)
