# Disc identity — Bomberman Party Edition (USA)

Local-only dump. **Do not commit** the disc image or extracted EXE.

| Field | Value |
|-------|-------|
| Title | Bomberman Party Edition (USA) |
| Serial | SLUS-01189 |
| Boot EXE | `SLUS_011.89` |
| Source path | `/mnt/crucial4tb/Emulation/roms/ps/Bomberman Party Edition.iso` |
| Source format | Irregular raw CD (sync marks not MotK-style aligned 2448-from-0) |
| Working format | `bpe/*.bin` + `bpe/*.cue` — rebuilt MODE2/2352 |

## Dump quirk

Sync marks are irregular. Map sectors by CD sync + MSF→LBA. User data for
this dump starts at **sync+21** (not +24). `tools/prepare_disc.py` rebuilds
standard MODE2/2352 sectors from the 2048-byte user payloads.

## Boot EXE (from `SYSTEM.CNF` + PS-X EXE header)

| Field | Value |
|-------|-------|
| `BOOT` | `cdrom:\SLUS_011.89;1` |
| `TCB` | 4 |
| `EVENT` | 16 |
| Load address | `0x80010000` |
| Entry PC | `0x800785D8` |
| Text size | `0x00083800` |
| Stack (`SYSTEM.CNF`) | `0x801FFF00` |
| Stack (EXE header / `game.toml`) | `0x801FFFF0` |

## Source image (as found)

| Field | Value |
|-------|-------|
| Size | 645,566,976 bytes |
| MD5 | `054204cd016afb3c56fd9e1eea80676e` |
| SHA-1 | `306888eee5bf95cb3cabd3a40cd836063887ab77` |

## Local working image (2352)

Produced by `tools/prepare_disc.py` (sync→LBA map, user@sync+21 → MODE2/2352).

| Field | Value |
|-------|-------|
| Path | `bpe/Bomberman Party Edition.bin` |
| Size | 660,775,584 bytes (280,942 × 2352) |
| MD5 | `74c5fbb8b2ca87b2f1d0f0c292241c03` |
| SHA-1 | `85c30e5da35c8d737b049079d8e72b7d3a45eb05` |

`prepare_disc.py` also **packs the ISO9660 root directory** (source has
mid-sector zero padding that stops a real BIOS walker before `SLUS_011.89`).

## Recreate `bpe/` from the source dump

```bash
python3 tools/prepare_disc.py
```
