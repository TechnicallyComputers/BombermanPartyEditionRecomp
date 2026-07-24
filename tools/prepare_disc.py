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


# Retail SLUS-01189 boot linkage is off by 0x60 in two places (confirmed on
# the source dump, user_off=21). MotK's CRT layout is the template:
#
#   1) Header PC0 is 0x800785D8 — a 16-insn leaf that `jr $ra` → SystemHalt(908).
#      Real CRT (BSS clear + jal init/main) starts at 0x80078638.
#   2) CRT `jal main` targets 0x80012ED8 (printf format rodata), not the first
#      real prologue at 0x80012F38.
#   3) main's first `jal` targets 0x80078680 (mid-CRT, still contains `jal main`)
#      instead of 0x800786E0 (post-`break` helper, MotK's 0x80065AEC analog).
#      That recurses CRT→main→CRT until SP walks into code and pc collapses to 0.
_SLUS_01189_BAD_PC0 = 0x800785D8
_SLUS_01189_CRT_PC0 = 0x80078638
# CRT sets $gp late (after BSS clear). Dump header gp0=0, so any IRQ during
# the clear saves/restores gp=0 and later gp-relative malloc slots (CD buffer
# at gp+0xBD8) never stick — CD init then CdSends with a NULL TOC base.
# BIOS LoadExe seeds $gp from the EXE header before pc0; match CRT's value.
_SLUS_01189_CRT_GP0 = 0x800926DC

# $gp force plants in the CdSend prologue zero hole (skipped by the existing
# j 6A13C bridge). CRT sets $gp only after BSS clear; IRQ/context restore can
# reload a gp=0 TCB/jmpbuf and then gp-relative CD buffer slots never stick.
# CdSend prologue jumps 6A0DC → 6A13C, so 6A0E4..6A138 is a dead hole (22 words).
# Layout:
#   6A0E4 force_gp + TCB publish (10)
#   6A10C abs sw gp+0xBD8 (6)
#   6A124 CRT BSS trampoline (6)
_SLUS_01189_FORCE_GP = 0x8006A0E4
_SLUS_01189_FORCE_GP_WORDS = (
    0x3C1C8009,  # lui   gp, 0x8009
    0x279C26DC,  # addiu gp, gp, 0x26DC
    0x8C080108,  # lw    t0, 0x0108($zero)  ; TCBH
    0x11000004,  # beq   t0, $zero, +4
    0x8D080000,  # lw    t0, 0(t0)
    0x11000002,  # beq   t0, $zero, +2
    0x00000000,  # nop
    0xAD1C0078,  # sw    gp, 0x78(t0)
    0x03E00008,  # jr    ra
    0x00000000,  # nop
)
_SLUS_01189_ABS_CD_BUF_SW = 0x8006A10C
_SLUS_01189_ABS_CD_BUF_SW_WORDS = (
    0x3C018009,  # lui   at, 0x8009
    0xAC2232B4,  # sw    v0, 0x32B4(at)
    0x3C1F8003,  # lui   ra, 0x8003
    0x37FFAF9C,  # ori   ra, ra, 0xAF9C
    0x08012E7E,  # j     0x8004B9F8
    0x02002821,  # addu  a1, s0, $zero
)
_SLUS_01189_FORCE_GP_CRT = 0x8006A124
_SLUS_01189_FORCE_GP_CRT_WORDS = (
    0x3C028009,  # lui   v0, 0x8009
    0x24423240,  # addiu v0, v0, 0x3240
    0x3C038010,  # lui   v1, 0x8010
    0x24639650,  # addiu v1, v1, -27056
    0x0801E192,  # j     0x80078648
    0x00000000,  # nop
)
# CRT entry: jal force_gp (set $gp + TCB), then j BSS trampoline.
_SLUS_01189_CRT_GP_ENTRY_WORDS = (
    0x0C01A839,  # jal   0x8006A0E4
    0x00000000,  # nop
    0x0801A849,  # j     0x8006A124
    0x00000000,  # nop
)
_SLUS_01189_CD_BUF_SW_SITE = 0x8003AF90
_SLUS_01189_TEXT_BASE = 0x80010000
# (jal site VA, bad target, good target, label)
_SLUS_01189_JAL_FIXES = (
    (0x800786D4, 0x80012ED8, 0x80012F38, "CRT jal main"),
    (0x80012F40, 0x80078680, 0x800786E0, "main jal once-ctors"),
    # Companion once-flag teardown (MotK-shaped entry at 78750). Dump jals
    # mid-prologue at 78768 → epilogue pops ra from an unframed stack
    # (BIOS EXE header addr A000B870) → PC=0 after InitHeap.
    (0x80012F60, 0x80078768, 0x80078750, "main jal once-flag/dtors"),
    # Next main jal must hit the A0:0x39 thunk at 0x800787B8. Dump lands on
    # the preceding nop (delay of once-flag's jr $ra) and falls into jr $t2
    # with a stale $t2 → PC=0 after the license GPU burst.
    (0x80012F68, 0x800787B4, 0x800787B8, "main jal A0:39 thunk"),
    # After InitHeap, main must call the pad/card init at 0x80057F50.
    # Dump jals mid-body 0x80057EF0 (li v0,2 / j status-poll) with no
    # prologue → stack smash / PC=0. Epilogue of 57F50 is restored separately.
    (0x80012F70, 0x80057EF0, 0x80057F50, "main jal pad/card init"),
    # Pad/card init's first jal is InitCARD (0x80081758), which installs the
    # card callback table at 0x80092634.. Before that, card-open (81340) jalrs
    # a NULL slot → PC=0. Dump jals mid card-open at 816F8; a prior plant that
    # retargeted to 81340 is also accepted and upgraded to InitCARD.
    (0x80057F64, 0x800816F8, 0x80081758, "padinit jal InitCARD"),
    (0x80057F64, 0x80081340, 0x80081758, "padinit jal was card-open"),
    # InitPAD follows; dump jals 4 words before the real prologue.
    (0x80057F6C, 0x8007F278, 0x8007F288, "padinit jal InitPAD"),
    # Second main call is phase2 (was mid-body 57FEC); enter trampoline in the
    # 57E00 nop hole that rebuilds s0/v0 seeds then joins 57FD8.
    (0x80012FF8, 0x80057FEC, 0x80057E00, "main jal pad phase2"),
    # Gs/libgpu helper: dump jals the epilogue at 0x8007BCCC (lw ra / jr)
    # instead of the -48 prologue at 0x8007BA88. Wrong frame → ra pops as
    # the GCB table address → execute jump-table words as code.
    (0x8007EB44, 0x8007BCCC, 0x8007BA88, "Gs helper jal epilogue"),
    (0x8005B138, 0x8007BCCC, 0x8007BA88, "Gs helper jal epilogue #2"),
    (0x8007D724, 0x8007BCCC, 0x8007BA88, "Gs helper jal epilogue #3"),
    (0x8007DB90, 0x8007BCCC, 0x8007BA88, "Gs helper jal epilogue #4"),
    (0x8007DBCC, 0x8007BCCC, 0x8007BA88, "Gs helper jal epilogue #5"),
    # Inside that helper: dump jals the OT clear (returns v0=0) instead of the
    # slot allocator at 0x8007B334, then `j`s the work head forever while s1≠0
    # (nop-hole ate the queue drain). Use the allocator and exit after one pass.
    (0x8007BB7C, 0x8007B2D4, 0x8007B334, "GsOT jal allocator"),
    # Sibling OT-fill block still jals the clear (v0=0) / self-j.
    (0x8007BC68, 0x8007B2D4, 0x8007B334, "GsOT jal allocator #2"),
    (0x8007BED4, 0x8007B2D4, 0x8007B334, "GsOT jal allocator #3"),
    (0x8007BFCC, 0x8007B2D4, 0x8007B334, "GsOT jal allocator #4"),
    # Same helper(s) then jal mid-body of the GPU/queue poll at 0x8007C70C
    # (skips addiu sp,-24 / sw ra). Epilogue still does sp+=24 → stack
    # underflow, ra=0, top-level PC=0 right after the license GPU burst.
    (0x8007BBD0, 0x8007C70C, 0x8007C6C0, "GsOT jal queue-poll entry"),
    (0x8007B580, 0x8007C70C, 0x8007C6C0, "GsOT jal queue-poll #2"),
    (0x8007B76C, 0x8007C70C, 0x8007C6C0, "GsOT jal queue-poll #3"),
    (0x8007B79C, 0x8007C70C, 0x8007C6C0, "GsOT jal queue-poll #4"),
    (0x8007BCB8, 0x8007C70C, 0x8007C6C0, "GsOT jal queue-poll #5"),
    (0x8007C314, 0x8007C70C, 0x8007C6C0, "GsOT jal queue-poll #6"),
    (0x8007C344, 0x8007C70C, 0x8007C6C0, "GsOT jal queue-poll #7"),
    (0x8007ECE4, 0x8007C70C, 0x8007C6C0, "GsOT jal queue-poll #8"),
    # After queue-poll returns 1, cb0/cb3 (and a sibling) must dispatch the
    # ring entry via (cmd=lbu+4, ptr=lw+12). Dump jals mid-epilogue of the
    # GPU-status clear at 0x8007C660 (`sw 0,44(s0)` … `lw ra,20(sp)`). cb0's
    # frame saves ra at 16(sp), so that lw yields 0 → jr ra → top-level PC=0
    # right after the license burst (last_store still at queue-poll C6CC).
    # Real handler is the orphaned (a0,a1) draw-notify at 0x8007D368
    # (andi cmd / jalr F6810 — safe with our cb1/cb2 jr-ra stubs).
    (0x8007B5CC, 0x8007C660, 0x8007D368, "Gs cb jal mid-epi → draw-notify"),
    (0x8007B7E4, 0x8007C660, 0x8007D368, "Gs cb jal mid-epi → draw-notify #2"),
    (0x8007C38C, 0x8007C660, 0x8007D368, "Gs cb jal mid-epi → draw-notify #3"),
    # GsInit (and two siblings) jal 0x8007EB40 — mid-body of the -40 frame
    # helper that starts at 0x8007EAE8 (`sw ra,32(sp)` … `jal BA88` …
    # `lw ra,32(sp)/jr ra/addiu sp,40`). Skipping the prologue means the
    # epilogue pops ra from GsInit's live frame (often 0) → top-level PC=0
    # immediately after the four F6808..F6814 callback stores.
    (0x8007B954, 0x8007EB40, 0x8007EAE8, "GsInit jal mid-epi → helper"),
    (0x8007BA58, 0x8007EB40, 0x8007EAE8, "GsInit jal mid-epi → helper #2"),
    (0x8007DA8C, 0x8007EB40, 0x8007EAE8, "GsInit jal mid-epi → helper #3"),
    # Thin wrapper jals into the shared epilogue tail (addu v0,s2 / lw ra).
    (0x8007BD48, 0x8007BD00, 0x8007BA88, "Gs wrapper jal mid-epi"),
    # GsInit registers draw/vsync callbacks via four setters at 0x8007C73C..
    # Dump jals mid GPU-queue-poll instead; a0 is the callback (7B578/…).
    # Wrong path → stack smash / callbacks never installed → later jalr of a
    # stale pointer lands in the rsin table at 0x80090000 (data-as-jal).
    (0x8007B928, 0x8007C6E8, 0x8007C73C, "GsInit jal callback setter0"),
    (0x8007B934, 0x8007C6F4, 0x8007C748, "GsInit jal callback setter1"),
    (0x8007B940, 0x8007C700, 0x8007C754, "GsInit jal callback setter2"),
    (0x8007B94C, 0x8007C6DC, 0x8007C760, "GsInit jal callback setter3"),
    # Immediately before those setters, GsInit jals 0x8007C4A8 — the delay
    # slot of an inner jal inside a -32 frame, which falls straight into
    # `lw ra,24(sp) / jr ra / addiu sp,32`. With GsInit's own frame still
    # live that pops the *caller's* ra (58B74) and returns, skipping every
    # setter → F6808..F6814 stay 0. Retarget to the adjacent null-checked
    # DrawSync-callback leaf at 0x8007C4DC (safe when the slot is 0).
    (0x8007B91C, 0x8007C4A8, 0x8007C4DC, "GsInit jal mid-epi → cb-check"),
    # Several sites jal the delay-slot `sw` of setter0 (0x8007C744) instead
    # of the `lui at` entry. Alias fallthrough then runs setter1 with a
    # stale $at on the first store.
    (0x8007E728, 0x8007C744, 0x8007C73C, "GsInit-path jal setter0 delay"),
    (0x8007EB00, 0x8007C744, 0x8007C73C, "GsInit-path jal setter0 delay #2"),
    (0x8007EB1C, 0x8007C744, 0x8007C73C, "GsInit-path jal setter0 delay #3"),
    (0x8007F0BC, 0x8007C744, 0x8007C73C, "GsInit-path jal setter0 delay #4"),
    (0x8007F0CC, 0x8007C744, 0x8007C73C, "GsInit-path jal setter0 delay #5"),
    # libetc thunk at 6BC08 has a long nop sled before jalr; under CPS the
    # fallthrough is split across functions with IRQ checks that can clobber
    # $v0 (the latched target). Call startIntr (table+0xC) directly instead.
    (0x80012F48, 0x8006BC08, 0x8006BDFC, "main jal startIntr"),
    # Dump corruption at the post-startIntr setup site (0x8006C4B8):
    #   addiu a0, → 0x80091844 ; jal ??? ; li a1, 8
    # Stock jal target 0x8006C730 is mid-DMACallback (sllv inside the
    # I_MASK update loop at 6C728..6C744) → infinite loop, RA stuck at
    # 6C4D0, license frozen with gp0_writes flat. Args are (ptr, count=8),
    # matching clear_words at 0x8006C790 — not DMACallback (index, value).
    (0x8006C4C8, 0x8006C730, 0x8006C790, "clear_words jal mid-DMACallback"),
    # Also accept a prior mistaken retarget to DMACallback entry.
    (0x8006C4C8, 0x8006C6E4, 0x8006C790, "clear_words jal DMACallback-entry"),
    # Same site's follow-up jal must call libetc+0x8 (set IRQ/DMA callback).
    # MotK's InterruptCallback thunk loads table+0x08; table+0x10 is
    # StopCallback. We previously pointed this at StopCallback via 6BD5C →
    # startIntr armed VBlank then immediately tore IRQs down (flag=0, I_MASK=0).
    (0x8006C4E4, 0x8006BC38, 0x8006C044, "InterruptCallback jal → libetc+8"),
    (0x8006C4E4, 0x8006BD5C, 0x8006C044, "InterruptCallback jal was StopThunk"),
    (0x8006C4E4, 0x8006C18C, 0x8006C044, "InterruptCallback jal was StopCallback"),
    # libgpu: dump left a systematic -0x60 bias on several jal targets
    # (call sites point 0x60 bytes before the real prologue / clear_bytes /
    # GPU_cw A0:0x49 trampoline). ResetGraph then jalrs mid-DrawSync /
    # mid-epilogue and never returns → license hang, eventual PC=0 exit.
    (0x800657B0, 0x8006871C, 0x8006877C, "ResetGraph clear_bytes"),
    (0x800657D0, 0x80068748, 0x800687A8, "ResetGraph GPU_cw"),
    (0x800657D8, 0x80067E38, 0x80067E98, "ResetGraph GPU_reset"),
    (0x80065884, 0x8006871C, 0x8006877C, "ResetGraph clear_bytes #2"),
    (0x80065894, 0x8006871C, 0x8006877C, "ResetGraph clear_bytes #3"),
    (0x80065AD0, 0x8006871C, 0x8006877C, "libgpu clear_bytes"),
    (0x80067F44, 0x8006871C, 0x8006877C, "GPU_reset clear_bytes"),
    (0x80067F58, 0x8006871C, 0x8006877C, "GPU_reset clear_bytes #2"),
    (0x80065544, 0x80065D60, 0x80065DC0, "libgpu jal mid-fn -0x60"),
    (0x800655B8, 0x80065D60, 0x80065DC0, "libgpu jal mid-fn -0x60 #2"),
    (0x8006561C, 0x80065D60, 0x80065DC0, "libgpu jal mid-fn -0x60 #3"),
    (0x800661CC, 0x80066BD4, 0x80066C34, "libgpu jal mid-fn -0x60 #4"),
    (0x80066298, 0x80066BD4, 0x80066C34, "libgpu jal mid-fn -0x60 #5"),
    # After ResetGraph(0), init jals SetGraphDebug. Stock target is ResetGraph
    # path-B (0x800658AC): that path jalrs GCB+0x34 after ResetGraph's
    # clear_bytes(a1=-1) filled the table with 0xFF → DISPATCH FATAL 0xFFFFFFFF
    # (RA=epilogue). MotK calls the real SetGraphDebug prologue at +0x60.
    (0x800585AC, 0x800658AC, 0x8006590C, "SetGraphDebug after ResetGraph"),
    (0x8003B960, 0x800658AC, 0x8006590C, "SetGraphDebug call #2"),
    # SetIMask: dump points at a jr-$ra stub 0x60 before the real
    # lhu/sh I_MASK helper. GPU_reset then never restores I_MASK → VBlank
    # pending forever with mask 0, display stays disabled after ResetGraph.
    (0x80067994, 0x8006BD84, 0x8006BDE4, "SetIMask"),
    (0x80067A3C, 0x8006BD84, 0x8006BDE4, "SetIMask #2"),
    (0x80067B90, 0x8006BD84, 0x8006BDE4, "SetIMask #3"),
    (0x80067C04, 0x8006BD84, 0x8006BDE4, "SetIMask #4"),
    (0x80067DF0, 0x8006BD84, 0x8006BDE4, "SetIMask #5"),
    (0x80067EA8, 0x8006BD84, 0x8006BDE4, "SetIMask GPU_reset entry"),
    (0x80067FBC, 0x8006BD84, 0x8006BDE4, "SetIMask GPU_reset exit"),
    (0x8006819C, 0x8006BD84, 0x8006BDE4, "SetIMask #8"),
    (0x80068218, 0x8006BD84, 0x8006BDE4, "SetIMask #9"),
    # startIntr's DMA-IRQ helper jals 6BC38 with a0=0; MotK calls libetc+0x8
    # there. Our thunk had collapsed 6BC38 → InterruptCallback (wrong for this
    # site) so VBlank never gets armed and I_MASK stays 0 after ResetGraph.
    (0x8006C3D0, 0x8006BC38, 0x8006C044, "startIntr libetc+8"),
    # Same dump address used as set-callback (libetc+0x8) elsewhere — NOT
    # StopCallback at table+0x10 / 0x8006C18C.
    (0x8006B3F0, 0x8006BC38, 0x8006C044, "set-callback via 6BC38"),
    (0x8006B3F0, 0x8006C18C, 0x8006C044, "set-callback was StopCallback"),
    (0x8006B470, 0x8006BC38, 0x8006C044, "set-callback via 6BC38 #2"),
    (0x8006B470, 0x8006C18C, 0x8006C044, "set-callback was StopCallback #2"),
    # startIntr: MotK jals A0:0x13 SysEnqIntRP to enqueue the IRQ element.
    # Dump left a local save-regs leaf at 6C318; without SysEnqIntRP the BIOS
    # exception exit stays broken (RAM vector lui/jr → PC=0 once I_MASK≠0).
    (0x8006BE5C, 0x8006C318, 0x8006BC40, "startIntr SysEnqIntRP"),
    # startIntr: after SysEnq success MotK jals the ISR once (0x8006D04C) to
    # probe/init VBlank state. Dump -0x60 lands on the continuation at 6BE74
    # instead of the ISR at 6BED4 → first VBlank RFE restores a dead context
    # (PC=0 at VSync, EPC 0x8006BA38).
    (0x8006BE6C, 0x8006BE74, 0x8006BED4, "startIntr ISR probe"),
    # SetDefDispEnv: dump points every caller at the mid-fn "bad RECT" error
    # path (0x80065C38) instead of the prologue (0x80065B7C). Init then jalrs
    # null GPU-printf (env+0x44) and never PutDispEnv → display stays off.
    (0x800585D8, 0x80065C38, 0x80065B7C, "SetDefDispEnv"),
    (0x800586AC, 0x80065C38, 0x80065B7C, "SetDefDispEnv #2"),
    (0x8003B2E8, 0x80065C38, 0x80065B7C, "SetDefDispEnv #3"),
    (0x8003B394, 0x80065C38, 0x80065B7C, "SetDefDispEnv #4"),
    (0x8003B5FC, 0x80065C38, 0x80065B7C, "SetDefDispEnv #5"),
    (0x8003B994, 0x80065C38, 0x80065B7C, "SetDefDispEnv #6"),
    (0x8003E8A4, 0x80065C38, 0x80065B7C, "SetDefDispEnv #7"),
    # Post-ResetGraph buffer setup jals the SetDefDispEnv helper at 586CC, but
    # dump applied a +0x68 bias into the helper's zero hole (58734). EPC then
    # sits in nops / orphan jal 40000; GPU stays disabled, gp0 stuck.
    (0x80058178, 0x80058734, 0x800586CC, "init SetDefDispEnv helper"),
    # Flip-display path: a0 = buf+0x5c (DISPENV). Dump -0x60 lands in the
    # PutDrawEnv epilogue (662E4) instead of PutDispEnv (66344).
    (0x8005807C, 0x800662E4, 0x80066344, "PutDispEnv"),
    # SetDispMask: dump -0x60 points every caller at a truncated leaf at
    # 0x80065A1C (stores env flag, returns — DrawSyncCallback printf string)
    # instead of the real prologue at +0x60 that emits GP1 0x03000001.
    # Without it, ResetGraph leaves display disabled and gp0 plateaus after
    # the license burst (VRAM may have content, but disabled=1 forever).
    (0x80012FF0, 0x80065A1C, 0x80065A7C, "SetDispMask"),
    (0x8003B14C, 0x80065A1C, 0x80065A7C, "SetDispMask #2"),
    (0x8003B39C, 0x80065A1C, 0x80065A7C, "SetDispMask #3"),
    (0x8003B968, 0x80065A1C, 0x80065A7C, "SetDispMask #4"),
    (0x8005859C, 0x80065A1C, 0x80065A7C, "SetDispMask #5"),
    (0x80058600, 0x80065A1C, 0x80065A7C, "SetDispMask #6"),
    (0x80058628, 0x80065A1C, 0x80065A7C, "SetDispMask #7"),
    (0x80058648, 0x80065A1C, 0x80065A7C, "SetDispMask #8"),
    # CdSync IRQ poll / callback dispatcher: dump -0x60 jals into CdSend's
    # DRQSTS wait (0x8006A258) instead of the real status/IRQ helper
    # (0x8006A2B8, MotK 0x80068960). Wrong entry skips the lui/lw that loads
    # the CD port pointer → forever spin on status bit 0x40 (EPC 0x8006A26C).
    (0x8006B790, 0x8006A258, 0x8006A2B8, "CdSync IRQ poll"),
    (0x8006A924, 0x8006A258, 0x8006A2B8, "CdSync IRQ poll #2"),
    (0x8006AC04, 0x8006A258, 0x8006A2B8, "CdSync IRQ poll #3"),
    (0x8006B000, 0x8006A258, 0x8006A2B8, "CdSync IRQ poll #4"),
    # CdInit: MotK jals CheckCallback (lhu started-flag). Dump -0x60 lands on
    # the StopCallback thunk at 6BD5C; a later mistaken plant turned that thunk
    # into `j InterruptCallback`, so CdInit called InterruptCallback with
    # leftover a0/a1 (callback-table addrs) → jalr TMR1 (0x1F801110).
    (0x8006A8FC, 0x8006BD5C, 0x8006BDBC, "CdInit CheckCallback"),
    (0x8006ABDC, 0x8006BD5C, 0x8006BDBC, "CdInit CheckCallback #2"),
    # Gs status poll (0x8007C070): dump jals a jr-$ra stub at 0x8007EB98
    # instead of the kick/flush body at +8 (0x8007EBA0). Without the kick,
    # ring status bytes never advance and 0x8007F218's andi/beq spins.
    (0x8007C08C, 0x8007EB98, 0x8007EBA0, "Gs status poll kick"),
    # BA88 / siblings: on immediate GPU-ready complete, dump jals B518 — that
    # is the epilogue of the -120 scratch builder (restores a foreign frame).
    # Retarget to the MotK-shaped F6778 completion leaf at C3A8 so the status
    # poll can FOUND-match the submitted handle.
    (0x8007BC14, 0x8007B518, 0x8007C3A8, "Gs immediate-complete → ring"),
    (0x8007BCFC, 0x8007B518, 0x8007C3A8, "Gs immediate-complete → ring #2"),
    (0x8007C030, 0x8007B518, 0x8007C3A8, "Gs immediate-complete → ring #3"),
    # After bumping F6770, BA88 jals queue-poll. While OT is busy that returns
    # 0 and never reaches BC14 — so C3A8 never ran. Route through the submit
    # trampoline (ring-complete + C6C0) planted in the BA9C hole.
    (0x8007BBD0, 0x8007C6C0, 0x8007BAA4, "Gs submit → ring+poll tramp"),
    (0x8007BCB8, 0x8007C6C0, 0x8007BAA4, "Gs submit → ring+poll tramp #2"),
)

# Absolute j sites with dump-corrupt targets (MotK control-flow matched).
# (site VA, bad target, good target, label)
_SLUS_01189_J_FIXES = (
    # ResetGraph: mode≠0/3 path and epilogue. Stock targets land in the
    # 0x60-byte zero gap / on a delay-slot insn → infinite j loop at 65898.
    (0x8006576C, 0x80065848, 0x800658A8, "ResetGraph path-B"),
    (0x8006577C, 0x80065848, 0x800658A8, "ResetGraph path-B #2"),
    (0x800658A0, 0x80065898, 0x800658F8, "ResetGraph epilogue"),
    # GPU_reset (called from ResetGraph): continue block, not clear loop.
    (0x80067EF0, 0x80067F54, 0x80067FB4, "GPU_reset continue"),
    (0x80067F60, 0x80067F54, 0x80067FB4, "GPU_reset continue #2"),
    # SetDefDispEnv: mode≠1/≠2 (SetGraphDebug 0) must skip validation and
    # return — MotK j's the epilogue. Dump j's into the mode==1 tail checks
    # (bltz/bgtz on stale regs) → "bad RECT", PutDispEnv never runs, GPU
    # stays disabled after ResetGraph.
    (0x80065BB0, 0x80065C28, 0x80065C88, "SetDefDispEnv mode0"),
    # Same function: bad-RECT fallthrough should share the printf/mode2
    # path at 65C4C; dump j's back into mid-validation at 65BEC.
    (0x80065C3C, 0x80065BEC, 0x80065C4C, "SetDefDispEnv badRECT"),
    # CdSync: after copying the result result-FIFO, MotK j's the epilogue
    # (0x800693F4). Dump left a self-j at 0x8006ACD4 (and the sibling at
    # 0x8006AD24) → infinite spin once any CD mode byte is non-zero; GPU stays
    # frozen post-ResetGraph while host frames still tick.
    (0x8006ACD4, 0x8006ACD4, 0x8006AD34, "CdSync epilogue self-j"),
    (0x8006AD24, 0x8006ACD4, 0x8006AD34, "CdSync epilogue via self-j"),
    # CdSync IRQ-callback path: after jalr ready-cb MotK j's back to the
    # status poll (0x800692BC). Dump -0x60 lands mid-printf setup at 6ABA4.
    (0x8006AC74, 0x8006ABA4, 0x8006AC04, "CdSync IRQ poll retry"),
    # Cd IRQ helper (0x8006A2B8): after reading irq_flag MotK j's the debounce
    # reload at +0x64. Dump j's back to the function entry (re-sb index=1).
    (0x8006A304, 0x8006A2BC, 0x8006A31C, "CdIRQ helper debounce"),
    # Same helper: success/error tails MotK j shared epilogue (68EC0). Dump
    # j's into mid-copy at 6A7A8; epilogue itself was lost in a zero gap —
    # plant at IRQ3 dead nops (see _CD_IRQ_EPILOGUE) and retarget.
    (0x8006A6E0, 0x8006A7A8, 0x8006C558, "CdIRQ helper epilogue"),
    (0x8006A760, 0x8006A7A8, 0x8006C558, "CdIRQ helper epilogue #2"),
    (0x8006A7E0, 0x8006A7A8, 0x8006C558, "CdIRQ helper epilogue #3"),
    # CdSend: set-IE bit path must fall into sb at 6A1E8 (MotK j 6B56C).
    # Dump j's back to the s1==1 test → infinite loop when enabling CD IRQ.
    (0x8006A1A8, 0x8006A188, 0x8006A1E8, "CdSend set-IE"),
    # CdSend: DMA-timeout printf return should retry the busy test (MotK
    # j 6B50C). Dump j's into the truncated prologue hole at 6A12C.
    (0x8006A1C8, 0x8006A12C, 0x8006A188, "CdSend timeout retry"),
    # GsOT fill: dump `j`s the work head forever while s1≠0 (nop-hole ate
    # the queue drain). Exit after one memcpy pass (same idea as MotK latch).
    (0x8007BBA4, 0x8007BB50, 0x8007BBAC, "GsOT fill j self → epi"),
    (0x8007BC8C, 0x8007BC38, 0x8007BC94, "GsOT fill #2 j self → epi"),
    (0x8007BF14, 0x8007BEC0, 0x8007BF1C, "GsOT fill #3 j self → epi"),
    # GPU queue-poll (0x8007C6C0): after jal 0x8007C7D0 the dump `j`s back to
    # the `sw ra,16(sp)` at C6CC. That overwrites the saved caller ra with
    # 0x8007C720 (post-jal), so the epilogue either infinite-loops or jr's
    # garbage. Resume at C6D0 (post-save) like a normal poll retry.
    (0x8007C720, 0x8007C6CC, 0x8007C6D0, "Gs queue-poll j skip sw ra"),
)

# LBA 283 of the irregular dump is an all-zero gap. That sector should hold the
# libetc interrupt callback table that MotK ships as initialized .data
# (slot 0x800A55C0 → table 0x800A55A0). BPE's slot is 0x800917F0; without the
# table, main's first lib thunk (`jalr` of word at table+0xC) jumps to VA 0.
# Rebuild the MotK-shaped table from BPE's matching libetc functions.
_SLUS_01189_LIBETC_SLOT = 0x800917F0
_SLUS_01189_LIBETC_TABLE = 0x800917D0
_SLUS_01189_LIBETC_WORDS = (
    0x80012868,  # +0x00 RCS ident "$Id: intr.c…" (MotK same pattern)
    0x00000000,  # +0x04
    0x8006C044,  # +0x08 set IRQ/DMA callback (MotK InterruptCallback thunk target)
    0x8006BDFC,  # +0x0C ResetCallback / startIntr
    0x8006C18C,  # +0x10 StopCallback (clears flag + I_MASK)
    0x00000000,  # +0x14
    0x8006C22C,  # +0x18 RestartCallback
    0x80090768,  # +0x1C callback state
    0x800917D0,  # +0x20 self (slot value)
    0x1F801070,  # +0x24 I_STAT
    0x1F801074,  # +0x28 I_MASK
    0x1F8010F0,  # +0x2C
)

# IRQ3 (CDROM/DMA) handler at 0x8006C504: dump punched a zero hole after a
# corrupt `srl` (0x00001602 instead of 0x00021602). The relocated body still
# exists at 0x8006C598; bridge the hole MotK-style.
_SLUS_01189_IRQ3_BRIDGE = 0x8006C534
_SLUS_01189_IRQ3_BRIDGE_WORDS = (
    0x00021602,  # srl  v0, v0, 16
    0x3051007F,  # andi s1, v0, 0x7F
    0x12200040,  # beq  s1, zero, 0x8006C640
    0x00000000,  # nop
    0x0801B169,  # j    0x8006C5A4
    0x00000000,  # nop
)

# startIntr jals here with a0 = irq-state+0x38 (MotK same). BIOS SysEnqIntRP
# is A0:0x13 (priority, elem*) — the dump delay-slot only sets a0=elem, so a
# thin wrapper fixes (prio=0, elem=a0), installs the libetc ISR, then A0.
# IntRP layout (nocash / ExceptionHandler walk): +0 next, +4 first func
# (MUST be non-NULL or the entry is skipped), +8 second func. An earlier
# plant put the ISR at +8 and left +4 zero → VBlank pending forever,
# VSync counter stuck, RFE → PC=0.
_SLUS_01189_SYSENQ_TRAMP = 0x8006BC40
_SLUS_01189_SYSENQ_ISR = 0x8006BED4
_SLUS_01189_SYSENQ_TRAMP_WORDS = (
    0x00802821,  # addu  a1, a0, zero     ; elem*
    0x24040000,  # addiu a0, zero, 0      ; priority 0
    0xACA00000,  # sw    zero, 0(a1)      ; next = 0
    0x3C088007,  # lui   t0, 0x8007
    0x2508BED4,  # addiu t0, t0, -0x412C  ; t0 = 0x8006BED4 (ISR)
    0xACA80004,  # sw    t0, 4(a1)        ; first func = ISR
    0xACA80008,  # sw    t0, 8(a1)        ; second func = ISR (survives later sw)
    0x240A00A0,  # addiu t2, zero, 0xA0
    0x01400008,  # jr    t2
    0x24090013,  # addiu t1, zero, 0x13   ; A0: SysEnqIntRP
)

# 0x800586CC SetDefDispEnv helper: after sh w=480 the dump punched a hole
# (0x0000970e remnant of jal 65C38) through the epilogue, leaving an orphan
# jal 40000 at 58774. Mirror the intact 58660 sibling: jal SetDefDispEnv with
# h=480 still in $v0, then restore/return. (Words after the plant stay zero.)
_SLUS_01189_SETDEF_HELPER = 0x80058714
_SLUS_01189_SETDEF_HELPER_WORDS = (
    0x0C0196DF,  # jal  0x80065B7C  SetDefDispEnv
    0xA7A20016,  # sh   v0, 22(sp)  ; h = 480 (delay)
    0x8FBF0024,  # lw   ra, 36(sp)
    0x8FB20020,  # lw   s2, 32(sp)
    0x8FB1001C,  # lw   s1, 28(sp)
    0x8FB00018,  # lw   s0, 24(sp)
    0x03E00008,  # jr   ra
    0x27BD0028,  # addiu sp, sp, 40
)

# startIntr jals B0:0x19 SetCustomExitFromException(0x800907A0) but never
# fills that jmp_buf's JB_PC (stays 0). ExceptionHandler then does
# `ra = EntryInt->JB_PC; jr ra` → PC=0 on the first VBlank. Use B0:0x18
# SetDefaultExitFromException so EntryInt keeps DefaultEntryInt
# (JB_PC=ReturnFromException). MotK also jals B0:19 — their jmp_buf is
# initialized elsewhere; our dump never does.
_SLUS_01189_SET_EXIT_THUNK = 0x8006C310
_SLUS_01189_SET_EXIT_THUNK_WORD = 0x24090018  # addiu t1, zero, 0x18

# libcd mode word (MotK 0x800923A4) + CdSync/CdReady callbacks / CdInit
# clears (MotK 0x80091FA0..). Dump repeated CDROM MMIO over 0x800903C8..E8, so
# mode reads as 0x1F801802 and CdInit's zeroed slots stay as ports. CdInit
# also clears +0x0C/+0x10 (903E4/E8).
_SLUS_01189_CD_MODE_BSS = 0x800903C8
_SLUS_01189_CD_MODE_BSS_WORDS = (
    0x00000000,  # 903C8 mode
    0x00000000,  # 903CC
    0x00000000,  # 903D0 (pad before callbacks)
    0x00000000,  # 903D4
    0x00000000,  # 903D8 CdSync cb
    0x00000000,  # 903DC CdReady cb
    0x00000000,  # 903E0
    0x00000000,  # 903E4 CdInit clear
    0x00000000,  # 903E8 CdInit clear
)

# Back-compat names used by older patch sites in this file.
_SLUS_01189_CD_CALLBACKS = 0x800903D8
_SLUS_01189_CD_CALLBACK_WORDS = (0x00000000, 0x00000000)

# libcd HW pointer table after the CDROM status slot (0x80090380). MotK lays
# out DMA/IRQ regs at status+0x10 (0x1F801018…0x1F801074). The dump kept
# repeating 0x1F801800–803 there, so CdSend's DPCR/DICR loads (90398/9039c)
# write DMA enable to CDROM ports and spin forever on status bit 0x40.
# Stop before the mode/callback island at 903C8.
_SLUS_01189_CD_HW_TABLE = 0x80090390
_SLUS_01189_CD_HW_TABLE_WORDS = (
    0x1F801018,  # +0x10
    0x1F801020,  # +0x14
    0x1F8010F0,  # +0x18 DPCR  (CdSend)
    0x1F8010F4,  # +0x1C DICR  (CdSend)
    0x1F801098,  # +0x20
    0x1F801090,  # +0x24
    0x1F8010A8,  # +0x28
    0x1F8010A0,  # +0x2C
    0x1F8010B8,  # +0x30
    0x1F8010B0,  # +0x34
    0x1F8010D8,  # +0x38
    0x1F8010D0,  # +0x3C
    0x1F801070,  # +0x40 I_STAT
    0x1F801074,  # +0x44 I_MASK
)

# CdSync timing helper (0x8006B9D8) loads GPUSTAT + TMR1 via a 3-word pointer
# table (MotK 0x800A4528: 1F801814 / 1F801110 / 0). Dump left 906F8 NULL and
# 906FC.. as CDROM ports — CdSync then derefs NULL / compares against CDROM
# status and returns garbage mode bytes into the CD-init stack.
_SLUS_01189_CDSYNC_TIMERS = 0x800906F8
_SLUS_01189_CDSYNC_TIMER_WORDS = (
    0x1F801814,  # GPUSTAT
    0x1F801110,  # TMR1_COUNT
    0x00000000,
)

# Status/IRQ helper at 0x8006A2B8 loads CDROM index via 0x8009069C
# (`lw v1, 0x069C(v1)`). MotK ships 0x80092264.. as 1F801800..803 (+020).
# Dump left these NULL; without the plant the fixed jal still writes through
# NULL / spins. CdInit would fill them later — plant MotK .data so early
# CdSync IRQ polls (from ResetGraph / startIntr) work before CdInit runs.
_SLUS_01189_CD_STATUS_PORTS = 0x8009069C
_SLUS_01189_CD_STATUS_PORT_WORDS = (
    0x1F801800,  # index/status
    0x1F801801,  # command / response / interrupt
    0x1F801802,  # parameter / data
    0x1F801803,  # interrupt enable / flags
    0x1F801020,  # MotK +0x10 companion
)

# VSync HSync-rate word at 0x80090704 (MotK 0x800A4534 = 0). Dump spilled
# 0x1F801020 (CD DMA) here; VSync timing then uses a CDROM port as a rate.
_SLUS_01189_VSYNC_HSYNC_RATE = 0x80090704
_SLUS_01189_VSYNC_HSYNC_RATE_WORD = 0x00000000

# StopCallback export thunk at 0x8006BD5C (MotK 0x8006CECC): load libetc
# slot → jalr table+0x10. A mistaken `j InterruptCallback` plant destroyed
# this; restore MotK-shaped body (ends jr $ra before RestartCallback @ BD8C).
_SLUS_01189_STOPCB_THUNK = 0x8006BD5C
_SLUS_01189_STOPCB_THUNK_WORDS = (
    0x3C028009,  # lui   v0, 0x8009
    0x8C4217F0,  # lw    v0, 0x17F0(v0)  ; libetc slot
    0x27BDFFE8,  # addiu sp, sp, -24
    0xAFBF0010,  # sw    ra, 0x10(sp)
    0x8C420010,  # lw    v0, 0x10(v0)    ; table+0x10 StopCallback
    0x00000000,  # nop
    0x0040F809,  # jalr  v0
    0x00000000,  # nop
    0x8FBF0010,  # lw    ra, 0x10(sp)
    0x27BD0018,  # addiu sp, sp, 24
    0x03E00008,  # jr    ra
    0x00000000,  # nop
)

# Cd IRQ helper (0x8006A2B8) lost its MotK epilogue (68EBC/68EC0) in a dump
# gap; the early-exit beq lands mid-CdInit and success j's land mid-copy.
# Plant MotK-shaped stubs in the dead nop run after the IRQ3 bridge jump
# (unreachable once the bridge j's to 6C5A4).
_SLUS_01189_CD_IRQ_EARLY = 0x8006C54C
_SLUS_01189_CD_IRQ_EPILOGUE = 0x8006C558
_SLUS_01189_CD_IRQ_EARLY_WORDS = (
    0x00001021,  # addu  v0, zero, zero
    0x0801B156,  # j     0x8006C558
    0x00000000,  # nop
)
_SLUS_01189_CD_IRQ_EPILOGUE_WORDS = (
    0x8FBF0028,  # lw    ra, 0x28(sp)
    0x8FB10024,  # lw    s1, 0x24(sp)
    0x8FB00020,  # lw    s0, 0x20(sp)
    0x27BD0030,  # addiu sp, sp, 48
    0x03E00008,  # jr    ra
    0x00000000,  # nop
)
# beq v0, zero, early-exit — replaces corrupt beq → 0x8006A804
_SLUS_01189_CD_IRQ_BEQ_SITE = 0x8006A2FC
_SLUS_01189_CD_IRQ_BEQ_WORD = 0x10400893  # offset to 0x8006C54C from 0x8006A300

# CdSync IRQ-poll wrapper (0x8006B764): after helper returns 0 MotK does
# `sb saved_index, (CD index port)` then epilogue. Dump left sll/bne/jalr-0
# garbage at 0x8006B808 → PC=0 once the helper can actually return.
_SLUS_01189_CD_POLL_DONE = 0x8006B808
_SLUS_01189_CD_POLL_DONE_WORDS = (
    0x3C028009,  # lui   v0, 0x8009
    0x8C42069C,  # lw    v0, 0x069C(v0)  ; CD index/status port
    0x00000000,  # nop
    0xA0520000,  # sb    s2, 0(v0)       ; restore saved index bits
    0x8FBF0020,  # lw    ra, 0x20(sp)
    0x8FB3001C,  # lw    s3, 0x1C(sp)
    0x8FB20018,  # lw    s2, 0x18(sp)
    0x8FB10014,  # lw    s1, 0x14(sp)
    0x8FB00010,  # lw    s0, 0x10(sp)
    0x03E00008,  # jr    ra
    0x27BD0028,  # addiu sp, sp, 40
)

# VSync timeout (0x8006BB9C) must jal B0:0x3F std_out_puts (MotK 0x800748c4).
# Dump instead jals 0x8006B7D8 — mid CdSync poll, past the prologue — so the
# poll epilogue restores ra=0 from an unframed stack → PC=0. Plant the MotK
# thunk in nops after the CdIRQ epilogue stub and retarget the jal.
_SLUS_01189_PUTS_THUNK = 0x8006C570
_SLUS_01189_PUTS_THUNK_WORDS = (
    0x240A00B0,  # addiu t2, zero, 0xB0
    0x01400008,  # jr    t2
    0x2409003F,  # addiu t1, zero, 0x3F  ; B0:std_out_puts
    0x00000000,  # nop
)
_SLUS_01189_VSYNC_PUTS_JAL = 0x8006BB9C
_SLUS_01189_VSYNC_PUTS_JAL_WORD = 0x0C01B15C  # jal 0x8006C570

# Pad/card init at 0x80057F50 is two PsyQ phases glued together:
#   phase1 57F50..57FCC — InitCARD/InitPAD/buffer clear (has stack frame)
#   phase2 57FD0..58018 — gp seeds / VSync / StartPAD-like (stackless)
# Dump fell through phase1 into phase2 then a bare `jr ra`, and main later
# re-enters at 57FEC. A single stacked epilogue at the shared tail then did
# `addiu sp,+24` without a matching prologue → sp walks to 0x80800000.
# Split: phase1 returns via planted epilogue; phase2 keeps stackless jr ra;
# main's second jal enters a trampoline that re-seeds s0/v0 then joins 57FD8.
_SLUS_01189_PADINIT_EPI = 0x8006C580
_SLUS_01189_PADINIT_EPI_WORDS = (
    0x8FBF0014,  # lw    ra, 0x14(sp)
    0x8FB00010,  # lw    s0, 0x10(sp)
    0x03E00008,  # jr    ra
    0x27BD0018,  # addiu sp, sp, 24
)
_SLUS_01189_PADINIT_P1_RET = 0x80057FD0
_SLUS_01189_PADINIT_P1_RET_WORDS = (
    0x0801B160,  # j     0x8006C580  ; phase1 return
    0x00000000,  # nop
)
_SLUS_01189_PADINIT_TAIL = 0x80058014
_SLUS_01189_PADINIT_TAIL_WORDS = (
    0x03E00008,  # jr    ra          ; phase2 stackless return
    0x00000000,  # nop
)
# Dead nop run inside the corrupt pad-status helper (57E00..57E5C).
_SLUS_01189_PADINIT_P2_TRAMP = 0x80057E00
_SLUS_01189_PADINIT_P2_TRAMP_WORDS = (
    0x00008021,  # addu  s0, zero, zero
    0x2402FFFC,  # addiu v0, zero, -4
    0x08015FF6,  # j     0x80057FD8
    0xAF820C74,  # sw    v0, 0x0C74(gp)  ; delay (was at 57FD8)
)

# Card-open path at 0x80081580 loads a callback into $v0 then must jalr it.
# Dump left `jalr $zero` → immediate PC=0.
_SLUS_01189_CARD_JALR = 0x80081588
_SLUS_01189_CARD_JALR_WORD = 0x0040F809  # jalr v0

# Gs/libgpu struct clear at 0x8007B158 ends with `jr $zero` instead of `jr $ra`.
_SLUS_01189_GS_CLEAR_JR = 0x8007B180
_SLUS_01189_GS_CLEAR_JR_WORD = 0x03E00008  # jr ra

# GsOT fill loop: dump `j 0x8007BB50` (self) after one memcpy; MotK-shaped
# fallthrough continues at 0x8007BBAC once s1 is latched.
_SLUS_01189_GSOT_LOOP_J = 0x8007BBA4
_SLUS_01189_GSOT_LOOP_J_WORD = 0x0801EEEB  # j 0x8007BBAC

# 0x80028FAC epilogue: dump `lw ra,16(sp)` reads the saved $s0 slot (main's
# s0=0x80090000) then `jr ra` → execute rsin table as code. Restore ra from
# 20(sp) and s0 from 16(sp) MotK-style.
_SLUS_01189_RA_S0_EPI = 0x80029000
_SLUS_01189_RA_S0_EPI_WORDS = (
    0x8FBF0014,  # lw    ra, 0x14(sp)
    0x8FB00010,  # lw    s0, 0x10(sp)
    0x03E00008,  # jr    ra
    0x27BD0018,  # addiu sp, sp, 24
)

# GsInit draw/vsync callbacks. Dump cb1 body at 0x8007C3A8 is smashed
# (jr-ra stub + hole through ~C414); cb2's a0 landed mid-body. Live: after
# BA88 submit, F6770=1 but completion ring F6778 stays all-zero — no non-zero
# sw to F6778 survives in the dump — so 0x8007C070 never FOUND-matches the
# handle and 0x8007F218 andi/beq spins. Plant a MotK-shaped leaf that records
# PsyQ/libgs completion into F6778[F67F8] from the work head at F676C (id at
# +0, status at +4; a0 overrides status when non-zero), bumps F67F8, returns.
# cb0 stays B578; cb3 stays C304; GsInit cb1/cb2 a0 addius keep pointing here.
# BA88's immediate-complete jals (were B518 epilogue) retarget here too.
_SLUS_01189_GS_CB_STUB = 0x8007C3A8
_SLUS_01189_GS_CB_STUB_WORDS = (
    0x3C01800F,  # lui   at, 0x800f
    0x8C22676C,  # lw    v0, 0x676c(at)        ; work cursor
    0x00021840,  # sll   v1, v0, 1
    0x00621821,  # addu  v1, v1, v0
    0x000318C0,  # sll   v1, v1, 3             ; *24
    0x00611821,  # addu  v1, v1, at
    0x8C6666A8,  # lw    a2, 0x66a8(v1)        ; id
    0x10C0000C,  # beq   a2, zero, done
    0x24020002,  # addiu v0, zero, 2           ; DrawSync-complete status
    0x8C2567F8,  # lw    a1, 0x67f8(at)        ; ring head
    0x00053900,  # sll   a3, a1, 4
    0x00273821,  # addu  a3, at, a3
    0xACE66778,  # sw    a2, 0x6778(a3)        ; ring[head].id
    0xA0E2677C,  # sb    v0, 0x677c(a3)        ; ring[head].status = 2
    0x24A50001,  # addiu a1, a1, 1
    0x28A60008,  # slti  a2, a1, 8
    0x14C00002,  # bne   a2, zero, +2
    0x00000000,  # nop
    0x00002821,  # addu  a1, zero, zero
    0xAC2567F8,  # sw    a1, 0x67f8(at)
    0x03E00008,  # jr    ra                    ; done
    0x00000000,  # nop
)

# BA88's post-count `jal C6C0` never reaches the IRQ completion path while the
# GPU OT is still busy. Plant a trampoline in the BA9C nop-hole that records
# completion for the *submitted* id in $s2 (BA88's work id — F676C is not
# advanced, so the C3A8 cursor leaf kept re-posting a stale id), then queue-poll.
_SLUS_01189_GS_SUBMIT_TRAMP = 0x8007BAA4
_SLUS_01189_GS_SUBMIT_TRAMP_WORDS = (
    0x27BDFFE8,  # addiu sp, sp, -24
    0xAFBF0010,  # sw    ra, 0x10(sp)
    0x3C01800F,  # lui   at, 0x800f
    0x8C2567F8,  # lw    a1, 0x67f8(at)        ; ring head
    0x00053900,  # sll   a3, a1, 4
    0x00273821,  # addu  a3, at, a3
    0xACF26778,  # sw    s2, 0x6778(a3)        ; ring[head].id = submit id
    0x24020002,  # addiu v0, zero, 2
    0xA0E2677C,  # sb    v0, 0x677c(a3)        ; status = done
    0x24A50001,  # addiu a1, a1, 1
    0x28A20008,  # slti  v0, a1, 8
    0x14400002,  # bne   v0, zero, +2
    0x00000000,  # nop
    0x00002821,  # addu  a1, zero, zero
    0xAC2567F8,  # sw    a1, 0x67f8(at)
    0x0C01F1B0,  # jal   0x8007C6C0            ; queue-poll
    0x00002021,  # addu  a0, zero, zero
    0x8FBF0010,  # lw    ra, 0x10(sp)
    0x03E00008,  # jr    ra
    0x27BD0018,  # addiu sp, sp, 24
)

# PutDrawEnv (0x80066118): after sw ra,28(sp) the dump has a ~0x60-byte hole
# (0x00000014 + nops) then a corrupt `sw s1,0(sp)`. Plant MotK-shaped
# prologue tail and jump to the intact lbu/debug body at 0x80066194.
_SLUS_01189_PUTDRAWENV_BRIDGE = 0x8006612C
_SLUS_01189_PUTDRAWENV_BRIDGE_WORDS = (
    0xAFB10014,  # sw    s1, 0x14(sp)
    0xAFB00010,  # sw    s0, 0x10(sp)
    0x08019865,  # j     0x80066194
    0x00808821,  # addu  s1, a0, zero  (delay)
)

# Gs queue submit at 0x8007BA88: dump zeroed ~0x60 after saving s1/s4.
# Body resumes at 0x8007BAFC (`sw s3` / `move s3,a0`). Nop fallthrough
# already "works" but skip the hole MotK-style.
_SLUS_01189_GS_BA88_BRIDGE = 0x8007BA9C
_SLUS_01189_GS_BA88_BRIDGE_WORDS = (
    0x0801EEBF,  # j     0x8007BAFC
    0x00000000,  # nop
)

# Post-GsInit wait (0x80058BD8) jals 0x8007D5D8 with (cmd=14, buf). Dump
# truncated the `jal BA88` opcode to `0x0000EEA2` and left a jal 0x80040000
# in the hole → v0 never becomes a work id, 0x80058B98 spins forever, gp0
# sticks at the license plateau. Restore thin wrapper → BA88 → epilogue.
_SLUS_01189_GS_CMD_BRIDGE = 0x8007D5E8
_SLUS_01189_GS_CMD_BRIDGE_WORDS = (
    0x0C01EEA2,  # jal   0x8007BA88
    0x2407FFFF,  # addiu a3, zero, -1
    0x0801F594,  # j     0x8007D650  ; existing lw ra / jr epi
    0x00000000,  # nop
)
_SLUS_01189_GS_CMD_ORPHAN_JAL = 0x8007D648  # jal 0x80040000 in the hole

# Gs status poll forward-search FOUND branch at 0x8007C0B4. Reverse-search
# sibling at 0x8007C150 correctly beq's to the copy-out block at 0x8007C1D8
# (imm 0x21). Dump left the forward beq with imm 0x14 → lands on
# `li v1,7` / `v0=0` return, so a handle sitting at the ring head forever
# reports status 0 and 0x8007F218 retries. Match the reverse target.
_SLUS_01189_GS_POLL_FOUND_BEQ = 0x8007C0B4
_SLUS_01189_GS_POLL_FOUND_BEQ_BAD = 0x10500014  # beq v0,s0,+20 → 0x8007C108
_SLUS_01189_GS_POLL_FOUND_BEQ_WORD = 0x10500048  # beq v0,s0,+72 → 0x8007C1D8

# Forward search also used `sll v0,v1,4` for the ring index, but FOUND copy-out
# at C1D8 indexes via $a2 (set correctly on the reverse path). Mirror reverse:
# keep the stride in $a2 across the beq delay.
_SLUS_01189_GS_POLL_SLL_A2 = 0x8007C0A0
_SLUS_01189_GS_POLL_SLL_A2_BAD = 0x00031100  # sll v0, v1, 4
_SLUS_01189_GS_POLL_SLL_A2_WORD = 0x00033100  # sll a2, v1, 4
_SLUS_01189_GS_POLL_ADDU_A2 = 0x8007C0A8
_SLUS_01189_GS_POLL_ADDU_A2_BAD = 0x00220821  # addu at, at, v0
_SLUS_01189_GS_POLL_ADDU_A2_WORD = 0x00260821  # addu at, at, a2
# Loop back `bne → C0A4` skips C0A0; its delay still did `sll v0,v1,4` so $a2
# stayed stuck at the head index (empty after bump) and FOUND never saw ring[0].
_SLUS_01189_GS_POLL_LOOP_SLL = 0x8007C0D8
_SLUS_01189_GS_POLL_LOOP_SLL_BAD = 0x00031100  # sll v0, v1, 4
_SLUS_01189_GS_POLL_LOOP_SLL_WORD = 0x00033100  # sll a2, v1, 4

# Queue retire entry 0x8007B188 (jal'd after draw-notify): prologue nop-hole;
# intact body resumes at 0x8007B1E8 (F6768 drain). Bridge MotK-style.
_SLUS_01189_GS_RETIRE_BRIDGE = 0x8007B188
_SLUS_01189_GS_RETIRE_BRIDGE_WORDS = (
    0x0801EC7A,  # j     0x8007B1E8
    0x00000000,  # nop
)

_SLUS_01189_GSINIT_CB0 = 0x8007B92C
_SLUS_01189_GSINIT_CB0_WORD = 0x2484B578  # addiu a0,a0,-19080 → 0x8007B578
_SLUS_01189_GSINIT_CB1 = 0x8007B938
_SLUS_01189_GSINIT_CB1_WORD = 0x2484C3A8  # → completion leaf 0x8007C3A8
_SLUS_01189_GSINIT_CB2 = 0x8007B944
_SLUS_01189_GSINIT_CB2_WORD = 0x2484C3A8  # → completion leaf 0x8007C3A8
_SLUS_01189_GSINIT_CB3 = 0x8007B950
_SLUS_01189_GSINIT_CB3_WORD = 0x2484C304  # → real 0x8007C304

# CdControl-class sender at 0x8006A0B0: after move s3,a2 the dump truncated
# `sw s4, 0x28(sp)` to `0x00000028` (syscall!) and zeroed the arg setup.
# MotK 0x8006B49C.. keeps sw s4 / move s4,a3 / a0=0 / sll a1,s0,4 then the
# I_STAT wait. Without it we spin forever on CDROM status bit 0x40.
_SLUS_01189_CD_SEND_BRIDGE = 0x8006A0CC
_SLUS_01189_CD_SEND_BRIDGE_WORDS = (
    0xAFB40028,  # sw    s4, 0x28(sp)
    0x00E0A021,  # addu  s4, a3, zero
    0x00002021,  # addu  a0, zero, zero
    0x00102900,  # sll   a1, s0, 4
    0x0801A84F,  # j     0x8006A13C  ; resume at lui v1,0x100
    0x00000000,  # nop
)

# CdSync body starts at 0x8006AA9C (MotK 0x80069158). Dump zeroed the
# prologue at 0x8006AA3C that the thin wrapper (0x8006B948) and another
# caller still jal — plant j → real body (same pattern as other bridges).
_SLUS_01189_CDSYNC_BRIDGE = 0x8006AA3C
_SLUS_01189_CDSYNC_BRIDGE_WORDS = (
    0x0801AAA7,  # j     0x8006AA9C
    0x00000000,  # nop
)

# CD init / CdRead setup at 0x80069768: dump zero hole 0x800697C8..697FC.
# MotK 0x8006AB28..6AB78 — when BSS flag!=0: ++counter + early-exit with
# mode store; when flag==0: jal CdSync(wrapper) filling sp+0x30, then the
# existing lbu path. Without the jal, 69800 reads uninit stack → wrong CD
# mode → CdSend spins forever on status bit 0x40 (EPC 0x8006A26C).
_SLUS_01189_CD_INIT_HOLE = 0x800697C8
_SLUS_01189_CD_INIT_HOLE_WORDS = (
    # 697C8: counter++ at 0x800F5170 ($at still 0x800F from prior delay)
    0x8C225170,  # lw    v0, 0x5170(at)
    0x00000000,  # nop
    0x24420001,  # addiu v0, v0, 1
    0xAC225170,  # sw    v0, 0x5170(at)
    0x00000000,  # nop
    0x00000000,  # nop
    # 697E0: early exit (beq 0x800F5180==0 lands here; fallthrough after ++)
    0x3C018009,  # lui   at, 0x8009
    0x0801A81D,  # j     0x8006A074
    0xAC2403C8,  # sw    a0, 0x03C8(at)  ; delay
    # 697EC: CdSync path (beq flag==0 / DMA-clear already target here)
    0x0C01AE52,  # jal   0x8006B948  ; CdSync wrapper → 6AA3C → 6AA9C
    0x27A50030,  # addiu a1, sp, 0x30
    0x24030005,  # addiu v1, zero, 5
    0x1043021E,  # beq   v0, v1, 0x8006A074
    0x00000000,  # nop
)

# libgpu GPU env / DrawSync state. The jump table at 0x8008B508.. is intact
# (and 0x8008B548 holds a self-pointer to 0x8008B4A8), but the env body
# 0x8008B4A8..0x8008B4FF is dump garbage. DrawSync then sees mode 0xF0 and
# jalrs 0xF109EFFF; ResetGraph follows other corrupt words → unknown
# dispatch. MotK's matching env is zeros + a few MMIO constants — zero the
# whole prefix up to the GPU version words at 0x8008B500.
_SLUS_01189_LIBGPU_ENV = 0x8008B4A8
_SLUS_01189_LIBGPU_ENV_WORDS = 22  # B4A8..B4FF (stop before version @ B500)

# libgpu jump-table slot at 0x8008B54C points at 0x80064618, but that VA is a
# zero gap in the dump (ResetGraph's `jal` → unknown dispatch / halt). MotK
# uses an A0:0x3F BIOS trampoline there; a plain jr $ra no-op is enough for
# bring-up (GPU debug printf is optional) and avoids A0 re-entry issues from
# game code on this stack.
_SLUS_01189_GPU_PRINTF = 0x80064618
_SLUS_01189_GPU_PRINTF_STUB = (
    0x03E00008,  # jr   $ra
    0x00000000,  # nop
)

# Active GPU/DMA register pointers used by ResetGraph (e.g. 0x8006788C):
#   lw v0, B5F8; sw 0x40000002, 0(v0)   ← must be GP1, not 0 (store-to-null → PC=0)
# A second intact copy already exists at 0x8008B654; the dump left B5F4..B614
# zero/garbage. Same layout as MotK 0x800A5748.
_SLUS_01189_GPU_REGS = 0x8008B5F4
_SLUS_01189_GPU_REG_WORDS = (
    0x1F801810,  # GP0
    0x1F801814,  # GP1
    0x1F8010A0,  # DMA2 MADR
    0x1F8010A4,  # DMA2 BCR
    0x1F8010A8,  # DMA2 CHCR
    0x1F8010E0,  # DMA6 MADR
    0x1F8010E4,  # DMA6 BCR
    0x1F8010E8,  # DMA6 CHCR
    0x1F8010F0,  # DPCR
)

# GCB jump table. Stock dump places it at 0x8008B508, but ResetGraph's
# clear_bytes from GPU_ENV (0x8008B4F0, len 0x80 / +16 len 0x5c) overlays
# that address and fills the table with 0xFF — MotK keeps its table
# (0x800A5630) *before* s0 (0x800A5678). Relocate to 0x8008B570 (first
# word past the clear window) and point env+0x40 there.
_SLUS_01189_GCB_TABLE_OLD = 0x8008B508
_SLUS_01189_GCB_TABLE = 0x8008B570
_SLUS_01189_GCB_WORDS = (
    0x800123A8,
    0x80067904,
    0x80067928,
    0x80067128,
    0x80067814,
    0x8006784C,
    0x8006788C,
    0x800675F4,  # was 0x60-low
    0x80067358,
    0x80067BD8,
    0x80067838,
    0x80067048,
    0x800678D4,
    0x80067E98,  # GPU_reset (was 0x60-low)
    0x80067030,
    0x80067FE8,  # was 0x60-low
    0x8008B570,  # self
    0x80064618,  # GPU printf stub
)


def _jal_word(target: int) -> int:
    return 0x0C000000 | ((target & 0x0FFFFFFC) >> 2)


def _j_word(target: int) -> int:
    return 0x08000000 | ((target & 0x0FFFFFFC) >> 2)


def _patch_jal_in_blob(out: bytearray, site_va: int, bad: int, good: int, label: str) -> None:
    jal_off = 0x800 + (site_va - _SLUS_01189_TEXT_BASE)
    if jal_off + 4 > len(out):
        return
    cur = struct.unpack_from("<I", out, jal_off)[0]
    want = _jal_word(good)
    bad_w = _jal_word(bad)
    if cur == bad_w:
        struct.pack_into("<I", out, jal_off, want)
        print(f"  patched {label} {bad:#010x} -> {good:#010x}")
    elif cur != want:
        print(
            f"  WARN: {label} at {site_va:#010x} is {cur:#010x} "
            f"(expected {bad_w:#010x} or {want:#010x})"
        )


def _patch_j_in_blob(out: bytearray, site_va: int, bad: int, good: int, label: str) -> None:
    j_off = 0x800 + (site_va - _SLUS_01189_TEXT_BASE)
    if j_off + 4 > len(out):
        return
    cur = struct.unpack_from("<I", out, j_off)[0]
    want = _j_word(good)
    bad_w = _j_word(bad)
    if cur == bad_w:
        struct.pack_into("<I", out, j_off, want)
        print(f"  patched {label} j {bad:#010x} -> {good:#010x}")
    elif cur != want:
        print(
            f"  WARN: {label} at {site_va:#010x} is {cur:#010x} "
            f"(expected {bad_w:#010x} or {want:#010x})"
        )


def fix_slus_01189_boot(blob: bytes) -> bytes:
    if len(blob) < 0x20 or blob[:8] != b"PS-X EXE":
        return blob
    out = bytearray(blob)
    pc0 = struct.unpack_from("<I", out, 0x10)[0]
    if pc0 == _SLUS_01189_BAD_PC0:
        struct.pack_into("<I", out, 0x10, _SLUS_01189_CRT_PC0)
        print(
            f"  patched SLUS_011.89 PC0 {_SLUS_01189_BAD_PC0:#010x} -> "
            f"{_SLUS_01189_CRT_PC0:#010x} (CRT / BSS-clear entry)"
        )
    gp0 = struct.unpack_from("<I", out, 0x14)[0]
    if gp0 != _SLUS_01189_CRT_GP0:
        struct.pack_into("<I", out, 0x14, _SLUS_01189_CRT_GP0)
        print(
            f"  patched SLUS_011.89 GP0 {gp0:#010x} -> "
            f"{_SLUS_01189_CRT_GP0:#010x} (CRT $gp; seed before BSS clear)"
        )
    for site, bad, good, label in _SLUS_01189_JAL_FIXES:
        _patch_jal_in_blob(out, site, bad, good, label)
    for site, bad, good, label in _SLUS_01189_J_FIXES:
        _patch_j_in_blob(out, site, bad, good, label)

    tab_off = 0x800 + (_SLUS_01189_LIBETC_TABLE - _SLUS_01189_TEXT_BASE)
    slot_off = 0x800 + (_SLUS_01189_LIBETC_SLOT - _SLUS_01189_TEXT_BASE)
    if slot_off + 4 <= len(out):
        cur_slot = struct.unpack_from("<I", out, slot_off)[0]
        if cur_slot == 0:
            for i, word in enumerate(_SLUS_01189_LIBETC_WORDS):
                struct.pack_into("<I", out, tab_off + i * 4, word)
            print(
                f"  patched libetc table at {_SLUS_01189_LIBETC_TABLE:#010x} "
                f"(slot {_SLUS_01189_LIBETC_SLOT:#010x} was zero — dump gap LBA 283)"
            )
        elif cur_slot != _SLUS_01189_LIBETC_TABLE:
            print(
                f"  WARN: libetc slot {_SLUS_01189_LIBETC_SLOT:#010x} is "
                f"{cur_slot:#010x} (expected 0 or {_SLUS_01189_LIBETC_TABLE:#010x})"
            )

    env_off = 0x800 + (_SLUS_01189_LIBGPU_ENV - _SLUS_01189_TEXT_BASE)
    if env_off + _SLUS_01189_LIBGPU_ENV_WORDS * 4 <= len(out):
        cb = struct.unpack_from("<I", out, env_off + 0x44)[0]  # 0x8008B4EC
        mode_byte = out[env_off + 0x4A]  # 0x8008B4F2
        head = struct.unpack_from("<I", out, env_off)[0]  # 0x8008B4A8
        if (
            (cb & 0xE0000003) != 0x80000000
            or mode_byte >= 2
            or (head != 0 and (head & 0xE0000003) != 0x80000000)
        ):
            for i in range(_SLUS_01189_LIBGPU_ENV_WORDS):
                struct.pack_into("<I", out, env_off + i * 4, 0)
            # GCB pointer at +0x40 (0x8008B4E8): must point past ResetGraph's
            # clear window (see _SLUS_01189_GCB_TABLE).
            struct.pack_into("<I", out, env_off + 0x40, _SLUS_01189_GCB_TABLE)
            # +0x44 (0x8008B4EC): GPU debug printf. MotK points at a real
            # printf; leave our jr-$ra stub so "bad RECT" paths don't jalr 0.
            struct.pack_into("<I", out, env_off + 0x44, _SLUS_01189_GPU_PRINTF)
            print(
                f"  patched libgpu GPU env at {_SLUS_01189_LIBGPU_ENV:#010x} "
                f"(head={head:#010x}, cb={cb:#010x}, mode={mode_byte}; "
                f"GCB→{_SLUS_01189_GCB_TABLE:#010x}, printf→{_SLUS_01189_GPU_PRINTF:#010x})"
            )

    printf_off = 0x800 + (_SLUS_01189_GPU_PRINTF - _SLUS_01189_TEXT_BASE)
    if printf_off + 8 <= len(out):
        cur0 = struct.unpack_from("<I", out, printf_off)[0]
        # Fresh dump is zero; prior prepare may have written the A0:3F stub.
        if cur0 in (0, 0x240A00A0):
            for i, word in enumerate(_SLUS_01189_GPU_PRINTF_STUB):
                struct.pack_into("<I", out, printf_off + i * 4, word)
            # Clear any leftover third word from the old A0:3F stub.
            struct.pack_into("<I", out, printf_off + 8, 0)
            print(
                f"  patched GPU printf stub at {_SLUS_01189_GPU_PRINTF:#010x} "
                f"(jr $ra no-op — was zero/A0 gap)"
            )

    regs_off = 0x800 + (_SLUS_01189_GPU_REGS - _SLUS_01189_TEXT_BASE)
    if regs_off + len(_SLUS_01189_GPU_REG_WORDS) * 4 <= len(out):
        gp1 = struct.unpack_from("<I", out, regs_off + 4)[0]  # B5F8
        if gp1 != 0x1F801814:
            for i, word in enumerate(_SLUS_01189_GPU_REG_WORDS):
                struct.pack_into("<I", out, regs_off + i * 4, word)
            print(
                f"  patched libgpu MMIO pointers at {_SLUS_01189_GPU_REGS:#010x} "
                f"(GP1 was {gp1:#010x} — ResetGraph store-to-null)"
            )

    gcb_off = 0x800 + (_SLUS_01189_GCB_TABLE - _SLUS_01189_TEXT_BASE)
    gcb_ptr_off = 0x800 + (0x8008B4E8 - _SLUS_01189_TEXT_BASE)
    if gcb_off + len(_SLUS_01189_GCB_WORDS) * 4 <= len(out):
        cur_ptr = (
            struct.unpack_from("<I", out, gcb_ptr_off)[0]
            if gcb_ptr_off + 4 <= len(out)
            else 0
        )
        cur0 = struct.unpack_from("<I", out, gcb_off)[0]
        if cur_ptr != _SLUS_01189_GCB_TABLE or cur0 != _SLUS_01189_GCB_WORDS[0]:
            for i, word in enumerate(_SLUS_01189_GCB_WORDS):
                struct.pack_into("<I", out, gcb_off + i * 4, word)
            if gcb_ptr_off + 4 <= len(out):
                struct.pack_into("<I", out, gcb_ptr_off, _SLUS_01189_GCB_TABLE)
            print(
                f"  patched GCB table at {_SLUS_01189_GCB_TABLE:#010x} "
                f"(was ptr={cur_ptr:#010x}; clear-safe vs ResetGraph)"
            )

    bridge_off = 0x800 + (_SLUS_01189_IRQ3_BRIDGE - _SLUS_01189_TEXT_BASE)
    if bridge_off + len(_SLUS_01189_IRQ3_BRIDGE_WORDS) * 4 <= len(out):
        cur = struct.unpack_from("<I", out, bridge_off)[0]
        if cur != _SLUS_01189_IRQ3_BRIDGE_WORDS[0]:
            for i, word in enumerate(_SLUS_01189_IRQ3_BRIDGE_WORDS):
                struct.pack_into("<I", out, bridge_off + i * 4, word)
            print(
                f"  patched IRQ3 handler bridge at {_SLUS_01189_IRQ3_BRIDGE:#010x} "
                f"(was {cur:#010x}; dump hole after corrupt srl)"
            )

    tramp_off = 0x800 + (_SLUS_01189_SYSENQ_TRAMP - _SLUS_01189_TEXT_BASE)
    if tramp_off + len(_SLUS_01189_SYSENQ_TRAMP_WORDS) * 4 <= len(out):
        cur = [
            struct.unpack_from("<I", out, tramp_off + i * 4)[0]
            for i in range(len(_SLUS_01189_SYSENQ_TRAMP_WORDS))
        ]
        if cur != list(_SLUS_01189_SYSENQ_TRAMP_WORDS):
            for i, word in enumerate(_SLUS_01189_SYSENQ_TRAMP_WORDS):
                struct.pack_into("<I", out, tramp_off + i * 4, word)
            was = ", ".join(hex(x) for x in cur[:3])
            print(
                f"  planted SysEnqIntRP trampoline at {_SLUS_01189_SYSENQ_TRAMP:#010x} "
                f"(A0:0x13; was [{was}]...)"
            )

    exit_off = 0x800 + (_SLUS_01189_SET_EXIT_THUNK - _SLUS_01189_TEXT_BASE)
    if exit_off + 4 <= len(out):
        cur = struct.unpack_from("<I", out, exit_off)[0]
        if cur != _SLUS_01189_SET_EXIT_THUNK_WORD:
            struct.pack_into("<I", out, exit_off, _SLUS_01189_SET_EXIT_THUNK_WORD)
            print(
                f"  patched SetExit thunk at {_SLUS_01189_SET_EXIT_THUNK:#010x} "
                f"B0:0x19 → B0:0x18 (was {cur:#010x}; DefaultEntryInt)"
            )

    helper_off = 0x800 + (_SLUS_01189_SETDEF_HELPER - _SLUS_01189_TEXT_BASE)
    if helper_off + len(_SLUS_01189_SETDEF_HELPER_WORDS) * 4 <= len(out):
        cur0 = struct.unpack_from("<I", out, helper_off)[0]
        if cur0 != _SLUS_01189_SETDEF_HELPER_WORDS[0]:
            for i, word in enumerate(_SLUS_01189_SETDEF_HELPER_WORDS):
                struct.pack_into("<I", out, helper_off + i * 4, word)
            # Kill orphan jal 40000 left in the old hole so discovery/dispatch
            # cannot re-enter the 40000 zero gap via that site.
            orphan = 0x800 + (0x80058774 - _SLUS_01189_TEXT_BASE)
            if orphan + 4 <= len(out) and struct.unpack_from("<I", out, orphan)[0] == 0x0C010000:
                struct.pack_into("<I", out, orphan, 0)
            print(
                f"  patched SetDefDispEnv helper epilogue at {_SLUS_01189_SETDEF_HELPER:#010x} "
                f"(was {cur0:#010x}; dump hole after sh w)"
            )

    mode_off = 0x800 + (_SLUS_01189_CD_MODE_BSS - _SLUS_01189_TEXT_BASE)
    if mode_off + len(_SLUS_01189_CD_MODE_BSS_WORDS) * 4 <= len(out):
        mode0 = struct.unpack_from("<I", out, mode_off)[0]
        if mode0 != 0:
            for i, word in enumerate(_SLUS_01189_CD_MODE_BSS_WORDS):
                struct.pack_into("<I", out, mode_off + i * 4, word)
            print(
                f"  patched libcd mode/callback BSS at {_SLUS_01189_CD_MODE_BSS:#010x} "
                f"(mode was {mode0:#010x}; MotK zeros through CdInit clears)"
            )

    hw_off = 0x800 + (_SLUS_01189_CD_HW_TABLE - _SLUS_01189_TEXT_BASE)
    if hw_off + len(_SLUS_01189_CD_HW_TABLE_WORDS) * 4 <= len(out):
        # Detect dump corruption: DPCR slot still holding a CDROM port.
        dpcr = struct.unpack_from("<I", out, hw_off + 8)[0]
        if dpcr != 0x1F8010F0:
            for i, word in enumerate(_SLUS_01189_CD_HW_TABLE_WORDS):
                struct.pack_into("<I", out, hw_off + i * 4, word)
            print(
                f"  patched libcd HW pointer table at {_SLUS_01189_CD_HW_TABLE:#010x} "
                f"(DPCR was {dpcr:#010x}; MotK DMA/IRQ regs after CDROM status)"
            )

    cds_off = 0x800 + (_SLUS_01189_CD_SEND_BRIDGE - _SLUS_01189_TEXT_BASE)
    if cds_off + len(_SLUS_01189_CD_SEND_BRIDGE_WORDS) * 4 <= len(out):
        cur0 = struct.unpack_from("<I", out, cds_off)[0]
        if cur0 != _SLUS_01189_CD_SEND_BRIDGE_WORDS[0]:
            for i, word in enumerate(_SLUS_01189_CD_SEND_BRIDGE_WORDS):
                struct.pack_into("<I", out, cds_off + i * 4, word)
            print(
                f"  patched libcd CdSend prologue at {_SLUS_01189_CD_SEND_BRIDGE:#010x} "
                f"(was {cur0:#010x}; truncated sw s4 / zero hole)"
            )

    fgp_off = 0x800 + (_SLUS_01189_FORCE_GP - _SLUS_01189_TEXT_BASE)
    fgp_crt_off = 0x800 + (_SLUS_01189_FORCE_GP_CRT - _SLUS_01189_TEXT_BASE)
    abs_sw_off = 0x800 + (_SLUS_01189_ABS_CD_BUF_SW - _SLUS_01189_TEXT_BASE)
    crt_off = 0x800 + (_SLUS_01189_CRT_PC0 - _SLUS_01189_TEXT_BASE)
    sw_site_off = 0x800 + (_SLUS_01189_CD_BUF_SW_SITE - _SLUS_01189_TEXT_BASE)
    if fgp_off + len(_SLUS_01189_FORCE_GP_WORDS) * 4 <= len(out):
        fgp1 = struct.unpack_from("<I", out, fgp_off + 4)[0]
        crt0 = struct.unpack_from("<I", out, crt_off)[0]
        if (
            fgp1 != _SLUS_01189_FORCE_GP_WORDS[1]
            or crt0 != _SLUS_01189_CRT_GP_ENTRY_WORDS[0]
        ):
            for i, word in enumerate(_SLUS_01189_FORCE_GP_WORDS):
                struct.pack_into("<I", out, fgp_off + i * 4, word)
            for i, word in enumerate(_SLUS_01189_ABS_CD_BUF_SW_WORDS):
                struct.pack_into("<I", out, abs_sw_off + i * 4, word)
            for i, word in enumerate(_SLUS_01189_FORCE_GP_CRT_WORDS):
                struct.pack_into("<I", out, fgp_crt_off + i * 4, word)
            for i, word in enumerate(_SLUS_01189_CRT_GP_ENTRY_WORDS):
                struct.pack_into("<I", out, crt_off + i * 4, word)
            struct.pack_into(
                "<II",
                out,
                sw_site_off,
                _j_word(_SLUS_01189_ABS_CD_BUF_SW),
                0,
            )
            # Undo any prior abs-lw redirect at 0x8003BA14.
            lw_site_off = 0x800 + (0x8003BA14 - _SLUS_01189_TEXT_BASE)
            struct.pack_into("<II", out, lw_site_off, 0x8F840BD8, 0x0C01A4A2)
            print(
                f"  planted $gp force + TCB publish + abs CD sw at "
                f"{_SLUS_01189_FORCE_GP:#010x}"
            )

    # Context restore at 0x8006C354 must `lw gp, 0x2c(a0)` then continue.
    # An earlier plant replaced that with `jal force_gp`, which clobbers the
    # just-restored $ra and sends every longjmp/thread restore to PC=0 / a
    # tight restore loop after the first VBlank. Restore the MotK/PsyQ insn.
    restore_off = 0x800 + (0x8006C358 - _SLUS_01189_TEXT_BASE)
    if restore_off + 4 <= len(out):
        cur = struct.unpack_from("<I", out, restore_off)[0]
        want_lw_gp = 0x8C9C002C  # lw gp, 0x2c(a0)
        if cur == _jal_word(_SLUS_01189_FORCE_GP) or cur != want_lw_gp:
            if cur != want_lw_gp:
                struct.pack_into("<I", out, restore_off, want_lw_gp)
                print(
                    f"  restored context-restore lw $gp at 0x8006C358 "
                    f"(was {cur:#010x}; jal force_gp clobbered $ra)"
                )

    cdt_off = 0x800 + (_SLUS_01189_CDSYNC_TIMERS - _SLUS_01189_TEXT_BASE)
    if cdt_off + len(_SLUS_01189_CDSYNC_TIMER_WORDS) * 4 <= len(out):
        cur0 = struct.unpack_from("<I", out, cdt_off)[0]
        if cur0 != _SLUS_01189_CDSYNC_TIMER_WORDS[0]:
            for i, word in enumerate(_SLUS_01189_CDSYNC_TIMER_WORDS):
                struct.pack_into("<I", out, cdt_off + i * 4, word)
            print(
                f"  patched libcd CdSync timer ptrs at {_SLUS_01189_CDSYNC_TIMERS:#010x} "
                f"(was {cur0:#010x}; MotK GPUSTAT+TMR1)"
            )

    csp_off = 0x800 + (_SLUS_01189_CD_STATUS_PORTS - _SLUS_01189_TEXT_BASE)
    if csp_off + len(_SLUS_01189_CD_STATUS_PORT_WORDS) * 4 <= len(out):
        cur0 = struct.unpack_from("<I", out, csp_off)[0]
        if cur0 != _SLUS_01189_CD_STATUS_PORT_WORDS[0]:
            for i, word in enumerate(_SLUS_01189_CD_STATUS_PORT_WORDS):
                struct.pack_into("<I", out, csp_off + i * 4, word)
            print(
                f"  patched libcd CD status port ptrs at {_SLUS_01189_CD_STATUS_PORTS:#010x} "
                f"(was {cur0:#010x}; MotK 1F801800.. for 0x8006A2B8)"
            )

    vhs_off = 0x800 + (_SLUS_01189_VSYNC_HSYNC_RATE - _SLUS_01189_TEXT_BASE)
    if vhs_off + 4 <= len(out):
        cur0 = struct.unpack_from("<I", out, vhs_off)[0]
        if cur0 != _SLUS_01189_VSYNC_HSYNC_RATE_WORD:
            struct.pack_into("<I", out, vhs_off, _SLUS_01189_VSYNC_HSYNC_RATE_WORD)
            print(
                f"  patched VSync HSync-rate at {_SLUS_01189_VSYNC_HSYNC_RATE:#010x} "
                f"(was {cur0:#010x}; MotK zero, not CD DMA spill)"
            )

    cdsync_off = 0x800 + (_SLUS_01189_CDSYNC_BRIDGE - _SLUS_01189_TEXT_BASE)
    if cdsync_off + len(_SLUS_01189_CDSYNC_BRIDGE_WORDS) * 4 <= len(out):
        cur0 = struct.unpack_from("<I", out, cdsync_off)[0]
        if cur0 != _SLUS_01189_CDSYNC_BRIDGE_WORDS[0]:
            for i, word in enumerate(_SLUS_01189_CDSYNC_BRIDGE_WORDS):
                struct.pack_into("<I", out, cdsync_off + i * 4, word)
            print(
                f"  patched libcd CdSync bridge at {_SLUS_01189_CDSYNC_BRIDGE:#010x} "
                f"(was {cur0:#010x}; j → 0x8006AA9C)"
            )

    cdinit_off = 0x800 + (_SLUS_01189_CD_INIT_HOLE - _SLUS_01189_TEXT_BASE)
    if cdinit_off + len(_SLUS_01189_CD_INIT_HOLE_WORDS) * 4 <= len(out):
        cur0 = struct.unpack_from("<I", out, cdinit_off)[0]
        if cur0 != _SLUS_01189_CD_INIT_HOLE_WORDS[0]:
            for i, word in enumerate(_SLUS_01189_CD_INIT_HOLE_WORDS):
                struct.pack_into("<I", out, cdinit_off + i * 4, word)
            print(
                f"  patched libcd CD-init hole at {_SLUS_01189_CD_INIT_HOLE:#010x} "
                f"(was {cur0:#010x}; MotK counter++/CdSync/early-exit)"
            )

    # Collapse long nop-sled jalr thunks to direct jumps. Same idea as MotK's
    # short stubs: preserve caller $ra, avoid $v0 latch across CPS splits.
    # Also rewrite the trailing jalr sites — discovery still splits the old
    # sled into mid-function entries (6BC10/6BC80) that IRQ paths can hit.
    thunk_jumps = (
        (0x8006BC08, 0x8006BDFC, "ResetCallback/startIntr thunk"),
        (0x8006BC80, 0x8006BDFC, "startIntr thunk jalr"),
        (0x8006BC98, 0x8006C044, "libetc +0x8 thunk"),
        (0x8006BCB0, 0x8006C044, "libetc +0x8 thunk jalr"),
        # MotK InterruptCallback thunk jalrs table+0x08 (set-callback), not
        # table+0x10 (StopCallback). Point mid-sled / export stubs at 6C044.
        # Do NOT plant over 6BD5C/6BD74/6BD84 — those are the StopCallback
        # export thunk (restored below); SetIMask callers are jal-fixed to BDE4.
        (0x8006BC38, 0x8006C044, "nop-sled mid -> set-callback"),
    )
    for site, dest, label in thunk_jumps:
        off = 0x800 + (site - _SLUS_01189_TEXT_BASE)
        if off + 8 > len(out):
            continue
        cur = struct.unpack_from("<I", out, off)[0]
        want = _j_word(dest)
        # Stock entry is lui v0; stock jalr site is jalr v0 (0x0040F809);
        # mid-sled sites are nops (0); SetIMask stub is jr $ra (0x03E00008).
        if cur in (0x3C028009, 0x0040F809, 0x00000000, 0x03E00008) or (
            cur & 0xFC000000
        ) in (
            0x0C000000,
            0x08000000,
        ):
            if cur != want:
                struct.pack_into("<I", out, off, want)
                struct.pack_into("<I", out, off + 4, 0)
                print(f"  patched {label} at {site:#010x} -> j {dest:#010x}")

    stop_off = 0x800 + (_SLUS_01189_STOPCB_THUNK - _SLUS_01189_TEXT_BASE)
    if stop_off + 4 * len(_SLUS_01189_STOPCB_THUNK_WORDS) <= len(out):
        cur = [
            struct.unpack_from("<I", out, stop_off + i * 4)[0]
            for i in range(len(_SLUS_01189_STOPCB_THUNK_WORDS))
        ]
        if cur != list(_SLUS_01189_STOPCB_THUNK_WORDS):
            for i, word in enumerate(_SLUS_01189_STOPCB_THUNK_WORDS):
                struct.pack_into("<I", out, stop_off + i * 4, word)
            print(
                f"  restored StopCallback thunk at {_SLUS_01189_STOPCB_THUNK:#010x} "
                f"(was [{cur[0]:#010x}, ...]; MotK table+0x10 jalr)"
            )

    early_off = 0x800 + (_SLUS_01189_CD_IRQ_EARLY - _SLUS_01189_TEXT_BASE)
    epi_off = 0x800 + (_SLUS_01189_CD_IRQ_EPILOGUE - _SLUS_01189_TEXT_BASE)
    beq_off = 0x800 + (_SLUS_01189_CD_IRQ_BEQ_SITE - _SLUS_01189_TEXT_BASE)
    if epi_off + 4 * len(_SLUS_01189_CD_IRQ_EPILOGUE_WORDS) <= len(out):
        cur_epi = [
            struct.unpack_from("<I", out, epi_off + i * 4)[0]
            for i in range(len(_SLUS_01189_CD_IRQ_EPILOGUE_WORDS))
        ]
        if cur_epi != list(_SLUS_01189_CD_IRQ_EPILOGUE_WORDS):
            for i, word in enumerate(_SLUS_01189_CD_IRQ_EARLY_WORDS):
                struct.pack_into("<I", out, early_off + i * 4, word)
            for i, word in enumerate(_SLUS_01189_CD_IRQ_EPILOGUE_WORDS):
                struct.pack_into("<I", out, epi_off + i * 4, word)
            print(
                f"  planted CdIRQ helper epilogue at {_SLUS_01189_CD_IRQ_EPILOGUE:#010x} "
                f"(early {_SLUS_01189_CD_IRQ_EARLY:#010x}; MotK 68EBC/68EC0)"
            )
    if beq_off + 4 <= len(out):
        cur_beq = struct.unpack_from("<I", out, beq_off)[0]
        if cur_beq != _SLUS_01189_CD_IRQ_BEQ_WORD:
            struct.pack_into("<I", out, beq_off, _SLUS_01189_CD_IRQ_BEQ_WORD)
            print(
                f"  patched CdIRQ helper early-exit beq at "
                f"{_SLUS_01189_CD_IRQ_BEQ_SITE:#010x} → {_SLUS_01189_CD_IRQ_EARLY:#010x} "
                f"(was {cur_beq:#010x})"
            )

    poll_off = 0x800 + (_SLUS_01189_CD_POLL_DONE - _SLUS_01189_TEXT_BASE)
    if poll_off + 4 * len(_SLUS_01189_CD_POLL_DONE_WORDS) <= len(out):
        cur_poll = [
            struct.unpack_from("<I", out, poll_off + i * 4)[0]
            for i in range(len(_SLUS_01189_CD_POLL_DONE_WORDS))
        ]
        if cur_poll != list(_SLUS_01189_CD_POLL_DONE_WORDS):
            for i, word in enumerate(_SLUS_01189_CD_POLL_DONE_WORDS):
                struct.pack_into("<I", out, poll_off + i * 4, word)
            print(
                f"  restored CdSync IRQ-poll done-path at "
                f"{_SLUS_01189_CD_POLL_DONE:#010x} (was [{cur_poll[0]:#010x}, ...])"
            )

    puts_off = 0x800 + (_SLUS_01189_PUTS_THUNK - _SLUS_01189_TEXT_BASE)
    if puts_off + 4 * len(_SLUS_01189_PUTS_THUNK_WORDS) <= len(out):
        cur_puts = [
            struct.unpack_from("<I", out, puts_off + i * 4)[0]
            for i in range(len(_SLUS_01189_PUTS_THUNK_WORDS))
        ]
        if cur_puts != list(_SLUS_01189_PUTS_THUNK_WORDS):
            for i, word in enumerate(_SLUS_01189_PUTS_THUNK_WORDS):
                struct.pack_into("<I", out, puts_off + i * 4, word)
            print(
                f"  planted B0:3F std_out_puts thunk at "
                f"{_SLUS_01189_PUTS_THUNK:#010x}"
            )
    jal_off = 0x800 + (_SLUS_01189_VSYNC_PUTS_JAL - _SLUS_01189_TEXT_BASE)
    if jal_off + 4 <= len(out):
        cur_jal = struct.unpack_from("<I", out, jal_off)[0]
        if cur_jal != _SLUS_01189_VSYNC_PUTS_JAL_WORD:
            struct.pack_into("<I", out, jal_off, _SLUS_01189_VSYNC_PUTS_JAL_WORD)
            print(
                f"  patched VSync timeout puts jal at "
                f"{_SLUS_01189_VSYNC_PUTS_JAL:#010x} → {_SLUS_01189_PUTS_THUNK:#010x} "
                f"(was {cur_jal:#010x})"
            )

    pe_off = 0x800 + (_SLUS_01189_PADINIT_EPI - _SLUS_01189_TEXT_BASE)
    if pe_off + 4 * len(_SLUS_01189_PADINIT_EPI_WORDS) <= len(out):
        cur_pe = [
            struct.unpack_from("<I", out, pe_off + i * 4)[0]
            for i in range(len(_SLUS_01189_PADINIT_EPI_WORDS))
        ]
        if cur_pe != list(_SLUS_01189_PADINIT_EPI_WORDS):
            for i, word in enumerate(_SLUS_01189_PADINIT_EPI_WORDS):
                struct.pack_into("<I", out, pe_off + i * 4, word)
            print(
                f"  planted pad/card init epilogue at {_SLUS_01189_PADINIT_EPI:#010x}"
            )
    for site, words, label in (
        (_SLUS_01189_PADINIT_P1_RET, _SLUS_01189_PADINIT_P1_RET_WORDS, "padinit phase1 return"),
        (_SLUS_01189_PADINIT_TAIL, _SLUS_01189_PADINIT_TAIL_WORDS, "padinit phase2 jr ra"),
        (_SLUS_01189_PADINIT_P2_TRAMP, _SLUS_01189_PADINIT_P2_TRAMP_WORDS, "padinit phase2 trampoline"),
        (_SLUS_01189_RA_S0_EPI, _SLUS_01189_RA_S0_EPI_WORDS, "28FAC epi ra←s0 slot"),
        (_SLUS_01189_GS_CB_STUB, _SLUS_01189_GS_CB_STUB_WORDS, "Gs cb1/cb2 ring-complete leaf"),
        (
            _SLUS_01189_GS_SUBMIT_TRAMP,
            _SLUS_01189_GS_SUBMIT_TRAMP_WORDS,
            "Gs submit ring+poll trampoline",
        ),
        (
            _SLUS_01189_GS_RETIRE_BRIDGE,
            _SLUS_01189_GS_RETIRE_BRIDGE_WORDS,
            "Gs retire B188 → B1E8",
        ),
        (
            _SLUS_01189_PUTDRAWENV_BRIDGE,
            _SLUS_01189_PUTDRAWENV_BRIDGE_WORDS,
            "PutDrawEnv prologue bridge",
        ),
        (
            _SLUS_01189_GS_BA88_BRIDGE,
            _SLUS_01189_GS_BA88_BRIDGE_WORDS,
            "Gs BA88 prologue bridge",
        ),
        (
            _SLUS_01189_GS_CMD_BRIDGE,
            _SLUS_01189_GS_CMD_BRIDGE_WORDS,
            "Gs cmd wrapper → BA88",
        ),
    ):
        off = 0x800 + (site - _SLUS_01189_TEXT_BASE)
        if off + 4 * len(words) > len(out):
            continue
        cur = [struct.unpack_from("<I", out, off + i * 4)[0] for i in range(len(words))]
        if cur != list(words):
            for i, word in enumerate(words):
                struct.pack_into("<I", out, off + i * 4, word)
            print(f"  planted {label} at {site:#010x}")

    orphan_off = 0x800 + (_SLUS_01189_GS_CMD_ORPHAN_JAL - _SLUS_01189_TEXT_BASE)
    if orphan_off + 4 <= len(out):
        cur = struct.unpack_from("<I", out, orphan_off)[0]
        if cur == 0x0C010000:  # jal 0x80040000
            struct.pack_into("<I", out, orphan_off, 0)
            print(
                f"  cleared orphan jal 0x80040000 at "
                f"{_SLUS_01189_GS_CMD_ORPHAN_JAL:#010x}"
            )

    for site, want, label in (
        (_SLUS_01189_CARD_JALR, _SLUS_01189_CARD_JALR_WORD, "card-open jalr $zero → jalr $v0"),
        (_SLUS_01189_GS_CLEAR_JR, _SLUS_01189_GS_CLEAR_JR_WORD, "Gs clear jr $zero → jr $ra"),
        (_SLUS_01189_GSOT_LOOP_J, _SLUS_01189_GSOT_LOOP_J_WORD, "GsOT fill j self → j epi"),
        (_SLUS_01189_GSINIT_CB0, _SLUS_01189_GSINIT_CB0_WORD, "GsInit cb0 → prologue"),
        (_SLUS_01189_GSINIT_CB1, _SLUS_01189_GSINIT_CB1_WORD, "GsInit cb1 → ring-complete"),
        (_SLUS_01189_GSINIT_CB2, _SLUS_01189_GSINIT_CB2_WORD, "GsInit cb2 → ring-complete"),
        (_SLUS_01189_GSINIT_CB3, _SLUS_01189_GSINIT_CB3_WORD, "GsInit cb3 → real entry"),
        (
            _SLUS_01189_GS_POLL_FOUND_BEQ,
            _SLUS_01189_GS_POLL_FOUND_BEQ_WORD,
            "Gs poll FOUND beq → copy-out",
        ),
        (
            _SLUS_01189_GS_POLL_SLL_A2,
            _SLUS_01189_GS_POLL_SLL_A2_WORD,
            "Gs poll forward sll → a2",
        ),
        (
            _SLUS_01189_GS_POLL_ADDU_A2,
            _SLUS_01189_GS_POLL_ADDU_A2_WORD,
            "Gs poll forward addu → a2",
        ),
        (
            _SLUS_01189_GS_POLL_LOOP_SLL,
            _SLUS_01189_GS_POLL_LOOP_SLL_WORD,
            "Gs poll loop delay sll → a2",
        ),
    ):
        off = 0x800 + (site - _SLUS_01189_TEXT_BASE)
        if off + 4 > len(out):
            continue
        cur = struct.unpack_from("<I", out, off)[0]
        if cur != want:
            # Only rewrite the known-bad FOUND beq (or already-good).
            if site == _SLUS_01189_GS_POLL_FOUND_BEQ and cur not in (
                _SLUS_01189_GS_POLL_FOUND_BEQ_BAD,
                _SLUS_01189_GS_POLL_FOUND_BEQ_WORD,
            ):
                print(
                    f"  WARN: Gs poll FOUND beq at {site:#010x} is {cur:#010x} "
                    f"(expected {_SLUS_01189_GS_POLL_FOUND_BEQ_BAD:#010x} or "
                    f"{_SLUS_01189_GS_POLL_FOUND_BEQ_WORD:#010x})"
                )
                continue
            if site == _SLUS_01189_GS_POLL_SLL_A2 and cur not in (
                _SLUS_01189_GS_POLL_SLL_A2_BAD,
                _SLUS_01189_GS_POLL_SLL_A2_WORD,
            ):
                print(
                    f"  WARN: Gs poll sll at {site:#010x} is {cur:#010x}"
                )
                continue
            if site == _SLUS_01189_GS_POLL_ADDU_A2 and cur not in (
                _SLUS_01189_GS_POLL_ADDU_A2_BAD,
                _SLUS_01189_GS_POLL_ADDU_A2_WORD,
            ):
                print(
                    f"  WARN: Gs poll addu at {site:#010x} is {cur:#010x}"
                )
                continue
            if site == _SLUS_01189_GS_POLL_LOOP_SLL and cur not in (
                _SLUS_01189_GS_POLL_LOOP_SLL_BAD,
                _SLUS_01189_GS_POLL_LOOP_SLL_WORD,
            ):
                print(
                    f"  WARN: Gs poll loop sll at {site:#010x} is {cur:#010x}"
                )
                continue
            struct.pack_into("<I", out, off, want)
            print(f"  patched {label} at {site:#010x} (was {cur:#010x})")
    return bytes(out)


def patch_bin_exe_boot(bin_path: str, exe_lba: int) -> None:
    """Write corrected PC0 + jal retargets into the MODE2/2352 EXE sectors."""
    with open(bin_path, "r+b") as f:
        f.seek(exe_lba * DST_SEC + 24 + 0x10)
        cur = struct.unpack("<I", f.read(4))[0]
        if cur == _SLUS_01189_BAD_PC0 or cur == _SLUS_01189_CRT_PC0:
            f.seek(exe_lba * DST_SEC + 24 + 0x10)
            f.write(struct.pack("<I", _SLUS_01189_CRT_PC0))
            print(
                f"  patched disc EXE header LBA {exe_lba} PC0 -> "
                f"{_SLUS_01189_CRT_PC0:#010x}"
            )
        f.seek(exe_lba * DST_SEC + 24 + 0x14)
        gp0 = struct.unpack("<I", f.read(4))[0]
        if gp0 != _SLUS_01189_CRT_GP0:
            f.seek(exe_lba * DST_SEC + 24 + 0x14)
            f.write(struct.pack("<I", _SLUS_01189_CRT_GP0))
            print(
                f"  patched disc EXE header LBA {exe_lba} GP0 -> "
                f"{_SLUS_01189_CRT_GP0:#010x}"
            )

        for site, bad, good, label in _SLUS_01189_JAL_FIXES:
            jal_file = 0x800 + (site - _SLUS_01189_TEXT_BASE)
            sec = jal_file // USER
            rem = jal_file % USER
            f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
            cur = struct.unpack("<I", f.read(4))[0]
            want = _jal_word(good)
            bad_w = _jal_word(bad)
            if cur == bad_w:
                f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
                f.write(struct.pack("<I", want))
                print(
                    f"  patched disc {label} LBA {exe_lba + sec}+{rem} -> {good:#010x}"
                )

        for site, bad, good, label in _SLUS_01189_J_FIXES:
            j_file = 0x800 + (site - _SLUS_01189_TEXT_BASE)
            sec = j_file // USER
            rem = j_file % USER
            f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
            cur = struct.unpack("<I", f.read(4))[0]
            want = _j_word(good)
            bad_w = _j_word(bad)
            if cur == bad_w:
                f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
                f.write(struct.pack("<I", want))
                print(
                    f"  patched disc {label} LBA {exe_lba + sec}+{rem} "
                    f"j -> {good:#010x}"
                )

        slot_file = 0x800 + (_SLUS_01189_LIBETC_SLOT - _SLUS_01189_TEXT_BASE)
        sec = slot_file // USER
        rem = slot_file % USER
        f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
        cur_slot = struct.unpack("<I", f.read(4))[0]
        if cur_slot == 0:
            tab_file = 0x800 + (_SLUS_01189_LIBETC_TABLE - _SLUS_01189_TEXT_BASE)
            # Table may span one sector (it fits in 48 bytes before slot).
            for i, word in enumerate(_SLUS_01189_LIBETC_WORDS):
                off = tab_file + i * 4
                s = off // USER
                r = off % USER
                f.seek((exe_lba + s) * DST_SEC + 24 + r)
                f.write(struct.pack("<I", word))
            print(
                f"  patched disc libetc table LBA {exe_lba + sec} "
                f"(slot was zero)"
            )

        env_file = 0x800 + (_SLUS_01189_LIBGPU_ENV - _SLUS_01189_TEXT_BASE)
        sec = env_file // USER
        rem = env_file % USER
        f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
        env_bytes = f.read(_SLUS_01189_LIBGPU_ENV_WORDS * 4)
        if len(env_bytes) == _SLUS_01189_LIBGPU_ENV_WORDS * 4:
            cb = struct.unpack_from("<I", env_bytes, 0x44)[0]
            mode_byte = env_bytes[0x4A]
            head = struct.unpack_from("<I", env_bytes, 0)[0]
            if (
                (cb & 0xE0000003) != 0x80000000
                or mode_byte >= 2
                or (head != 0 and (head & 0xE0000003) != 0x80000000)
            ):
                f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
                f.write(b"\x00" * (_SLUS_01189_LIBGPU_ENV_WORDS * 4))
                # GCB at +0x40 / printf at +0x44 (see fix_slus_01189_boot).
                for rel, word in (
                    (0x40, _SLUS_01189_GCB_TABLE),
                    (0x44, _SLUS_01189_GPU_PRINTF),
                ):
                    off = env_file + rel
                    s, r = off // USER, off % USER
                    f.seek((exe_lba + s) * DST_SEC + 24 + r)
                    f.write(struct.pack("<I", word))
                print(
                    f"  patched disc libgpu GPU env LBA {exe_lba + sec}+{rem} "
                    f"(head={head:#010x}, cb={cb:#010x}, mode={mode_byte}; "
                    f"GCB→{_SLUS_01189_GCB_TABLE:#010x}, "
                    f"printf→{_SLUS_01189_GPU_PRINTF:#010x})"
                )

        printf_file = 0x800 + (_SLUS_01189_GPU_PRINTF - _SLUS_01189_TEXT_BASE)
        sec = printf_file // USER
        rem = printf_file % USER
        f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
        cur0 = struct.unpack("<I", f.read(4))[0]
        if cur0 in (0, 0x240A00A0):
            f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
            f.write(struct.pack("<III", *_SLUS_01189_GPU_PRINTF_STUB, 0))
            print(
                f"  patched disc GPU printf stub LBA {exe_lba + sec}+{rem} "
                f"(jr $ra no-op)"
            )

        regs_file = 0x800 + (_SLUS_01189_GPU_REGS - _SLUS_01189_TEXT_BASE)
        sec = regs_file // USER
        rem = regs_file % USER
        f.seek((exe_lba + sec) * DST_SEC + 24 + rem + 4)
        gp1 = struct.unpack("<I", f.read(4))[0]
        if gp1 != 0x1F801814:
            f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
            f.write(struct.pack(f"<{len(_SLUS_01189_GPU_REG_WORDS)}I", *_SLUS_01189_GPU_REG_WORDS))
            print(
                f"  patched disc libgpu MMIO pointers LBA {exe_lba + sec}+{rem} "
                f"(GP1 was {gp1:#010x})"
            )

        gcb_file = 0x800 + (_SLUS_01189_GCB_TABLE - _SLUS_01189_TEXT_BASE)
        sec = gcb_file // USER
        rem = gcb_file % USER
        f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
        cur0 = struct.unpack("<I", f.read(4))[0]
        gcb_ptr_file = 0x800 + (0x8008B4E8 - _SLUS_01189_TEXT_BASE)
        ps, pr = gcb_ptr_file // USER, gcb_ptr_file % USER
        f.seek((exe_lba + ps) * DST_SEC + 24 + pr)
        cur_ptr = struct.unpack("<I", f.read(4))[0]
        if cur_ptr != _SLUS_01189_GCB_TABLE or cur0 != _SLUS_01189_GCB_WORDS[0]:
            for i, word in enumerate(_SLUS_01189_GCB_WORDS):
                off = gcb_file + i * 4
                s, r = off // USER, off % USER
                f.seek((exe_lba + s) * DST_SEC + 24 + r)
                f.write(struct.pack("<I", word))
            f.seek((exe_lba + ps) * DST_SEC + 24 + pr)
            f.write(struct.pack("<I", _SLUS_01189_GCB_TABLE))
            print(
                f"  patched disc GCB table LBA {exe_lba + sec}+{rem} "
                f"at {_SLUS_01189_GCB_TABLE:#010x}"
            )

        for site, dest, label in (
            (0x8006BC08, 0x8006BDFC, "startIntr thunk"),
            (0x8006BC80, 0x8006BDFC, "startIntr jalr"),
            (0x8006BC98, 0x8006C044, "libetc+8 thunk"),
            (0x8006BCB0, 0x8006C044, "libetc+8 jalr"),
            (0x8006BC38, 0x8006C044, "nop-sled mid"),
        ):
            jal_file = 0x800 + (site - _SLUS_01189_TEXT_BASE)
            sec = jal_file // USER
            rem = jal_file % USER
            f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
            cur = struct.unpack("<I", f.read(4))[0]
            want = _j_word(dest)
            # Nop-sled mid is all zeros.
            if cur in (0x3C028009, 0x0040F809, 0x00000000, 0x03E00008) or (
                cur & 0xFC000000
            ) in (0x0C000000, 0x08000000):
                if cur != want:
                    f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
                    f.write(struct.pack("<II", want, 0))
                    print(f"  patched disc {label} -> j {dest:#010x}")

        stop_file = 0x800 + (_SLUS_01189_STOPCB_THUNK - _SLUS_01189_TEXT_BASE)
        sec = stop_file // USER
        rem = stop_file % USER
        cur = []
        for i in range(len(_SLUS_01189_STOPCB_THUNK_WORDS)):
            off = stop_file + i * 4
            s, r = off // USER, off % USER
            f.seek((exe_lba + s) * DST_SEC + 24 + r)
            cur.append(struct.unpack("<I", f.read(4))[0])
        if cur != list(_SLUS_01189_STOPCB_THUNK_WORDS):
            for i, word in enumerate(_SLUS_01189_STOPCB_THUNK_WORDS):
                off = stop_file + i * 4
                s, r = off // USER, off % USER
                f.seek((exe_lba + s) * DST_SEC + 24 + r)
                f.write(struct.pack("<I", word))
            print(
                f"  restored disc StopCallback thunk LBA {exe_lba + sec}+{rem} "
                f"at {_SLUS_01189_STOPCB_THUNK:#010x}"
            )

        early_file = 0x800 + (_SLUS_01189_CD_IRQ_EARLY - _SLUS_01189_TEXT_BASE)
        epi_file = 0x800 + (_SLUS_01189_CD_IRQ_EPILOGUE - _SLUS_01189_TEXT_BASE)
        beq_file = 0x800 + (_SLUS_01189_CD_IRQ_BEQ_SITE - _SLUS_01189_TEXT_BASE)
        cur_epi = []
        for i in range(len(_SLUS_01189_CD_IRQ_EPILOGUE_WORDS)):
            off = epi_file + i * 4
            s, r = off // USER, off % USER
            f.seek((exe_lba + s) * DST_SEC + 24 + r)
            cur_epi.append(struct.unpack("<I", f.read(4))[0])
        if cur_epi != list(_SLUS_01189_CD_IRQ_EPILOGUE_WORDS):
            for i, word in enumerate(_SLUS_01189_CD_IRQ_EARLY_WORDS):
                off = early_file + i * 4
                s, r = off // USER, off % USER
                f.seek((exe_lba + s) * DST_SEC + 24 + r)
                f.write(struct.pack("<I", word))
            for i, word in enumerate(_SLUS_01189_CD_IRQ_EPILOGUE_WORDS):
                off = epi_file + i * 4
                s, r = off // USER, off % USER
                f.seek((exe_lba + s) * DST_SEC + 24 + r)
                f.write(struct.pack("<I", word))
            print(
                f"  planted disc CdIRQ helper epilogue at "
                f"{_SLUS_01189_CD_IRQ_EPILOGUE:#010x}"
            )
        sec = beq_file // USER
        rem = beq_file % USER
        f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
        cur_beq = struct.unpack("<I", f.read(4))[0]
        if cur_beq != _SLUS_01189_CD_IRQ_BEQ_WORD:
            f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
            f.write(struct.pack("<I", _SLUS_01189_CD_IRQ_BEQ_WORD))
            print(
                f"  patched disc CdIRQ early-exit beq LBA {exe_lba + sec}+{rem} "
                f"→ {_SLUS_01189_CD_IRQ_EARLY:#010x}"
            )

        poll_file = 0x800 + (_SLUS_01189_CD_POLL_DONE - _SLUS_01189_TEXT_BASE)
        cur_poll = []
        for i in range(len(_SLUS_01189_CD_POLL_DONE_WORDS)):
            off = poll_file + i * 4
            s, r = off // USER, off % USER
            f.seek((exe_lba + s) * DST_SEC + 24 + r)
            cur_poll.append(struct.unpack("<I", f.read(4))[0])
        if cur_poll != list(_SLUS_01189_CD_POLL_DONE_WORDS):
            for i, word in enumerate(_SLUS_01189_CD_POLL_DONE_WORDS):
                off = poll_file + i * 4
                s, r = off // USER, off % USER
                f.seek((exe_lba + s) * DST_SEC + 24 + r)
                f.write(struct.pack("<I", word))
            print(
                f"  restored disc CdSync IRQ-poll done-path at "
                f"{_SLUS_01189_CD_POLL_DONE:#010x}"
            )

        puts_file = 0x800 + (_SLUS_01189_PUTS_THUNK - _SLUS_01189_TEXT_BASE)
        cur_puts = []
        for i in range(len(_SLUS_01189_PUTS_THUNK_WORDS)):
            off = puts_file + i * 4
            s, r = off // USER, off % USER
            f.seek((exe_lba + s) * DST_SEC + 24 + r)
            cur_puts.append(struct.unpack("<I", f.read(4))[0])
        if cur_puts != list(_SLUS_01189_PUTS_THUNK_WORDS):
            for i, word in enumerate(_SLUS_01189_PUTS_THUNK_WORDS):
                off = puts_file + i * 4
                s, r = off // USER, off % USER
                f.seek((exe_lba + s) * DST_SEC + 24 + r)
                f.write(struct.pack("<I", word))
            print(
                f"  planted disc B0:3F puts thunk at {_SLUS_01189_PUTS_THUNK:#010x}"
            )
        jal_file = 0x800 + (_SLUS_01189_VSYNC_PUTS_JAL - _SLUS_01189_TEXT_BASE)
        sec, rem = jal_file // USER, jal_file % USER
        f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
        cur_jal = struct.unpack("<I", f.read(4))[0]
        if cur_jal != _SLUS_01189_VSYNC_PUTS_JAL_WORD:
            f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
            f.write(struct.pack("<I", _SLUS_01189_VSYNC_PUTS_JAL_WORD))
            print(
                f"  patched disc VSync timeout puts jal → {_SLUS_01189_PUTS_THUNK:#010x}"
            )

        for site, words, label in (
            (_SLUS_01189_PADINIT_EPI, _SLUS_01189_PADINIT_EPI_WORDS, "padinit epilogue"),
            (_SLUS_01189_PADINIT_P1_RET, _SLUS_01189_PADINIT_P1_RET_WORDS, "padinit phase1 return"),
            (_SLUS_01189_PADINIT_TAIL, _SLUS_01189_PADINIT_TAIL_WORDS, "padinit phase2 jr ra"),
            (_SLUS_01189_PADINIT_P2_TRAMP, _SLUS_01189_PADINIT_P2_TRAMP_WORDS, "padinit phase2 trampoline"),
            (_SLUS_01189_RA_S0_EPI, _SLUS_01189_RA_S0_EPI_WORDS, "28FAC epi ra←s0 slot"),
            (_SLUS_01189_GS_CB_STUB, _SLUS_01189_GS_CB_STUB_WORDS, "Gs cb1/cb2 ring-complete leaf"),
            (
                _SLUS_01189_PUTDRAWENV_BRIDGE,
                _SLUS_01189_PUTDRAWENV_BRIDGE_WORDS,
                "PutDrawEnv prologue bridge",
            ),
            (
                _SLUS_01189_GS_BA88_BRIDGE,
                _SLUS_01189_GS_BA88_BRIDGE_WORDS,
                "Gs BA88 prologue bridge",
            ),
            (
                _SLUS_01189_GS_CMD_BRIDGE,
                _SLUS_01189_GS_CMD_BRIDGE_WORDS,
                "Gs cmd wrapper → BA88",
            ),
            (
                _SLUS_01189_GS_RETIRE_BRIDGE,
                _SLUS_01189_GS_RETIRE_BRIDGE_WORDS,
                "Gs retire B188 → B1E8",
            ),
            (
                _SLUS_01189_GS_SUBMIT_TRAMP,
                _SLUS_01189_GS_SUBMIT_TRAMP_WORDS,
                "Gs submit ring+poll trampoline",
            ),
        ):
            site_file = 0x800 + (site - _SLUS_01189_TEXT_BASE)
            cur = []
            for i in range(len(words)):
                off = site_file + i * 4
                s, r = off // USER, off % USER
                f.seek((exe_lba + s) * DST_SEC + 24 + r)
                cur.append(struct.unpack("<I", f.read(4))[0])
            if cur != list(words):
                for i, word in enumerate(words):
                    off = site_file + i * 4
                    s, r = off // USER, off % USER
                    f.seek((exe_lba + s) * DST_SEC + 24 + r)
                    f.write(struct.pack("<I", word))
                print(f"  planted disc {label} at {site:#010x}")

        orphan_file = 0x800 + (_SLUS_01189_GS_CMD_ORPHAN_JAL - _SLUS_01189_TEXT_BASE)
        sec, rem = orphan_file // USER, orphan_file % USER
        f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
        cur_orphan = struct.unpack("<I", f.read(4))[0]
        if cur_orphan == 0x0C010000:
            f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
            f.write(struct.pack("<I", 0))
            print(
                f"  cleared disc orphan jal 0x80040000 at "
                f"{_SLUS_01189_GS_CMD_ORPHAN_JAL:#010x}"
            )

        for site, want, label in (
            (_SLUS_01189_CARD_JALR, _SLUS_01189_CARD_JALR_WORD, "card-open jalr → jalr $v0"),
            (_SLUS_01189_GS_CLEAR_JR, _SLUS_01189_GS_CLEAR_JR_WORD, "Gs clear jr → jr $ra"),
            (_SLUS_01189_GSOT_LOOP_J, _SLUS_01189_GSOT_LOOP_J_WORD, "GsOT fill j self → j epi"),
            (_SLUS_01189_GSINIT_CB0, _SLUS_01189_GSINIT_CB0_WORD, "GsInit cb0 → prologue"),
            (_SLUS_01189_GSINIT_CB1, _SLUS_01189_GSINIT_CB1_WORD, "GsInit cb1 → ring-complete"),
            (_SLUS_01189_GSINIT_CB2, _SLUS_01189_GSINIT_CB2_WORD, "GsInit cb2 → ring-complete"),
            (_SLUS_01189_GSINIT_CB3, _SLUS_01189_GSINIT_CB3_WORD, "GsInit cb3 → real entry"),
            (
                _SLUS_01189_GS_POLL_FOUND_BEQ,
                _SLUS_01189_GS_POLL_FOUND_BEQ_WORD,
                "Gs poll FOUND beq → copy-out",
            ),
            (
                _SLUS_01189_GS_POLL_SLL_A2,
                _SLUS_01189_GS_POLL_SLL_A2_WORD,
                "Gs poll forward sll → a2",
            ),
            (
                _SLUS_01189_GS_POLL_ADDU_A2,
                _SLUS_01189_GS_POLL_ADDU_A2_WORD,
                "Gs poll forward addu → a2",
            ),
            (
                _SLUS_01189_GS_POLL_LOOP_SLL,
                _SLUS_01189_GS_POLL_LOOP_SLL_WORD,
                "Gs poll loop delay sll → a2",
            ),
        ):
            site_file = 0x800 + (site - _SLUS_01189_TEXT_BASE)
            sec, rem = site_file // USER, site_file % USER
            f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
            cur = struct.unpack("<I", f.read(4))[0]
            if cur != want:
                if site == _SLUS_01189_GS_POLL_FOUND_BEQ and cur not in (
                    _SLUS_01189_GS_POLL_FOUND_BEQ_BAD,
                    _SLUS_01189_GS_POLL_FOUND_BEQ_WORD,
                ):
                    print(
                        f"  WARN: disc Gs poll FOUND beq at {site:#010x} is "
                        f"{cur:#010x}"
                    )
                    continue
                if site == _SLUS_01189_GS_POLL_SLL_A2 and cur not in (
                    _SLUS_01189_GS_POLL_SLL_A2_BAD,
                    _SLUS_01189_GS_POLL_SLL_A2_WORD,
                ):
                    print(
                        f"  WARN: disc Gs poll sll at {site:#010x} is {cur:#010x}"
                    )
                    continue
                if site == _SLUS_01189_GS_POLL_ADDU_A2 and cur not in (
                    _SLUS_01189_GS_POLL_ADDU_A2_BAD,
                    _SLUS_01189_GS_POLL_ADDU_A2_WORD,
                ):
                    print(
                        f"  WARN: disc Gs poll addu at {site:#010x} is {cur:#010x}"
                    )
                    continue
                if site == _SLUS_01189_GS_POLL_LOOP_SLL and cur not in (
                    _SLUS_01189_GS_POLL_LOOP_SLL_BAD,
                    _SLUS_01189_GS_POLL_LOOP_SLL_WORD,
                ):
                    print(
                        f"  WARN: disc Gs poll loop sll at {site:#010x} is "
                        f"{cur:#010x}"
                    )
                    continue
                f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
                f.write(struct.pack("<I", want))
                print(f"  patched disc {label} at {site:#010x}")

        bridge_file = 0x800 + (_SLUS_01189_IRQ3_BRIDGE - _SLUS_01189_TEXT_BASE)
        sec = bridge_file // USER
        rem = bridge_file % USER
        f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
        cur = struct.unpack("<I", f.read(4))[0]
        if cur != _SLUS_01189_IRQ3_BRIDGE_WORDS[0]:
            for i, word in enumerate(_SLUS_01189_IRQ3_BRIDGE_WORDS):
                off = bridge_file + i * 4
                s, r = off // USER, off % USER
                f.seek((exe_lba + s) * DST_SEC + 24 + r)
                f.write(struct.pack("<I", word))
            print(
                f"  patched disc IRQ3 bridge LBA {exe_lba + sec}+{rem} "
                f"at {_SLUS_01189_IRQ3_BRIDGE:#010x}"
            )

        tramp_file = 0x800 + (_SLUS_01189_SYSENQ_TRAMP - _SLUS_01189_TEXT_BASE)
        sec = tramp_file // USER
        rem = tramp_file % USER
        cur = []
        for i in range(len(_SLUS_01189_SYSENQ_TRAMP_WORDS)):
            off = tramp_file + i * 4
            s, r = off // USER, off % USER
            f.seek((exe_lba + s) * DST_SEC + 24 + r)
            cur.append(struct.unpack("<I", f.read(4))[0])
        if cur != list(_SLUS_01189_SYSENQ_TRAMP_WORDS):
            for i, word in enumerate(_SLUS_01189_SYSENQ_TRAMP_WORDS):
                off = tramp_file + i * 4
                s, r = off // USER, off % USER
                f.seek((exe_lba + s) * DST_SEC + 24 + r)
                f.write(struct.pack("<I", word))
            print(
                f"  planted disc SysEnqIntRP trampoline LBA {exe_lba + sec}+{rem} "
                f"at {_SLUS_01189_SYSENQ_TRAMP:#010x}"
            )

        exit_file = 0x800 + (_SLUS_01189_SET_EXIT_THUNK - _SLUS_01189_TEXT_BASE)
        sec = exit_file // USER
        rem = exit_file % USER
        f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
        cur = struct.unpack("<I", f.read(4))[0]
        if cur != _SLUS_01189_SET_EXIT_THUNK_WORD:
            f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
            f.write(struct.pack("<I", _SLUS_01189_SET_EXIT_THUNK_WORD))
            print(
                f"  patched disc SetExit thunk LBA {exe_lba + sec}+{rem} "
                f"at {_SLUS_01189_SET_EXIT_THUNK:#010x} B0:0x19 → 0x18"
            )

        helper_file = 0x800 + (_SLUS_01189_SETDEF_HELPER - _SLUS_01189_TEXT_BASE)
        sec = helper_file // USER
        rem = helper_file % USER
        f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
        cur0 = struct.unpack("<I", f.read(4))[0]
        if cur0 != _SLUS_01189_SETDEF_HELPER_WORDS[0]:
            for i, word in enumerate(_SLUS_01189_SETDEF_HELPER_WORDS):
                off = helper_file + i * 4
                s, r = off // USER, off % USER
                f.seek((exe_lba + s) * DST_SEC + 24 + r)
                f.write(struct.pack("<I", word))
            orphan_file = 0x800 + (0x80058774 - _SLUS_01189_TEXT_BASE)
            os_, or_ = orphan_file // USER, orphan_file % USER
            f.seek((exe_lba + os_) * DST_SEC + 24 + or_)
            if struct.unpack("<I", f.read(4))[0] == 0x0C010000:
                f.seek((exe_lba + os_) * DST_SEC + 24 + or_)
                f.write(struct.pack("<I", 0))
            print(
                f"  patched disc SetDefDispEnv helper LBA {exe_lba + sec}+{rem} "
                f"at {_SLUS_01189_SETDEF_HELPER:#010x}"
            )

        mode_file = 0x800 + (_SLUS_01189_CD_MODE_BSS - _SLUS_01189_TEXT_BASE)
        sec = mode_file // USER
        rem = mode_file % USER
        f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
        mode0 = struct.unpack("<I", f.read(4))[0]
        if mode0 != 0:
            for i, word in enumerate(_SLUS_01189_CD_MODE_BSS_WORDS):
                off = mode_file + i * 4
                s, r = off // USER, off % USER
                f.seek((exe_lba + s) * DST_SEC + 24 + r)
                f.write(struct.pack("<I", word))
            print(
                f"  patched disc libcd mode/callback BSS LBA {exe_lba + sec}+{rem} "
                f"at {_SLUS_01189_CD_MODE_BSS:#010x} (mode was {mode0:#010x})"
            )

        hw_file = 0x800 + (_SLUS_01189_CD_HW_TABLE - _SLUS_01189_TEXT_BASE)
        sec = hw_file // USER
        rem = hw_file % USER
        f.seek((exe_lba + sec) * DST_SEC + 24 + rem + 8)
        dpcr = struct.unpack("<I", f.read(4))[0]
        if dpcr != 0x1F8010F0:
            for i, word in enumerate(_SLUS_01189_CD_HW_TABLE_WORDS):
                off = hw_file + i * 4
                s, r = off // USER, off % USER
                f.seek((exe_lba + s) * DST_SEC + 24 + r)
                f.write(struct.pack("<I", word))
            print(
                f"  patched disc libcd HW table LBA {exe_lba + sec}+{rem} "
                f"at {_SLUS_01189_CD_HW_TABLE:#010x} (DPCR was {dpcr:#010x})"
            )

        cds_file = 0x800 + (_SLUS_01189_CD_SEND_BRIDGE - _SLUS_01189_TEXT_BASE)
        sec = cds_file // USER
        rem = cds_file % USER
        f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
        cur0 = struct.unpack("<I", f.read(4))[0]
        if cur0 != _SLUS_01189_CD_SEND_BRIDGE_WORDS[0]:
            for i, word in enumerate(_SLUS_01189_CD_SEND_BRIDGE_WORDS):
                off = cds_file + i * 4
                s, r = off // USER, off % USER
                f.seek((exe_lba + s) * DST_SEC + 24 + r)
                f.write(struct.pack("<I", word))
            print(
                f"  patched disc libcd CdSend prologue LBA {exe_lba + sec}+{rem} "
                f"at {_SLUS_01189_CD_SEND_BRIDGE:#010x}"
            )

        fgp_file = 0x800 + (_SLUS_01189_FORCE_GP - _SLUS_01189_TEXT_BASE)
        fgp_crt_file = 0x800 + (_SLUS_01189_FORCE_GP_CRT - _SLUS_01189_TEXT_BASE)
        abs_sw_file = 0x800 + (_SLUS_01189_ABS_CD_BUF_SW - _SLUS_01189_TEXT_BASE)
        crt_file = 0x800 + (_SLUS_01189_CRT_PC0 - _SLUS_01189_TEXT_BASE)
        f.seek((exe_lba + fgp_file // USER) * DST_SEC + 24 + (fgp_file % USER) + 4)
        fgp1 = struct.unpack("<I", f.read(4))[0]
        s, r = crt_file // USER, crt_file % USER
        f.seek((exe_lba + s) * DST_SEC + 24 + r)
        crt0 = struct.unpack("<I", f.read(4))[0]
        if (
            fgp1 != _SLUS_01189_FORCE_GP_WORDS[1]
            or crt0 != _SLUS_01189_CRT_GP_ENTRY_WORDS[0]
        ):
            for words, base in (
                (_SLUS_01189_FORCE_GP_WORDS, fgp_file),
                (_SLUS_01189_ABS_CD_BUF_SW_WORDS, abs_sw_file),
                (_SLUS_01189_FORCE_GP_CRT_WORDS, fgp_crt_file),
                (_SLUS_01189_CRT_GP_ENTRY_WORDS, crt_file),
            ):
                for i, word in enumerate(words):
                    off = base + i * 4
                    s, r = off // USER, off % USER
                    f.seek((exe_lba + s) * DST_SEC + 24 + r)
                    f.write(struct.pack("<I", word))
            site_file = 0x800 + (_SLUS_01189_CD_BUF_SW_SITE - _SLUS_01189_TEXT_BASE)
            for i, word in enumerate(
                (_j_word(_SLUS_01189_ABS_CD_BUF_SW), 0)
            ):
                off = site_file + i * 4
                s, r = off // USER, off % USER
                f.seek((exe_lba + s) * DST_SEC + 24 + r)
                f.write(struct.pack("<I", word))
            # Undo any prior abs-lw redirect at 0x8003BA14.
            lw_file = 0x800 + (0x8003BA14 - _SLUS_01189_TEXT_BASE)
            for i, word in enumerate((0x8F840BD8, 0x0C01A4A2)):
                off = lw_file + i * 4
                s, r = off // USER, off % USER
                f.seek((exe_lba + s) * DST_SEC + 24 + r)
                f.write(struct.pack("<I", word))
            print(
                f"  planted disc $gp force + TCB publish + abs CD sw at "
                f"{_SLUS_01189_FORCE_GP:#010x}"
            )

        # Always undo the ra-clobbering jal force_gp in context restore.
        restore_file = 0x800 + (0x8006C358 - _SLUS_01189_TEXT_BASE)
        s, r = restore_file // USER, restore_file % USER
        f.seek((exe_lba + s) * DST_SEC + 24 + r)
        cur = struct.unpack("<I", f.read(4))[0]
        want_lw_gp = 0x8C9C002C
        if cur != want_lw_gp:
            f.seek((exe_lba + s) * DST_SEC + 24 + r)
            f.write(struct.pack("<I", want_lw_gp))
            print(
                f"  restored disc context-restore lw $gp LBA {exe_lba + s}+{r} "
                f"at 0x8006C358 (was {cur:#010x})"
            )

        cdt_file = 0x800 + (_SLUS_01189_CDSYNC_TIMERS - _SLUS_01189_TEXT_BASE)
        sec = cdt_file // USER
        rem = cdt_file % USER
        f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
        cur0 = struct.unpack("<I", f.read(4))[0]
        if cur0 != _SLUS_01189_CDSYNC_TIMER_WORDS[0]:
            for i, word in enumerate(_SLUS_01189_CDSYNC_TIMER_WORDS):
                off = cdt_file + i * 4
                s, r = off // USER, off % USER
                f.seek((exe_lba + s) * DST_SEC + 24 + r)
                f.write(struct.pack("<I", word))
            print(
                f"  patched disc libcd CdSync timers LBA {exe_lba + sec}+{rem} "
                f"at {_SLUS_01189_CDSYNC_TIMERS:#010x}"
            )

        csp_file = 0x800 + (_SLUS_01189_CD_STATUS_PORTS - _SLUS_01189_TEXT_BASE)
        sec = csp_file // USER
        rem = csp_file % USER
        f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
        cur0 = struct.unpack("<I", f.read(4))[0]
        if cur0 != _SLUS_01189_CD_STATUS_PORT_WORDS[0]:
            for i, word in enumerate(_SLUS_01189_CD_STATUS_PORT_WORDS):
                off = csp_file + i * 4
                s, r = off // USER, off % USER
                f.seek((exe_lba + s) * DST_SEC + 24 + r)
                f.write(struct.pack("<I", word))
            print(
                f"  patched disc libcd CD status ports LBA {exe_lba + sec}+{rem} "
                f"at {_SLUS_01189_CD_STATUS_PORTS:#010x}"
            )

        vhs_file = 0x800 + (_SLUS_01189_VSYNC_HSYNC_RATE - _SLUS_01189_TEXT_BASE)
        sec = vhs_file // USER
        rem = vhs_file % USER
        f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
        cur0 = struct.unpack("<I", f.read(4))[0]
        if cur0 != _SLUS_01189_VSYNC_HSYNC_RATE_WORD:
            f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
            f.write(struct.pack("<I", _SLUS_01189_VSYNC_HSYNC_RATE_WORD))
            print(
                f"  patched disc VSync HSync-rate LBA {exe_lba + sec}+{rem} "
                f"at {_SLUS_01189_VSYNC_HSYNC_RATE:#010x}"
            )

        cdsync_file = 0x800 + (_SLUS_01189_CDSYNC_BRIDGE - _SLUS_01189_TEXT_BASE)
        sec = cdsync_file // USER
        rem = cdsync_file % USER
        f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
        cur0 = struct.unpack("<I", f.read(4))[0]
        if cur0 != _SLUS_01189_CDSYNC_BRIDGE_WORDS[0]:
            for i, word in enumerate(_SLUS_01189_CDSYNC_BRIDGE_WORDS):
                off = cdsync_file + i * 4
                s, r = off // USER, off % USER
                f.seek((exe_lba + s) * DST_SEC + 24 + r)
                f.write(struct.pack("<I", word))
            print(
                f"  patched disc libcd CdSync bridge LBA {exe_lba + sec}+{rem} "
                f"at {_SLUS_01189_CDSYNC_BRIDGE:#010x}"
            )

        cdinit_file = 0x800 + (_SLUS_01189_CD_INIT_HOLE - _SLUS_01189_TEXT_BASE)
        sec = cdinit_file // USER
        rem = cdinit_file % USER
        f.seek((exe_lba + sec) * DST_SEC + 24 + rem)
        cur0 = struct.unpack("<I", f.read(4))[0]
        if cur0 != _SLUS_01189_CD_INIT_HOLE_WORDS[0]:
            for i, word in enumerate(_SLUS_01189_CD_INIT_HOLE_WORDS):
                off = cdinit_file + i * 4
                s, r = off // USER, off % USER
                f.seek((exe_lba + s) * DST_SEC + 24 + r)
                f.write(struct.pack("<I", word))
            print(
                f"  patched disc libcd CD-init hole LBA {exe_lba + sec}+{rem} "
                f"at {_SLUS_01189_CD_INIT_HOLE:#010x}"
            )


def pack_root_directory(
    bin_path: str, root_extent: int, entries: dict[str, tuple[int, int]]
) -> None:
    """Rewrite the ISO9660 root as contiguous MODE2/2352 user payloads.

    The irregular source dump inserts zero runs inside the root sector. BIOS
    directory walkers treat a zero record length as end-of-directory, so files
    after the hole (including ``SLUS_011.89``) are invisible until we pack.
    """
    with open(bin_path, "r+b") as f:
        def read_user(lba: int) -> bytes:
            f.seek(lba * DST_SEC + 24)
            return f.read(USER)

        def write_user(lba: int, user: bytes) -> None:
            if len(user) != USER:
                user = user.ljust(USER, b"\x00")[:USER]
            f.seek(lba * DST_SEC + 24)
            f.write(user)

        orig = read_user(root_extent)
        if not orig or orig[0] < 34:
            raise SystemExit("root directory sector unreadable for pack")
        dot = orig[0 : orig[0]]
        ddot = orig[orig[0] : orig[0] + orig[orig[0]]]

        # Rebuild minimal dirents from the name→(extent,size) map already parsed
        # from the quirky source (plus . / ..).
        packed = bytearray()
        packed += dot
        packed += ddot
        for name in sorted(entries):
            extent, size = entries[name]
            name_bytes = (name + ";1").encode("ascii")
            namelen = len(name_bytes)
            reclen = 33 + namelen
            if reclen % 2:
                reclen += 1
            if (len(packed) % USER) + reclen > USER:
                packed += b"\x00" * (USER - (len(packed) % USER))
            rec = bytearray(reclen)
            rec[0] = reclen
            struct.pack_into("<I", rec, 2, extent)
            struct.pack_into(">I", rec, 6, extent)
            struct.pack_into("<I", rec, 10, size)
            struct.pack_into(">I", rec, 14, size)
            rec[25] = 0  # file
            rec[32] = namelen
            rec[33 : 33 + namelen] = name_bytes
            packed += rec
        while len(packed) % USER:
            packed += b"\x00"
        nsec = len(packed) // USER
        for i in range(nsec):
            write_user(root_extent + i, bytes(packed[i * USER : (i + 1) * USER]))

        # Keep PVD root size in sync (both endian fields).
        pvd = bytearray(read_user(16))
        struct.pack_into("<I", pvd, 166, len(packed))
        struct.pack_into(">I", pvd, 170, len(packed))
        write_user(16, bytes(pvd))
        print(
            f"  packed root directory at LBA {root_extent} "
            f"({len(entries)} files, {len(packed)} bytes)"
        )


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

    exe_extent = None
    for name in ("SYSTEM.CNF", "SLUS_011.89"):
        extent, size = entries[name]
        blob = extract_file(data, lba_map, extent, size)
        if name == "SLUS_011.89":
            if blob[:8] != b"PS-X EXE":
                raise SystemExit("SLUS_011.89 is not a PS-X EXE")
            exe_extent = extent
            blob = fix_slus_01189_boot(blob)
        out_path = os.path.join(args.out_dir, name)
        with open(out_path, "wb") as out:
            out.write(blob)
        print(f"wrote {out_path} ({len(blob)} bytes, LBA {extent})")

    bin_path = os.path.join(args.out_dir, BIN_NAME)
    print(f"writing {bin_path} …")
    n_sec, missing = write_bin(data, lba_map, bin_path)
    print(f"  {n_sec} sectors ({n_sec * DST_SEC} bytes), {missing} gap sectors zero-filled")
    # Source root dirents are split by mid-sector zero padding; a real BIOS
    # ISO9660 walker stops at the first zero and never sees SLUS_011.89.
    # Repack the root so HLE boot-skip / LLE shell can LoadExe from disc.
    pack_root_directory(bin_path, root_extent, entries)
    if exe_extent is not None:
        patch_bin_exe_boot(bin_path, exe_extent)
    write_cue(args.out_dir)

    bin_md5, bin_sha1, bin_size = file_hashes(bin_path)
    print(f"working bin:")
    print(f"  size  {bin_size}")
    print(f"  md5   {bin_md5}")
    print(f"  sha1  {bin_sha1}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
