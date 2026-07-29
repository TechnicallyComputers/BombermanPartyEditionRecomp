# BombermanPartyEditionRecomp

*Bomberman Party Edition* (USA, **SLUS-01189**) — game project for
[PSXRecomp](https://github.com/mstan/psxrecomp).

Holds game config, seeds, build glue, and (for private CI) recompiler
`generated/` output. Disc images and BIOS stay local and gitignored.

## Layout

| Path | Role |
|------|------|
| `game.toml` | Game / recompiler / runtime config |
| `seeds/` | Function-start seeds for `psxrecomp-game` |
| `bpe/` | Local disc `.bin`/`.cue`, `SLUS_011.89`, `SYSTEM.CNF` (gitignored) |
| `psxrecomp/` | Framework submodule (`mstan/psxrecomp`) |
| `generated/` | Recompiler output (tracked for CI; regenerate when seeds change) |
| `VERSION` | Release / lobby match pin (e.g. `0.1.0`) |
| `DISC.md` | Disc identity + hashes |
| `tools/prepare_disc.py` | Rebuild `bpe/` from the source dump |

## Disc

Source dump (irregular raw CD with subchannel; see `DISC.md`):

`/mnt/crucial4tb/Emulation/roms/ps/Bomberman Party Edition.iso`

Working image for the runtime is MODE2/2352 under `bpe/`. Recreate with:

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

### Linux netplay release (ICE)

One-shot configure + Release build with MotK online ICE, then zip (no BIOS/disc):

```bash
bash scripts/build_linux_netplay.sh
# → build-linux-netplay/Bomberman_Party_Edition_Recompiled
# → dist/bpe-<VERSION>-linux-netplay.zip
```

Build only: `bash scripts/build_linux_netplay.sh --no-package`  
Repack an existing build: `bash scripts/package_release.sh build-linux-netplay linux-netplay`

### Windows MinGW release from Linux (ICE)

Cross-compile with MinGW-w64, statically link SDL2 + libgcc when possible, zip
with launcher assets and any remaining DLLs:

```bash
# Arch / CachyOS:
#   pacman -S --needed mingw-w64-gcc mingw-w64-sdl2 cmake ninja zip

bash scripts/build_windows_mingw.sh
# → build-mingw-netplay/Bomberman_Party_Edition_Recompiled.exe
# → dist/bpe-<VERSION>-windows-x64-mingw.zip
```

Build only: `bash scripts/build_windows_mingw.sh --no-package`  
Dynamic SDL2/libgcc (bundle DLLs): `bash scripts/build_windows_mingw.sh --dynamic`  
Repack: `bash scripts/package_release.sh build-mingw-netplay windows-x64-mingw`

### PGO (intro + OPENING.STR)

Profile-guided optimization trains the compiler on logos through the opening
FMV. One-shot (windowed; prefer a real display so present/audio paths count):

```bash
DISPLAY=:0 ./scripts/pgo_bpe_intro.sh
```

Optional: `PGO_TRAIN_RUNS=3` `PGO_TRAIN_SECS=120`. After large runtime edits,
retrain so profiles stay fresh (`-DPSX_PGO=use` with stale `.gcda` underperforms).

## CI / release packages

GitHub Actions workflow: `.github/workflows/release.yml`

| Artifact | Runner |
|----------|--------|
| `linux-x64` | `ubuntu-24.04` |
| `windows-x64` | `windows-2022` (MSYS2 MinGW64) |
| `macos-arm64` | `macos-15` |
| `macos-x64` | `macos-15-intel` (older Intel Macs) |

- Manual: **Actions → Release builds → Run workflow**
- Tag `v0.1.0` (matching `VERSION`): builds + GitHub Release with zips
- Packages include the exe, `assets/` (recomp-ui fonts/img), `game.toml`, and
  `VERSION` — never BIOS/disc
- CI builds the exact committed **psxrecomp**, game-root **recomp-ui**, and
  nested **recomp-net** gitlink pins
- CI configures `-DRNET_ENABLE_ICE=ON`. Every matrix OS runs intro PGO train
  on that runner (needs LFS disc + `SCPH1001.BIN` in `psxrecomp-ci-assets`
  under `bpe/`; GCC `.gcda` on Linux/Windows, Clang profdata on macOS)
- Local pack: `scripts/package_release.sh build-release linux-x64`

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

Multitap (SCPH-1070) arms offline when `players >= 3` after game entry, and
for netplay when `slot_count >= 3`. Default tap is console Port 1; set
`[controller] multitap_port = 2` for titles that need the tap on Port 2
(Bomberman Party Edition). Bulk polls follow the real TAP/REQ latch.

## Framework pin

Both framework submodules track **`mstan` `master`**:

| Submodule | Repo | Branch |
|-----------|------|--------|
| `psxrecomp/` | [mstan/psxrecomp](https://github.com/mstan/psxrecomp) | `master` |
| `recomp-ui/` | [mstan/recomp-ui](https://github.com/mstan/recomp-ui) | `master` |

`psxrecomp` vendors **`lib/recomp-net`** only (nested gitlink). The Dear ImGui
launcher is the repo-root **`recomp-ui`** submodule. Lobby server counterpart:
`recomp-net-server`.

## Status

Generated game C + Release/`MAX_PLAYERS 5` runtime build. LLE boot reaches the
SCEA license screen; title/menu bring-up continues — see `ISSUES.md`.
