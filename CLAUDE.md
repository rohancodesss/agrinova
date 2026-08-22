# AgriNova — project notes for agents

IoT crop-protection prototype: **VSDSquadron FM (iCE40UP5K FPGA) + Arduino UNO + Mac bridge + Windows VM camera → Telegram bot.**
Read this whole file before touching anything. Hardware is physically wired and cannot be changed easily.

## Hard rules (from the user)
- **Always use `arduino-cli`** for compile/upload. Never suggest the Arduino IDE GUI.
- **Never run `mac_bridge.py` yourself.** The user runs it with the shell alias `agri`. A second instance causes Telegram HTTP 409 (two pollers) and steals the serial ports. After editing the bridge, tell the user to restart `agri`.
- **Do not flip relay polarity or touch wiring-related constants** unless asked. User has routed wires behind the board and has said "fixed the wiring, don't change code".
- Hardcoded Telegram token/chat-id in `mac_bridge.py` is accepted by the user ("I'm fine with the risk"). Don't refactor it out.
- When the user says "DO NOT WRITE CODE YET", discuss only.
- Don't do any work the user didn't ask for; their requests are terse and literal.

## Files
| Path | What |
|---|---|
| `mac_bridge.py` | Mac bridge. FPGA UART listener, Arduino serial, Telegram long-poll, camera relay HTTP server (port 5001). |
| `agrinova_soil_sensor/agrinova_soil_sensor.ino` | **The Arduino sketch that gets uploaded** (arduino-cli requires folder == sketch name). |
| `agrinova_soil_sensor.ino` (root) | Stale copy — may differ (`MIST_RELAY_ACTIVE_LOW`). Ignore or sync from the folder copy. |
| `top.v`, `VSDSquadronFM.pcf` | FPGA design currently flashed (IR-only). |
| `top_soil_variant.v.bak`, `VSDSquadronFM_soil.pcf.bak` | Older soil-on-FPGA variant, unused. |
| `sensor_probe/sensor_probe.ino` | I2C scan + DHT11/22 probe sketch for debugging. |
| `windows/camera_service.py` | Copy of the Windows VM camera client (canonical copy lives at `~/Desktop/WINDOWS_AGRI/`, shared into the VM as `Z:\WINDOWS_AGRI`). Keep both in sync. |
| `com.agrinova.bridge.plist`, `install_service.sh` | Optional launchd service (`./install_service.sh install`). Only one bridge may run — service OR `agri`, never both. |
| `agrinova_state.json` | Persisted settings (lockdown, alerts, auto_mist, schedules, soil calibration). Gitignored. |
| `agrinova_log.csv`, `agrinova_events.csv`, `bridge.log` | Runtime logs. Gitignored. |
| `~/Desktop/WINDOWS_AGRI/camera_service.py` | Windows VM camera client (run with `python camera_service.py`). Shared folder `Z:\WINDOWS_AGRI` in the VM. |
| `~/Desktop/WINDOWS_AGRI/AgriNova.pptx` | 11-slide project deck. `wiring_mist_relay_arduino.png` wiring image. |
| `~/Desktop/PI_AGRI/` | Raspberry Pi variant — **abandoned/reverted**, do not use. |

## Architecture / data flow
```
IR sensor ─► FPGA pin 38 ─(UART 9600 8N1, pin 14)─► Mac /dev/cu.usbserial-* ─► mac_bridge.py ─► Telegram
Arduino UNO (soil A0, DHT11 A1, mist relay A2, servo A3) ─(USB serial 9600)─► mac_bridge.py
Windows VM (UTM, 192.168.64.3) camera_service.py ──polls──► Mac 192.168.64.1:5001 (GET /job, POST /result/<id>)
```
- **Direction VM→Mac is mandatory.** Mac→VM is blocked by macOS Local Network privacy (EHOSTUNREACH); do not try to reverse it. No Tailscale on the VM.
- The Logitech C170 must be attached to the VM via UTM's USB menu, else ffmpeg sees a phantom device (blank black frames, rejected by the blank-frame guard).

## FPGA (top.v)
- 12 MHz clock, baud tick 1250 clocks. IR `DO` is active-LOW; falling edge → toggles `ir_req`, 10 s hardware cooldown; sequencer sends `"IR\n"` with request/ack toggle handshake. `led_blue` = IR detecting, `led_green` = transmitting, red disabled.
- PCF: hw_clk 20, uarttx 14, led_red 39, led_green 40, led_blue 41, ir_in 38.
- Toolchain: `~/Downloads/oss-cad-suite/bin` — `yosys → nextpnr-ice40 → icepack → iceprog`. If iceprog says `Write error rc=-1`, run `iceprog -b` (bulk erase) then retry.
- IR wiring: VCC→FPGA 3.3V, GND→GND, OUT→pin 38.

## Arduino sketch
- Serial 9600. Every 5 s prints `DATA:moisture,temp,humidity,WET|DRY,raw,MIST_ON|MIST_OFF,SERVO_ON|SERVO_OFF` plus `[sensor] ...` debug line. Bridge tolerates the old 4-field form.
- Commands: `PING`→`PONG`, `MOISTURE?`, `RAW?`→`RAW:<adc>`, `STATUS`, `ROTATE` (A3 HIGH), `STOP` (A3 LOW), `MIST_ON`→`MIST:ON`, `MIST_OFF`→`MIST:OFF`.
- `MIST_RELAY_ACTIVE_LOW = true` is what's on the board and matches the user's wiring. **Do not flip.**
- Soil calibration: Arduino default `map(raw, 800, 400, 0, 100)`, wet = ≥40 %. The **bridge overrides** this once the user runs `/calibrate air` + `/calibrate water` (stored in `agrinova_state.json`); no re-upload needed.
- DHT11 on A1 via `DHT` library; NaN readings keep last value. Read 0 at one boot — reseat if it happens.
- The relay and mist maker are powered from an external 5 V wall charger via a split USB-C cable (Arduino can't source enough current). USB colours: red +5V, grey/black GND, white/green data. Servo also needs external 5 V.
- Upload:
  ```
  arduino-cli compile --fqbn arduino:avr:uno agrinova_soil_sensor
  arduino-cli upload  --fqbn arduino:avr:uno -p /dev/cu.usbmodem* agrinova_soil_sensor
  ```
  Kill the user's `agri` bridge first (it holds the port) and tell them to restart it afterwards.

## mac_bridge.py behaviour
- Structure: `settings` (persisted, `settings_lock`) + `state` (live, `state_lock`); threads: `listen_arduino`, `camera_relay_server`, `telegram_reply_loop`, `camera_heartbeat`, `auto_mist_loop`, `scheduler_loop`; main thread = FPGA UART loop.
- Commands go through `normalise()` (lowercase, collapse spaces, strip `@bot`, `ALIASES` table so `/mist off` == `/mist_off`) then `handle_command(text)`; each runs in its own thread via `safe_handle`. Inline-keyboard buttons (`/menu`) send `callback_data` into the same path.
- Commands: `/help /menu /about /status /snapshot /graph /summary /intruders /soil_raw /calibrate air|water|reset /lockdown /unlock /stop /continue /photo /video [s] /mist_on /mist_off /spray [s] /auto_mist on|off /schedule HH:MM [s]|list|clear|remove HH:MM /rotate /rotate_stop`.
- Only `ALLOWED_CHAT_IDS` may command the bot; others are logged and ignored.
- `settings["lockdown"]` gates IR alerts. On `IR` while locked down: alert → `intruder_response` (photo first, then 5 s video) + 10 s `mist_burst`. Every IR is counted in `state["intruder_events"]` regardless.
- Weather: `fetch_weather()` hits `https://wttr.in/<location>?format=j1` (no key, 15 min cache). `rain_expected()` → skip scheduled/auto sprays when chance ≥ 60 % in next 6 h (`settings["rain_skip"]`, default on). `/weather`, `/weather set <city>`, `/rain_skip on|off`. Location persisted in `settings["location"]`.
- Auto-irrigation: when `auto_mist` is on and soil has been DRY ≥ 5 min (`AUTO_MIST_DRY_MINUTES`), burst 10 s, max 4/h. Schedules fire daily via `scheduler_loop`; daily summary at `DAILY_SUMMARY_TIME` (20:00).
- Alerts: soil every 10 min + on WET/DRY change; climate warning (≥35 °C or ≤30 % RH) every 30 min; FPGA/Arduino/camera disconnect + reconnect; bridge-online on start; camera-removal CRITICAL.
- Logging: every reading → `agrinova_log.csv`; events → `agrinova_events.csv`. `/graph` renders the in-memory 24 h history with matplotlib (installed `--user` for /usr/bin/python3).
- Camera relay: `request_capture(kind, duration, timeout)`; Windows POSTs bytes back; header `X-Camera-Status: removed` → CRITICAL alert. `camera_online()` = polled within 60 s.
- Telegram uploads use hand-built multipart (`build_multipart`), urllib only. All loops catch `Exception` and reconnect.
- Check syntax with `python3 -m py_compile mac_bridge.py`; handlers can be exercised offline by importing the module and monkeypatching `send_telegram_message`. Never launch the bridge.

## camera_service.py (Windows)
- Detects dshow device names with `ffmpeg -list_devices`; records with `-rtbufsize 100M -preset ultrafast`, audio via aac; checks duration with ffprobe; falls back to OpenCV (measured-fps writer). Partial clip is kept and flagged removed if camera vanishes mid-record.
- Windows python lacks working SSL certs → uses `requests` to the Mac only, never to Telegram directly.

## Known history / gotchas
- HC-SR04 ultrasonic was removed (floating echo → MOTION spam). There is **no motion sensor**; only IR.
- Both USB boards have vanished from the bus twice (hub brownout) — tell the user to replug, don't debug software.
- Live video (`/live`) was discussed and scrapped; it lives in the deck roadmap only.
- Scratchpad gets wiped; PPTX generator script is gone — edit `AgriNova.pptx` via slide-XML patching.

## Outstanding
- Upload the latest Arduino sketch (adds `RAW?` + extended DATA line) — was compiled but boards were unplugged.
- User to run `/calibrate air` then `/calibrate water`.
- Full lockdown → IR → photo+video + mist-burst retest; test `/menu` buttons, `/graph`, `/auto_mist on`.

## Git
- Remote: https://github.com/rohancodesss/agrinova (push with `git push`). Telegram token is committed by the user's explicit choice.
