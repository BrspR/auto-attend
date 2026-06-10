"""
Multi-user supervisor — the deployment mode.

Loads every enabled user from users.json, runs one UserWorker per user,
and runs the token-gated onboarding/command poller. New signups start a
worker on the fly; /stop cancels one (including its in-room holds).

Each worker isolates its own Bale chat (via notify.bind_chat) and uses on-demand
browsers, so this stays within the box's limits for a small group of friends.
"""
import asyncio
import logging
from datetime import datetime, timedelta

import commands
import config
import state
import users
from worker import UserWorker

logger = logging.getLogger(__name__)

_workers: dict = {}  # chat_id -> (task, UserWorker)


def _start_worker(chat_id):
    cid = str(chat_id)
    existing = _workers.get(cid)
    if existing and not existing[0].done():
        return
    u = users.get_user(cid)
    if not u or not u.get("enabled"):
        return
    creds = users.get_credentials(cid)
    if not creds:
        return
    w = UserWorker(cid, creds[0], creds[1])
    task = asyncio.create_task(w.run())
    _workers[cid] = (task, w)
    logger.info(f"[supervisor] started worker for {cid}")


def _stop_worker(chat_id):
    cid = str(chat_id)
    entry = _workers.pop(cid, None)
    if entry:
        task, w = entry
        w.shutdown()   # stops the loop AND cancels in-flight presence holds
        task.cancel()
        logger.info(f"[supervisor] stopped worker for {cid}")


async def _daily_reset():
    """Clear yesterday's per-user statuses every morning so /today is accurate."""
    while True:
        now = datetime.now(tz=config.TIMEZONE)
        nxt = (now + timedelta(days=1)).replace(hour=4, minute=0, second=0, microsecond=0)
        await asyncio.sleep((nxt - now).total_seconds())
        state.reset_day()
        logger.info("[supervisor] daily status reset")


async def run_supervisor():
    logger.info("=" * 50)
    logger.info("Multi-user supervisor starting (token-gated)")
    for cid, u in users.all_users().items():
        if u.get("enabled"):
            _start_worker(cid)
    logger.info(f"[supervisor] {len(_workers)} worker(s) running, {users.count()} registered (max {users.MAX_USERS})")
    asyncio.create_task(_daily_reset())
    # The poller runs forever, handling token auth + commands.
    await commands.run_poller(on_register=_start_worker, on_stop=_stop_worker)
