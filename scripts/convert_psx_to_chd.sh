#!/usr/bin/env bash
# Converts PSX zip (BIN/CUE) files to CHD format using chdman.
# Processes one game at a time: unzip → convert → delete zip → clean temp.
# This keeps disk usage low by freeing the zip before moving to the next game.
#
# Usage: ./convert_psx_to_chd.sh [PSX_DIR]
# Default PSX_DIR: /mnt/f/ROMs/psx

set -euo pipefail

PSX_DIR="${1:-/mnt/f/ROMs/psx}"
WORK_DIR="$PSX_DIR/_chd_tmp"
LOG="$PSX_DIR/_convert_psx_chd.log"

if ! command -v chdman &>/dev/null; then
    echo "ERROR: chdman not found. Install it with: sudo apt-get install mame-tools"
    exit 1
fi

if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found (needed for unzipping)"
    exit 1
fi

mkdir -p "$WORK_DIR"
echo "=== PSX → CHD conversion started $(date) ===" | tee -a "$LOG"

mapfile -d '' ZIPS < <(find "$PSX_DIR" -maxdepth 1 -name '*.zip' -print0 | sort -z)
TOTAL=${#ZIPS[@]}
DONE=0
FAILED=0

for ZIP in "${ZIPS[@]}"; do
    BASENAME=$(basename "$ZIP" .zip)
    CHD="$PSX_DIR/$BASENAME.chd"

    if [[ -f "$CHD" ]]; then
        echo "[SKIP] Already converted: $BASENAME" | tee -a "$LOG"
        DONE=$((DONE + 1))
        continue
    fi

    echo "[$(( DONE + FAILED + 1 ))/$TOTAL] $BASENAME" | tee -a "$LOG"

    # Clean and recreate temp work dir for this game
    rm -rf "$WORK_DIR"
    mkdir -p "$WORK_DIR"

    # Extract zip
    if ! python3 -c "
import zipfile, sys
with zipfile.ZipFile(sys.argv[1]) as z:
    z.extractall(sys.argv[2])
" "$ZIP" "$WORK_DIR"; then
        echo "  [FAIL] Could not unzip: $ZIP" | tee -a "$LOG"
        FAILED=$((FAILED + 1))
        continue
    fi

    # Find the .cue file
    CUE=$(find "$WORK_DIR" -maxdepth 2 -iname '*.cue' | head -1)
    if [[ -z "$CUE" ]]; then
        echo "  [FAIL] No .cue found in: $ZIP" | tee -a "$LOG"
        FAILED=$((FAILED + 1))
        rm -rf "$WORK_DIR"
        continue
    fi

    # Convert
    if chdman createcd -i "$CUE" -o "$CHD" 2>>"$LOG"; then
        echo "  [OK] Created: $(basename "$CHD")" | tee -a "$LOG"
        rm -f "$ZIP"         # free zip space immediately
        rm -rf "$WORK_DIR"   # free extracted files
        DONE=$((DONE + 1))
    else
        echo "  [FAIL] chdman failed for: $BASENAME" | tee -a "$LOG"
        rm -f "$CHD" 2>/dev/null || true   # remove partial CHD
        rm -rf "$WORK_DIR"
        FAILED=$((FAILED + 1))
    fi
done

rm -rf "$WORK_DIR"
echo "" | tee -a "$LOG"
echo "=== Done $(date): $DONE converted, $FAILED failed ===" | tee -a "$LOG"
echo "Log: $LOG"
