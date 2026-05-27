#!/usr/bin/env python3
"""
Extracts PSP DLC zips into PPSSPP's PSP/GAME/<DISC_ID>/ directory.
Each DLC zip must contain a PARAM.PBP with a DISC_ID in its SFO metadata.

Usage: python3 install_psp_dlc.py [PSP_ROM_DIR] [PPSSPP_GAME_DIR]
Defaults:
  PSP_ROM_DIR:      /mnt/d/ROMs/psp
  PPSSPP_GAME_DIR:  /mnt/c/Users/spnar/OneDrive/Documents/PPSSPP/PSP/GAME
"""

import os
import struct
import sys
import zipfile

PSP_ROM_DIR    = sys.argv[1] if len(sys.argv) > 1 else "/mnt/d/ROMs/psp"
PPSSPP_GAME_DIR = sys.argv[2] if len(sys.argv) > 2 else "/mnt/c/Users/spnar/OneDrive/Documents/PPSSPP/PSP/GAME"


def parse_sfo(data):
    magic, ver, key_off, val_off, count = struct.unpack('<4sIIII', data[:20])
    if magic != b'\x00PSF':
        return {}
    result = {}
    for i in range(count):
        e = data[20 + i*16 : 20 + (i+1)*16]
        if len(e) < 16:
            break
        k_off, fmt, val_len, val_max, d_off = struct.unpack('<HHIII', e)
        key = data[key_off + k_off:].split(b'\x00')[0].decode()
        val = data[val_off + d_off : val_off + d_off + val_len]
        if fmt == 0x0204:
            result[key] = val.rstrip(b'\x00').decode('utf-8', errors='replace')
        elif fmt == 0x0404 and len(val) >= 4:
            result[key] = struct.unpack('<I', val[:4])[0]
    return result


def disc_id_from_zip(zf):
    pbp_name = next((n for n in zf.namelist() if n.upper().endswith('PARAM.PBP')), None)
    if not pbp_name:
        return None
    pbp = zf.read(pbp_name)
    if pbp[:4] != b'\x00PBP':
        return None
    offsets = struct.unpack('<8I', pbp[8:40])
    sfo = parse_sfo(pbp[offsets[0]:offsets[1]])
    return sfo.get('DISC_ID')


dlc_zips = sorted(f for f in os.listdir(PSP_ROM_DIR) if '(DLC)' in f and f.endswith('.zip'))
print(f"Found {len(dlc_zips)} DLC zips\n")

done = 0
skipped = 0
failed = 0

for fname in dlc_zips:
    zip_path = os.path.join(PSP_ROM_DIR, fname)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            disc_id = disc_id_from_zip(zf)
            if not disc_id:
                print(f"[FAIL] No DISC_ID: {fname}")
                failed += 1
                continue

            dest_dir = os.path.join(PPSSPP_GAME_DIR, disc_id)
            os.makedirs(dest_dir, exist_ok=True)

            extracted = 0
            for member in zf.namelist():
                if member.upper().endswith('PARAM.PBP'):
                    continue  # skip the metadata file
                out_path = os.path.join(dest_dir, os.path.basename(member))
                if os.path.exists(out_path):
                    continue  # don't overwrite existing files
                with zf.open(member) as src, open(out_path, 'wb') as dst:
                    dst.write(src.read())
                extracted += 1

            if extracted == 0:
                print(f"[SKIP] Already installed ({disc_id}): {fname}")
                skipped += 1
            else:
                print(f"[OK] {disc_id} <- {fname} ({extracted} file(s))")
                done += 1

    except Exception as e:
        print(f"[FAIL] {fname}: {e}")
        failed += 1

print(f"\nDone: {done} installed, {skipped} already present, {failed} failed")
