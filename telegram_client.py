"""telegram_client.py -- Telegram message delivery with retry and a persistent queue.

Delivery has two layers of protection against transient failures:
    1. Automatic retry with increasing backoff on send.
    2. A durable queue (caller-provided list, normally state["telegram_queue"])
       for messages that still fail after all retries -- flushed again at
       the start of every polling cycle via flush_pending_queue().
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

import requests

from cctv_monitor_timeutils import WIB

SEND_RETRIES = 3           # attempts on a direct send before queuing
SEND_TIMEOUT_SECONDS = 15  # per-attempt HTTP timeout
RETRY_BACKOFF_SECONDS = 5  # backoff grows linearly: 5s, 10s, 15s, ...
QUEUE_MAX_AGE_HOURS = 24   # messages older than this are dropped, not retried forever


def send_message(
    bot_token: str,
    chat_id: str,
    text: str,
    retries: int = SEND_RETRIES,
    timeout: float = SEND_TIMEOUT_SECONDS,
    backoff_seconds: float = RETRY_BACKOFF_SECONDS,
) -> bool:
    """Send one message to Telegram, retrying on failure.

    Returns True if delivered on any attempt, False if all attempts failed.
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    for attempt in range(1, retries + 1):
        try:
            r = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=timeout)
            if r.status_code == 200:
                if attempt > 1:
                    logging.info(f"Kirim Telegram berhasil di percobaan ke-{attempt}.")
                return True
            logging.error(f"Gagal kirim Telegram (percobaan {attempt}/{retries}): HTTP {r.status_code} {r.text}")
        except requests.exceptions.RequestException as e:
            logging.error(f"Error saat kirim Telegram (percobaan {attempt}/{retries}): {e}")

        if attempt < retries:
            wait = backoff_seconds * attempt
            logging.info(f"Menunggu {wait}s sebelum coba kirim ulang...")
            time.sleep(wait)

    logging.error(f"Semua {retries} percobaan kirim Telegram gagal, pesan masuk antrian.")
    return False


def send_or_queue(bot_token: str, chat_id: str, text: str, queue: list[dict]) -> bool:
    """Send immediately; on failure, append to `queue` for later retry instead of dropping it."""
    ok = send_message(bot_token, chat_id, text)
    if not ok:
        queue.append({"text": text, "queued_at": datetime.now(WIB).isoformat()})
        logging.warning(f"Pesan dimasukkan ke antrian (total tertunda sekarang: {len(queue)}).")
    return ok


def flush_pending_queue(bot_token: str, chat_id: str, queue: list[dict]) -> None:
    """Retry every message still pending in `queue` (mutated in place).

    Called at the start of each polling cycle, before new alerts are
    processed. Messages older than QUEUE_MAX_AGE_HOURS are dropped as a
    safety valve -- if they're still failing after that long the cause is
    probably a bad bot token/chat ID, not a transient outage.
    """
    if not queue:
        return

    logging.info(f"Mencoba mengirim ulang {len(queue)} pesan Telegram yang tertunda di antrian...")

    now = datetime.now(WIB)
    still_pending = []
    sent_count = 0
    dropped_count = 0

    for item in queue:
        age_hours = (now - datetime.fromisoformat(item["queued_at"])).total_seconds() / 3600

        if age_hours > QUEUE_MAX_AGE_HOURS:
            logging.error(
                f"Pesan di antrian sudah > {QUEUE_MAX_AGE_HOURS} jam dan tetap gagal, "
                f"dibuang. Isi: {item['text'][:100]}..."
            )
            dropped_count += 1
            continue

        # Single attempt per message per flush cycle -- if Telegram is still
        # down, this cycle shouldn't block on full retry+backoff for every
        # queued message. Anything still failing gets retried next cycle.
        if send_message(bot_token, chat_id, item["text"], retries=1):
            sent_count += 1
        else:
            still_pending.append(item)

    queue[:] = still_pending

    if sent_count:
        logging.info(f"{sent_count} pesan tertunda berhasil dikirim ulang.")
    if dropped_count:
        logging.warning(f"{dropped_count} pesan dibuang dari antrian (> {QUEUE_MAX_AGE_HOURS} jam).")
    if still_pending:
        logging.warning(f"{len(still_pending)} pesan masih tertunda, dicoba lagi siklus berikutnya.")
