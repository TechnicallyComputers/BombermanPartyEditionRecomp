# Disc identity — Bomberman Party Edition (USA)

Local-only dump. **Do not commit** the disc image or extracted EXE.

| Field | Value |
|-------|-------|
| Title | Bomberman Party Edition (USA) |
| Serial | SLUS-01189 |
| Boot EXE | `SLUS_011.89` |
| Source path | `/mnt/crucial4tb/Emulation/roms/ps/Bomberman - Party Edition (USA)/Bomberman - Party Edition (USA).{bin,cue}` |
| Source format | Redump MODE2/2352 ([redump.org/disc/10806](http://redump.org/disc/10806/)) |
| Working format | `bpe/*.bin` + `bpe/*.cue` — copy of Redump |

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
| Stack (`game.toml`) | `0x801FFFF0` |

## Redump track

| Field | Value |
|-------|-------|
| Sectors | 280,940 |
| Size | 660,770,880 |
| MD5 | `e0ceba6e448677f3d938b1dd176be3af` |
| SHA-1 | `53a509dbe859f773856f26d966f5edacbc701b4e` |
| CRC-32 | `98275a08` |

## Working image (`bpe/`)

Produced by `tools/prepare_disc.py` (copy + extract; no sector rebuild, no EXE patches).

| Field | Value |
|-------|-------|
| Path | `bpe/Bomberman Party Edition.bin` |
| Size / hashes | same as Redump above |
| EXE MD5 | `35d48773d79a784614dcfa45b9df8f04` |

## Recreate `bpe/` from Redump

```bash
python3 tools/prepare_disc.py
# or:
python3 tools/prepare_disc.py "/path/to/Bomberman - Party Edition (USA).bin"
```

## Note on the old dump

An earlier irregular `.iso` (`Bomberman Party Edition.iso`, MD5 `054204cd…`) was **not** Redump and produced a heavily corrupted `SLUS_011.89` (~190k byte diffs vs Redump). Do not use it.
