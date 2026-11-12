"""AgriNova farm phone — the farm's own Telegram account, both directions.

OUT: the farm rings the farmer and speaks an alert (replaces CallMeBot).
IN:  the farmer CALLS the farm — it answers and speaks the live status.
     No typing, no reading: dial "AgriNova Farm", hear your farm.

One-time login (interactive; Jio number + the OTP Telegram sends):
    /usr/bin/python3 farm_caller.py login

Standalone test call (only while the bridge is NOT running — they share
the session file):
    /usr/bin/python3 farm_caller.py call @rohanlanka en "Alert! ..."

mac_bridge.py imports FarmPhone and keeps it online the whole time, so
incoming calls are answered whenever the bridge is up.

Deps: pyrogram, py-tgcalls, ffmpeg, edge-tts. Exit codes (CLI call):
0 played, 2 not answered/declined, 1 error.
"""

import asyncio
import os
import subprocess
import sys
import tempfile
import threading

SESSION = "agrinova_farm"          # -> agrinova_farm.session next to this file
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)   # PyInstaller .exe: data sits beside it
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Telegram app credentials live in agrinova_secrets.json (gitignored)
import json as _json
with open(os.path.join(BASE_DIR, "agrinova_secrets.json")) as _f:
    _sec = _json.load(_f)
API_ID = int(_sec["tg_api_id"])
API_HASH = _sec["tg_api_hash"]
RING_TIMEOUT = 60                  # seconds to let the farmer's phone ring
CALL_MAX_SECONDS = 180             # hard cap on any call
REPEATS = 3                        # say an outgoing alert this many times

VOICE_NEURAL = {"en": "en-IN-PrabhatNeural", "hi": "hi-IN-MadhurNeural",
                "te": "te-IN-MohanNeural", "te_en": "te-IN-MohanNeural"}
VOICE_FALLBACK = {"en": "Aman (English (India))", "hi": "Lekha", "te": "Geeta",
                  "te_en": "Geeta"}

# The farm's side of an incoming call, per language.
PROMPTS = {
    "greeting": {
        "en": "Hello! Your farm here. What should I do?",
        "hi": "नमस्ते! मैं आपका खेत बोल रहा हूँ। बताइए, क्या करूँ?",
        "te": "నమస్కారం! నేను మీ పొలం. చెప్పండి, ఏం చేయాలి?",
    },
    "no_hear": {
        "en": "I did not hear anything. Say a command, or say goodbye.",
        "hi": "कुछ सुनाई नहीं दिया। कोई आदेश बोलिए, या अलविदा कहिए।",
        "te": "ఏమీ వినిపించలేదు. ఒక ఆదేశం చెప్పండి, లేదా వీడ్కోలు చెప్పండి.",
    },
    "goodbye": {
        "en": "Goodbye from your farm!",
        "hi": "अलविदा! आपका खेत ध्यान रख रहा है।",
        "te": "వీడ్కోలు! మీ పొలం జాగ్రత్తగా చూసుకుంటుంది.",
    },
    "no_camera": {
        "en": "I could not reach the camera.",
        "hi": "कैमरा नहीं मिला।",
        "te": "కెమెరా దొరకలేదు.",
    },
    "live_intro": {
        "en": "I'm getting a live view of your crops...",
        "hi": "मैं आपकी फसलों का लाइव नज़ारा ला रहा हूँ...",
        "te": "మీ పంటల ప్రత్యక్ష దృశ్యం తెస్తున్నాను...",
    },
}

VIDEO_CLIP_SECONDS = 8

# Fragments of the farm's own replies: if the transcript contains one, the
# phone's speaker echoed our own voice into the mic — ignore it, don't answer
# it (otherwise the call becomes "I heard: I heard you..." forever). Also
# covers Whisper's classic silence hallucinations.
ECHO_MARKERS = (
    "i heard", "i've heard", "heard you", "you can say", "say a command", "your farm here",
    "what should i do", "did not hear", "one moment", "goodbye from",
    "thanks for watching", "thank you for watching", "subscribe",
    "आपने कहा", "कहिए", "क्या करूँ", "सुनाई नहीं",
    "మీరు అన్నది", "చెప్పండి", "ఏం చేయాలి", "వినిపించలేదు",
)


def sounds_like_echo(transcript):
    t = transcript.lower()
    return any(m in t for m in ECHO_MARKERS)


def capture_clip(seconds=VIDEO_CLIP_SECONDS):
    """Record a short clip using the bridge's proven camera path: the Windows
    prototype camera when connected, else the Mac's own webcam (device found
    by name, 640x480 — the mode this camera actually supports)."""
    try:
        import mac_bridge  # already loaded inside the bridge process
        data = mac_bridge.request_capture("video", duration=seconds,
                                          timeout=seconds + 40)
    except Exception as e:
        print(f"[phone] capture via bridge failed: {e}", file=sys.stderr)
        return None
    if not data:
        return None
    out = os.path.join(tempfile.mkdtemp(prefix="agrieye_"), "farm.mp4")
    with open(out, "wb") as f:
        f.write(data)
    return out


def prompt(key, lang):
    d = PROMPTS[key]
    return d.get("te" if lang == "te_en" else lang, d["en"])


def session_exists():
    return os.path.exists(os.path.join(BASE_DIR, SESSION + ".session"))


def wav_seconds(path):
    """Real duration of a wav file (0 if unreadable)."""
    try:
        import wave as _wave
        with _wave.open(path) as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return 0


def silence_wav(seconds=1.2):
    """Short silent wav — used to answer a call and let audio settle
    before the greeting, so its first words aren't lost to call setup."""
    tmp = tempfile.mkdtemp(prefix="agrisil_")
    wav = os.path.join(tmp, "sil.wav")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "anullsrc=r=48000:cl=mono", "-t", str(seconds), wav],
                   check=True, timeout=30, capture_output=True)
    return wav


def tts_wav(text, lang="en", repeats=REPEATS):
    """Text -> 48 kHz wav path (edge-tts neural, macOS `say` as fallback)."""
    text = " ... ".join([text] * max(1, repeats))
    tmp = tempfile.mkdtemp(prefix="agricall_")
    mp3, wav = os.path.join(tmp, "say.mp3"), os.path.join(tmp, "say.wav")
    made = False
    try:
        subprocess.run([sys.executable, "-m", "edge_tts",
                        "--voice", VOICE_NEURAL.get(lang, VOICE_NEURAL["en"]),
                        "--text", text, "--write-media", mp3],
                       check=True, timeout=60, capture_output=True)
        made = True
    except Exception as e:
        print(f"[phone] edge-tts failed ({e}); trying macOS say", file=sys.stderr)
    if not made:
        if sys.platform == "darwin":
            aiff = os.path.join(tmp, "say.aiff")
            subprocess.run(["say", "-v", VOICE_FALLBACK.get(lang, VOICE_FALLBACK["en"]),
                            "-o", aiff, text], check=True, timeout=60)
            mp3 = aiff
        elif sys.platform.startswith("win"):
            # Windows SAPI via PowerShell writes a wav directly (no net needed).
            raw = os.path.join(tmp, "sapi.wav")
            ps = ("Add-Type -AssemblyName System.Speech; "
                  "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                  f"$s.SetOutputToWaveFile('{raw}'); "
                  f"$s.Speak([Console]::In.ReadToEnd()); $s.Dispose()")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           input=text, text=True, check=True, timeout=60)
            mp3 = raw
        else:
            # Linux: espeak-ng if present.
            aiff = os.path.join(tmp, "espeak.wav")
            subprocess.run(["espeak-ng", "-w", aiff, text], check=True, timeout=60)
            mp3 = aiff
    subprocess.run(["ffmpeg", "-y", "-i", mp3, "-ar", "48000", "-ac", "1", wav],
                   check=True, timeout=60, capture_output=True)
    return wav


class FarmPhone:
    """Persistent farm Telegram account: places calls, answers calls.

    status_cb() -> (text, lang): spoken when an allowed user calls in.
    command_cb(transcript) -> (reply_text, done): voice-command executor;
        done=True ends the call after the reply ("goodbye").
    allowed: set of Telegram user ids permitted to call the farm.
    """

    LISTEN_SECONDS = 6
    MAX_ROUNDS = 8
    LIVE_SECONDS = 30                 # how long "video" shows the field live

    def __init__(self, status_cb=None, command_cb=None, allowed=None):
        self.status_cb = status_cb
        self.command_cb = command_cb
        self.allowed = set(allowed or [])
        self.loop = None
        self.calls = None
        self.ready = threading.Event()
        self._outgoing = {}            # chat_id -> {"answered","done","ok","why","wav"}
        self._serving = {}             # chat_id -> {"task", "prompt_done"}
        self._video_call = {}          # caller id -> incoming call had video on
        self._whisper = None
        self._whisper_lock = threading.Lock()

    # Bias Whisper toward our command words — short single words over call
    # audio otherwise decode as random filler ("video" -> "this").
    HINT = {
        "en": "Farm voice commands: video, status, photo, mist on, mist off, "
              "spray, water, lockdown, unlock, goodbye.",
        "hi": "खेत के आदेश: वीडियो, स्टेटस, फोटो, पानी चालू, पानी बंद, छिड़काव, लॉकडाउन, अलविदा.",
        "te": "పొల ఆదేశాలు: వీడియో, స్టేటస్, ఫోటో, నీళ్లు పెట్టు, నీళ్లు ఆపు, లాక్ డౌన్, వీడ్కోలు.",
    }

    def _ensure_whisper(self):
        """Load the model once; stays in RAM for the life of the bridge."""
        with self._whisper_lock:
            if self._whisper is None:
                import whisper
                print("[phone] loading whisper model ...", flush=True)
                self._whisper = whisper.load_model("base")
                print("[phone] whisper ready", flush=True)
        return self._whisper

    def _transcribe(self, wav, lang):
        """Blocking: speech in the recorded call audio -> text ('' if silent)."""
        try:
            self._ensure_whisper()
            code = {"en": "en", "hi": "hi", "te": "te", "te_en": "te"}.get(lang, "en")
            r = self._whisper.transcribe(
                wav, language=code, fp16=False, temperature=0.0,
                condition_on_previous_text=False,
                initial_prompt=self.HINT.get(code, self.HINT["en"]))
            return r["text"].strip()
        except Exception as e:
            print(f"[phone] transcribe failed: {e}", file=sys.stderr)
            return ""

    # ---- lifecycle ----
    def start(self, wait=30):
        threading.Thread(target=self._thread, daemon=True, name="farm-phone").start()
        # warm the speech model now, not on the first caller's clock
        threading.Thread(target=lambda: self._ensure_whisper() and None,
                         daemon=True, name="whisper-preload").start()
        return self.ready.wait(wait)

    def _thread(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        except Exception as e:
            print(f"[phone] service died: {e}", file=sys.stderr)

    async def _main(self):
        from pyrogram import Client
        from pytgcalls import PyTgCalls, filters as cf
        from pytgcalls.types import ChatUpdate, RawCallUpdate

        app = Client(SESSION, api_id=API_ID, api_hash=API_HASH, workdir=BASE_DIR)
        self.calls = PyTgCalls(app)
        calls = self.calls

        # pytgcalls doesn't surface whether the caller pressed "video call";
        # pyrogram's raw update does. Remember it so _serve can lead with video.
        from pyrogram.handlers import RawUpdateHandler
        from pyrogram.raw.types import UpdatePhoneCall, PhoneCallRequested

        async def _raw_call(_, upd, users, chats):
            if isinstance(upd, UpdatePhoneCall) \
                    and isinstance(upd.phone_call, PhoneCallRequested):
                self._video_call[upd.phone_call.admin_id] = \
                    bool(getattr(upd.phone_call, "video", False))
        app.add_handler(RawUpdateHandler(_raw_call), group=-99)

        @calls.on_update(cf.chat_update(ChatUpdate.Status.INCOMING_CALL))
        async def _incoming(_, u):
            if self.allowed and u.chat_id not in self.allowed:
                print(f"[phone] ignoring call from stranger id {u.chat_id}")
                return
            prev = self._serving.get(u.chat_id)
            if prev:                                # signaling fires this repeatedly
                if prev["task"] and not prev["task"].done():
                    return
                self._serving.pop(u.chat_id, None)  # stale entry from a failed call
            print(f"[phone] incoming call from {u.chat_id} — starting conversation")
            st = {"prompt_done": asyncio.Event(), "task": None}
            self._serving[u.chat_id] = st
            st["task"] = asyncio.ensure_future(self._serve(u.chat_id))

        @calls.on_update(cf.stream_end())
        async def _ended(_, u):
            st = self._outgoing.get(u.chat_id)
            if st:                                  # outgoing alert finished
                if st["answered"].is_set():
                    st["ok"], st["why"] = True, "played"
                    st["done"].set()
                return
            sv = self._serving.get(u.chat_id)
            if sv:                                  # a prompt finished playing
                sv["prompt_done"].set()

        @calls.on_update()
        async def _watch(_, u):
            if isinstance(u, RawCallUpdate):
                st = self._outgoing.get(u.chat_id)
                if st and (u.status & RawCallUpdate.Type.UPDATED_CALL) \
                        and not st["answered"].is_set():
                    st["answered"].set()
                    print("[phone] answered — restarting message", flush=True)
                    try:
                        # let the call's audio transport come up first, or the
                        # message plays into the void and the farmer hears nothing
                        await asyncio.sleep(2.0)
                        await calls.play(u.chat_id, st["wav"])
                    except Exception as e:
                        print(f"[phone] restart failed: {e!r}", file=sys.stderr)
                return
            if isinstance(u, ChatUpdate):
                ended = u.status & (ChatUpdate.Status.DISCARDED_CALL
                                    | ChatUpdate.Status.BUSY_CALL
                                    | ChatUpdate.Status.LEFT_CALL)
                st = self._outgoing.get(u.chat_id)
                if st and ended:
                    if st["answered"].is_set():
                        st["ok"], st["why"] = True, "heard and hung up"
                    else:
                        st["why"] = "declined or busy"
                    st["done"].set()
                    return
                sv = self._serving.get(u.chat_id)
                if sv and ended:                    # caller hung up mid-chat
                    print("[phone] caller hung up")
                    self._serving.pop(u.chat_id, None)
                    if sv["task"]:
                        sv["task"].cancel()

        await calls.start()
        me = await app.get_me()
        print(f"[phone] farm phone online as {me.first_name} (id {me.id})", flush=True)
        self.ready.set()
        await asyncio.Event().wait()                # serve forever

    # ---- incoming conversation ----
    async def _live_video(self, chat_id, sv, pre=None):
        """Stream the camera into the call LIVE for LIVE_SECONDS.
        Returns True if a live stream actually played.
        pre: an already-started (proc, port) from local_live_stream."""
        from pytgcalls.types import MediaStream, VideoQuality

        # Low-latency knobs for py-tgcalls' internal ffmpeg reader: without
        # these it probes ~5 s of stream before decoding and buffers freely,
        # which is exactly the start delay + ever-growing lag.
        LOWLAT = ("--video ---start -fflags nobuffer -flags low_delay "
                  "-probesize 300000 -analyzeduration 800000")

        streams = []
        # 1) ntgcalls native device capture (may be unsupported on macOS)
        try:
            from pytgcalls.media_devices import MediaDevices
            cams = MediaDevices.camera_devices()
            cam = next((d for d in cams if "c170" in d.name.lower()),
                       cams[0] if cams else None)
            if cam is not None:
                streams.append(("device", MediaStream(
                    cam, video_parameters=VideoQuality.SD_480p,
                    audio_flags=MediaStream.Flags.IGNORE)))
        except Exception as e:
            print(f"[phone] camera device list failed: {e}", file=sys.stderr)
        # 2) ffmpeg -> local UDP -> MediaStream (the reliable path)
        proc = None
        if streams and pre and pre[0]:
            pre[0].kill()                   # device route won; drop the spare
        if not streams:
            if pre is not None:
                proc, port = pre
            else:
                import mac_bridge  # already loaded inside the bridge process
                proc, port = await self.loop.run_in_executor(
                    None, mac_bridge.local_live_stream)
            if proc:
                # big receive buffer: keyframe bursts overflow the default
                # UDP socket buffer and the lost packets decode as green junk
                streams.append(("udp", MediaStream(
                    f"udp://127.0.0.1:{port}?buffer_size=1048576",
                    video_parameters=VideoQuality.SD_480p,
                    audio_flags=MediaStream.Flags.IGNORE,
                    ffmpeg_parameters=LOWLAT)))
        played = False
        try:
            for label, stream in streams:
                try:
                    sv["prompt_done"].clear()
                    await asyncio.wait_for(self.calls.play(chat_id, stream), 15)
                    played = True
                    print(f"[phone] live video via {label}", flush=True)
                    break
                except Exception as e:
                    print(f"[phone] live {label} failed: {e!r}", file=sys.stderr)
            if played:
                try:
                    await asyncio.wait_for(sv["prompt_done"].wait(),
                                           self.LIVE_SECONDS)
                except asyncio.TimeoutError:
                    pass
        finally:
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
        if played:
            # replace the live stream with a breath of silence so the
            # conversation loop can go back to listening cleanly
            try:
                sil = await self.loop.run_in_executor(None, silence_wav, 0.5)
                sv["prompt_done"].clear()
                await self.calls.play(chat_id, sil)
                try:
                    await asyncio.wait_for(sv["prompt_done"].wait(), 5)
                except asyncio.TimeoutError:
                    pass
            except Exception:
                pass
        return played

    async def _serve(self, chat_id):
        """Answer the farmer's call: speak status, then listen/act in rounds."""
        import tempfile as _tf
        sv = self._serving[chat_id]

        async def say(text, lang, repeats=1):
            wav = await self.loop.run_in_executor(None, tts_wav, text, lang, repeats)
            dur = wav_seconds(wav)
            sv["prompt_done"].clear()
            t0 = self.loop.time()
            await asyncio.wait_for(self.calls.play(chat_id, wav), 20)
            try:
                await asyncio.wait_for(sv["prompt_done"].wait(), max(90, dur + 10))
            except asyncio.TimeoutError:
                pass
            # A stray stream_end during call setup can fire early; never move
            # on (record/next prompt) before the audio had time to finish.
            left = dur - (self.loop.time() - t0)
            if left > 0:
                await asyncio.sleep(left)

        try:
            lang = (self.status_cb()[1] if self.status_cb else "en")
            # Answer with ~1 s of silence first: the first play() answers the
            # call while audio is still negotiating, which chopped the start
            # of the greeting. Speak only once the transport is up.
            # This play ANSWERS the call — if it fails the caller stays stuck
            # on "requesting", so retry once and log the real exception.
            sil = await self.loop.run_in_executor(None, silence_wav)
            for attempt in (1, 2):
                try:
                    sv["prompt_done"].clear()
                    await asyncio.wait_for(self.calls.play(chat_id, sil), 15)
                    try:
                        await asyncio.wait_for(sv["prompt_done"].wait(), 8)
                    except asyncio.TimeoutError:
                        pass
                    break
                except Exception as e:
                    print(f"[phone] answer attempt {attempt} failed: {e!r}",
                          file=sys.stderr)
                    await asyncio.sleep(1.5)
            # Farmer placed a VIDEO call: show the field immediately,
            # no need to ask for it. Greeting follows the live window.
            if self._video_call.pop(chat_id, False):
                print("[phone] video call — going live right away", flush=True)
                # start the camera while the intro line plays, so the
                # spoken sentence hides the stream's startup time
                import mac_bridge
                spawn = self.loop.run_in_executor(
                    None, mac_bridge.local_live_stream)
                await say(prompt("live_intro", lang), lang)
                if not await self._live_video(chat_id, sv, pre=await spawn):
                    await say(prompt("no_camera", lang), lang)
            await say(prompt("greeting", lang), lang)
            for _ in range(self.MAX_ROUNDS):
                if chat_id not in self._serving:
                    return
                rec = os.path.join(_tf.mkdtemp(prefix="agriear_"), "heard.wav")
                try:
                    await self.calls.record(chat_id, rec)
                    await asyncio.sleep(self.LISTEN_SECONDS)
                    await self.calls.record(chat_id, None)   # stop recording
                except Exception as e:
                    print(f"[phone] record failed: {e}", file=sys.stderr)
                    break
                heard = ""
                if os.path.exists(rec) and os.path.getsize(rec) > 4000:
                    heard = await self.loop.run_in_executor(
                        None, self._transcribe, rec, lang)
                print(f"[phone] heard: {heard!r}", flush=True)
                if heard and sounds_like_echo(heard):
                    print("[phone] (own echo — ignoring)", flush=True)
                    continue                        # listen again, say nothing
                if not heard:
                    await say(prompt("no_hear", lang), lang)
                    continue
                if self.command_cb:
                    reply, action = self.command_cb(heard)
                else:
                    reply, action = f"You said: {heard}", None
                await say(reply, lang)
                if action == "video":              # show the farm inside the call
                    if await self._live_video(chat_id, sv):
                        continue                   # streamed live; next round
                    # live path unavailable — fall back to a recorded clip
                    clip = await self.loop.run_in_executor(None, capture_clip)
                    played = False
                    if clip:
                        from pytgcalls.types import MediaStream
                        sv["prompt_done"].clear()
                        print("[phone] streaming webcam clip into the call")
                        for stream in (MediaStream(clip),
                                       MediaStream(clip,
                                                   audio_flags=MediaStream.Flags.IGNORE)):
                            try:
                                await self.calls.play(chat_id, stream)
                                played = True
                                break
                            except Exception as e:
                                print(f"[phone] clip play failed: {e}", file=sys.stderr)
                        if played:
                            try:
                                await asyncio.wait_for(sv["prompt_done"].wait(),
                                                       VIDEO_CLIP_SECONDS + 30)
                            except asyncio.TimeoutError:
                                pass
                    if not played:
                        await say(prompt("no_camera", lang), lang)
                    continue
                if action is True or action == "hangup":
                    break
            else:
                await say(prompt("goodbye", lang), lang)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[phone] conversation error: {e!r}", file=sys.stderr)
        finally:
            self._serving.pop(chat_id, None)
            try:
                await self.calls.leave_call(chat_id)
                print("[phone] call ended")
            except Exception:
                pass

    # ---- outgoing (threadsafe, called from the bridge) ----
    def call(self, target, text, lang="en"):
        """Ring `target` and speak `text`. Returns (ok, why)."""
        if not self.ready.is_set():
            return False, "farm phone not running"
        fut = asyncio.run_coroutine_threadsafe(
            self._call(target, text, lang), self.loop)
        return fut.result(CALL_MAX_SECONDS + 90)

    async def _call(self, target, text, lang):
        from pytgcalls.types import CallConfig
        wav = await self.loop.run_in_executor(None, tts_wav, text, lang)
        chat_id = await self.calls.resolve_chat_id(target)
        st = {"answered": asyncio.Event(), "done": asyncio.Event(),
              "ok": False, "why": "timeout", "wav": wav}
        self._outgoing[chat_id] = st
        print(f"[phone] ringing {target} ...", flush=True)
        try:
            await self.calls.play(chat_id, wav, config=CallConfig(timeout=RING_TIMEOUT))
            await asyncio.wait_for(st["done"].wait(), CALL_MAX_SECONDS)
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            st["why"] = str(e)
        finally:
            self._outgoing.pop(chat_id, None)
            try:
                await self.calls.leave_call(chat_id)
            except Exception:
                pass
        print(f"[phone] result: {st['why']}", flush=True)
        return st["ok"], st["why"]


# ---- CLI ----
async def _login():
    from pyrogram import Client
    app = Client(SESSION, api_id=API_ID, api_hash=API_HASH, workdir=BASE_DIR)
    await app.start()
    me = await app.get_me()
    print(f"\n✅ Logged in as: {me.first_name} {me.last_name or ''} "
          f"(@{me.username or 'no username'}, id {me.id})")
    print("Session saved — the bridge can place and answer calls now.")
    await app.stop()


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "login":
        asyncio.get_event_loop().run_until_complete(_login())
        return 0
    if len(sys.argv) >= 5 and sys.argv[1] == "call":
        if not session_exists():
            print("[phone] no session — run: python3 farm_caller.py login", file=sys.stderr)
            return 1
        phone = FarmPhone()
        if not phone.start():
            print("[phone] could not start", file=sys.stderr)
            return 1
        ok, why = phone.call(sys.argv[2], " ".join(sys.argv[4:]), sys.argv[3])
        return 0 if ok else 2
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
