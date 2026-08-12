"""cctv_directory.py -- optional camera-name lookup, loaded from a CSV file.

Lets the monitor show a friendlier camera name/location in alerts instead
of a raw NVR channel ID, by matching on the camera's physical IP.
"""

from __future__ import annotations

import csv
import logging
import os


def load_cctv_directory(csv_path: str | None) -> dict[str, dict[str, str]]:
    """Load camera name/location by IP from a CSV (columns: Name, IP, Location, RTSP_Port).

    Returns {ip: {"name": ..., "location": ...}}. Rows with a blank IP or
    Name are skipped; duplicate IPs use the last row (last-write-wins).
    Returns an empty dict if the file is missing.
    """
    directory: dict[str, dict[str, str]] = {}
    if not csv_path or not os.path.exists(csv_path):
        logging.warning(f"File daftar CCTV tidak ditemukan: {csv_path}")
        return directory

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            ip = (row.get("IP") or "").strip()
            name = (row.get("Name") or "").strip()
            location = (row.get("Location") or "").strip()
            if not ip or not name:
                continue
            directory[ip] = {"name": name, "location": location}
    return directory
