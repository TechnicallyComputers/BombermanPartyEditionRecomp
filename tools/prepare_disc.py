#!/usr/bin/env python3
"""Install Redump Bomberman Party Edition (USA) into bpe/ and extract the boot EXE.

Source is a verified Redump MODE2/2352 image (SLUS-01189). No sector rebuild and
no MotK-shaped EXE retargets — the previous irregular dump was corrupt.

  1. Copies .bin + writes a matching .cue under bpe/
  2. Parses ISO9660 from standard MODE2 Form1 user data (sync+24)
  3. Extracts SYSTEM.CNF + SLUS_011.89 unchanged
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import struct
import sys

DST_SEC = 2352
USER = 2048
USER_OFF = 24  # standard MODE2 Form1
SYNC = bytes([0x00] + [0xFF] * 10 + [0x00])

DEFAULT_SRC_DIR = (
    "/mnt/crucial4tb/Emulation/roms/ps/Bomberman - Party Edition (USA)"
)
DEFAULT_SRC_BIN = os.path.join(
    DEFAULT_SRC_DIR, "Bomberman - Party Edition (USA).bin"
)
BIN_NAME = "Bomberman Party Edition.bin"
CUE_NAME = "Bomberman Party Edition.cue"

# http://redump.org/disc/10806/
REDUMP_SIZE = 660770880
REDUMP_MD5 = "e0ceba6e448677f3d938b1dd176be3af"
REDUMP_SHA1 = "53a509dbe859f773856f26d966f5edacbc701b4e"


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


def read_user(data: bytes, lba: int) -> bytes:
    off = lba * DST_SEC
    if off + DST_SEC > len(data):
        raise KeyError(lba)
    if data[off : off + 12] != SYNC:
        raise SystemExit(f"bad sync at LBA {lba} (offset {off})")
    return data[off + USER_OFF : off + USER_OFF + USER]


def parse_root_entries(root: bytes) -> dict[str, tuple[int, int]]:
    entries: dict[str, tuple[int, int]] = {}
    i = 0
    while i < len(root):
        reclen = root[i]
        if reclen == 0:
            i = ((i // USER) + 1) * USER
            if i >= len(root):
                break
            continue
        if i + reclen > len(root):
            break
        extent = struct.unpack_from("<I", root, i + 2)[0]
        size = struct.unpack_from("<I", root, i + 10)[0]
        namelen = root[i + 32]
        name = root[i + 33 : i + 33 + namelen]
        if b";" in name:
            name = name.split(b";")[0]
        if name not in (b"\x00", b"\x01"):
            entries[name.decode("ascii", "replace")] = (extent, size)
        i += reclen
    return entries


def extract_file(data: bytes, extent: int, size: int) -> bytes:
    out = bytearray()
    rem = size
    lba = extent
    while rem > 0:
        sector = read_user(data, lba)
        take = min(USER, rem)
        out += sector[:take]
        rem -= take
        lba += 1
    return bytes(out)


def write_cue(out_dir: str) -> None:
    path = os.path.join(out_dir, CUE_NAME)
    with open(path, "w", encoding="ascii", newline="\n") as f:
        f.write(f'FILE "{BIN_NAME}" BINARY\n')
        f.write("  TRACK 01 MODE2/2352\n")
        f.write("    INDEX 01 00:00:00\n")
    print(f"wrote {path}")


def main() -> int:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "source",
        nargs="?",
        default=DEFAULT_SRC_BIN,
        help="path to Redump MODE2/2352 .bin (default: USA Redump path)",
    )
    ap.add_argument(
        "--out-dir",
        default=os.path.join(repo_root, "bpe"),
        help="output directory for bin/cue/EXE (default: <repo>/bpe)",
    )
    ap.add_argument(
        "--skip-hash-check",
        action="store_true",
        help="do not require Redump size/MD5/SHA-1",
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

    if not args.skip_hash_check:
        if (
            src_size != REDUMP_SIZE
            or src_md5 != REDUMP_MD5
            or src_sha1 != REDUMP_SHA1
        ):
            print(
                "source does not match Redump SLUS-01189 "
                f"(expected size={REDUMP_SIZE} md5={REDUMP_MD5} sha1={REDUMP_SHA1})",
                file=sys.stderr,
            )
            print("pass --skip-hash-check to force (not recommended)", file=sys.stderr)
            return 1
        print("  Redump hashes OK")

    if src_size % DST_SEC != 0:
        print(f"source size {src_size} is not a multiple of {DST_SEC}", file=sys.stderr)
        return 1

    with open(args.source, "rb") as f:
        data = f.read()

    pvd = read_user(data, 16)
    if pvd[1:6] != b"CD001":
        raise SystemExit("PVD not found at LBA 16 (expected CD001 at user+1)")
    root_extent = struct.unpack_from("<I", pvd, 158)[0]
    root_size = struct.unpack_from("<I", pvd, 166)[0]
    root = bytearray()
    for i in range((root_size + USER - 1) // USER):
        root += read_user(data, root_extent + i)
    root = bytes(root[:root_size])

    entries = parse_root_entries(root)
    print(f"  root entries: {sorted(entries)}")
    for need in ("SYSTEM.CNF", "SLUS_011.89"):
        if need not in entries:
            raise SystemExit(f"missing {need} on disc")

    for name in ("SYSTEM.CNF", "SLUS_011.89"):
        extent, size = entries[name]
        blob = extract_file(data, extent, size)
        if name == "SLUS_011.89":
            if blob[:8] != b"PS-X EXE":
                raise SystemExit("SLUS_011.89 is not a PS-X EXE")
            pc0 = struct.unpack_from("<I", blob, 0x10)[0]
            print(f"  SLUS_011.89 PC0={pc0:#010x} ({len(blob)} bytes, LBA {extent})")
        out_path = os.path.join(args.out_dir, name)
        with open(out_path, "wb") as out:
            out.write(blob)
        print(f"wrote {out_path} ({len(blob)} bytes, LBA {extent})")

    bin_path = os.path.join(args.out_dir, BIN_NAME)
    if os.path.abspath(args.source) != os.path.abspath(bin_path):
        print(f"copying {args.source} → {bin_path}")
        shutil.copy2(args.source, bin_path)
    else:
        print(f"source already at {bin_path}")
    write_cue(args.out_dir)

    bin_md5, bin_sha1, bin_size = file_hashes(bin_path)
    print("working bin:")
    print(f"  size  {bin_size}")
    print(f"  md5   {bin_md5}")
    print(f"  sha1  {bin_sha1}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
