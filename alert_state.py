"""alert_state.py -- load/save the monitor's persisted state file.

State shape:
    {
      "pending":   {key: iso_timestamp_first_seen_offline},
      "confirmed": {key: iso_timestamp_first_seen_offline},   # moved here once alerted
      "last_known": {key: {"name": ..., "ip": ...}},
      "telegram_queue": [{"text": ..., "queued_at": ...}, ...]
    }

key format:
    "NVR|<nvr_name>"       -- the NVR itself is unreachable
    "<nvr_name>|<chan_id>" -- an individual channel/camera
"""

from __future__ import annotations

import json
import os


def load_state(state_file: str) -> dict:
    if os.path.exists(state_file):
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
