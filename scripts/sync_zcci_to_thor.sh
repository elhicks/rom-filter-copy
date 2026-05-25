#!/usr/bin/env bash
# For each .zip in the Thor n3ds ROM dir, copy the matching .zcci from the
# source collection and delete the .zip. Skips any ROM not found in source.
#
# Usage: sync_zcci_to_thor.sh --source DIR --thor-roms WIN_PATH
set -uo pipefail

SOURCE_DIR=""
THOR_ROMS_WIN=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)     SOURCE_DIR="$2";    shift 2 ;;
        --thor-roms)  THOR_ROMS_WIN="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "$SOURCE_DIR" || -z "$THOR_ROMS_WIN" ]]; then
    echo "Usage: $0 --source DIR --thor-roms WIN_PATH" >&2
    exit 1
fi

# Derive Windows-style path for PowerShell (e.g. /mnt/f/ROMs -> F:\ROMs)
_rel="${SOURCE_DIR#/mnt/}"
_letter="${_rel:0:1}"; _letter="${_letter^^}"
_rest="${_rel:2}"
SOURCE_WIN="${_letter}:\\${_rest//\//\\}"

echo "Source: $SOURCE_DIR  ($SOURCE_WIN)"
echo "Thor:   $THOR_ROMS_WIN"
echo ""

errors=()
skipped=()

mapfile -t zips < <(powershell.exe -command "
    Get-ChildItem '$THOR_ROMS_WIN\*.zip' | ForEach-Object { \$_.BaseName }
" | tr -d '\r')

total=${#zips[@]}
idx=0

for name in "${zips[@]}"; do
    idx=$(( idx + 1 ))
    zcci_src="$SOURCE_DIR/$name.zcci"
    zip_win="$THOR_ROMS_WIN\\$name.zip"

    echo "[$idx/$total] $name"

    if [[ ! -f "$zcci_src" ]]; then
        echo "  SKIP (no .zcci in source)"
        skipped+=("$name")
        continue
    fi

    # Escape ' as '' for PowerShell single-quoted string literals
    name_ps="${name//\'/\'\'}"

    powershell.exe -command "Copy-Item -LiteralPath '$SOURCE_WIN\\$name_ps.zcci' -Destination '$THOR_ROMS_WIN\\$name_ps.zcci'"
    copy_exit=$?

    if [[ $copy_exit -eq 0 ]]; then
        powershell.exe -command "Remove-Item -LiteralPath '$THOR_ROMS_WIN\\$name_ps.zip'"
        echo "  OK"
    else
        echo "  ERROR (copy exit $copy_exit)"
        errors+=("$name")
    fi
done

echo ""
echo "Done. ${#errors[@]} error(s), ${#skipped[@]} skipped."
for e in "${errors[@]}"; do echo "  FAILED:  $e"; done
for s in "${skipped[@]}"; do echo "  SKIPPED: $s"; done
