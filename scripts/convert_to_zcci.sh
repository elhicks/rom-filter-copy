#!/usr/bin/env bash
# WARNING: IF ES-DE is running on the device, close it before running this script.
# ES-DE rewrites gamelist.xml on exit and will overwrite the path update.
#
# Usage: convert_to_zcci.sh --compressor PATH --input DIR --output DIR [--gamelist WIN_PATH]
#   --gamelist is optional; omit it to skip the gamelist update (e.g. for smoke tests).
set -uo pipefail

COMPRESSOR=""
INPUT_DIR=""
OUTPUT_DIR=""
GAMELIST_WIN=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --compressor) COMPRESSOR="$2"; shift 2 ;;
        --input)      INPUT_DIR="$2";  shift 2 ;;
        --output)     OUTPUT_DIR="$2"; shift 2 ;;
        --gamelist)   GAMELIST_WIN="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "$COMPRESSOR" || -z "$INPUT_DIR" || -z "$OUTPUT_DIR" ]]; then
    echo "Usage: $0 --compressor PATH --input DIR --output DIR [--gamelist WIN_PATH]" >&2
    exit 1
fi

echo "Compressor: $COMPRESSOR"
echo "Input:      $INPUT_DIR"
echo "Output:     $OUTPUT_DIR"
[[ -n "$GAMELIST_WIN" ]] && echo "Gamelist:   $GAMELIST_WIN" || echo "Gamelist:   (update skipped)"
echo ""

# Guard: refuse to run if extraction is still in progress.
# Pattern is built at runtime so the literal string never appears in this process's
# command line (which would cause pgrep to match itself via the bash -c argument).
_guard="7z"; if pgrep -f "${_guard}.*n3ds" > /dev/null 2>&1; then
    echo "ERROR: 7-zip extraction appears to still be running. Wait for it to finish."
    exit 1
fi

errors=()
files=("$INPUT_DIR"/*.3ds)
total=${#files[@]}
idx=0

for f in "${files[@]}"; do
    idx=$(( idx + 1 ))
    name="$(basename "${f%.3ds}")"
    zcci_out="$OUTPUT_DIR/$name.zcci"
    zip_src="$OUTPUT_DIR/$name.zip"

    echo "[$idx/$total] [$(date +%H:%M:%S)] $name"

    "$COMPRESSOR" "$f" "$zcci_out" > /dev/null 2>&1
    compressor_exit=$?

    if [ "$compressor_exit" -eq 0 ] && [ -s "$zcci_out" ]; then
        [ -f "$zip_src" ] && rm "$zip_src"
        rm "$f"
        echo "  OK"
    else
        echo "  ERROR (exit $compressor_exit)"
        [ -f "$zcci_out" ] && rm "$zcci_out"
        errors+=("$name")
    fi
done

echo ""
echo "Done. ${#errors[@]} error(s)."
for e in "${errors[@]}"; do echo "  FAILED: $e"; done

if [ ${#errors[@]} -eq 0 ]; then
    if [[ -n "$GAMELIST_WIN" ]]; then
        echo ""
        echo "Updating gamelist paths .zip -> .zcci ..."
        powershell.exe -command "
            \$content = Get-Content -Path '$GAMELIST_WIN' -Raw -Encoding UTF8
            \$content -replace '\.zip</path>', '.zcci</path>' | Set-Content -Path '$GAMELIST_WIN' -Encoding UTF8 -NoNewline
        "
        echo "Gamelist updated."
    else
        echo ""
        echo "Skipping gamelist update (--gamelist not provided)."
    fi
else
    echo ""
    echo "Skipping gamelist update — fix errors above first."
fi
