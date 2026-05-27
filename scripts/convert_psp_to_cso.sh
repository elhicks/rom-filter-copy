#!/usr/bin/env bash
# Converts PSP zip (ISO) files to CSO format using ciso.
# Processes one game at a time: unzip → convert → delete zip → clean temp.
# This keeps disk usage low by freeing the zip before moving to the next game.
#
# Usage: ./convert_psp_to_cso.sh [PSP_DIR]
# Default PSP_DIR: /mnt/d/ROMs/psp

set -euo pipefail

PSP_DIR="${1:-/mnt/d/ROMs/psp}"
WORK_DIR="$PSP_DIR/_cso_tmp"
LOG="$PSP_DIR/_convert_psp_cso.log"

if ! command -v ciso &>/dev/null; then
    echo "ERROR: ciso not found. Install it with: sudo apt-get install ciso"
    exit 1
fi

if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found (needed for unzipping)"
    exit 1
fi

mkdir -p "$WORK_DIR"
echo "=== PSP → CSO conversion started $(date) ===" | tee -a "$LOG"

mapfile -d '' ZIPS < <(find "$PSP_DIR" -maxdepth 1 -name '*.zip' -print0 | sort -z)
TOTAL=${#ZIPS[@]}
DONE=0
FAILED=0

for ZIP in "${ZIPS[@]}"; do
    BASENAME=$(basename "$ZIP" .zip)
    CSO="$PSP_DIR/$BASENAME.cso"

    if [[ -f "$CSO" ]]; then
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

    # Find the .iso file
    ISO=$(find "$WORK_DIR" -maxdepth 2 -iname '*.iso' | head -1)
    if [[ -z "$ISO" ]]; then
        echo "  [FAIL] No .iso found in: $ZIP" | tee -a "$LOG"
        FAILED=$((FAILED + 1))
        rm -rf "$WORK_DIR"
        continue
    fi

    # Convert (ciso compression level 9 = best)
    if ciso 9 "$ISO" "$CSO" 2>>"$LOG"; then
        echo "  [OK] Created: $(basename "$CSO")" | tee -a "$LOG"
        rm -f "$ZIP"         # free zip space immediately
        rm -rf "$WORK_DIR"   # free extracted files
        DONE=$((DONE + 1))
    else
        echo "  [FAIL] ciso failed for: $BASENAME" | tee -a "$LOG"
        rm -f "$CSO" 2>/dev/null || true   # remove partial CSO
        rm -rf "$WORK_DIR"
        FAILED=$((FAILED + 1))
    fi
done

rm -rf "$WORK_DIR"
echo "" | tee -a "$LOG"
echo "=== Done $(date): $DONE converted, $FAILED failed ===" | tee -a "$LOG"
echo "Log: $LOG"
