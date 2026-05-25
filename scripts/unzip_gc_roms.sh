#!/usr/bin/env bash
# Unzip all GameCube ROM zips, deleting each archive only after successful extraction.
#
# Usage: unzip_gc_roms.sh [--dir DIR]
#   --dir  Directory containing .zip files (default: /mnt/f/ROMs/gc)
#
# Safety: each zip is tested before extraction; the zip is only deleted if extraction
# succeeds and the output directory is non-empty.
set -uo pipefail

DIR="/mnt/f/ROMs/gc"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dir) DIR="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

mapfile -t zips < <(find "$DIR" -maxdepth 1 -name "*.zip" | sort)
total=${#zips[@]}

if [[ $total -eq 0 ]]; then
    echo "No .zip files found in $DIR"
    exit 0
fi

echo "$total zip(s) to process in $DIR"
echo ""

done_count=0
skip_count=0
error_count=0
errors=()

for zip in "${zips[@]}"; do
    name="$(basename "$zip")"
    base="${name%.zip}"
    out_dir="$DIR/$base"
    size=$(du -sh "$zip" 2>/dev/null | cut -f1)
    idx=$(( done_count + skip_count + error_count + 1 ))

    echo "[$idx/$total] $name  ($size)"

    # Already extracted — just clean up the zip.
    if [[ -d "$out_dir" ]]; then
        echo "  Already extracted — deleting zip."
        rm "$zip"
        done_count=$(( done_count + 1 ))
        continue
    fi

    # Test the zip before extracting.
    if ! python3 -m zipfile -t "$zip" > /dev/null 2>&1; then
        echo "  ERROR: zip failed integrity test — skipping."
        errors+=("$name (bad zip)")
        error_count=$(( error_count + 1 ))
        continue
    fi

    # Extract.
    if python3 -m zipfile -e "$zip" "$out_dir" > /dev/null 2>&1; then
        # Verify something was actually written.
        if [[ -d "$out_dir" ]] && [[ -n "$(ls -A "$out_dir" 2>/dev/null)" ]]; then
            rm "$zip"
            echo "  OK"
            done_count=$(( done_count + 1 ))
        else
            echo "  ERROR: output directory empty after extraction — zip kept."
            [[ -d "$out_dir" ]] && rm -rf "$out_dir"
            errors+=("$name (empty output)")
            error_count=$(( error_count + 1 ))
        fi
    else
        echo "  ERROR: extraction failed — zip kept, cleaning up partial output."
        [[ -d "$out_dir" ]] && rm -rf "$out_dir"
        errors+=("$name (extraction failed)")
        error_count=$(( error_count + 1 ))
    fi
done

echo ""
echo "Done. $done_count extracted, $skip_count skipped, $error_count error(s)."
for e in "${errors[@]}"; do echo "  FAILED: $e"; done
