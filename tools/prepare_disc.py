#!/usr/bin/env python3
"""Rebuild Bomberman Party Edition working disc image from the irregular source dump.

The source dump is NOT MotK-style aligned 2448-from-0. Sync marks are irregular.
Sectors are located by CD sync + MSF→LBA. User data starts at sync+21 (not +24).
This tool:

  1. Maps every sync → LBA via the 3-byte MSF header
  2. Reads 2048-byte user payloads at sync+21
  3. Parses ISO9660 (tolerating mid-sector zero padding / odd dirents)
  4. Extracts SYSTEM.CNF + SLUS_011.89
  5. Writes a standard MODE2/2352 .bin + .cue under bpe/
"""

from __future__ import annotations

import argparse
import hashlib
import os
import struct
import sys

DST_SEC = 2352
USER = 2048
USER_OFF = 21  # dump quirk: user payload at sync+21, not +24
SYNC = bytes([0x00] + [0xFF] * 10 + [0x00])

DEFAULT_SRC = "/mnt/crucial4tb/Emulation/roms/ps/Bomberman Party Edition.iso"
BIN_NAME = "Bomberman Party Edition.bin"
CUE_NAME = "Bomberman Party Edition.cue"

# Standard MODE2 Form1 sector: sync + header + subheader + user + EDC/ECC (zeros OK)
_MODE2_SUBHEADER = bytes([0x00, 0x00, 0x08, 0x00, 0x00, 0x00, 0x08, 0x00])
_EDC_ECC_ZEROS = bytes(280)  # 2352 - 12 - 4 - 8 - 2048


def _bcd_to_int(b: int) -> int:
    return ((b >> 4) * 10) + (b & 0x0F)


def _int_to_bcd(v: int) -> int:
    return ((v // 10) << 4) | (v % 10)


def msf_to_lba(m: int, s: int, f: int) -> int:
    return (_bcd_to_int(m) * 60 + _bcd_to_int(s)) * 75 + _bcd_to_int(f) - 150


def lba_to_msf_bcd(lba: int) -> tuple[int, int, int]:
    abs_frame = lba + 150
    mm = abs_frame // (60 * 75)
    ss = (abs_frame // 75) % 60
    ff = abs_frame % 75
    return _int_to_bcd(mm), _int_to_bcd(ss), _int_to_bcd(ff)


def file_hashes(path: str) -> tuple[str, str, int]:
    h_md5 = hashlib.md5()
    h_sha1 = hashlib.sha1()
    size = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            h_md5.update(chunk)
            h_sha1.update(chunk)
    return h_md5.hexdigest(), h_sha1.hexdigest(), size


def map_syncs(src_path: str) -> dict[int, int]:
    """Return {lba: file_offset_of_sync} for every CD sync in the dump."""
    lba_map: dict[int, int] = {}
    with open(src_path, "rb") as f:
        data = f.read()
    off = 0
    while True:
        j = data.find(SYNC, off)
        if j < 0:
            break
        if j + 16 > len(data):
            break
        m, s, fr = data[j + 12], data[j + 13], data[j + 14]
        lba = msf_to_lba(m, s, fr)
        # First sync wins if duplicates appear.
        if lba not in lba_map:
            lba_map[lba] = j
        off = j + 1
    if not lba_map:
        raise SystemExit("no CD sync marks found in source dump")
    return lba_map


def read_user(data: bytes, lba_map: dict[int, int], lba: int) -> bytes:
    if lba not in lba_map:
        raise KeyError(lba)
    base = lba_map[lba]
    return data[base + USER_OFF : base + USER_OFF + USER]


def parse_root_entries(root: bytes) -> dict[str, tuple[int, int]]:
    """Find files by ISO9660 name (tolerates mid-sector zero padding)."""
    entries: dict[str, tuple[int, int]] = {}
    # Name-anchored scan: locate NAME;1 then back up to the dirent header.
    i = 0
    while i < len(root):
        semi = root.find(b";1", i)
        if semi < 0:
            break
        # Walk back over the filename characters.
        name_end = semi
        name_start = name_end
        while name_start > 0 and root[name_start - 1] not in (0x00, 0x01) and root[name_start - 1] >= 0x20:
            # stop at namelen byte — detected below
            name_start -= 1
            if name_end - name_start > 64:
                break
        # namelen is the byte immediately before the name.
        # Try plausible name starts (ISO name is [A-Z0-9._]).
        for ns in range(max(0, name_end - 32), name_end):
            namelen = name_end + 2 - ns  # include ";1"
            if namelen < 3 or namelen > 37:
                continue
            if ns == 0 or root[ns - 1] != namelen:
                continue
            off = ns - 33
            if off < 0:
                continue
            reclen = root[off]
            if reclen < 34 or off + reclen > len(root):
                continue
            if root[off + 32] != namelen:
                continue
            name = root[ns:name_end].decode("ascii", "replace")
            if not name or name in ("\x00", "\x01"):
                continue
            extent = struct.unpack_from("<I", root, off + 2)[0]
            size = struct.unpack_from("<I", root, off + 10)[0]
            if extent == 0 and size == 0:
                continue
            entries[name] = (extent, size)
            break
        i = semi + 2
    return entries


def extract_file(
    data: bytes, lba_map: dict[int, int], extent: int, size: int
) -> bytes:
    out = bytearray()
    rem = size
    lba = extent
    while rem > 0:
        sector = read_user(data, lba_map, lba)
        take = min(USER, rem)
        out += sector[:take]
        rem -= take
        lba += 1
    return bytes(out)


def build_mode2_sector(lba: int, user: bytes) -> bytes:
    if len(user) != USER:
        user = user.ljust(USER, b"\x00")[:USER]
    mm, ss, ff = lba_to_msf_bcd(lba)
    header = bytes([mm, ss, ff, 0x02])  # MODE2
    return SYNC + header + _MODE2_SUBHEADER + user + _EDC_ECC_ZEROS


def write_bin(
    data: bytes, lba_map: dict[int, int], bin_path: str
) -> tuple[int, int]:
    max_lba = max(l for l in lba_map if l >= 0)
    n = max_lba + 1
    missing = 0
    with open(bin_path, "wb") as out:
        for lba in range(n):
            if lba in lba_map:
                user = read_user(data, lba_map, lba)
            else:
                user = bytes(USER)
                missing += 1
            out.write(build_mode2_sector(lba, user))
    return n, missing


def write_cue(out_dir: str) -> None:
    cue_path = os.path.join(out_dir, CUE_NAME)
    with open(cue_path, "w", encoding="utf-8") as c:
        c.write(f'FILE "{BIN_NAME}" BINARY\n')
        c.write("  TRACK 01 MODE2/2352\n")
        c.write("    INDEX 01 00:00:00\n")
    print(f"wrote {cue_path}")


def main() -> int:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "source",
        nargs="?",
        default=DEFAULT_SRC,
        help="path to the irregular source dump",
    )
    ap.add_argument(
        "--out-dir",
        default=os.path.join(repo_root, "bpe"),
        help="output directory for bin/cue/EXE (default: <repo>/bpe)",
    )
    args = ap.parse_args()

    if not os.path.isfile(args.source):
        print(f"source not found: {args.source}", file=sys.stderr)
        return 1

    os.makedirs(args.out_dir, exist_ok=True)

    src_md5, src_sha1, src_size = file_hashes(args.source)
    print(f"source: {args.source}")
    print(f"  size  {src_size}")
    print(f"  md5   {src_md5}")
    print(f"  sha1  {src_sha1}")

    print("mapping sync → LBA …")
    with open(args.source, "rb") as f:
        data = f.read()
    lba_map = map_syncs(args.source)
    data_lbas = [l for l in lba_map if l >= 0]
    print(
        f"  mapped {len(lba_map)} syncs "
        f"(data LBA {min(data_lbas)}..{max(data_lbas)})"
    )

    pvd = read_user(data, lba_map, 16)
    if pvd[1:6] != b"CD001":
        raise SystemExit("PVD not found at LBA 16 (expected CD001 at user+1)")
    root_extent = struct.unpack_from("<I", pvd, 158)[0]
    root_size = struct.unpack_from("<I", pvd, 166)[0]
    root = bytearray()
    for i in range((root_size + USER - 1) // USER):
        root += read_user(data, lba_map, root_extent + i)
    root = bytes(root[:root_size]) if root_size <= len(root) else bytes(root)

    entries = parse_root_entries(root)
    print(f"  root entries: {sorted(entries)}")
    for need in ("SYSTEM.CNF", "SLUS_011.89"):
        if need not in entries:
            raise SystemExit(f"missing {need} on disc")

    for name in ("SYSTEM.CNF", "SLUS_011.89"):
        extent, size = entries[name]
        blob = extract_file(data, lba_map, extent, size)
        out_path = os.path.join(args.out_dir, name)
        with open(out_path, "wb") as out:
            out.write(blob)
        print(f"wrote {out_path} ({len(blob)} bytes, LBA {extent})")
        if name == "SLUS_011.89" and blob[:8] != b"PS-X EXE":
            raise SystemExit("SLUS_011.89 is not a PS-X EXE")

    bin_path = os.path.join(args.out_dir, BIN_NAME)
    print(f"writing {bin_path} …")
    n_sec, missing = write_bin(data, lba_map, bin_path)
    print(f"  {n_sec} sectors ({n_sec * DST_SEC} bytes), {missing} gap sectors zero-filled")
    write_cue(args.out_dir)

    bin_md5, bin_sha1, bin_size = file_hashes(bin_path)
    print(f"working bin:")
    print(f"  size  {bin_size}")
    print(f"  md5   {bin_md5}")
    print(f"  sha1  {bin_sha1}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
