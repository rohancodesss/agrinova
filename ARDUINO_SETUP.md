# AgriNova Arduino Setup

## Hardware

### Required Components
- Arduino Uno/Nano/Mega
- SG90 servo motor
- 16x2 LCD I2C display (common address: 0x27)
- USB cable (micro-USB for Nano, USB-B for Uno)

### Wiring

**Servo (pin 9):**
- Signal (yellow) → D9
- Power (red) → 5V
- Ground (brown) → GND

**LCD I2C Display (addresses 0x27 or 0x3F):**
- SDA → A4 (Uno/Nano)
- SCL → A5 (Uno/Nano)
- 5V → 5V
- GND → GND

### Arduino Libraries Required
Install via Arduino IDE → Sketch → Include Library → Manage Libraries:
1. **Servo** (built-in, Arduino)
2. **LiquidCrystal I2C** (by Frank de Brabander)

## Upload

1. Open `agrinova_arduino.ino` in Arduino IDE
2. Select board: Tools → Board → Arduino Uno (or your variant)
3. Select port: Tools → Port → /dev/cu.usbserial-* (or COM port on Windows)
4. Click Upload

## Serial Commands

The Arduino listens on serial at 9600 baud for commands from the Mac bridge:

| Command | Action |
|---------|--------|
| `ROTATE` | Servo sweeps 0° ↔ 180° continuously |
| `STOP` | Stop servo rotation |
| `ANGLE:90` | Set servo to 90° (0-180 range) |
| `PING` | Returns `PONG` (for connectivity check) |

## Display Output

**Line 1:** Current servo angle + status (ROT/STOP)
```
ROT: 120deg
```

**Line 2:** Last command received or idle time
```
Cmd: ROTATE
```
or
```
Idle 42s
```

## Integration with Mac Bridge

The Mac bridge will auto-detect Arduino on the same USB hub. Update `mac_bridge.py` to:

1. Detect Arduino on a second serial port
2. Send ROTATE/STOP commands based on Telegram `/rotate` command
3. Send ANGLE:value commands if needed

Example flow:
```
Telegram /rotate → Mac bridge → Arduino → Servo rotates + display updates
```

## Troubleshooting

**Arduino not detected:**
```bash
ls /dev/cu.usbserial-*
```
Should show multiple ports if both FPGA and Arduino are connected.

**Display shows garbage:**
- Check I2C address: try 0x3F if 0x27 doesn't work
- Edit line 18 in sketch: `LiquidCrystal_I2C lcd(0x3F, 16, 2);`

**Servo jerky/doesn't respond:**
- Check power — servo needs dedicated 5V supply for smooth operation
- Verify D9 connection
- Test with simple angle command: `ANGLE:45`

**Serial timeout:**
- Check baud rate (9600 in both Arduino and mac_bridge.py)
- Verify USB cable quality
