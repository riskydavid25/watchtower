"""alert_engine.py -- polls all NVRs and drives the debounce/alerting state machine.

This module owns the only genuinely stateful logic in the project. It
depends on isapi_client (I/O) and telegram_client + alert_messages
(notification), but nothing else depends on it -- cctv_monitor.py is a thin
CLI wrapper around check_all_nvr() + process_and_alert().
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import isapi_client
import telegram_client
from alert_messages import format_offline_message, format_online_message
from cctv_monitor_timeutils import WIB


def resolve_camera_info(channel: dict, names_map: dict[str, str], cctv_directory: dict) -> dict[str, str]:
    """Pick the best available display name/IP for a channel, in priority order:
    1. cctv_directory match on the channel's physical source IP (most accurate)
    2. the channel's name from the ISAPI /channels endpoint
    3. fallback: "Channel <id>"
    """
    source_ip = channel.get("source_ip")
    if source_ip and source_ip in cctv_directory:
        return {"name": cctv_directory[source_ip]["name"], "ip": source_ip}
    ch_id = channel["id"]
    if ch_id in names_map and names_map[ch_id]:
        return {"name": names_map[ch_id], "ip": source_ip or "-"}
    return {"name": f"Channel {ch_id}", "ip": source_ip or "-"}


def _check_one_nvr(nvr: dict, username: str, password: str, default_timeout: float, cctv_directory: dict) -> tuple[str, dict]:
    """Poll SATU NVR dan return (nama_nvr, hasil). Dipisah dari check_all_nvr()
    supaya bisa dijalankan di thread terpisah lewat ThreadPoolExecutor -- tiap
    NVR independen, tidak saling nunggu."""
    name, ip = nvr["name"], nvr["ip"]
    # Timeout per-NVR opsional (field "request_timeout_seconds" di
    # entry NVR itu sendiri di config.json) -- fallback ke timeout
    # global kalau NVR ini tidak override. Berguna untuk NVR yang
    # secara konsisten lebih lambat merespons channels/status (mis.
    # link jaringan lebih jauh/padat), tanpa perlu menaikkan timeout
    # SEMUA NVR (yang bikin deteksi NVR lain yang genuinely down jadi
    # ikut lebih lambat juga).
    timeout = nvr.get("request_timeout_seconds", default_timeout)

    try:
        channels = isapi_client.get_channel_status(ip, username, password, timeout)
        names_map = isapi_client.get_channel_names(ip, username, password, timeout)

        channels_by_id = {}
        for c in channels:
            info = resolve_camera_info(c, names_map, cctv_directory)
            channels_by_id[c["id"]] = {"online": c["online"], "name": info["name"], "ip": info["ip"]}

        result = {
            "reachable": True,
            "channels": channels_by_id,
            "channels_stale": False,
            "reason": "OK",
        }

        offline_names = [v["name"] for v in channels_by_id.values() if not v["online"]]
        logging.info(
            f"{name} ({ip}): OK - {len(channels)} channel, "
            f"offline saat ini: {offline_names if offline_names else 'tidak ada'}"
        )
        return name, result

    except Exception as e:
        # channels/status failed -- verify via a lighter endpoint before
        # concluding the NVR itself is down.
        logging.warning(f"{name} ({ip}): Gagal ambil channels/status ({e}). Verifikasi via endpoint cadangan...")
        reachable, reason = isapi_client.verify_nvr_reachable(ip, username, password, timeout)

        if reachable:
            # NVR is alive, only the channels API is having trouble this
            # cycle. Channel data is stale/skipped, not treated as offline.
            logging.warning(
                f"{name} ({ip}): NVR online (verified via {reason}), tapi channels/status "
                f"gagal siklus ini -- status channel dilewati, tidak ada alert."
            )
            return name, {"reachable": True, "channels": {}, "channels_stale": True, "reason": reason}
        else:
            logging.error(f"{name} ({ip}): NVR tidak bisa diakses sama sekali - {reason}")
            return name, {"reachable": False, "channels": {}, "channels_stale": True, "reason": reason}


def check_all_nvr(config: dict, cctv_directory: dict) -> dict[str, dict]:
    """Poll every NVR in config["nvr_list"] ONCE, secara PARALEL (bukan
    satu-satu berurutan) -- tiap NVR dicek di thread-nya sendiri lewat
    ThreadPoolExecutor, supaya 1 (atau beberapa) NVR yang lambat/timeout
    TIDAK bikin NVR lain ikut nunggu antrian. Ini murni I/O-bound (nunggu
    respons HTTP), jadi thread biasa sudah cukup -- tidak perlu asyncio.

    Total waktu 1 siklus sekarang idealnya mendekati waktu NVR PALING
    LAMBAT sendiri (mis. ~15-20 detik kalau ada yang timeout), BUKAN lagi
    penjumlahan semua NVR yang lambat (yang sebelumnya bisa bikin 1
    siklus molor sampai puluhan detik/lebih dari semenit kalau banyak NVR
    kena giliran timeout bersamaan).

    Returns, per NVR name:
        {
          "reachable": bool,
          "channels": {channel_id: {"online": bool, "name": ..., "ip": ...}},
          "channels_stale": bool,  # True = channel data NOT refreshed this
                                    # cycle (channels/status failed but the
                                    # NVR was confirmed alive via fallback)
          "reason": str,           # short debug label
        }
    """
    current_state: dict[str, dict] = {}
    username = config["nvr_username"]
    password = config["nvr_password"]
    default_timeout = config.get("request_timeout_seconds", 8)
    nvr_list = config["nvr_list"]

    # max_workers = jumlah NVR (semua dicek bersamaan) -- aman karena tiap
    # NVR beda IP/koneksi sendiri-sendiri, tidak ada resource bersama yang
    # dipakai rebutan antar thread di sini.
    with ThreadPoolExecutor(max_workers=max(1, len(nvr_list))) as executor:
        futures = {
            executor.submit(_check_one_nvr, nvr, username, password, default_timeout, cctv_directory): nvr["name"]
            for nvr in nvr_list
        }
        for future in as_completed(futures):
            nvr_name = futures[future]
            try:
                name, result = future.result()
                current_state[name] = result
            except Exception as e:
                # Safety net -- _check_one_nvr() sendiri sudah nangkep semua
                # exception yang diharapkan, tapi kalau ada yang lolos
                # (mis. bug tak terduga), NVR ini jangan sampai bikin
                # keseluruhan siklus crash -- catat sebagai stale/error dan
                # lanjut proses NVR lain.
                logging.error(f"{nvr_name}: Error tak terduga saat cek paralel: {e}")
                current_state[nvr_name] = {"reachable": False, "channels": {}, "channels_stale": True, "reason": f"UNEXPECTED_ERROR: {e}"}

    return current_state


def process_and_alert(current_state: dict, config: dict, state: dict) -> None:
    """Debounce/anti-flapping state machine. Mutates `state` in place.

    Flow, per NVR and per channel:
        1. First detected offline           -> "pending" (no alert yet)
        2. Still offline after >= threshold -> "confirmed" + OFFLINE alert sent
        3. Recovers before threshold         -> removed from "pending" (no alert at all)
        4. Recovers after "confirmed"        -> ONLINE alert sent, duration measured
                                                 from the ORIGINAL offline timestamp

    The OFFLINE alert's "Downtime" field and the ONLINE alert's duration
    both use the timestamp stored when the outage was first detected --
    never the time the alert itself happens to be sent.

    If a channel's data is stale this cycle (see check_all_nvr), its
    pending/confirmed entries are left untouched rather than guessed at.
    """
    threshold = config.get("offline_confirm_seconds", 300)
    bot_token = config["telegram_bot_token"]
    chat_id = config["telegram_chat_id"]
    username = config["nvr_username"]
    password = config["nvr_password"]
    default_timeout = config.get("request_timeout_seconds", 8)
    ping_timeout = config.get("ping_timeout_seconds", 2)
    now = datetime.now(WIB)
    nvr_ip_by_name = {nvr["name"]: nvr["ip"] for nvr in config["nvr_list"]}
    # Timeout per-NVR opsional -- lihat catatan lengkap di check_all_nvr().
    # Dipakai lagi di sini (bukan cuma di check_all_nvr) karena
    # final_offline_verification() (dipanggil dari _process_nvr_reachability
    # di bawah) juga melakukan request HTTP ke NVR yang sama, harus pakai
    # timeout yang konsisten sama seperti siklus polling normalnya.
    nvr_timeout_by_name = {
        nvr["name"]: nvr.get("request_timeout_seconds", default_timeout) for nvr in config["nvr_list"]
    }

    pending = state.setdefault("pending", {})
    confirmed = state.setdefault("confirmed", {})
    last_known = state.setdefault("last_known", {})
    telegram_queue = state.setdefault("telegram_queue", [])

    for name, cur in current_state.items():
        api_timeout = nvr_timeout_by_name.get(name, default_timeout)
        _process_nvr_reachability(
            name, cur, nvr_ip_by_name, pending, confirmed, threshold, now,
            username, password, api_timeout, ping_timeout,
            bot_token, chat_id, telegram_queue,
        )

        if not cur["reachable"]:
            continue  # can't check individual channels if the NVR itself is down

        if cur.get("channels_stale"):
            logging.info(f"NVR {name}: status channel dilewati siklus ini (data stale, reason={cur.get('reason')})")
            continue

        for ch_id, chinfo in cur["channels"].items():
            _process_channel(
                name, ch_id, chinfo, pending, confirmed, last_known,
                threshold, now, bot_token, chat_id, telegram_queue,
            )


def _process_nvr_reachability(
    name, cur, nvr_ip_by_name, pending, confirmed, threshold, now,
    username, password, api_timeout, ping_timeout, bot_token, chat_id, telegram_queue,
) -> None:
    """Debounce logic for the NVR itself (fully unreachable, not just a channel)."""
    key = f"NVR|{name}"
    nvr_ip = nvr_ip_by_name.get(name, "-")

    if not cur["reachable"]:
        if key not in pending and key not in confirmed:
            pending[key] = now.isoformat()
            logging.info(f"NVR {name} terdeteksi offline, menunggu konfirmasi {threshold}s...")
            return

        if key not in pending:
            return

        elapsed = (now - datetime.fromisoformat(pending[key])).total_seconds()
        if elapsed < threshold:
            return

        truly_offline, detail = isapi_client.final_offline_verification(
            nvr_ip, username, password, api_timeout, ping_timeout
        )
        if truly_offline:
            first_offline_at = datetime.fromisoformat(pending[key])
            confirmed[key] = pending.pop(key)
            msg = format_offline_message("SEMUA CCTV (NVR tidak bisa diakses)", nvr_ip, name, first_offline_at)
            telegram_client.send_or_queue(bot_token, chat_id, msg, telegram_queue)
            logging.info(f"ALERT OFFLINE (confirmed, verifikasi final: {detail}): NVR {name} tidak bisa diakses")
        else:
            pending.pop(key, None)
            logging.warning(f"NVR {name}: alert OFFLINE dibatalkan -- verifikasi final: NVR masih hidup ({detail}).")
        return

    # NVR is reachable.
    if key in pending:
        logging.info(f"NVR {name} kembali online sebelum {threshold}s tercapai - alert dibatalkan (flapping)")
        pending.pop(key, None)

    if key in confirmed:
        since_str = confirmed.pop(key)
        duration = (now - datetime.fromisoformat(since_str)).total_seconds()
        msg = format_online_message("SEMUA CCTV (NVR kembali normal)", nvr_ip, name, now, duration)
        telegram_client.send_or_queue(bot_token, chat_id, msg, telegram_queue)
        logging.info(f"ALERT ONLINE: NVR {name} kembali normal (downtime {_format_log_duration(duration)})")


def _process_channel(
    nvr_name, ch_id, chinfo, pending, confirmed, last_known,
    threshold, now, bot_token, chat_id, telegram_queue,
) -> None:
    """Debounce logic for a single channel/camera."""
    key = f"{nvr_name}|{ch_id}"
    last_known[key] = {"name": chinfo["name"], "ip": chinfo["ip"]}

    if not chinfo["online"]:
        if key not in pending and key not in confirmed:
            pending[key] = now.isoformat()
            logging.info(f"{chinfo['name']} ({chinfo['ip']}) {nvr_name} terdeteksi offline, menunggu konfirmasi {threshold}s...")
            return

        if key not in pending:
            return

        elapsed = (now - datetime.fromisoformat(pending[key])).total_seconds()
        if elapsed < threshold:
            return

        first_offline_at = datetime.fromisoformat(pending[key])
        confirmed[key] = pending.pop(key)
        msg = format_offline_message(chinfo["name"], chinfo["ip"], nvr_name, first_offline_at)
        telegram_client.send_or_queue(bot_token, chat_id, msg, telegram_queue)
        logging.info(f"ALERT OFFLINE (confirmed): {chinfo['name']} ({chinfo['ip']}) {nvr_name}")
        return

    # Channel is online.
    if key in pending:
        logging.info(f"{chinfo['name']} ({chinfo['ip']}) {nvr_name} kembali online sebelum {threshold}s - alert dibatalkan (flapping)")
        pending.pop(key, None)

    if key in confirmed:
        since_str = confirmed.pop(key)
        duration = (now - datetime.fromisoformat(since_str)).total_seconds()
        cam = last_known.get(key, {"name": chinfo["name"], "ip": chinfo["ip"]})
        msg = format_online_message(cam["name"], cam["ip"], nvr_name, now, duration)
        telegram_client.send_or_queue(bot_token, chat_id, msg, telegram_queue)
        logging.info(f"ALERT ONLINE: {cam['name']} ({cam['ip']}) {nvr_name} (downtime {_format_log_duration(duration)})")


def _format_log_duration(seconds: float) -> str:
    from alert_messages import format_duration
    return format_duration(seconds)
