# 🛰️ Hikvision Watchtower — Multi-Site CCTV/NVR Uptime Monitor

**Resilient, false-positive-resistant availability monitoring for Hikvision ISAPI NVRs, with debounced Telegram alerting.**

Dibangun untuk memantau ratusan kamera CCTV yang tersebar di puluhan NVR/lokasi
remote (termasuk site dengan koneksi tidak stabil), tanpa membanjiri channel
alert dengan false alarm setiap kali link jaringan "kedip" sebentar.

> Proyek ini adalah versi bersih (data & kredensial di-scrub) dari sistem yang
> saya bangun dan operasikan untuk memantau CCTV di puluhan lokasi remote —
> dijalankan 24/7 di production.

---

## ✨ Kenapa proyek ini menarik

Ini bukan sekadar "ping kamera lalu kirim Telegram". Ada beberapa masalah dunia
nyata yang harus diselesaikan:

- **Paralel polling, bukan sekuensial** — 10+ NVR dicek bersamaan via
  `ThreadPoolExecutor`, jadi 1 NVR yang lambat/timeout tidak membuat NVR lain
  ikut mengantre. Total waktu 1 siklus ≈ waktu NVR paling lambat, bukan
  penjumlahan semua NVR.
- **Anti-flapping / debounce** — kamera yang offline sebentar (kurang dari
  threshold, mis. 5 menit) tidak memicu alert sama sekali. Mencegah spam saat
  link jaringan sekadar berkedip.
- **Verifikasi berlapis sebelum alert final dikirim** — sebelum benar-benar
  menyatakan NVR mati, sistem mencoba endpoint utama → endpoint cadangan →
  ICMP ping, baru mengirim alert kalau semuanya gagal.
- **Graceful degradation** — kalau endpoint status channel gagal tapi NVR-nya
  sendiri masih hidup (diverifikasi via endpoint lain), data channel siklus
  itu ditandai *stale* dan dilewati — bukan disalahartikan sebagai "semua
  kamera offline".
- **Delivery yang tahan gangguan** — pengiriman Telegram punya retry dengan
  backoff, dan pesan yang tetap gagal masuk antrian persisten yang di-flush
  ulang di siklus berikutnya (dengan TTL 24 jam supaya tidak menumpuk selamanya).
- **Pemisahan tanggung jawab yang jelas** — I/O (`isapi_client`,
  `telegram_client`), business logic/state machine (`alert_engine`),
  presentation (`alert_messages`), dan orchestration (`cctv_monitor.py`)
  masing-masing terpisah dan bisa diuji independen.

## 🏗️ Arsitektur

```
                    ┌────────────────────┐
                    │   cctv_monitor.py   │  ← entry point / CLI / loop
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │   alert_engine.py    │  ← state machine (debounce,
                    │                       │     anti-flap, verifikasi final)
                    └──────┬───────┬────────┘
                           │       │
              ┌────────────▼─┐   ┌─▼─────────────────┐
              │ isapi_client  │   │  telegram_client    │
              │ (ISAPI I/O,   │   │  (retry + backoff +  │
              │  paralel via  │   │   persistent queue)  │
              │  threadpool)  │   │                      │
              └───────────────┘   └──────┬───────────────┘
                                          │
                                  ┌───────▼────────┐
                                  │ alert_messages   │  ← pure formatting
                                  └──────────────────┘

              alert_state.py    → persist state ke JSON
              cctv_directory.py → resolve nama kamera dari CSV (by IP)
```

## ⚙️ Cara kerja singkat

1. Setiap siklus, semua NVR dipoll paralel via ISAPI (`/ContentMgmt/InputProxy/channels/status`).
2. Kamera yang terdeteksi offline masuk status `pending` — **belum** ada alert.
3. Kalau masih offline setelah melewati `offline_confirm_seconds`, sistem
   menjalankan verifikasi final (channels → fallback endpoint → ping) sebelum
   alert dikirim.
4. Kamera yang online lagi sebelum threshold tercapai → dianggap flapping,
   alert **dibatalkan otomatis**.
5. Kamera yang sempat `confirmed` offline lalu online lagi → alert ONLINE
   dikirim, lengkap dengan durasi downtime yang dihitung dari waktu **pertama
   kali** terdeteksi offline (bukan waktu alert dikirim).

## 🚀 Menjalankan

```bash
pip install requests

cp config.example.json config.json
cp cctv_list.example.csv cctv_list.csv
# edit config.json: isi kredensial NVR, bot token Telegram, dan daftar NVR kamu

python3 cctv_monitor.py --once      # sekali jalan (cocok untuk cron)
python3 cctv_monitor.py             # loop terus-menerus
```

Bikin bot Telegram via **@BotFather**, tambahkan ke grup tujuan alert, lalu
ambil `chat_id` dari `https://api.telegram.org/bot<TOKEN>/getUpdates`.

## 📁 Struktur file

| File | Peran |
|---|---|
| `cctv_monitor.py` | Entry point, CLI, logging setup, main loop |
| `alert_engine.py` | State machine debounce + polling paralel |
| `isapi_client.py` | Client Hikvision ISAPI (Digest/Basic auth, XML parsing, ping) |
| `telegram_client.py` | Delivery + retry + persistent queue |
| `alert_messages.py` | Template pesan Telegram |
| `alert_state.py` | Load/save state ke JSON |
| `cctv_directory.py` | Resolve nama kamera dari CSV berdasarkan IP |
| `cctv_monitor_timeutils.py` | Util timezone |
| `config.example.json` | Contoh konfigurasi (copy jadi `config.json`) |
| `cctv_list.example.csv` | Contoh daftar kamera (copy jadi `cctv_list.csv`) |

## 🔧 Tech stack

Python 3.10+ · `requests` · `concurrent.futures.ThreadPoolExecutor` ·
Hikvision ISAPI (XML over HTTP, Digest Auth) · Telegram Bot API

## 📝 Catatan

Data konfigurasi, daftar kamera, dan log pada repo ini adalah **contoh/dummy**.
Sistem asli berjalan di production memantau ratusan kamera di puluhan lokasi
remote dengan kondisi jaringan yang jauh lebih menantang (satelit/microwave
link, DFS channel switching, dsb).

## 📄 Lisensi

MIT — silakan pakai/modifikasi untuk kebutuhan kamu sendiri.
