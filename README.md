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

Clone with submodules:

```bash
git clone --recurse-submodules https://github.com/TechnicallyComputers/BombermanPartyEditionRecomp.git
```

## Status

Repo scaffold only. Disc prepare + EXE extract work locally; recompiler
seeds / `generated/` and netplay bring-up are next.
