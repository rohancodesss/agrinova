#!/usr/bin/env python3
"""AgriNova Camera Service for Windows (client mode).
Polls the Mac bridge for capture jobs and pushes photos/videos back.
All connections are outbound (VM -> Mac), so no firewall setup is needed.
"""

import cv2
import os
import re
import requests
import subprocess
import sys
import tempfile
import time

MAC_HOST = "http://192.168.64.1:5001"  # the Mac, from inside the UTM shared network

VIDEO_DEVICE = None  # detected at startup from ffmpeg's dshow device list
AUDIO_DEVICE = None

CAMERA_EVER_WORKED = False  # set once a capture succeeds
CAMERA_REMOVED = False      # set when the camera vanishes mid-capture / after working


def detect_dshow_devices():
    """Ask ffmpeg for the real DirectShow device names so audio capture works."""
    global VIDEO_DEVICE, AUDIO_DEVICE
    try:
        result = subprocess.run(
            ["ffmpeg", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, timeout=15
        )
        out = result.stderr or result.stdout or ""
        for line in out.splitlines():
            m = re.search(r'"([^"]+)"\s*\((video|audio)\)', line)
            if m:
                name, kind = m.group(1), m.group(2)
                if kind == "video" and VIDEO_DEVICE is None:
                    VIDEO_DEVICE = name
                elif kind == "audio" and AUDIO_DEVICE is None:
                    AUDIO_DEVICE = name
        print(f"[camera] ffmpeg devices — video: {VIDEO_DEVICE!r}, audio: {AUDIO_DEVICE!r}")
        if VIDEO_DEVICE:
            global CAMERA_EVER_WORKED
            CAMERA_EVER_WORKED = True  # a detected camera counts as known-present
    except Exception as e:
        print(f"[camera] device detection failed ({e}); will use OpenCV only", file=sys.stderr)


def open_camera():
    """Open the webcam robustly: try DirectShow first, then default, indices 0-2,
    and confirm a real frame comes out before trusting the handle."""
    global CAMERA_EVER_WORKED, CAMERA_REMOVED
    backends = [cv2.CAP_DSHOW, cv2.CAP_ANY]
    for attempt in range(2):  # one retry after a short pause
        for backend in backends:
            for idx in range(3):
                cap = cv2.VideoCapture(idx, backend)
                if cap.isOpened():
                    ret, _ = cap.read()
                    if ret:
                        CAMERA_EVER_WORKED = True
                        CAMERA_REMOVED = False
                        return cap
                cap.release()
        time.sleep(2)
    if CAMERA_EVER_WORKED:
        CAMERA_REMOVED = True  # it worked before and is gone now
    return None


def capture_webcam():
    """Capture single photo from webcam"""
    try:
        cap = open_camera()
        if cap is None:
            print("[camera] no working webcam found", file=sys.stderr)
            return None
        ret, frame = cap.read()
        cap.release()
        if not ret:
            return None
        _, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return buffer.tobytes()
    except Exception as e:
        print(f"[camera] webcam capture failed: {e}", file=sys.stderr)
        return None


def probe_duration(path):
    """Return the media file's duration in seconds via ffprobe, or None."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=15
        )
        return float(r.stdout.strip())
    except Exception:
        return None


def capture_video(duration_seconds=5):
    """Capture video (+ audio when ffmpeg knows the devices), OpenCV fallback."""
    global CAMERA_EVER_WORKED, CAMERA_REMOVED
    partial_bytes = None
    temp_video_path = os.path.join(tempfile.gettempdir(), "agrinova_video.mp4")

    # Never serve a stale clip from a previous capture
    try:
        if os.path.exists(temp_video_path):
            os.remove(temp_video_path)
    except OSError:
        pass

    if VIDEO_DEVICE:
        try:
            if AUDIO_DEVICE:
                dshow_input = f"video={VIDEO_DEVICE}:audio={AUDIO_DEVICE}"
            else:
                dshow_input = f"video={VIDEO_DEVICE}"
            # -rtbufsize 100M: default 3MB overflows in a VM and drops most frames
            # -preset ultrafast: real-time encoding must keep up with capture
            cmd = [
                "ffmpeg", "-f", "dshow", "-rtbufsize", "100M", "-i", dshow_input,
                "-t", str(duration_seconds),
                "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac",
                "-y", temp_video_path
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=duration_seconds + 25)

            if (result.returncode != 0 or not os.path.exists(temp_video_path)) and AUDIO_DEVICE:
                print("[camera] audio capture failed, trying video only", file=sys.stderr)
                cmd = [
                    "ffmpeg", "-f", "dshow", "-rtbufsize", "100M", "-i", f"video={VIDEO_DEVICE}",
                    "-t", str(duration_seconds),
                    "-c:v", "libx264", "-preset", "ultrafast",
                    "-y", temp_video_path
                ]
                subprocess.run(cmd, capture_output=True, timeout=duration_seconds + 25)

            if os.path.exists(temp_video_path) and os.path.getsize(temp_video_path) > 0:
                actual = probe_duration(temp_video_path)
                if actual is not None and actual < duration_seconds * 0.7:
                    # short clip = capture cut off early; keep it as evidence
                    with open(temp_video_path, "rb") as f:
                        partial_bytes = f.read()
                    print(f"[camera] ffmpeg clip only {actual:.1f}s of {duration_seconds}s — falling back to OpenCV", file=sys.stderr)
                else:
                    CAMERA_EVER_WORKED = True
                    CAMERA_REMOVED = False
                    print(f"[camera] ffmpeg capture OK ({actual if actual else '?'}s"
                          + (", with audio)" if AUDIO_DEVICE else ")"))
                    with open(temp_video_path, "rb") as f:
                        return f.read()
        except Exception as e:
            print(f"[camera] ffmpeg error: {e}", file=sys.stderr)

    print("[camera] using OpenCV fallback (no audio)", file=sys.stderr)
    data = capture_video_opencv(duration_seconds)
    if data:
        return data

    if partial_bytes:
        # ffmpeg got footage, then the camera vanished before the fallback
        CAMERA_REMOVED = True
        print("[camera] CAMERA LOST MID-RECORDING — sending partial clip as evidence", file=sys.stderr)
        return partial_bytes
    return None


def capture_video_opencv(duration_seconds=5):
    """Fallback: OpenCV capture (video only, no audio).
    Buffers frames, then writes the file at the MEASURED frame rate so the
    clip plays back in real time even when the webcam delivers few fps."""
    cap = None
    out = None
    try:
        cap = open_camera()
        if cap is None:
            print("[camera] no working webcam found", file=sys.stderr)
            return None

        frames = []
        start_time = time.time()
        yanked = False
        while (time.time() - start_time) < duration_seconds:
            ret, frame = cap.read()
            if not ret:
                # camera died mid-recording? try to reopen once
                cap.release()
                cap = open_camera()
                if cap is None:
                    yanked = True
                    break
                continue
            frames.append(frame)
        elapsed = time.time() - start_time
        if cap is not None:
            cap.release()

        if yanked:
            global CAMERA_REMOVED
            CAMERA_REMOVED = True
            print("[camera] CAMERA REMOVED MID-RECORDING", file=sys.stderr)

        if not frames or elapsed <= 0:
            return None

        # A phantom capture device delivers identical, noiseless black frames.
        # A real camera in a dark room still has sensor noise (std > 0).
        if all(f.std() < 1.0 for f in frames):
            print("[camera] frames are blank — phantom device, not a real camera", file=sys.stderr)
            return None

        real_fps = max(1.0, len(frames) / elapsed)
        height, width = frames[0].shape[:2]
        print(f"[camera] captured {len(frames)} frames in {elapsed:.1f}s -> {real_fps:.1f} fps")

        temp_video_path = os.path.join(tempfile.gettempdir(), "agrinova_video.mp4")
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        out = cv2.VideoWriter(temp_video_path, fourcc, real_fps, (width, height))
        for frame in frames:
            out.write(frame)
        out.release()

        with open(temp_video_path, "rb") as f:
            return f.read()
    except Exception as e:
        print(f"[camera] OpenCV capture failed: {e}", file=sys.stderr)
    finally:
        try:
            if cap: cap.release()
            if out: out.release()
        except Exception:
            pass

    return None


def main():
    print("[camera] AgriNova camera client")
    detect_dshow_devices()
    print(f"[camera] polling Mac bridge at {MAC_HOST} for capture jobs...")
    connected = False

    while True:
        try:
            r = requests.get(f"{MAC_HOST}/job", timeout=30)
            job = r.json()
            if not connected:
                print("[camera] connected to Mac bridge")
                connected = True
        except requests.RequestException as e:
            if connected:
                print(f"[camera] lost connection to Mac bridge: {e}", file=sys.stderr)
            connected = False
            time.sleep(3)
            continue

        kind = job.get("job")
        if not kind:
            continue  # long-poll came back empty; poll again

        job_id = job.get("id", "")
        print(f"[camera] job received: {kind}")

        global CAMERA_REMOVED
        if kind == "photo":
            data = capture_webcam()
        elif kind == "video":
            data = capture_video(int(job.get("duration", 5)))
        else:
            data = None

        # safety net: a camera that used to work returning nothing = removed
        if data is None and kind in ("photo", "video") and CAMERA_EVER_WORKED:
            CAMERA_REMOVED = True

        headers = {}
        if CAMERA_REMOVED:
            headers["X-Camera-Status"] = "removed"

        try:
            requests.post(f"{MAC_HOST}/result/{job_id}", data=data or b"", headers=headers, timeout=90)
            print(f"[camera] {kind} result sent ({len(data) if data else 0} bytes)"
                  + (" [CAMERA REMOVED]" if CAMERA_REMOVED else ""))
        except requests.RequestException as e:
            print(f"[camera] failed to send result: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
