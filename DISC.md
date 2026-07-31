# Disc identity — Bomberman Party Edition (USA)

Local-only dump. **Do not commit** the disc image or extracted EXE.

| Field | Value |
|-------|-------|
| Title | Bomberman Party Edition (USA) |
| Serial | SLUS-01189 |
| Boot EXE | `SLUS_011.89` |
| Preferred dump | [Redump #10806](http://redump.org/disc/10806/) MODE2/2352 `.bin` + `.cue` |
| Working format | `bpe/*.bin` + `bpe/*.cue` — MODE2/2352 |

## Library formats

Normalize dumps with the framework tool (game wrapper forwards the same args):

```bash
python3 psxrecomp/tools/prepare_disc.py --config game.toml <dump>
# or: python3 tools/prepare_disc.py <dump>
```

Config lives in `game.toml` under `[prepare_disc]` (out dir, names, digests).

| Input | Behavior |
|-------|----------|
| Redump `.bin` / `.cue` | Hash-gated copy into `bpe/` + write `.cue` |
| Cooked 2048 `.iso` | Verify boot EXE / serial; rebuild MODE2/2352 `.bin`+`.cue` |
| 2448-byte/sector raw | Trim to 2352 (MotK-style dumps) |
| Other 2352 / ISO | `--skip-hash-check` + serial/EXE gate (not recommended) |

ISO rebuild sets sync/header/subheader/EDC; ECC is zeroed (fine for software
readers). Rebuilt bins are **not** bit-identical to Redump.

## Redump image (preferred)

| Field | Value |
|-------|-------|
| Size | 660,770,880 bytes |
| MD5 | `e0ceba6e448677f3d938b1dd176be3af` |
| SHA-1 | `53a509dbe859f773856f26d966f5edacbc701b4e` |

## Cooked ISO (from Redump user payloads)

Useful when a frontend library stores PS1 titles as `.iso`:

| Field | Value |
|-------|-------|
| Size | 575,365,120 bytes (280,940 × 2048) |
| MD5 | `13cf2c80a2811015d604ecf4496c3287` |
| SHA-1 | `4735882768678923796f62878c05f7f0e81308d5` |

## Boot EXE (from `SYSTEM.CNF` + PS-X EXE header)

| Field | Value |
|-------|-------|
| `BOOT` | `cdrom:\SLUS_011.89;1` |
| Load address | `0x80010000` |
| Entry PC | `0x800785D8` |

## Recreate `bpe/`

```bash
# Redump bin or cue:
python3 psxrecomp/tools/prepare_disc.py --config game.toml \
  "/path/to/Bomberman - Party Edition (USA).bin"

# Cooked ISO from a RomM-style library:
python3 tools/prepare_disc.py "/path/to/Bomberman Party Edition.iso"
```

On success the script prints `RESULT_CUE=…` for the launcher/host to pick up.

## Local generate & rebuild

Missing `generated/SLUS_011.89_dispatch.c` (or `BPE_FORCE_SETUP=1`) opens the
first-run **Generate & rebuild** flow (`psxrecomp/docs/LOCAL_CODEGEN_SDK.md`).
CI release zips are setup hosts (no game C); the same flow creates `generated/`
locally then rebuilds. BPE leaves `[pgo] enabled = false`.
