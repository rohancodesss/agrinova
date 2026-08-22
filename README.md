# AgriNova

IoT crop-protection prototype: **VSDSquadron FM (iCE40 FPGA) + Arduino UNO + Mac bridge + Windows VM camera → Telegram bot**.

```
IR sensor ─► FPGA (Verilog, 10 s cooldown) ─UART─► mac_bridge.py ─► Telegram
Arduino UNO: soil A0 · DHT11 A1 · mist relay A2 · servo A3 ─USB─► mac_bridge.py
Windows VM camera_service.py ──polls──► Mac :5001 (photo / video / camera-removal alert)
```

## Features
- 🔒 `/lockdown` arms IR intruder alerts → instant photo, 5 s video, 10 s mist burst
- 🚨 CRITICAL alert if the camera is physically removed mid-recording
- 🌱 Soil / temperature / humidity updates, WET↔DRY change alerts, heat & dry-air warnings
- 💨 Mist control: `/mist on|off`, `/spray 10`, `/auto_mist on` (dry ≥5 min → spray, max 4/h), `/schedule 07:00 10`
- 📊 `/status`, `/snapshot`, `/graph` (24 h plot), `/summary` (daily at 20:00), `/intruders`
- 🎛 `/menu` inline-button control panel; chat-ID allowlist; settings persist across restarts
- 🔁 Online/offline notifications for FPGA, Arduino and camera; CSV logging

## Layout
| Path | Purpose |
|---|---|
| `mac_bridge.py` | Mac bridge (run: `python3 -u mac_bridge.py`, or `./install_service.sh install` for launchd) |
| `agrinova_soil_sensor/` | Arduino sketch — `arduino-cli compile/upload --fqbn arduino:avr:uno agrinova_soil_sensor` |
| `top.v`, `uart_tx_byte.v`, `VSDSquadronFM.pcf` | FPGA design (yosys → nextpnr-ice40 → icepack → iceprog) |
| `windows/camera_service.py` | Windows VM camera client |
| `docs/` | Wiring diagram, presentation |
| `CLAUDE.md` | Detailed engineering notes / gotchas |
