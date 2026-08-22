AgriNova Bridge for Windows
============================

This script runs on Windows and communicates with the VSDSquadron FPGA board via USB serial.

REQUIREMENTS:
- Python 3.7+ (download from python.org)
- pyserial: pip install pyserial
- opencv-python: pip install opencv-python
- USB-to-UART driver: FTDI FT232H drivers (usually auto-install on Windows)

SETUP:
1. Install Python 3 from https://www.python.org/downloads/
2. Open Command Prompt or PowerShell
3. Run: pip install pyserial opencv-python
4. Connect the FPGA board via USB to your Windows PC
5. Run: python agrinova_bridge.py

The script will:
- Auto-detect the FPGA's serial port (COM port)
- Listen for "TRIGGER" messages from the FPGA
- Forward them to Telegram
- Handle /photo, /rotate, /about, /stop, /continue commands

TROUBLESHOOTING:
- If the script can't find the serial port, check Device Manager for "FTDI USB UART" or similar
- Make sure the FPGA board is powered and plugged in
- Verify the BOT_TOKEN and CHAT_ID are correct in the script

COMMANDS via Telegram:
/about    - Show system info
/photo    - Capture a photo from connected webcam
/rotate   - Show servo status
/stop     - Mute FPGA alerts
/continue - Resume FPGA alerts
