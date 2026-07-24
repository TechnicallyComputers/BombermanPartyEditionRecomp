# BombermanPartyEditionRecomp

*Bomberman Party Edition* (USA, **SLUS-01189**) — game project for
[PSXRecomp](https://github.com/TechnicallyComputers/psxrecomp).

Holds game config, seeds, build glue, and (for private CI) recompiler
`generated/` output. Disc images and BIOS stay local and gitignored.

## Layout

| Path | Role |
|------|------|
| `game.toml` | Game / recompiler / runtime config |
| `seeds/` | Function-start seeds for `psxrecomp-game` |
| `bpe/` | Local disc `.bin`/`.cue`, `SLUS_011.89`, `SYSTEM.CNF` (gitignored) |
| `psxrecomp/` | Framework submodule (`TechnicallyComputers/psxrecomp`) |
| `generated/` | Recompiler output (tracked for CI; regenerate when seeds change) |
| `VERSION` | Release / lobby match pin (e.g. `0.1.0`) |
| `DISC.md` | Disc identity + hashes |
| `tools/prepare_disc.py` | Install Redump disc + extract boot EXE into `bpe/` |

## Disc

Use the Redump USA image (see `DISC.md`):

`/mnt/crucial4tb/Emulation/roms/ps/Bomberman - Party Edition (USA)/`

```bash
python3 tools/prepare_disc.py
```

## Bring-up (next steps)

1. Place `SCPH1001.BIN` under `psxrecomp/bios/` (or point the runtime at your BIOS).
2. Prepare the disc (`tools/prepare_disc.py`) and seed the boot EXE entry.
3. Build the framework recompiler, then generate game C:

```bash
./psxrecomp/recompiler/build/psxrecomp-game --config game.toml
```

4. Configure and build the runtime (prefer **Release** for playtesting):

```bash
cmake -S . -B build-release -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build-release --target psx-runtime -j"$(nproc)"

./build-release/Bomberman_Party_Edition_Recompiled \
  --game game.toml \
  --disc "bpe/Bomberman Party Edition.cue"
```

For debugging (port **4530**), use `-DCMAKE_BUILD_TYPE=RelWithDebInfo`.

Clone with submodules:

```bash
git clone --recurse-submodules https://github.com/TechnicallyComputers/BombermanPartyEditionRecomp.git
```

## 5-player / netplay

This title opts into five pads at **compile time**:

```cmake
psxrecomp_add_runtime_target(psx-runtime
    ...
    MAX_PLAYERS 5
)
```

That bakes `PSX_MAX_PLAYERS=5`. `game.toml` keeps `players = 5` for lobby
`max_slots` and launcher controller cards. Games that omit `MAX_PLAYERS`
stay at the framework default of **2** (MotK).

Multitap (SCPH-1070 on port 1 + pad on port 2) is enabled when a netplay
session uses 3+ slots.

## Framework pin

Nested `psxrecomp` tracks `feat/max-players-5` (local tip includes multitap +
`MAX_PLAYERS`) and keeps **`lib/recomp-net` only**. The Dear ImGui launcher is
the repo-root **`recomp-ui`** submodule. Lobby server counterpart:
`recomp-net-server` `feat/max-slots-5`.

## Status

Generated game C + Release/`MAX_PLAYERS 5` runtime build. LLE boot reaches the
SCEA license screen; title/menu bring-up continues — see `ISSUES.md`.
