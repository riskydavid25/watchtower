#!/usr/bin/env python3
"""cctv_monitor.py -- CCTV/NVR availability monitor (Hikvision ISAPI) with Telegram alerting.

Polls channel status on one or more NVRs and sends an OFFLINE/ONLINE alert to
Telegram when a camera or NVR's reachability changes, after a debounce
window to avoid alerting on brief flapping. See alert_engine.py for the
debounce state machine and isapi_client.py / telegram_client.py for the
underlying I/O.

Usage:
    python3 cctv_monitor.py --once      # single run, e.g. for cron/Task Scheduler
    python3 cctv_monitor.py             # continuous loop (interval from config.json)

Configuration (config.json):
    nvr_username, nvr_password           NVR/ISAPI credentials
    nvr_list                             [{"name": ..., "ip": ...}, ...]
    telegram_bot_token, telegram_chat_id Telegram destination
    offline_confirm_seconds              debounce window in seconds (default 300)
    check_interval_seconds               polling interval in seconds (default 20)
    request_timeout_seconds              ISAPI HTTP timeout (default 8)
    ping_timeout_seconds                 ICMP timeout for final verification (default 2)
    cctv_directory_csv                   optional CSV (Name,IP,Location,RTSP_Port)
                                          used to resolve friendlier camera names/IPs
    state_file, log_file                 paths for persisted state and logs

Requirements:
    pip install requests

Related modules (must be in the same directory):
    isapi_client.py, telegram_client.py, alert_messages.py, alert_engine.py,
    alert_state.py, cctv_directory.py, cctv_monitor_timeutils.py
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

import telegram_client
from alert_engine import check_all_nvr, process_and_alert
from alert_state import load_state, save_state
from cctv_directory import load_cctv_directory


def load_config(path: str = "config.json") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def setup_logging(log_file: str) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def run_once(config: dict) -> None:
    cctv_directory = load_cctv_directory(config.get("cctv_directory_csv"))
    logging.info(f"Kamus nama CCTV dimuat: {len(cctv_directory)} entri dari {config.get('cctv_directory_csv')}")

    state = load_state(config["state_file"])

    # Flush any messages still pending from previous cycles before
    # processing new NVR status, so a transient outage doesn't leave old
    # alerts stuck forever.
    telegram_queue = state.setdefault("telegram_queue", [])
    telegram_client.flush_pending_queue(config["telegram_bot_token"], config["telegram_chat_id"], telegram_queue)

    current_state = check_all_nvr(config, cctv_directory)
    process_and_alert(current_state, config, state)
    save_state(config["state_file"], state)


def main() -> None:
    parser = argparse.ArgumentParser(description="CCTV/NVR Monitoring via Hikvision ISAPI")
    parser.add_argument("--config", default="config.json", help="Path ke file config.json")
    parser.add_argument("--once", action="store_true", help="Jalankan satu kali lalu keluar (cocok untuk cron)")
    parser.add_argument("--telegram-token", default=None, help="Override telegram_bot_token dari config.json")
    parser.add_argument("--telegram-chat-id", default=None, help="Override telegram_chat_id dari config.json")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.telegram_token:
        config["telegram_bot_token"] = args.telegram_token
    if args.telegram_chat_id:
        config["telegram_chat_id"] = args.telegram_chat_id

    setup_logging(config.get("log_file", "cctv_monitor.log"))

    # Log nilai threshold yang BENERAN kepakai saat startup -- config.json
    # cuma dibaca sekali di sini, BUKAN di dalam loop while di bawah, jadi
    # kalau config.json diedit setelah service ini jalan, perubahannya
    # TIDAK kepakai sampai service di-restart. Baris log ini supaya gampang
    # dicek langsung dari log tanpa perlu buka file config.json manual --
    # kalau nilainya beda dari yang diharapkan, itu tandanya file config
    # yang dibaca beda dari yang dikira, ATAU service belum di-restart
    # setelah config diubah.
    logging.info(
        f"Config dimuat dari '{args.config}': offline_confirm_seconds="
        f"{config.get('offline_confirm_seconds', 300)}s, check_interval_seconds="
        f"{config.get('check_interval_seconds', 20)}s, cctv_directory_csv="
        f"{config.get('cctv_directory_csv')}"
    )

    if args.once:
        run_once(config)
        return

    interval = config.get("check_interval_seconds", 20)
    threshold = config.get("offline_confirm_seconds", 300)
    logging.info(f"Memulai monitoring loop, interval {interval}s, konfirmasi offline setelah {threshold}s. Ctrl+C untuk berhenti.")
    while True:
        try:
            run_once(config)
        except Exception as e:
            logging.error(f"Error saat siklus pengecekan: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
