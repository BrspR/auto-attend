"""
Per-user in-memory status of today's attendance.

Everything is keyed by (chat_id, class_name) so users only see their own data.
"""
from datetime import datetime

import config

# (chat_id, class_name) -> {"state": "pending"|"attended"|"failed", "detail": str, "ts": datetime}
_today: dict = {}


def set_status(name: str, st: str, detail: str = "", chat_id: str = "__owner__"):
    """Set status for a class. chat_id isolates per-user data."""
    _today[(chat_id, name)] = {"state": st, "detail": detail, "ts": datetime.now(tz=config.TIMEZONE)}


def get_status(name: str, chat_id: str = "__owner__"):
    """Get status for a single class."""
    return _today.get((chat_id, name))


def get_user_statuses(chat_id: str) -> dict:
    """Return {class_name: status_dict} for a specific user."""
    return {
        name: st for (cid, name), st in _today.items()
        if cid == chat_id
    }


def all_status() -> dict:
    """Return all statuses (for debugging). Not exposed to users."""
    return dict(_today)


def reset_day(chat_id: str | None = None):
    """Clear statuses. If chat_id given, only that user. Otherwise ALL."""
    if chat_id is None:
        _today.clear()
    else:
        keys = [k for k in _today if k[0] == chat_id]
        for k in keys:
            del _today[k]
