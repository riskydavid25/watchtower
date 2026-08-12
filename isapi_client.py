"""isapi_client.py -- Hikvision ISAPI client for NVR channel status and reachability.

Pure I/O layer: talks to a single NVR over HTTP/ICMP. Has no knowledge of
alerting, debounce state, or Telegram -- callers (alert_engine.py) decide
what to do with the results.
"""

from __future__ import annotations

import logging
import platform
import subprocess
from typing import Optional
from xml.etree import ElementTree as ET
from xml.etree.ElementTree import Element

import requests
from requests import Response
from requests.auth import HTTPDigestAuth, HTTPBasicAuth
from requests.packages.urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# Lighter-weight endpoints used to verify an NVR is still alive when the
# main channels/status endpoint is failing (timeout, overload, etc).
FALLBACK_ENDPOINTS = [
    ("/ISAPI/System/deviceInfo", "DEVICEINFO"),
    ("/ISAPI/System/status", "SYSTEM_STATUS"),
]


def strip_ns(tag: str) -> str:
    """Strip the XML namespace from a tag name, e.g. '{ns}online' -> 'online'."""
    return tag.split("}")[-1] if "}" in tag else tag


def find_text(elem: Element, tag_name: str) -> Optional[str]:
    """Find a direct child element by tag name, ignoring namespace, and return its text."""
    for child in elem:
        if strip_ns(child.tag) == tag_name:
            return child.text
    return None


def _get(url: str, username: str, password: str, timeout: float, auth_mode: str = "digest") -> Response:
    auth = HTTPDigestAuth(username, password) if auth_mode == "digest" else HTTPBasicAuth(username, password)
    # "Connection: close" avoids reusing a stale keep-alive connection from
    # a previous polling cycle.
    resp = requests.get(url, auth=auth, timeout=timeout, verify=False, headers={"Connection": "close"})
    resp.raise_for_status()
    return resp


def _get_with_fallback(url: str, username: str, password: str, timeout: float) -> Response:
    """Try Digest auth first, then fall back to Basic auth on 401."""
    try:
        return _get(url, username, password, timeout, "digest")
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            return _get(url, username, password, timeout, "basic")
        raise


def get_channel_names(nvr_ip: str, username: str, password: str, timeout: float) -> dict[str, str]:
    """Fetch the display name of each channel from /ISAPI/ContentMgmt/InputProxy/channels.

    Returns an empty dict on failure -- callers should fall back to using
    the channel ID as a display name.
    """
    url = f"http://{nvr_ip}/ISAPI/ContentMgmt/InputProxy/channels"
    names: dict[str, str] = {}
    try:
        resp = _get_with_fallback(url, username, password, timeout)
        root = ET.fromstring(resp.content)
        for ch_elem in root:
            if strip_ns(ch_elem.tag) != "InputProxyChannel":
                continue
            ch_id = find_text(ch_elem, "id")
            ch_name = find_text(ch_elem, "name")
            if ch_id:
                names[ch_id] = ch_name or f"Channel {ch_id}"
    except Exception as e:
        logging.warning(f"Gagal ambil nama channel dari {nvr_ip}: {e}")
    return names


def get_channel_status(nvr_ip: str, username: str, password: str, timeout: float) -> list[dict]:
    """Fetch the online/offline status of every channel on one NVR.

    Each entry: {"id", "online" (bool), "detect_status", "source_ip"}.
    `source_ip` is the physical camera's IP (from sourceInputPortDescriptor),
    used by the caller to match against a friendly-name directory.

    Raises on failure -- callers decide how to handle an unreachable NVR.
    """
    url = f"http://{nvr_ip}/ISAPI/ContentMgmt/InputProxy/channels/status"
    resp = _get_with_fallback(url, username, password, timeout)

    root = ET.fromstring(resp.content)
    channels = []
    for status_elem in root:
        if strip_ns(status_elem.tag) != "InputProxyChannelStatus":
            continue
        ch_id = find_text(status_elem, "id")
        online_raw = find_text(status_elem, "online")
        detect_status = find_text(status_elem, "chanDetectStatus")
        online = str(online_raw).strip().lower() == "true"

        source_ip = None
        for child in status_elem:
            if strip_ns(child.tag) == "sourceInputPortDescriptor":
                source_ip = find_text(child, "ipAddress")
                break

        channels.append({
            "id": ch_id,
            "online": online,
            "detect_status": detect_status,
            "source_ip": source_ip,
        })
    return channels


def verify_nvr_reachable(nvr_ip: str, username: str, password: str, timeout: float) -> tuple[bool, str]:
    """Check whether the NVR itself is reachable via lighter fallback endpoints.

    Used when channels/status fails, to distinguish "the NVR is down" from
    "only this one API endpoint is having trouble".

    Returns (reachable, reason). reason is a short machine-readable label
    describing which endpoint confirmed reachability, or why all failed.
    """
    for path, label in FALLBACK_ENDPOINTS:
        url = f"http://{nvr_ip}{path}"
        try:
            _get_with_fallback(url, username, password, timeout)
            return True, f"VERIFIED_ONLINE_VIA_{label}"
        except Exception:
            continue
    return False, "ALL_ENDPOINTS_FAILED"


def ping_host(ip: str, timeout_seconds: float = 2) -> bool:
    """Send a single ICMP ping (cross-platform: Windows and Linux/macOS).

    Returns True if a reply was received, False on timeout/error.
    """
    is_windows = platform.system().lower().startswith("win")
    cmd = (
        ["ping", "-n", "1", "-w", str(int(timeout_seconds * 1000)), ip]
        if is_windows
        else ["ping", "-c", "1", "-W", str(int(timeout_seconds)), ip]
    )
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds + 3,  # safety margin so ping can't hang the process
        )
        return result.returncode == 0
    except Exception as e:
        logging.warning(f"Ping ke {ip} gagal dijalankan: {e}")
        return False


def final_offline_verification(
    nvr_ip: str, username: str, password: str, timeout: float, ping_timeout: float = 2
) -> tuple[bool, str]:
    """Last-chance check before an NVR-offline alert is actually sent.

    Called right after the debounce threshold has elapsed, not at initial
    detection. Tries, in order, until one succeeds: channels/status, the
    fallback endpoints, then an ICMP ping. If any succeed, the NVR is
    considered still alive and the caller should cancel the alert.

    Returns (truly_offline, detail):
        truly_offline=True  -- every check failed, safe to alert
        truly_offline=False -- at least one check succeeded, do not alert
    """
    try:
        get_channel_status(nvr_ip, username, password, timeout)
        return False, "MASIH_HIDUP_VIA_CHANNELS_STATUS"
    except Exception:
        pass

    reachable, reason = verify_nvr_reachable(nvr_ip, username, password, timeout)
    if reachable:
        return False, f"MASIH_HIDUP_VIA_{reason}"

    if ping_host(nvr_ip, ping_timeout):
        return False, "MASIH_HIDUP_VIA_PING"

    return True, "SEMUA_VERIFIKASI_GAGAL_TERMASUK_PING"
