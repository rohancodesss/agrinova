#!/usr/bin/env python3
"""Build a standalone Windows .exe of the AgriNova bridge with PyInstaller.

Bot scope: Telegram bot, sensors (FPGA + Arduino over COM), camera (dshow),
weather, scheduling, logging, graphs, and (if its runtime is present) crop
vision. The two-way CALLS / live-video stack (pyrogram, py-tgcalls, Whisper,
torch) is deliberately excluded — it is ~1 GB and fragile to freeze; run that
from Python if needed. Produces dist/AgriNova/ (a folder with AgriNova.exe).

Runs on Windows in CI (GitHub Actions) or on the cube itself:  python build_exe.py
"""
import importlib.util
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def have(mod):
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


args = [
    sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
    "--onedir", "--contents-directory", ".", "--name", "AgriNova",
    "--console",
    "--hidden-import", "serial.tools.list_ports",
    "--collect-submodules", "serial",
]

# bundle the data the bridge reads at runtime, if present
for d in ("models", "kiosk"):
    p = os.path.join(HERE, d)
    if os.path.isdir(p):
        args += ["--add-data", f"{p}{os.pathsep}{d}"]
        print(f"[build] bundling {d}/")

# the heavy calls / speech stack is out of scope for the exe
for ex in ("pyrogram", "pytgcalls", "ntgcalls", "whisper", "torch",
           "torchaudio", "tgcrypto", "tkinter"):
    args += ["--exclude-module", ex]

# collect optional deps only if installed (keeps the build from failing)
for mod in ("ai_edge_litert", "tflite_runtime", "edge_tts", "matplotlib",
            "PIL", "numpy", "qrcode"):
    if have(mod):
        args += ["--collect-all", mod]
        print(f"[build] collecting {mod}")
    else:
        print(f"[build] skipping {mod} (not installed)")

args += [os.path.join(HERE, "mac_bridge.py")]

print("[build] " + " ".join(args))
subprocess.run(args, check=True)
print("[build] done -> dist/AgriNova/AgriNova.exe")
print("[build] On the cube: put agrinova_secrets.json beside AgriNova.exe, then run it.")
