"""alert_messages.py -- Telegram alert text templates.

Pure formatting functions: given data in, text out. No I/O, no state.
Kept separate so the wording/format can change without touching any
detection or delivery logic.
"""

from __future__ import annotations

from datetime import datetime

TIMESTAMP_FORMAT = "%d/%m/%Y %H.%M.%S"


def format_offline_message(cam_name: str, cam_ip: str, cluster: str, downtime_dt: datetime) -> str:
    """`downtime_dt` must be the timestamp the camera/NVR was FIRST detected
    offline -- not the time this alert is being sent."""
    ts = downtime_dt.strftime(TIMESTAMP_FORMAT)
    return (
        "🗣 CCTV MONITOR ALERT !!\n\n"
        f"Name : {cam_name}\n"
        f"IP : {cam_ip}\n"
        f"Cluster : NVR {cluster}\n"
        f"Downtime : {ts} WIB\n"
        "Status : OFFLINE ❌❌\n\n"
        "--CCTV Monitor Notification--"
    )


def format_online_message(cam_name: str, cam_ip: str, cluster: str, uptime_dt: datetime, duration_seconds: float) -> str:
    ts = uptime_dt.strftime(TIMESTAMP_FORMAT)
    return (
        "🗣 CCTV MONITOR ALERT !!\n\n"
        f"Name : {cam_name}\n"
        f"IP : {cam_ip}\n"
        f"Cluster : NVR {cluster}\n"
        f"Uptime : {ts} WIB\n"
        f"Duration : (Downtime: {format_duration(duration_seconds)})\n"
        "Status : ONLINE ✅✅\n\n"
        "--CCTV Monitor Notification--"
    )


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} m {sec} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} m {sec} s"
