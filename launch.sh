#!/usr/bin/env bash
pkill -f "python3 gui.py" 2>/dev/null || true
sleep 0.5
nohup python3 "$(dirname "$0")/gui.py" >/dev/null 2>&1 &
disown
