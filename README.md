<div align="center">

<img src="https://readme-typing-svg.herokuapp.com?font=Fira+Code&weight=600&size=28&duration=3000&pause=800&color=2E8B57&center=true&vCenter=true&width=600&lines=%F0%9F%8C%BF+AgriNova;A+farm+that+texts+you+back.;FPGA+%E2%80%A2+Arduino+%E2%80%A2+Telegram;Detect+%E2%80%A2+Deter+%E2%80%A2+Irrigate" alt="AgriNova" />

**An FPGA-based smart crop-protection & irrigation system, controlled entirely from Telegram.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.9+-3776AB?logo=python&logoColor=white)](mac_bridge.py)
[![Verilog](https://img.shields.io/badge/Verilog-iCE40UP5K-orange?logo=lattice)](top.v)
[![Arduino](https://img.shields.io/badge/Arduino-UNO-00979D?logo=arduino&logoColor=white)](agrinova_soil_sensor/)
[![Telegram Bot](https://img.shields.io/badge/Telegram-Bot%20API-26A5E4?logo=telegram&logoColor=white)](https://core.telegram.org/bots/api)
[![Made with ❤️ for](https://img.shields.io/badge/Made%20for-Small%20Farmers-brightgreen)](#-why)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-blueviolet)](#-contributing)

<i>Hardware-level reliability. Chat-level simplicity. ~₹2,500 total.</i>

</div>

---

> **Choose how you want to read this:**

<details open>
<summary><h3>🧑‍🌾 &nbsp;SIMPLE — explain it like I'm not an engineer</h3></summary>

## 🧑‍🌾 What is this, in plain words?

AgriNova is a **guard and a gardener for a small farm**, that talks to you on **Telegram** — the normal chat app.

**What it does for you:**

- 👀 **It watches your field.** If an animal or a person walks in, your phone gets a message: *"🚨 Someone is in your farm!"* — with a **photo and a video**, in seconds. It even sprays water to scare the animal away.
- 💧 **It waters your plants for you.** A small sensor sits in the soil. When the soil gets dry, the mist switches on. The moment the soil is wet enough, it switches off. Not one drop wasted.
- 🌧 **It checks the sky first.** Before watering, it looks at the weather forecast. Rain coming? It skips the watering and tells you.
- 🌡 **It tells you how the field is doing.** Send `/status` and you get the soil, temperature and humidity right now. Send `/graph` and you get a picture of the whole day.
- 🗣 **It speaks your language.** Send `/lang` and choose English, తెలుగు, or Telugu written in English letters.
- 🔘 **No typing needed.** Send `/menu` and you get buttons to press.

**What you need:** the small electronics kit (about **₹2,500** in parts), a computer at home, and Telegram on your phone. That's all — no monthly fee, no special app, no internet plan for the field.

**Why it's trustworthy:** the part that watches for intruders is built in *hardware* — a chip that does only that one job. Even if the computer hangs or crashes, the watching never stops, and everything reconnects by itself. If someone pulls out the camera, you get an emergency message with the video up to that moment.

*Want the engineering details? Open the Technical section below.* 🔬

</details>

<details>
<summary><h3>🔬 &nbsp;TECHNICAL — architecture, commands, build guide</h3></summary>

## 🚜 What is AgriNova?

AgriNova turns a small farm plot into something that **watches, waters, and reports back** — all through the Telegram app the farmer already has. No dashboard, no subscription, no custom app.

```
        🐦 intruder                       🥀 dry soil
            │                                 │
            ▼                                 ▼
   ┌────────────────┐               ┌────────────────┐
   │  IR sensor     │               │  soil probe    │
   │  ↓ (µs, HW)    │               │  DHT11 temp/RH │
   │  FPGA (Verilog)│               │  Arduino UNO   │
   └───────┬────────┘               └──────┬─────────┘
           │ UART "IR"                     │ USB serial
           ▼                               ▼
   ┌──────────────────────────────────────────────┐
   │            Python bridge (the brain)         │
   │  Telegram bot · camera relay · weather ·     │
   │  auto-irrigation · logging · graphs          │
   └───────┬──────────────────────────┬───────────┘
           ▼                          ▼
   📱 "🚨 Someone is in       💨 mist ON → soil wet
       your farm!" + 📸🎥         → mist OFF
```

**The core idea:** safety-critical detection lives in *hardware* (an iCE40 FPGA that cannot crash, with a 10 s cool-down burned into logic), while intelligence lives in *software*. If every computer in the system dies, the FPGA is still watching — and everything reconnects and resumes by itself.

## ✨ Features

| | Feature | How |
|---|---|---|
| 🔒 | **Intruder detection** | IR → FPGA edge-detect (µs) → alert + photo (~2 s) + 5 s video + mist deterrent |
| 🚨 | **Tamper alarm** | Camera yanked mid-recording → `CRITICAL` alert + partial clip as evidence |
| 💧 | **Closed-loop irrigation** | Sprays when soil is dry, stops **the moment** the target moisture is reached |
| 🌦 | **Weather-aware** | Checks wttr.in first — rain coming in 6 h? Spray skipped, water saved |
| ⏰ | **Schedules** | `/schedule 07:00 10` — daily sprays, persisted across restarts |
| 📊 | **Data** | Every reading logged to CSV · `/graph` 24 h chart · `/summary` daily digest |
| 📷 | **Camera** | `/photo`, `/video 15`, `/snapshot` (photo + live readings) with audio |
| 🎛 | **Button UI** | `/menu` — inline keyboard, no typing needed |
| 🛡 | **Hardened** | Chat-ID allowlist · 3-step confirmed shutdown · offline/online notifications for every device |
| 🔁 | **Self-healing** | USB dropouts, camera loss, network errors — everything reconnects automatically |

## 💬 Telegram in action

```text
You:      /status
AgriNova: 📡 AgriNova status — 18:42:07
          ✅ FPGA   ✅ Arduino   ✅ Camera
          🌱 Soil: 34% (DRY)  🌡️ 29.4°C  💧 61%
          💨 Mist: OFF   🔒 Lockdown: ON   🤖 Auto-mist: ON

AgriNova: 🚨 Someone is in your farm! 2026-08-22 18:44:03
AgriNova: 📸 Intruder snapshot — [photo]
AgriNova: 🎥 Intruder clip — [video]

AgriNova: 🤖 Auto-irrigation: soil 52% → misting until ≤30%
AgriNova: 💧 Soil reached 29% — mist stopped after 41.2s
```

<details>
<summary><b>📖 Full command reference (click to expand)</b></summary>

| Command | Action |
|---|---|
| `/status` | Everything at a glance — devices, sensors, modes |
| `/lockdown` / `/unlock` | Arm / disarm intruder alerts |
| `/photo` · `/video [s]` · `/snapshot` | Camera capture |
| `/mist on` / `/mist off` · `/spray [s]` | Manual mist control |
| `/auto_mist on\|off` | Sensor-driven irrigation |
| `/schedule HH:MM [s]` · `list` · `clear` | Daily sprays |
| `/calibrate air` → `/calibrate water` | 2-step soil calibration from chat |
| `/weather` · `/weather set <city>` · `/rain_skip on\|off` | Forecast & rain-skip |
| `/graph` · `/summary` · `/intruders` · `/soil_raw` | Data & logs |
| `/menu` | Inline button panel |
| `/mute` / `/unmute` · `/stop` (3× confirm) | Alert control · shutdown |

Commands are case-insensitive; `/mist off`, `/MIST_OFF` and `/mistoff` all work.
</details>

## 🔧 Hardware

<table>
<tr><td>

| Part | Role | ≈₹ |
|---|---|---|
| VSDSquadron FM (iCE40UP5K) | HW intruder logic | 1200 |
| Arduino UNO | Sensors + relay | 400 |
| IR obstacle sensor | Detection | 50 |
| Soil probe + LM393 board | Moisture | 80 |
| DHT11 | Temp / humidity | 80 |
| 5 V relay + USB mist maker | Irrigation | 410 |
| Webcam (any UVC) | Evidence | reuse |
| **Total** | | **≈2,500** |

</td><td>

```
FPGA  pin 38 ◄─ IR OUT (3.3 V only!)
      pin 14 ─► UART TX 9600 8N1

UNO   A0 ◄─ soil AO      A2 ─► relay IN
      A1 ◄─ DHT11 DATA   A3 ─► servo

Relay COM◄─5V charger  NO─►mist +5V
      (mist is NEVER powered
       from the Arduino)
```
See **[docs/](docs/)** for the full wiring diagram.

</td></tr>
</table>

## 🚀 Getting started

```bash
git clone https://github.com/rohancodesss/agrinova && cd agrinova
```

**1 · FPGA** (open-source toolchain: yosys → nextpnr → icepack)
```bash
yosys -p "synth_ice40 -json top.json" top.v uart_tx_byte.v
nextpnr-ice40 --up5k --package sg48 --json top.json --pcf VSDSquadronFM.pcf --asc top.asc
icepack top.asc top.bin && iceprog top.bin
```

**2 · Arduino**
```bash
arduino-cli compile --fqbn arduino:avr:uno agrinova_soil_sensor
arduino-cli upload  --fqbn arduino:avr:uno -p /dev/cu.usbmodem* agrinova_soil_sensor
```

**3 · Bridge** — set your bot token + chat ID in `mac_bridge.py`, then
```bash
python3 -u mac_bridge.py          # or ./install_service.sh install (launchd)
```

**4 · Camera node** (optional, Windows) — `python windows/camera_service.py`

Then open Telegram and send `/help`. 🎉

## 🧠 Why an FPGA?

> *"Couldn't an Arduino do all of this?"*

The detection path must never fail. On the FPGA it is **pure combinational + registered logic**: input synchroniser → edge detector → hardware cool-down counter → UART sequencer with request/acknowledge handshake. It responds in **microseconds**, cannot hang, cannot be crashed by software, and needs no OS, no boot, no updates. Software crashing costs you features; it never costs you detection.

## 🗺 Roadmap

- [x] Weather-aware irrigation (wttr.in)
- [x] Inline-keyboard UI, calibration-from-chat, daily summaries
- [ ] `/live` real-time video streaming
- [ ] Crop profiles (per-crop moisture targets)
- [x] Telugu alerts (native script + Latin script, `/lang`)
- [x] Hindi alerts (हिंदी, `/lang hi`)
- [ ] ESP32 / Raspberry Pi standalone build (no laptop)
- [ ] Solar power + FPGA-driven buzzer deterrent

## 🤝 Contributing

Issues and PRs welcome — this is a student project built to be reproduced. Good first contributions: new sensor drivers, translations, an ESP32 port of the bridge.

## 📄 License

[MIT](LICENSE) — do anything, just keep the notice.

</details>

---

<div align="center">
<img src="https://img.shields.io/badge/🌿-AgriNova-2E8B57?style=for-the-badge" alt="AgriNova"/>

**Built with** Verilog · Python · Arduino C++ · FFmpeg · OpenCV · Telegram Bot API · wttr.in

*If this helped you, ⭐ the repo.*
</div>
