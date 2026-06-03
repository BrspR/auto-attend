"""
BigBlueButton in-room presence helpers, shared by lms_main and lms_nima.

After the bot joins the BBB session it runs hold_with_presence(), which:
  - enters the room as "Listen only" (dismisses the audio modal),
  - auto-answers attendance polls by picking the first option (with a small
    randomized human delay),
  - posts a plain "خسته نباشید" in the public chat ~2 min before the hold ends,
  - keeps the tab alive until hold_until_ts.

IMPORTANT: the BBB HTML5 client DOM varies by version and these selectors are
best-effort. The first time the bot is genuinely inside a live room (and the first
time it sees a poll) it saves an HTML + screenshot dump (bbb_debug_*) so the real
selectors can be confirmed/corrected. Treat this module as "pending live capture".
"""
import asyncio
import logging
import random
import time
from pathlib import Path

logger = logging.getLogger(__name__)

KHASTE_TEXT = "خسته نباشید"          # plain, no emoji, by request
KHASTE_LEAD_SECS = 120               # send ~2 min before the hold ends
POLL_CHECK_SECS = 20                 # how often to look for a live poll

# --- selectors (multiple fallbacks; BBB versions differ) ---
_LISTEN_ONLY = (
    "[aria-label*='شنونده'], [aria-label*='فقط گوش'], "
    "button:has-text('فقط گوش'), button:has-text('شنونده'), "
    "[data-test='listenOnlyBtn'], [aria-label='Listen only'], "
    "button:has-text('Listen only')"
)
_POLL_CONTAINER = (
    "[data-test='pollingContainer'], [data-test='pollingComponent'], "
    "[aria-label*='نظرسنجی'], .pollingContainer"
)
_POLL_OPTION = (
    "[data-test='pollAnswerOption'], [data-test^='pollAnswer'], "
    "[data-test='pollingContainer'] button, [data-test='pollingComponent'] button"
)
_CHAT_INPUT = (
    "#message-input, [data-test='chatInput'], "
    "textarea[aria-label*='پیام'], textarea[placeholder*='پیام'], "
    "textarea[aria-label*='message'], textarea[placeholder*='message']"
)
_CHAT_SEND = "[data-test='sendMessageButton'], button[aria-label*='ارسال'], button[aria-label*='Send']"


async def _dump(page, label: str, tag: str):
    try:
        safe = label.replace(" ", "_").replace("/", "_")[:30]
        base = Path(__file__).parent / f"bbb_debug_{safe}_{tag}"
        base.with_suffix(".html").write_text(await page.content(), encoding="utf-8")
        await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        logger.info(f"[BBB/{label}] Saved debug dump: {base.name}.html/.png")
    except Exception as e:
        logger.warning(f"[BBB/{label}] dump failed: {e}")


async def enter_room(page, label: str) -> bool:
    """Dismiss the join-audio modal as Listen-only so the bot is fully in the room."""
    try:
        btn = page.locator(_LISTEN_ONLY).first
        await btn.wait_for(timeout=20000)
        await btn.click()
        await page.wait_for_timeout(2500)
        logger.info(f"[BBB/{label}] Entered room (Listen only)")
        return True
    except Exception:
        logger.info(f"[BBB/{label}] No audio modal (already in room, or different layout)")
        return False


async def answer_poll_if_present(page, label: str, answered: set, dumped_flag: dict) -> bool:
    """If a poll is live and unanswered, click the first option after a human delay."""
    try:
        container = page.locator(_POLL_CONTAINER).first
        if await container.count() == 0:
            return False
        if not await container.is_visible():
            return False
    except Exception:
        return False

    # Use the visible option labels as a fingerprint so we answer each poll once.
    try:
        options = page.locator(_POLL_OPTION)
        n = await options.count()
        if n == 0:
            return False
        fingerprint = await container.inner_text()
        fingerprint = fingerprint.strip()[:80]
    except Exception:
        return False

    if fingerprint in answered:
        return False

    if not dumped_flag.get("poll"):
        await _dump(page, label, "poll")
        dumped_flag["poll"] = True

    delay = random.uniform(4, 12)  # look human, not instant
    logger.info(f"[BBB/{label}] Poll detected — answering option A in {delay:.0f}s")
    await asyncio.sleep(delay)
    try:
        await options.first.click()
        answered.add(fingerprint)
        logger.info(f"[BBB/{label}] ✓ Answered poll (option A)")
        return True
    except Exception as e:
        logger.warning(f"[BBB/{label}] poll click failed: {e}")
        return False


async def send_chat(page, label: str, text: str) -> bool:
    """Type a message into the public chat and send it."""
    try:
        box = page.locator(_CHAT_INPUT).first
        await box.wait_for(timeout=8000)
        await box.click()
        await box.fill(text)
        send_btn = page.locator(_CHAT_SEND).first
        if await send_btn.count() > 0:
            await send_btn.click()
        else:
            await box.press("Enter")
        logger.info(f"[BBB/{label}] ✓ Sent chat: {text!r}")
        return True
    except Exception as e:
        logger.warning(f"[BBB/{label}] chat send failed: {e}")
        return False


async def hold_with_presence(page, label: str, hold_until_ts: float, *, send_khaste: bool = True):
    """
    Stay in the BBB room until hold_until_ts: keep alive, auto-answer attendance
    polls, and post خسته نباشید ~2 min before the end.
    """
    await enter_room(page, label)

    answered: set = set()
    dumped = {"room": False, "poll": False}
    khaste_sent = False
    khaste_target = hold_until_ts - KHASTE_LEAD_SECS

    # One-shot dump of a real in-room DOM for selector verification.
    await _dump(page, label, "inroom")
    dumped["room"] = True

    remaining = hold_until_ts - time.time()
    logger.info(f"[BBB/{label}] Presence hold for {remaining/60:.1f} min (poll-watch on, khaste at end)")

    while time.time() < hold_until_ts:
        try:
            await answer_poll_if_present(page, label, answered, dumped)
        except Exception as e:
            logger.warning(f"[BBB/{label}] poll-check error: {e}")

        if send_khaste and not khaste_sent and time.time() >= khaste_target:
            khaste_sent = await send_chat(page, label, KHASTE_TEXT)

        sleep_for = min(POLL_CHECK_SECS, max(1.0, hold_until_ts - time.time()))
        await asyncio.sleep(sleep_for)
        try:
            await page.evaluate("window.scrollBy(0, 1)")  # keepalive
        except Exception:
            logger.warning(f"[BBB/{label}] page closed — ending presence hold")
            break

    # If the hold ended before we sent it (e.g. cap), try once more while alive.
    if send_khaste and not khaste_sent:
        await send_chat(page, label, KHASTE_TEXT)

    logger.info(f"[BBB/{label}] ✓ Presence hold complete")
