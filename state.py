"""In-memory status of today's attendance, read by the /status & /today commands."""
from datetime import datetime

import config

# class_name -> {"state": "pending"|"attended"|"failed", "detail": str, "ts": datetime}
_today: dict = {}


def set_status(name: str, st: str, detail: str = ""):
    _today[name] = {"state": st, "detail": detail, "ts": datetime.now(tz=config.TIMEZONE)}


def get_status(name: str):
    return _today.get(name)


def all_status() -> dict:
    return dict(_today)


def reset_day():
    _today.clear()
