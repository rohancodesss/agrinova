#!/bin/bash
# Install the AgriNova bridge as a launchd service (auto-start at login, auto-restart on crash).
# IMPORTANT: stop any manually-run bridge (agri) first, or Telegram will return 409.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST=~/Library/LaunchAgents/com.agrinova.bridge.plist
case "$1" in
  install)
    pkill -f mac_bridge.py || true
    cp "$DIR/com.agrinova.bridge.plist" "$PLIST"
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    echo "installed + started. logs: $DIR/bridge.log" ;;
  uninstall)
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"; echo "removed" ;;
  restart)  launchctl kickstart -k gui/$(id -u)/com.agrinova.bridge; echo "restarted" ;;
  stop)     launchctl unload "$PLIST"; echo "stopped (use install to start again)" ;;
  logs)     tail -f "$DIR/bridge.log" ;;
  *) echo "usage: $0 install|uninstall|restart|stop|logs"; exit 1 ;;
esac
