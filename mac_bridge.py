#!/usr/bin/env python
"""AgriNova Mac bridge.

Listens to the VSDSquadron FM (IR trigger over UART) and the Arduino node
(soil / DHT11 / mist relay / servo), relays camera jobs to the Windows VM
(which polls us on :5001), and talks to Telegram.
"""

import csv
import glob
import http.server
import io
import json
import os
import queue
import serial
import sys
import threading
import time
import urllib.request
import urllib.parse
import urllib.error
from collections import deque

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "agrinova_state.json")
LOG_FILE = os.path.join(BASE_DIR, "agrinova_log.csv")
EVENT_FILE = os.path.join(BASE_DIR, "agrinova_events.csv")

# Windows VM polls US for camera jobs (VM->Mac direction works; Mac->VM is
# blocked by macOS Local Network privacy). The VM connects to 192.168.64.1:5001.
CAMERA_RELAY_PORT = 5001

SERIAL_PORT_HINT = "/dev/cu.usbserial-120"
BAUD_RATE = 9600

# Secrets live in agrinova_secrets.json (gitignored) — never in this file.
SECRETS_FILE = os.path.join(BASE_DIR, "agrinova_secrets.json")
try:
    with open(SECRETS_FILE) as _f:
        _secrets = json.load(_f)
    BOT_TOKEN = _secrets["bot_token"]
    CHAT_ID = str(_secrets["chat_id"])
except (FileNotFoundError, KeyError, json.JSONDecodeError) as _e:
    sys.exit(f"[bridge] missing/invalid {SECRETS_FILE}: {_e}\n"
             "Create it with: {\"bot_token\": \"<token from @BotFather>\", \"chat_id\": \"<your chat id>\"}")
ALLOWED_CHAT_IDS = {CHAT_ID}  # anyone else is ignored

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
SEND_MESSAGE_API = f"{API}/sendMessage"
SEND_PHOTO_API = f"{API}/sendPhoto"
SEND_VIDEO_API = f"{API}/sendVideo"
GET_UPDATES_API = f"{API}/getUpdates"
ANSWER_CALLBACK_API = f"{API}/answerCallbackQuery"

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------
SOIL_ALERT_INTERVAL = 10 * 60      # routine soil update cadence (s)
CLIMATE_ALERT_INTERVAL = 30 * 60   # heat / dry-air warning cadence (s)
TEMP_HIGH_C = 35.0
HUMIDITY_LOW_PCT = 30.0
MIST_BURST_SECONDS = 10            # intruder / auto-irrigation burst
INTRUDER_VIDEO_SECONDS = 5
AUTO_MIST_DRY_SECONDS = 0          # spray as soon as soil reads DRY (0 = immediately)
AUTO_MIST_MAX_PER_HOUR = 4
CAMERA_OFFLINE_AFTER = 60          # seconds without a poll from the VM
DAILY_SUMMARY_TIME = "20:00"
WEATHER_CACHE_SECONDS = 15 * 60
RAIN_SKIP_CHANCE = 60              # % chance of rain (next few hours) that cancels a spray
RAIN_SKIP_HOURS = 6
HISTORY_HOURS = 24

DEFAULT_REPLY_TEXT = "Unknown command. Send /help for the list."
HELP_TEXT = (
    "🌿 AgriNova commands\n"
    "\n"
    "📊 Info\n"
    "/status — everything at a glance\n"
    "/snapshot — photo + live readings\n"
    "/graph — last 24 h sensor graph\n"
    "/summary — today's summary\n"
    "/intruders — intruder events today\n"
    "/soil_raw — raw soil ADC value\n"
    "\n"
    "🔒 Security\n"
    "/lockdown  /unlock — arm / disarm IR alerts\n"
    "/mute  /unmute — silence / resume all alerts\n"
    "/messages [n] — routine updates per day (alarms always sent)\n"
    "/photo — take a photo\n"
    "/video [sec] — record video (1-30)\n"
    "\n"
    "💧 Irrigation\n"
    "/mist_on  /mist_off  (or '/mist on')\n"
    "/spray [sec] — timed burst (default 10)\n"
    "/auto_mist on|off — spray when soil is dry\n"
    "/schedule HH:MM [sec] — daily spray\n"
    "/schedule list  /schedule clear\n"
    "/calibrate air|water — set soil calibration\n"
    "/weather — forecast (wttr.in)\n"
    "/weather set <city> — set location\n"
    "/rain_skip on|off — skip sprays if rain likely\n"
    "\n"
    "⚙️ Other\n"
    "/lang — language: English / हिंदी / తెలుగు / Telugu (English)\n"
    "/rotate  /rotate_stop — servo\n"
    "/setup — guided setup wizard (5 questions)\n"
    "/menu — button keyboard\n"
    "/about — project info\n"
    "/stop — shut the bridge down (asks 3 times)"
)
ABOUT_TEXT = (
    "AgriNova — IoT crop protection prototype. Signal path: "
    "VSDSquadron FPGA Mini (Verilog trigger logic) → UART → Mac bridge → Telegram, "
    "Arduino UNO (soil, DHT11, mist relay, servo), Windows VM camera. "
    "Send /help for commands."
)
ROTATE_TEXT = "⚙️ Servo ON (A3 HIGH). Send /rotate_stop to stop."
ROTATE_STOP_TEXT = "⚙️ Servo OFF (A3 LOW)."
MIST_ON_TEXT = "💨 Mist ON."
MIST_OFF_TEXT = "💨 Mist OFF."
ARDUINO_MISSING_TEXT = "Arduino is not connected — command not sent."
STOP_TEXT = "🔕 Alerts muted. Send /unmute to resume."
SHUTDOWN_CONFIRM_WINDOW = 60  # seconds to complete the 3-step /stop
CONTINUE_TEXT = "Alerts resumed."
LOCKDOWN_TEXT = "🔒 Lockdown ON — IR intruder alerts are now active."
UNLOCK_TEXT = "🔓 Lockdown OFF — intruder alerts muted. Send /lockdown to arm again."


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def fmt_duration(secs):
    secs = int(secs)
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


# --------------------------------------------------------------------------
# Persistent settings (survive restarts)
# --------------------------------------------------------------------------
settings = {
    "lockdown": False,
    "alerts": True,
    "auto_mist": False,
    "schedules": [],            # [{"time": "07:00", "secs": 10}]
    "soil_raw_air": None,       # calibration: raw value in air (dry)
    "soil_raw_water": None,     # calibration: raw value in water (wet)
    "location": "",             # wttr.in location, e.g. "Hyderabad" (empty = geo-IP guess)
    "rain_skip": True,          # skip scheduled/auto sprays when rain is likely
    "daily_msgs": 30,           # farmer's cap on routine messages/day (0 = unlimited)
    "lang": "en",               # en | hi (Hindi) | te (Telugu) | te_en (Telugu in English script)
}

# ---------------------------------------------------------------------------
# Languages: en = English, te = Telugu, te_en = Telugu in English letters
# ---------------------------------------------------------------------------
MSGS = {
    "intruder": {
        "hi": "🚨 आपके खेत में कोई है! {t}",
        "en": "🚨 Someone is in your farm! {t}",
        "te": "🚨 మీ పొలంలో ఎవరో ఉన్నారు! {t}",
        "te_en": "🚨 Mee polam lo evaro unnaru! {t}",
    },
    "lockdown_on": {
        "hi": "🔒 लॉकडाउन चालू — घुसपैठ की चेतावनियाँ अब सक्रिय हैं।",
        "en": "🔒 Lockdown ON — IR intruder alerts are now active.",
        "te": "🔒 లాక్‌డౌన్ ఆన్ — దొంగల హెచ్చరికలు ఇప్పుడు యాక్టివ్‌గా ఉన్నాయి.",
        "te_en": "🔒 Lockdown ON — dongala hechcharikalu ippudu active ga unnayi.",
    },
    "lockdown_off": {
        "hi": "🔓 लॉकडाउन बंद — चेतावनियाँ रुक गई हैं। फिर से चालू करने के लिए /lockdown भेजें।",
        "en": "🔓 Lockdown OFF — intruder alerts muted. Send /lockdown to arm again.",
        "te": "🔓 లాక్‌డౌన్ ఆఫ్ — హెచ్చరికలు ఆగిపోయాయి. మళ్ళీ ఆన్ చేయడానికి /lockdown పంపండి.",
        "te_en": "🔓 Lockdown OFF — hechcharikalu aagipoyayi. Malli on cheyataniki /lockdown pampandi.",
    },
    "mist_on": {
        "hi": "💨 मिस्ट चालू हो गई।",
        "en": "💨 Mist ON.",
        "te": "💨 మిస్ట్ ఆన్ అయింది.",
        "te_en": "💨 Mist ON ayyindi.",
    },
    "mist_off": {
        "hi": "💨 मिस्ट बंद हो गई।",
        "en": "💨 Mist OFF.",
        "te": "💨 మిస్ట్ ఆఫ్ అయింది.",
        "te_en": "💨 Mist OFF ayyindi.",
    },
    "soil_line": {
        "hi": "{p}🌱 मिट्टी की नमी: {m}% ({st}) | 🌡️ तापमान: {t}°C | 💧 हवा में नमी: {h}%",
        "en": "{p}🌱 Soil: {m}% ({st}) | 🌡️ {t}°C | 💧 {h}%",
        "te": "{p}🌱 నేల తేమ: {m}% ({st}) | 🌡️ ఉష్ణోగ్రత: {t}°C | 💧 గాలిలో తేమ: {h}%",
        "te_en": "{p}🌱 Nela thema: {m}% ({st}) | 🌡️ Ushnograta: {t}°C | 💧 Gaalilo thema: {h}%",
    },
    "soil_changed": {
        "hi": "⚠️ मिट्टी की स्थिति बदल गई! ",
        "en": "⚠️ Soil state changed! ",
        "te": "⚠️ నేల స్థితి మారింది! ",
        "te_en": "⚠️ Nela sthithi maarindi! ",
    },
    "auto_mist_start": {
        "hi": "🤖 ऑटो सिंचाई: मिट्टी {m}% → ≤{tgt}% होने तक मिस्ट ({n}/{max} इस घंटे)",
        "en": "🤖 Auto-irrigation: soil {m}% → misting until it reads ≤{tgt}% ({n}/{max} this hour)",
        "te": "🤖 ఆటో నీటిపారుదల: నేల {m}% → ≤{tgt}% అయ్యే వరకు మిస్ట్ ({n}/{max} ఈ గంటలో)",
        "te_en": "🤖 Auto neeti paarudala: nela {m}% → ≤{tgt}% ayye varaku mist ({n}/{max} ee gantalo)",
    },
    "mist_stopped_wet": {
        "hi": "💧 मिट्टी {m}% पर पहुँची (≤{tgt}%) — {s} सेकंड बाद मिस्ट बंद।",
        "en": "💧 Soil reached {m}% (≤{tgt}%) — mist stopped after {s}s.",
        "te": "💧 నేల {m}% కి చేరింది (≤{tgt}%) — {s} సెకన్లకు మిస్ట్ ఆగింది.",
        "te_en": "💧 Nela {m}% ki cherindi (≤{tgt}%) — {s} seconds ki mist aagindi.",
    },
    "camera_removed": {
        "hi": "🚨 [आपातकाल]: कैमरा ज़बरदस्ती निकाला गया — {t}",
        "en": "🚨 [CRITICAL]: THE CAMERA WAS FORCEFULLY REMOVED — {t}",
        "te": "🚨 [అత్యవసరం]: కెమెరా బలవంతంగా తీసివేయబడింది — {t}",
        "te_en": "🚨 [CRITICAL]: Camera balavanthanga theesivesaru — {t}",
    },
    "rain_skipped": {
        "hi": "🌧 बारिश की संभावना है — स्प्रे रोक दिया गया ({why})।",
        "en": "🌧 Spray skipped — {why}.",
        "te": "🌧 వర్షం వచ్చే అవకాశం ఉంది — స్ప్రే ఆపివేయబడింది ({why}).",
        "te_en": "🌧 Varsham vacche avakasam undi — spray aapivesamu ({why}).",
    },
    "heat_warn": {
        "hi": "🔥 तापमान बहुत अधिक: {t}°C",
        "en": "🔥 High temperature: {t}°C",
        "te": "🔥 అధిక ఉష్ణోగ్రత: {t}°C",
        "te_en": "🔥 Adhika ushnograta: {t}°C",
    },
    "dry_air_warn": {
        "hi": "🏜️ हवा में नमी कम है: {h}%",
        "en": "🏜️ Low humidity: {h}%",
        "te": "🏜️ గాలిలో తేమ తక్కువగా ఉంది: {h}%",
        "te_en": "🏜️ Gaalilo thema takkuva ga undi: {h}%",
    },
    "climate_header": {
        "hi": "⚠️ मौसम चेतावनी",
        "en": "⚠️ Climate warning",
        "te": "⚠️ వాతావరణ హెచ్చరిక",
        "te_en": "⚠️ Vaatavarana hechcharika",
    },
    "lang_set": {
        "hi": "✅ भाषा हिंदी में बदल दी गई।",
        "en": "✅ Language set to English.",
        "te": "✅ భాష తెలుగుకి మార్చబడింది.",
        "te_en": "✅ Bhasha Telugu (English letters) ki marchabadindi.",
    },
}


def cur_lang():
    with settings_lock:
        return settings.get("lang", "en")


def msg(key, **kw):
    entry = MSGS.get(key)
    if not entry:
        return key
    template = entry.get(cur_lang(), entry["en"])
    return template.format(**kw)
settings_lock = threading.Lock()


def load_settings():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        with settings_lock:
            settings.update({k: v for k, v in data.items() if k in settings})
        print(f"[state] loaded {STATE_FILE}")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[state] could not load settings: {e}", file=sys.stderr)


def save_settings():
    try:
        with settings_lock:
            data = dict(settings)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"[state] could not save settings: {e}", file=sys.stderr)


# --------------------------------------------------------------------------
# Live state
# --------------------------------------------------------------------------
START_TIME = time.time()
state = {
    "moisture": None, "temp": None, "humidity": None, "wet": None, "raw": None,
    "mist_on": False, "servo_on": False,
    "last_reading": 0.0,
    "fpga_connected": False, "arduino_connected": False,
    "dry_since": None,
    "mist_started": None, "mist_runtime_today": 0.0,
    "intruder_events": [],      # list of epoch times (today)
    "routine_sent_today": 0,    # routine (non-critical) messages sent since midnight
    "last_routine": 0.0,
    "last_sent_reading": None,  # (moisture, temp, humidity) of last soil update sent
    "auto_mist_times": deque(maxlen=50),
    "history": deque(maxlen=HISTORY_HOURS * 60 * 12 // 5),  # ~1 reading per 5s
    "day": time.strftime("%Y-%m-%d"),
}
state_lock = threading.Lock()


def rollover_day_if_needed():
    today = time.strftime("%Y-%m-%d")
    with state_lock:
        if state["day"] != today:
            state["day"] = today
            state["intruder_events"] = []
            state["mist_runtime_today"] = 0.0
            state["routine_sent_today"] = 0


def log_reading(moisture, temp, humidity, wet, raw):
    try:
        new = not os.path.exists(LOG_FILE)
        with open(LOG_FILE, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["timestamp", "moisture_pct", "temp_c", "humidity_pct", "state", "raw"])
            w.writerow([now_str(), moisture, temp, humidity, "WET" if wet else "DRY", raw])
    except Exception as e:
        print(f"[log] write failed: {e}", file=sys.stderr)


def log_event(kind, detail=""):
    try:
        new = not os.path.exists(EVENT_FILE)
        with open(EVENT_FILE, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["timestamp", "event", "detail"])
            w.writerow([now_str(), kind, detail])
    except Exception as e:
        print(f"[log] event write failed: {e}", file=sys.stderr)


# --------------------------------------------------------------------------
# Serial port discovery
# --------------------------------------------------------------------------
def find_serial_port():
    import serial.tools.list_ports
    try:
        ports = serial.tools.list_ports.comports()
        for port, desc, hwid in ports:
            if "usbserial" in port:
                return port
        if glob.glob(SERIAL_PORT_HINT):
            return SERIAL_PORT_HINT
        candidates = sorted(glob.glob("/dev/cu.usbserial-*"))
        if candidates:
            return candidates[0]
    except Exception as e:
        print(f"[bridge] port enumeration failed: {e}", file=sys.stderr)
    return None


def find_arduino_port():
    """Find Arduino on a different port (not FPGA)"""
    try:
        import serial.tools.list_ports
        ports = serial.tools.list_ports.comports()
        fpga_port = find_serial_port()
        for port, desc, hwid in ports:
            if ("usbmodem" in port or "usbserial" in port) and port != fpga_port:
                print(f"[bridge] detected Arduino on {port} ({desc})")
                return port
    except Exception as e:
        print(f"[bridge] Arduino port search failed: {e}", file=sys.stderr)
    return None


# --------------------------------------------------------------------------
# Telegram send helpers
# --------------------------------------------------------------------------
def send_telegram_message(text, reply_markup=None):
    payload = {"chat_id": CHAT_ID, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    data = urllib.parse.urlencode(payload).encode()
    try:
        with urllib.request.urlopen(SEND_MESSAGE_API, data=data, timeout=10) as resp:
            resp.read()
        print(f"[telegram] sent: {text.splitlines()[0]!r}")
    except Exception as e:
        print(f"[telegram] send failed: {e}", file=sys.stderr)


def send_routine(text, force=False):
    """Send a non-critical message, respecting the farmer's daily budget.

    The budget spreads across the day: with /messages 24 a routine update can
    go out at most once an hour. `force` skips the spacing (state changes,
    auto-mist events) but never the daily cap. Critical alerts don't come
    through here — they always use send_telegram_message directly.
    Returns True if the message was sent."""
    rollover_day_if_needed()
    with settings_lock:
        limit = settings.get("daily_msgs", 30)
    now = time.time()
    with state_lock:
        sent = state["routine_sent_today"]
        last = state["last_routine"]
    if limit > 0:
        if sent >= limit:
            print(f"[budget] dropped (cap {limit}/day reached): {text.splitlines()[0]!r}")
            return False
        if not force and now - last < 86400 / limit:
            return False
    with state_lock:
        state["routine_sent_today"] += 1
        state["last_routine"] = now
    send_telegram_message(text)
    return True


def answer_callback(callback_id, text=""):
    data = urllib.parse.urlencode({"callback_query_id": callback_id, "text": text}).encode()
    try:
        with urllib.request.urlopen(ANSWER_CALLBACK_API, data=data, timeout=10) as resp:
            resp.read()
    except Exception as e:
        print(f"[telegram] answerCallback failed: {e}", file=sys.stderr)


def build_multipart(fields, file_field, filename, content_type, file_bytes):
    boundary = "----AgriNovaBoundary7MA4YWxkTrZu0gW"
    body = io.BytesIO()
    for name, value in fields.items():
        body.write(f"--{boundary}\r\n".encode())
        body.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.write(f"{value}\r\n".encode())
    body.write(f"--{boundary}\r\n".encode())
    body.write(f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode())
    body.write(f"Content-Type: {content_type}\r\n\r\n".encode())
    body.write(file_bytes)
    body.write(f"\r\n--{boundary}--\r\n".encode())
    return boundary, body.getvalue()


def send_telegram_photo(photo_bytes, caption="", filename="photo.jpg", content_type="image/jpeg"):
    fields = {"chat_id": CHAT_ID}
    if caption:
        fields["caption"] = caption
    boundary, body = build_multipart(fields, "photo", filename, content_type, photo_bytes)
    req = urllib.request.Request(SEND_PHOTO_API, data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        print("[telegram] photo sent")
    except Exception as e:
        print(f"[telegram] photo send failed: {e}", file=sys.stderr)


def send_telegram_video(video_bytes, caption="", duration_seconds=5):
    fields = {"chat_id": CHAT_ID, "duration": duration_seconds}
    if caption:
        fields["caption"] = caption
    boundary, body = build_multipart(fields, "video", "video.mp4", "video/mp4", video_bytes)
    req = urllib.request.Request(SEND_VIDEO_API, data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
        print(f"[telegram] video ({duration_seconds}s) sent")
    except Exception as e:
        print(f"[telegram] video send failed: {e}", file=sys.stderr)


def get_updates(offset=None, timeout=25):
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    url = f"{GET_UPDATES_API}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=timeout + 10) as resp:
        return json.loads(resp.read())


# --------------------------------------------------------------------------
# Camera relay (Windows VM long-polls GET /job, POSTs bytes to /result/<id>)
# --------------------------------------------------------------------------
camera_jobs = queue.Queue()
camera_results = {}
camera_results_lock = threading.Lock()
camera_client_seen = [0.0]


def camera_online():
    return time.time() - camera_client_seen[0] < CAMERA_OFFLINE_AFTER


def request_capture(kind, duration=None, timeout=60):
    job_id = str(int(time.time() * 1000))
    ev = threading.Event()
    with camera_results_lock:
        camera_results[job_id] = {"event": ev, "data": None}
    job = {"id": job_id, "job": kind}
    if duration:
        job["duration"] = duration
    camera_jobs.put(job)
    ok = ev.wait(timeout)
    with camera_results_lock:
        entry = camera_results.pop(job_id, {"data": None})
    if not ok:
        print(f"[camera] {kind} job timed out — is camera_service.py running on Windows?", file=sys.stderr)
    return entry["data"]


class CameraRelayHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/job":
            camera_client_seen[0] = time.time()
            try:
                job = camera_jobs.get(timeout=20)
            except queue.Empty:
                job = {"job": None}
            body = json.dumps(job).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path.startswith("/result/"):
            job_id = self.path.split("/result/", 1)[1]
            length = int(self.headers.get("Content-Length", 0))
            data = self.rfile.read(length) if length else b""
            if self.headers.get("X-Camera-Status") == "removed":
                log_event("CAMERA_REMOVED")
                threading.Thread(
                    target=send_telegram_message,
                    args=(msg("camera_removed", t=now_str()),),
                    daemon=True,
                ).start()
            with camera_results_lock:
                entry = camera_results.get(job_id)
            if entry:
                entry["data"] = data if data else None
                entry["event"].set()
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass


def camera_relay_server():
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", CAMERA_RELAY_PORT), CameraRelayHandler)
    print(f"[camera] relay listening on port {CAMERA_RELAY_PORT} (Windows VM connects to 192.168.64.1:{CAMERA_RELAY_PORT})")
    srv.serve_forever()


def camera_heartbeat():
    """Announce when the Windows camera service goes away / comes back."""
    was_online = None
    while True:
        time.sleep(10)
        online = camera_online()
        if was_online is None:
            if online:
                was_online = True
            elif time.time() - START_TIME > CAMERA_OFFLINE_AFTER:
                was_online = False
                send_telegram_message("📷 Camera service offline — start camera_service.py on Windows.")
            continue
        if online != was_online:
            was_online = online
            if online:
                send_telegram_message("📷 Camera service back online.")
                log_event("CAMERA_ONLINE")
            else:
                send_telegram_message("📷 Camera service offline (no poll from Windows for 60 s).")
                log_event("CAMERA_OFFLINE")


# --------------------------------------------------------------------------
# Arduino link
# --------------------------------------------------------------------------
arduino_ser = None
arduino_lock = threading.Lock()


def arduino_send(cmd):
    with arduino_lock:
        ser = arduino_ser
        if ser is None:
            return False
        try:
            ser.write((cmd + "\n").encode())
            print(f"[arduino] sent: {cmd}")
            return True
        except Exception as e:
            print(f"[arduino] send failed: {e}", file=sys.stderr)
            return False


def set_mist_state(on):
    """Track mist ON/OFF locally for status + runtime accounting."""
    with state_lock:
        if on and not state["mist_on"]:
            state["mist_started"] = time.time()
        elif not on and state["mist_on"] and state["mist_started"]:
            state["mist_runtime_today"] += time.time() - state["mist_started"]
            state["mist_started"] = None
        state["mist_on"] = on


mist_burst_busy = threading.Lock()


AUTO_MIST_STOP_PCT = 30            # auto-mist stops once soil reads this % or less
AUTO_MIST_MAX_SECONDS = 600        # hard safety cap (10 min) so a dead sensor cannot run the mist forever


def mist_burst(seconds, reason="", stop_when_wet=False):
    """Run the mist for up to `seconds`. With stop_when_wet, cut it the moment
    the soil reads WET (checked every 0.2 s)."""
    if not mist_burst_busy.acquire(blocking=False):
        return
    try:
        if arduino_send("MIST_ON"):
            set_mist_state(True)
            log_event("MIST_BURST", f"{seconds}s {reason}".strip())
            t0 = time.time()
            stopped_wet = False
            while time.time() - t0 < seconds:
                time.sleep(0.2)
                if stop_when_wet and state["moisture"] is not None and state["moisture"] <= AUTO_MIST_STOP_PCT:
                    stopped_wet = True
                    break
            arduino_send("MIST_OFF")
            set_mist_state(False)
            if stopped_wet:
                send_routine(msg("mist_stopped_wet", m=state["moisture"], tgt=AUTO_MIST_STOP_PCT,
                                 s=f"{time.time() - t0:.1f}"), force=True)
                log_event("MIST_STOPPED_WET", f"{time.time() - t0:.1f}s")
    finally:
        mist_burst_busy.release()


def soil_percent_from_raw(raw, fallback):
    """Use user calibration if both air/water values exist, else Arduino's value."""
    with settings_lock:
        air, water = settings["soil_raw_air"], settings["soil_raw_water"]
    if raw is None or air is None or water is None or air == water:
        return fallback
    pct = (air - raw) * 100.0 / (air - water)
    return int(max(0, min(100, round(pct))))


def calibrated():
    with settings_lock:
        return settings["soil_raw_air"] is not None and settings["soil_raw_water"] is not None


def listen_arduino(port):
    global arduino_ser
    ser = None
    buf = b""
    last_soil_alert = 0.0
    last_soil_state = None
    last_climate_alert = 0.0
    announced_missing = False
    while True:
        if ser is None:
            if port is None:
                port = find_arduino_port()
            if port is None:
                if not announced_missing and time.time() - START_TIME > 30:
                    announced_missing = True
                    send_telegram_message("⚠️ Arduino not found — check USB.")
                time.sleep(5)
                continue
            try:
                ser = serial.Serial(port, BAUD_RATE, timeout=1)
                print(f"[arduino] listening on {port}")
                buf = b""
                with arduino_lock:
                    arduino_ser = ser
                with state_lock:
                    state["arduino_connected"] = True
                if announced_missing:
                    send_telegram_message("✅ Arduino reconnected.")
                    log_event("ARDUINO_RECONNECT")
                announced_missing = False
            except serial.SerialException as e:
                print(f"[arduino] could not open {port}: {e} — retrying in 5s", file=sys.stderr)
                port = None
                time.sleep(5)
                continue
        try:
            chunk = ser.read(64)
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode(errors="replace").strip()
                if not text:
                    continue
                print(f"[arduino] {text}")

                if text == "MIST:ON":
                    set_mist_state(True)
                elif text == "MIST:OFF":
                    set_mist_state(False)
                elif text.startswith("RAW:"):
                    try:
                        with state_lock:
                            state["raw"] = int(text[4:])
                    except ValueError:
                        pass
                elif text.startswith("DATA:"):
                    parts = text[5:].split(",")
                    if len(parts) < 4:
                        continue
                    try:
                        ard_moist = int(parts[0])
                        temp = float(parts[1])
                        humidity = float(parts[2])
                    except ValueError:
                        continue
                    raw = None
                    if len(parts) >= 5:
                        try:
                            raw = int(parts[4])
                        except ValueError:
                            raw = None
                    moisture = soil_percent_from_raw(raw, ard_moist)
                    wet = moisture >= 40 if (raw is not None and calibrated()) else parts[3] == "WET"
                    soil_state = "WET" if wet else "DRY"
                    now = time.time()

                    rollover_day_if_needed()
                    with state_lock:
                        state.update({"moisture": moisture, "temp": temp, "humidity": humidity,
                                      "wet": wet, "raw": raw, "last_reading": now})
                        if len(parts) >= 7:
                            state["servo_on"] = parts[6] == "SERVO_ON"
                        if wet:
                            state["dry_since"] = None
                        elif state["dry_since"] is None:
                            state["dry_since"] = now
                        state["history"].append((now, moisture, temp, humidity))
                    if len(parts) >= 6:
                        set_mist_state(parts[5] == "MIST_ON")
                    log_reading(moisture, temp, humidity, wet, raw)

                    # Soil message: only when something meaningfully changed,
                    # rationed by the farmer's /messages budget.
                    state_changed = last_soil_state is not None and soil_state != last_soil_state
                    with state_lock:
                        prev = state["last_sent_reading"]
                    meaningful = (prev is None or state_changed
                                  or abs(moisture - prev[0]) >= 3
                                  or abs(temp - prev[1]) >= 1.5
                                  or abs(humidity - prev[2]) >= 8)
                    if meaningful and settings["alerts"]:
                        prefix = msg("soil_changed") if state_changed else ""
                        if send_routine(msg("soil_line", p=prefix, m=moisture, st=soil_state,
                                            t=f"{temp:.1f}", h=f"{humidity:.0f}"),
                                        force=state_changed):
                            with state_lock:
                                state["last_sent_reading"] = (moisture, temp, humidity)
                    last_soil_state = soil_state

                    # Heat / dry-air warnings
                    if settings["alerts"] and now - last_climate_alert >= CLIMATE_ALERT_INTERVAL:
                        warn = []
                        if temp >= TEMP_HIGH_C:
                            warn.append(msg("heat_warn", t=f"{temp:.1f}"))
                        if 0 < humidity <= HUMIDITY_LOW_PCT:
                            warn.append(msg("dry_air_warn", h=f"{humidity:.0f}"))
                        if warn and send_routine(msg("climate_header") + "\n" + "\n".join(warn), force=True):
                            log_event("CLIMATE_WARNING", "; ".join(warn))
                            last_climate_alert = now
        except Exception as e:
            print(f"[arduino] connection lost: {e} — will reconnect", file=sys.stderr)
            with arduino_lock:
                arduino_ser = None
            with state_lock:
                state["arduino_connected"] = False
            try:
                ser.close()
            except Exception:
                pass
            ser = None
            port = None
            send_telegram_message("⚠️ Arduino disconnected — will keep retrying.")
            log_event("ARDUINO_DISCONNECT", str(e))
            announced_missing = True
            time.sleep(5)


# --------------------------------------------------------------------------
# Weather via wttr.in (no API key)
# --------------------------------------------------------------------------
weather_cache = {"time": 0.0, "data": None, "location": None}
weather_lock = threading.Lock()


def fetch_weather(force=False):
    """Return wttr.in j1 JSON (cached 15 min) or None."""
    with settings_lock:
        loc = settings["location"]
    with weather_lock:
        if (not force and weather_cache["data"] and weather_cache["location"] == loc
                and time.time() - weather_cache["time"] < WEATHER_CACHE_SECONDS):
            return weather_cache["data"]
    url = f"https://wttr.in/{urllib.parse.quote(loc)}?format=j1"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0 AgriNova"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        with weather_lock:
            weather_cache.update({"time": time.time(), "data": data, "location": loc})
        return data
    except Exception as e:
        print(f"[weather] fetch failed: {e}", file=sys.stderr)
        return None


def rain_outlook(data):
    """(max % chance of rain over the next RAIN_SKIP_HOURS, total mm) from wttr.in j1."""
    try:
        hour_now = time.localtime().tm_hour
        chances, mm = [], 0.0
        for day_idx, day in enumerate(data["weather"][:2]):
            for h in day["hourly"]:
                t = int(h["time"]) // 100 + 24 * day_idx
                if hour_now <= t < hour_now + RAIN_SKIP_HOURS:
                    chances.append(int(h.get("chanceofrain", 0)))
                    mm += float(h.get("precipMM", 0))
        return (max(chances) if chances else 0), mm
    except Exception:
        return 0, 0.0


def rain_expected():
    """True if a spray should be skipped because rain is likely soon."""
    if not settings["rain_skip"]:
        return False, ""
    data = fetch_weather()
    if not data:
        return False, ""
    chance, mm = rain_outlook(data)
    if chance >= RAIN_SKIP_CHANCE:
        return True, f"{chance}% chance of rain in the next {RAIN_SKIP_HOURS} h ({mm:.1f} mm)"
    return False, ""


def build_weather():
    data = fetch_weather()
    if not data:
        return "🌦 Weather unavailable (wttr.in not reachable)."
    try:
        c = data["current_condition"][0]
        area = data["nearest_area"][0]
        place = f"{area['areaName'][0]['value']}, {area['region'][0]['value']}"
        chance, mm = rain_outlook(data)
        lines = [f"🌦 Weather — {place}",
                 f"{c['weatherDesc'][0]['value']}, {c['temp_C']}°C (feels {c['FeelsLikeC']}°C)",
                 f"💧 Humidity {c['humidity']}%  🌬 Wind {c['windspeedKmph']} km/h  ☔ Precip {c['precipMM']} mm",
                 f"🌧 Rain next {RAIN_SKIP_HOURS} h: {chance}% ({mm:.1f} mm)"
                 + ("  → sprays will be SKIPPED" if settings["rain_skip"] and chance >= RAIN_SKIP_CHANCE else ""),
                 ""]
        for day in data["weather"][:3]:
            rain = max(int(h.get("chanceofrain", 0)) for h in day["hourly"])
            lines.append(f"📅 {day['date']}: {day['mintempC']}–{day['maxtempC']}°C, rain {rain}%, {day['totalSnow_cm']}cm snow"
                         .replace(", 0.0cm snow", ""))
        with settings_lock:
            loc = settings["location"]
        if not loc:
            lines.append("\nℹ️ Location is auto-guessed. Set it: /weather set Hyderabad")
        return "\n".join(lines)
    except Exception as e:
        return f"🌦 Could not parse weather: {e}"


# --------------------------------------------------------------------------
# Automation: auto-irrigation + schedules + daily summary
# --------------------------------------------------------------------------
def auto_mist_loop():
    while True:
        time.sleep(5)
        if not settings["auto_mist"]:
            continue
        with state_lock:
            moisture = state["moisture"]
            mist_on = state["mist_on"]
            recent = [t for t in state["auto_mist_times"] if time.time() - t < 3600]
            fresh = time.time() - state["last_reading"] < 60
        if not fresh or mist_on or moisture is None:
            continue
        if moisture <= AUTO_MIST_STOP_PCT:
            continue  # already at/below target — nothing to do
        if len(recent) >= AUTO_MIST_MAX_PER_HOUR:
            continue
        if mist_burst_busy.locked():
            continue
        skip, why = rain_expected()
        with state_lock:
            state["auto_mist_times"].append(time.time())
            state["dry_since"] = time.time()  # restart the dry timer after a spray
        if skip:
            send_routine(msg("rain_skipped", why=why), force=True)
            log_event("AUTO_MIST_SKIPPED_RAIN", why)
            continue
        send_routine(msg("auto_mist_start", m=moisture, tgt=AUTO_MIST_STOP_PCT,
                         n=len(recent) + 1, max=AUTO_MIST_MAX_PER_HOUR), force=True)
        threading.Thread(target=mist_burst, args=(AUTO_MIST_MAX_SECONDS, "auto", True), daemon=True).start()


def scheduler_loop():
    fired = set()  # (date, time) already run
    while True:
        time.sleep(20)
        today = time.strftime("%Y-%m-%d")
        hhmm = time.strftime("%H:%M")
        with settings_lock:
            schedules = list(settings["schedules"])
        for s in schedules:
            key = (today, s["time"])
            if s["time"] == hhmm and key not in fired:
                fired.add(key)
                skip, why = rain_expected()
                if skip:
                    send_telegram_message(f"🌧 Scheduled spray {s['time']} skipped — {why}.")
                    log_event("SCHEDULE_SKIPPED_RAIN", why)
                    continue
                send_telegram_message(f"⏰ Scheduled spray {s['time']} → {s['secs']}s")
                threading.Thread(target=mist_burst, args=(s["secs"], "scheduled"), daemon=True).start()
        if DAILY_SUMMARY_TIME == hhmm and (today, "summary") not in fired:
            fired.add((today, "summary"))
            send_telegram_message(build_summary())
        if len(fired) > 500:
            fired = {k for k in fired if k[0] == today}


def build_summary():
    rollover_day_if_needed()
    with state_lock:
        hist = [h for h in state["history"] if time.time() - h[0] < 24 * 3600]
        intr = list(state["intruder_events"])
        mist_rt = state["mist_runtime_today"]
        if state["mist_on"] and state["mist_started"]:
            mist_rt += time.time() - state["mist_started"]
    lines = [f"📋 AgriNova daily summary — {time.strftime('%Y-%m-%d')}"]
    if hist:
        ms = [h[1] for h in hist]; ts = [h[2] for h in hist]; hs = [h[3] for h in hist]
        lines.append(f"🌱 Soil: min {min(ms)}% / avg {sum(ms)/len(ms):.0f}% / max {max(ms)}%")
        lines.append(f"🌡️ Temp: min {min(ts):.1f} / avg {sum(ts)/len(ts):.1f} / max {max(ts):.1f} °C")
        lines.append(f"💧 Humidity: min {min(hs):.0f} / avg {sum(hs)/len(hs):.0f} / max {max(hs):.0f} %")
        lines.append(f"📈 {len(hist)} readings in last 24 h")
    else:
        lines.append("No sensor readings yet.")
    lines.append(f"🚨 Intruder events today: {len(intr)}")
    lines.append(f"💨 Mist runtime today: {fmt_duration(mist_rt)}")
    lines.append(f"🔒 Lockdown: {'ON' if settings['lockdown'] else 'OFF'} | 🤖 Auto-mist: {'ON' if settings['auto_mist'] else 'OFF'}")
    data = fetch_weather()
    if data:
        chance, mm = rain_outlook(data)
        lines.append(f"🌦 Outside: {data['current_condition'][0]['temp_C']}°C, rain next {RAIN_SKIP_HOURS} h {chance}%")
    return "\n".join(lines)


def build_status():
    rollover_day_if_needed()
    with state_lock:
        s = dict(state)
        intr = len(state["intruder_events"])
    age = time.time() - s["last_reading"] if s["last_reading"] else None
    reading_age = f"{fmt_duration(age)} ago" if age is not None else "never"

    def yn(b):
        return "✅" if b else "❌"

    lines = [f"📡 AgriNova status — {time.strftime('%H:%M:%S')}",
             f"⏱ Uptime: {fmt_duration(time.time() - START_TIME)}",
             "",
             f"{yn(s['fpga_connected'])} FPGA   {yn(s['arduino_connected'])} Arduino   {yn(camera_online())} Camera",
             ""]
    if s["moisture"] is not None:
        lines.append(f"🌱 Soil: {s['moisture']}% ({'WET' if s['wet'] else 'DRY'})"
                     + (f"  raw {s['raw']}" if s["raw"] is not None else ""))
        lines.append(f"🌡️ Temp: {s['temp']:.1f}°C   💧 Humidity: {s['humidity']:.0f}%")
        lines.append(f"📥 Last reading: {reading_age}")
    else:
        lines.append("🌱 No sensor data yet.")
    lines += ["",
              f"💨 Mist: {'ON' if s['mist_on'] else 'OFF'}   ⚙️ Servo: {'ON' if s['servo_on'] else 'OFF'}",
              f"🔒 Lockdown: {'ON' if settings['lockdown'] else 'OFF'}   🔔 Alerts: {'ON' if settings['alerts'] else 'MUTED'}",
              f"🤖 Auto-mist: {'ON' if settings['auto_mist'] else 'OFF'}   ⏰ Schedules: {len(settings['schedules'])}",
              f"🚨 Intruders today: {intr}",
              f"🌧 Rain-skip: {'ON' if settings['rain_skip'] else 'OFF'}"
              + f"   ✉️ {s['routine_sent_today']}/{settings.get('daily_msgs') or '∞'} msgs today"]
    if not calibrated():
        lines.append("ℹ️ Soil not calibrated — /calibrate air then /calibrate water")
    return "\n".join(lines)


def build_graph_png():
    with state_lock:
        hist = list(state["history"])
    if len(hist) < 2:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from datetime import datetime
    except ImportError:
        return None
    xs = [datetime.fromtimestamp(h[0]) for h in hist]
    fig, ax1 = plt.subplots(figsize=(9, 4.5), dpi=120)
    ax1.plot(xs, [h[1] for h in hist], color="tab:green", label="Soil %")
    ax1.plot(xs, [h[3] for h in hist], color="tab:blue", label="Humidity %")
    ax1.set_ylim(0, 100)
    ax1.set_ylabel("%")
    ax2 = ax1.twinx()
    ax2.plot(xs, [h[2] for h in hist], color="tab:red", label="Temp °C")
    ax2.set_ylabel("°C")
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    fig.autofmt_xdate()
    h1, l1 = ax1.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax1.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=8)
    ax1.set_title(f"AgriNova — last {HISTORY_HOURS} h ({len(hist)} readings)")
    ax1.grid(alpha=0.3)
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def readings_caption():
    with state_lock:
        s = dict(state)
    if s["moisture"] is None:
        return f"AgriNova — {now_str()} (no sensor data)"
    return (f"AgriNova — {now_str()}\n🌱 Soil {s['moisture']}% ({'WET' if s['wet'] else 'DRY'}) | "
            f"🌡️ {s['temp']:.1f}°C | 💧 {s['humidity']:.0f}% | 💨 Mist {'ON' if s['mist_on'] else 'OFF'}")


# --------------------------------------------------------------------------
# Intruder response: photo fast, then video, plus mist burst
# --------------------------------------------------------------------------
intruder_video_busy = threading.Lock()


def intruder_response():
    if not intruder_video_busy.acquire(blocking=False):
        return
    try:
        photo = request_capture("photo", timeout=30)
        if photo:
            send_telegram_photo(photo, caption=f"📸 Intruder snapshot — {now_str()}")
        video_bytes = request_capture("video", duration=INTRUDER_VIDEO_SECONDS,
                                      timeout=INTRUDER_VIDEO_SECONDS + 60)
        if video_bytes:
            send_telegram_video(video_bytes, caption=f"🎥 Intruder clip — {now_str()}",
                                duration_seconds=INTRUDER_VIDEO_SECONDS)
        elif not photo:
            send_telegram_message("Could not capture intruder photo/video. Is the Windows camera service running?")
    finally:
        intruder_video_busy.release()


def handle_ir_event():
    rollover_day_if_needed()
    with state_lock:
        state["intruder_events"].append(time.time())
    log_event("INTRUDER")
    if not settings["alerts"] or not settings["lockdown"]:
        return
    send_telegram_message(msg("intruder", t=now_str()))
    if not intruder_video_busy.locked():
        threading.Thread(target=intruder_response, daemon=True).start()
    threading.Thread(target=mist_burst, args=(MIST_BURST_SECONDS, "intruder"), daemon=True).start()


# --------------------------------------------------------------------------
# Telegram command dispatch
# --------------------------------------------------------------------------
MENU_KEYBOARD = {"inline_keyboard": [
    [{"text": "📡 Status", "callback_data": "/status"},
     {"text": "📸 Snapshot", "callback_data": "/snapshot"},
     {"text": "📈 Graph", "callback_data": "/graph"}],
    [{"text": "🔒 Lockdown", "callback_data": "/lockdown"},
     {"text": "🔓 Unlock", "callback_data": "/unlock"}],
    [{"text": "💨 Mist ON", "callback_data": "/mist_on"},
     {"text": "💨 Mist OFF", "callback_data": "/mist_off"},
     {"text": "⏱ Spray 10s", "callback_data": "/spray"}],
    [{"text": "📷 Photo", "callback_data": "/photo"},
     {"text": "🎥 Video 5s", "callback_data": "/video"}],
    [{"text": "🌐 Language", "callback_data": "/lang"}],
    [{"text": "🤖 Auto-mist ON", "callback_data": "/auto_mist on"},
     {"text": "🤖 Auto-mist OFF", "callback_data": "/auto_mist off"},
     {"text": "🌦 Weather", "callback_data": "/weather"}],
]}

ALIASES = {
    "/mist on": "/mist_on", "/miston": "/mist_on",
    "/mist off": "/mist_off", "/mistoff": "/mist_off",
    "/rotate stop": "/rotate_stop", "/rotatestop": "/rotate_stop",
    "/lock down": "/lockdown", "/un lock": "/unlock",
    "/auto mist on": "/auto_mist on", "/auto mist off": "/auto_mist off",
    "/automist on": "/auto_mist on", "/automist off": "/auto_mist off",
    "/soil raw": "/soil_raw", "/soilraw": "/soil_raw",
    "/rain skip on": "/rain_skip on", "/rain skip off": "/rain_skip off",
    "/rainskip on": "/rain_skip on", "/rainskip off": "/rain_skip off",
    "/forecast": "/weather",
    "/start": "/setup_or_help", "/keyboard": "/menu", "/buttons": "/menu",
    "/wizard": "/setup", "/settings": "/setup",
    "/language": "/lang", "/telugu": "/lang te", "/english": "/lang en",
    "/hindi": "/lang hi",
    "/msgs": "/messages", "/message": "/messages",
    "/continue": "/unmute", "/silence": "/mute", "/shutdown": "/stop",
    "/stop yes": "/stop", "/stop confirm": "/stop",
}

shutdown_state = {"count": 0, "last": 0.0}
shutdown_lock = threading.Lock()


def handle_stop(text):
    """Three-step confirmation before the bridge exits."""
    with shutdown_lock:
        now = time.time()
        if now - shutdown_state["last"] > SHUTDOWN_CONFIRM_WINDOW:
            shutdown_state["count"] = 0
        if text == "/stop cancel":
            shutdown_state["count"] = 0
            send_telegram_message("✅ Shutdown cancelled. Bridge keeps running.")
            return
        shutdown_state["count"] += 1
        shutdown_state["last"] = now
        n = shutdown_state["count"]
    kb = {"inline_keyboard": [[{"text": "🛑 YES, continue shutdown", "callback_data": "/stop"},
                               {"text": "✅ Cancel", "callback_data": "/stop cancel"}]]}
    if n == 1:
        send_telegram_message(
            "⚠️ This will STOP the AgriNova bridge.\n"
            "No more intruder alerts, soil updates, mist control or camera until you restart it on the Mac.\n\n"
            "Are you sure? (1/3) — send /stop again within 60 s, or /stop cancel.", reply_markup=kb)
    elif n == 2:
        send_telegram_message(
            "⚠️⚠️ Are you REALLY sure? (2/3)\n"
            "Lockdown, auto-mist and schedules will all go dark.\n"
            "Send /stop one more time to shut down, or /stop cancel.", reply_markup=kb)
    else:
        send_telegram_message("🛑 Final confirmation received (3/3). Shutting the bridge down now.\n"
                              "Restart it on the Mac with `agri`.")
        log_event("BRIDGE_STOP", "telegram /stop x3")
        try:
            arduino_send("MIST_OFF")
            arduino_send("STOP")
        except Exception:
            pass
        print("[bridge] shutdown requested via Telegram (3x /stop)")
        time.sleep(1)
        os._exit(0)


def normalise(text):
    text = " ".join(text.strip().lower().split())
    if text and "@" in text.split(" ")[0]:  # strip /cmd@botname
        head, *rest = text.split(" ")
        text = " ".join([head.split("@")[0]] + rest)
    return ALIASES.get(text, text)


# ---------------------------------------------------------------------------
# /setup wizard: one question at a time, answered with buttons.
# ---------------------------------------------------------------------------
SETUP_STEPS = [
    {"key": "lang",
     "q": "1/5 🌐 Which language for alerts?\nभाषा चुनें / భాషను ఎంచుకోండి:",
     "opts": [("English", "en"), ("हिंदी", "hi"), ("తెలుగు", "te"), ("Telugu (English)", "te_en")]},
    {"key": "lockdown",
     "q": "2/5 🔒 Arm intruder alerts now?\nWhen armed: IR trigger → alert + photo + video + mist burst.",
     "opts": [("🔒 Arm now", "on"), ("🔓 Keep off (arm later with /lockdown)", "off")]},
    {"key": "auto_mist",
     "q": "3/5 🤖 Water automatically when the soil is dry?\nMist runs until the soil reads wet again (max 4 starts/hour).",
     "opts": [("💧 Yes, auto-water", "on"), ("✋ No, I'll use /spray myself", "off")]},
    {"key": "rain_skip",
     "q": "4/5 🌧 Skip watering when rain is coming?\nChecks the forecast before every spray.",
     "opts": [("🌧 Yes, save water", "on"), ("🚿 No, always spray", "off")]},
    {"key": "daily_msgs",
     "q": "5/5 ✉️ How many routine updates per day?\n(Alarms — intruders, camera removed — are ALWAYS sent.)",
     "opts": [("10/day", "10"), ("30/day", "30"), ("60/day", "60"), ("Unlimited", "0")]},
]
setup_idx = [None]  # None = wizard not running


def setup_send_step():
    i = setup_idx[0]
    if i is None:
        return
    if i >= len(SETUP_STEPS):
        setup_idx[0] = None
        with settings_lock:
            st = dict(settings)
        lang_names = {"en": "English", "hi": "हिंदी", "te": "తెలుగు", "te_en": "Telugu (English)"}
        send_telegram_message(
            "✅ Setup complete!\n"
            f"🌐 Language: {lang_names.get(st['lang'], st['lang'])}\n"
            f"🔒 Lockdown: {'ON' if st['lockdown'] else 'OFF'}\n"
            f"🤖 Auto-mist: {'ON' if st['auto_mist'] else 'OFF'}\n"
            f"🌧 Rain-skip: {'ON' if st['rain_skip'] else 'OFF'}\n"
            f"✉️ Messages: {st['daily_msgs'] or 'unlimited'}/day\n"
            f"📍 Weather location: {st['location'] or 'not set — send /weather set <your city>'}\n\n"
            "Change any answer any time — /setup runs again, or use the single commands. /help lists them.")
        return
    step = SETUP_STEPS[i]
    rows, row = [], []
    for label, val in step["opts"]:
        row.append({"text": label, "callback_data": f"/setup pick {val}"})
        if len(row) == 2 or len(label) > 22:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([{"text": "⏭ Skip", "callback_data": "/setup pick skip"},
                 {"text": "✖ Cancel setup", "callback_data": "/setup cancel"}])
    send_telegram_message(step["q"], reply_markup={"inline_keyboard": rows})


def setup_apply(val):
    step = SETUP_STEPS[setup_idx[0]]
    key = step["key"]
    if val != "skip":
        with settings_lock:
            if key == "lang":
                settings["lang"] = val
            elif key == "daily_msgs":
                settings["daily_msgs"] = int(val)
            else:
                settings[key] = (val == "on")
        save_settings()
    setup_idx[0] += 1
    setup_send_step()


def handle_command(text):
    """Run one normalised command. Safe to call from the poll loop or a button."""
    parts = text.split()
    cmd = parts[0] if parts else ""
    arg = parts[1] if len(parts) > 1 else None

    if cmd == "/setup_or_help":
        if not os.path.exists(STATE_FILE):
            handle_command("/setup")
        else:
            handle_command("/help")
        return
    if cmd == "/help":
        send_telegram_message(HELP_TEXT)
    elif cmd == "/menu":
        send_telegram_message("AgriNova control panel:", reply_markup=MENU_KEYBOARD)
    elif cmd == "/about":
        send_telegram_message(ABOUT_TEXT)
    elif cmd == "/status":
        send_telegram_message(build_status())
    elif cmd == "/summary":
        send_telegram_message(build_summary())
    elif cmd == "/intruders":
        rollover_day_if_needed()
        with state_lock:
            ev = list(state["intruder_events"])
        if not ev:
            send_telegram_message("🚨 No intruder events today.")
        else:
            lines = [f"🚨 Intruder events today: {len(ev)}"]
            lines += [f"• {time.strftime('%H:%M:%S', time.localtime(t))}" for t in ev[-20:]]
            send_telegram_message("\n".join(lines))
    elif cmd == "/graph":
        png = build_graph_png()
        if png:
            send_telegram_photo(png, caption=f"📈 Last {HISTORY_HOURS} h — {now_str()}",
                                filename="graph.png", content_type="image/png")
        else:
            send_telegram_message("Not enough readings for a graph yet (need a couple of minutes of data).")
    elif cmd == "/soil_raw":
        with state_lock:
            raw = state["raw"]
        arduino_send("RAW?")
        if raw is None:
            send_telegram_message("No raw value yet — is the Arduino running the latest sketch?")
        else:
            with settings_lock:
                air, water = settings["soil_raw_air"], settings["soil_raw_water"]
            send_telegram_message(f"🌱 Soil raw ADC: {raw}\nCalibration: air={air} water={water}\n"
                                  "Hold probe in air → /calibrate air, then in water → /calibrate water")
    elif cmd == "/calibrate":
        with state_lock:
            raw = state["raw"]
        if arg in ("reset", "clear"):
            with settings_lock:
                settings["soil_raw_air"] = settings["soil_raw_water"] = None
            save_settings()
            send_telegram_message("Calibration cleared — using Arduino defaults.")
        elif raw is None:
            send_telegram_message("No raw value yet — can't calibrate.")
        elif arg in ("air", "dry"):
            with settings_lock:
                settings["soil_raw_air"] = raw
            save_settings()
            send_telegram_message(f"✅ Air (dry) calibration = {raw}. Now put the probe in water and send /calibrate water.")
        elif arg in ("water", "wet"):
            with settings_lock:
                air = settings["soil_raw_air"]
            if air is not None and abs(air - raw) < 50:
                send_telegram_message(
                    f"❌ Water reading ({raw}) is the same as the air reading ({air}). "
                    "The sensor is not responding to water — check the fork-to-board wires / VCC on 5V, or replace the module. "
                    "Calibration NOT saved.")
                return
            with settings_lock:
                settings["soil_raw_water"] = raw
            save_settings()
            send_telegram_message(f"✅ Water (wet) calibration = {raw}. Soil % now uses your calibration.")
        else:
            send_telegram_message("Usage: /calibrate air | water | reset")
    elif cmd == "/setup":
        if arg == "pick" and setup_idx[0] is not None and len(parts) > 2:
            setup_apply(parts[2])
        elif arg == "cancel":
            setup_idx[0] = None
            send_telegram_message("Setup cancelled — nothing else changed. Run /setup anytime.")
        else:
            setup_idx[0] = 0
            send_telegram_message("🌿 Welcome to AgriNova setup! 5 quick questions — tap an answer, "
                                  "Skip keeps the current value.")
            setup_send_step()
    elif cmd == "/messages":
        with settings_lock:
            limit = settings.get("daily_msgs", 30)
        with state_lock:
            used = state["routine_sent_today"]
        if arg is None:
            kb = {"inline_keyboard": [[
                {"text": "10/day", "callback_data": "/messages 10"},
                {"text": "30/day", "callback_data": "/messages 30"},
                {"text": "60/day", "callback_data": "/messages 60"},
                {"text": "Unlimited", "callback_data": "/messages unlimited"},
            ]]}
            send_telegram_message(
                f"✉️ Routine messages: {used}/{limit if limit else '∞'} used today.\n"
                "How many updates do you want per day? (Alarms like intruders and camera "
                "removal are ALWAYS sent and don't count.)", reply_markup=kb)
        else:
            if arg in ("unlimited", "0", "off"):
                n = 0
            else:
                try:
                    n = max(1, min(500, int(arg)))
                except ValueError:
                    send_telegram_message("Usage: /messages [number] — e.g. /messages 20, or /messages unlimited")
                    return
            with settings_lock:
                settings["daily_msgs"] = n
            save_settings()
            if n:
                send_telegram_message(f"✉️ Got it — at most {n} routine messages per day "
                                      f"(about one every {fmt_duration(86400 / n)}). Alarms always come through.")
            else:
                send_telegram_message("✉️ Unlimited routine messages.")
    elif cmd == "/lang":
        if arg in ("en", "hi", "te", "te_en"):
            with settings_lock:
                settings["lang"] = arg
            save_settings()
            send_telegram_message(MSGS["lang_set"][arg])
        else:
            kb = {"inline_keyboard": [[
                {"text": "English", "callback_data": "/lang en"},
                {"text": "हिंदी", "callback_data": "/lang hi"},
            ], [
                {"text": "తెలుగు", "callback_data": "/lang te"},
                {"text": "Telugu (English)", "callback_data": "/lang te_en"},
            ]]}
            send_telegram_message("🌐 Choose language / भाषा चुनें / భాషను ఎంచుకోండి:", reply_markup=kb)
    elif cmd == "/weather":
        if arg == "set":
            loc = " ".join(parts[2:])
            with settings_lock:
                settings["location"] = loc
            save_settings()
            send_telegram_message(f"📍 Location set to {loc or 'auto (geo-IP)'}. Fetching...")
            send_telegram_message(build_weather())
        else:
            send_telegram_message(build_weather())
    elif cmd == "/rain_skip":
        if arg in ("on", "off"):
            settings["rain_skip"] = arg == "on"
            save_settings()
            send_telegram_message(f"🌧 Rain-skip {'ON — sprays are skipped when rain chance ≥ ' + str(RAIN_SKIP_CHANCE) + '%' if arg == 'on' else 'OFF'}.")
        else:
            send_telegram_message(f"🌧 Rain-skip is {'ON' if settings['rain_skip'] else 'OFF'}. Usage: /rain_skip on|off")
    elif cmd == "/lockdown":
        if settings["lockdown"]:
            send_telegram_message("🔒 Already locked down.")
        else:
            settings["lockdown"] = True
            save_settings()
            log_event("LOCKDOWN_ON")
            send_telegram_message(msg("lockdown_on"))
    elif cmd == "/unlock":
        if not settings["lockdown"]:
            send_telegram_message("🔓 Already unlocked.")
        else:
            settings["lockdown"] = False
            save_settings()
            log_event("LOCKDOWN_OFF")
            send_telegram_message(msg("lockdown_off"))
    elif cmd == "/stop":
        handle_stop(text)
    elif cmd == "/mute":
        if not settings["alerts"]:
            send_telegram_message("Alerts are already muted.")
        else:
            settings["alerts"] = False
            save_settings()
            send_telegram_message(STOP_TEXT)
    elif cmd == "/unmute":
        if settings["alerts"]:
            send_telegram_message("Alerts are already on.")
        else:
            settings["alerts"] = True
            save_settings()
            send_telegram_message(CONTINUE_TEXT)
    elif cmd in ("/photo", "/snapshot"):
        if not camera_online():
            send_telegram_message("📷 Camera service is offline — start camera_service.py on Windows.")
            return
        send_telegram_message("Capturing photo from Windows...")
        photo_bytes = request_capture("photo", timeout=45)
        if photo_bytes:
            cap = readings_caption() if cmd == "/snapshot" else f"AgriNova snapshot — {now_str()}"
            send_telegram_photo(photo_bytes, caption=cap)
        else:
            send_telegram_message("Could not capture photo. Is Windows camera service running?")
    elif cmd == "/video":
        try:
            duration = int(arg) if arg else 5
        except ValueError:
            send_telegram_message("Usage: /video [duration]\nExample: /video 5 (default 5, max 30)")
            return
        duration = max(1, min(30, duration))
        if not camera_online():
            send_telegram_message("📷 Camera service is offline — start camera_service.py on Windows.")
            return
        send_telegram_message(f"Recording {duration}s video from Windows...")
        video_bytes = request_capture("video", duration=duration, timeout=duration + 60)
        if video_bytes:
            send_telegram_video(video_bytes, caption=f"AgriNova video ({duration}s) — {now_str()}", duration_seconds=duration)
        else:
            send_telegram_message("Could not capture video. Is Windows camera service running?")
    elif cmd == "/rotate":
        if state["servo_on"]:
            send_telegram_message("⚙️ Servo is already ON.")
        elif arduino_send("ROTATE"):
            with state_lock:
                state["servo_on"] = True
            send_telegram_message(ROTATE_TEXT)
        else:
            send_telegram_message(ARDUINO_MISSING_TEXT)
    elif cmd == "/rotate_stop":
        if not state["servo_on"]:
            send_telegram_message("⚙️ Servo is already OFF.")
        elif arduino_send("STOP"):
            with state_lock:
                state["servo_on"] = False
            send_telegram_message(ROTATE_STOP_TEXT)
        else:
            send_telegram_message(ARDUINO_MISSING_TEXT)
    elif cmd == "/mist_on":
        if state["mist_on"]:
            send_telegram_message("💨 Mist is already ON.")
        elif arduino_send("MIST_ON"):
            set_mist_state(True)
            log_event("MIST_ON", "manual")
            send_telegram_message(msg("mist_on"))
        else:
            send_telegram_message(ARDUINO_MISSING_TEXT)
    elif cmd == "/mist_off":
        if not state["mist_on"] and not mist_burst_busy.locked():
            send_telegram_message("💨 Mist is already OFF.")
        elif arduino_send("MIST_OFF"):
            set_mist_state(False)
            log_event("MIST_OFF", "manual")
            send_telegram_message(msg("mist_off"))
        else:
            send_telegram_message(ARDUINO_MISSING_TEXT)
    elif cmd == "/spray":
        try:
            secs = int(arg) if arg else MIST_BURST_SECONDS
        except ValueError:
            send_telegram_message("Usage: /spray [seconds]  (default 10, max 120)")
            return
        secs = max(1, min(120, secs))
        if arduino_ser is None:
            send_telegram_message(ARDUINO_MISSING_TEXT)
        elif mist_burst_busy.locked():
            send_telegram_message("💨 A spray is already running.")
        else:
            send_telegram_message(f"💨 Spraying for {secs}s...")
            threading.Thread(target=mist_burst, args=(secs, "manual"), daemon=True).start()
    elif cmd == "/auto_mist":
        if arg in ("on", "off"):
            want = arg == "on"
            if settings["auto_mist"] == want:
                send_telegram_message(f"🤖 Auto-mist is already {'ON' if want else 'OFF'}.")
            else:
                settings["auto_mist"] = want
                save_settings()
                log_event("AUTO_MIST", arg)
                send_telegram_message(
                    f"🤖 Auto-mist {'ON' if want else 'OFF'}"
                    + (f" — mists whenever soil is above {AUTO_MIST_STOP_PCT}% and stops once it reads ≤{AUTO_MIST_STOP_PCT}%, max {AUTO_MIST_MAX_PER_HOUR}/h." if want else "."))
        else:
            send_telegram_message(f"🤖 Auto-mist is {'ON' if settings['auto_mist'] else 'OFF'}. Usage: /auto_mist on|off")
    elif cmd == "/schedule":
        if arg is None or arg == "list":
            with settings_lock:
                sch = list(settings["schedules"])
            if not sch:
                send_telegram_message("⏰ No schedules. Add one: /schedule 07:00 10")
            else:
                send_telegram_message("⏰ Daily sprays:\n" + "\n".join(f"• {s['time']} → {s['secs']}s" for s in sch))
        elif arg == "clear":
            with settings_lock:
                settings["schedules"] = []
            save_settings()
            send_telegram_message("⏰ All schedules cleared.")
        elif arg in ("remove", "delete", "del") and len(parts) > 2:
            with settings_lock:
                settings["schedules"] = [s for s in settings["schedules"] if s["time"] != parts[2]]
            save_settings()
            send_telegram_message(f"⏰ Removed {parts[2]}.")
        else:
            try:
                hh, mm = arg.split(":")
                hh, mm = int(hh), int(mm)
                assert 0 <= hh < 24 and 0 <= mm < 60
                secs = int(parts[2]) if len(parts) > 2 else MIST_BURST_SECONDS
                secs = max(1, min(120, secs))
            except Exception:
                send_telegram_message("Usage: /schedule HH:MM [sec]  |  /schedule list  |  /schedule clear  |  /schedule remove HH:MM")
                return
            t = f"{hh:02d}:{mm:02d}"
            with settings_lock:
                settings["schedules"] = [s for s in settings["schedules"] if s["time"] != t] + [{"time": t, "secs": secs}]
                settings["schedules"].sort(key=lambda s: s["time"])
            save_settings()
            send_telegram_message(f"⏰ Scheduled: spray {secs}s daily at {t}.")
    else:
        send_telegram_message(DEFAULT_REPLY_TEXT)


def safe_handle(text):
    try:
        handle_command(text)
    except Exception as e:
        print(f"[telegram] command {text!r} failed: {e}", file=sys.stderr)
        send_telegram_message(f"Command failed: {e}")


def telegram_reply_loop():
    try:
        backlog = get_updates(timeout=0)
        offset = backlog["result"][-1]["update_id"] + 1 if backlog.get("result") else None
    except Exception as e:
        print(f"[telegram] could not fetch initial offset, starting from now: {e}", file=sys.stderr)
        offset = None

    print("[telegram] command loop started")
    while True:
        try:
            data = get_updates(offset=offset, timeout=25)
        except Exception as e:
            print(f"[telegram] getUpdates failed: {e}", file=sys.stderr)
            time.sleep(5)
            continue

        for update in data.get("result", []):
            offset = update["update_id"] + 1

            cb = update.get("callback_query")
            if cb:
                chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
                from_id = str(cb.get("from", {}).get("id", ""))
                if chat_id not in ALLOWED_CHAT_IDS and from_id not in ALLOWED_CHAT_IDS:
                    print(f"[telegram] ignored button press from {from_id}")
                    answer_callback(cb["id"], "Not authorised.")
                    continue
                text = normalise(cb.get("data", ""))
                print(f"[telegram] button: {text!r}")
                answer_callback(cb["id"])
                threading.Thread(target=safe_handle, args=(text,), daemon=True).start()
                continue

            message = update.get("message")
            if not message:
                continue
            chat_id = str(message.get("chat", {}).get("id", ""))
            if chat_id not in ALLOWED_CHAT_IDS:
                print(f"[telegram] ignored message from unauthorised chat {chat_id}: {message.get('text')!r}")
                continue
            raw_text = message.get("text", "")
            print(f"[telegram] received: {raw_text!r}")
            threading.Thread(target=safe_handle, args=(normalise(raw_text),), daemon=True).start()


# --------------------------------------------------------------------------
# Main: FPGA UART loop
# --------------------------------------------------------------------------
def main():
    load_settings()
    threading.Thread(target=listen_arduino, args=(None,), daemon=True).start()
    threading.Thread(target=camera_relay_server, daemon=True).start()
    threading.Thread(target=telegram_reply_loop, daemon=True).start()
    threading.Thread(target=camera_heartbeat, daemon=True).start()
    threading.Thread(target=auto_mist_loop, daemon=True).start()
    threading.Thread(target=scheduler_loop, daemon=True).start()

    send_telegram_message(
        f"🟢 AgriNova bridge online — {now_str()}\n"
        f"🔒 Lockdown {'ON' if settings['lockdown'] else 'OFF'} | 🤖 Auto-mist {'ON' if settings['auto_mist'] else 'OFF'} "
        f"| ⏰ {len(settings['schedules'])} schedule(s)\nSend /help or /menu.")
    log_event("BRIDGE_START")

    ser = None
    buf = b""
    announced_missing = False
    while True:
        if ser is None:
            port = find_serial_port()
            if port is None:
                if not announced_missing and time.time() - START_TIME > 30:
                    announced_missing = True
                    send_telegram_message("⚠️ FPGA not found — check USB.")
                print("[bridge] waiting for FPGA (/dev/cu.usbserial-*) — retrying in 5s")
                time.sleep(5)
                continue
            try:
                ser = serial.Serial(port, BAUD_RATE, timeout=1)
                print(f"[bridge] listening on {port} @ {BAUD_RATE} baud")
                buf = b""
                with state_lock:
                    state["fpga_connected"] = True
                if announced_missing:
                    send_telegram_message("✅ FPGA reconnected.")
                    log_event("FPGA_RECONNECT")
                announced_missing = False
            except serial.SerialException as e:
                print(f"[bridge] could not open {port}: {e} — retrying in 5s", file=sys.stderr)
                time.sleep(5)
                continue

        try:
            chunk = ser.read(64)
        except Exception as e:
            print(f"[bridge] FPGA connection lost: {e} — will reconnect", file=sys.stderr)
            try:
                ser.close()
            except Exception:
                pass
            ser = None
            with state_lock:
                state["fpga_connected"] = False
            send_telegram_message("⚠️ FPGA disconnected — will keep retrying.")
            log_event("FPGA_DISCONNECT", str(e))
            announced_missing = True
            time.sleep(5)
            continue

        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = line.decode(errors="replace").strip()
            if not text:
                continue
            print(f"[serial] received: {text!r}")
            if text == "IR":
                handle_ir_event()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[bridge] stopped")
