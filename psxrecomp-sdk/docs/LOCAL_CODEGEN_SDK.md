# Local codegen SDK

Headless contract for regenerating an existing psxrecomp game project from a
user-supplied disc. Intended for `recomp-ui` setup flows and RetComM launcher
automation. This does **not** redistribute disc images.

PGO (optional) runs **only on the user’s machine** during local rebuild when
`game.toml` has `[pgo] enabled = true`. CI must not set `PSX_PGO`.

## Setup host (CI without `generated/`)

Games can ship a **setup host**: the normal `psx-runtime` target linked
**without** `GAME_GENERATED_*` (BIOS-only / no game dispatch), plus
`PSX_HAS_GAME_CODEGEN` and `host/psxrecomp_codegen_host.c`. CI builds this when
game `generated/` is absent; the first-run UI runs `generate` → `rebuild`, which
reconfigures cmake so the same target then links real game C.

| Piece | Role |
|-------|------|
| Setup exe | `recomp-ui` + codegen host; opens Generate & rebuild |
| `psxrecomp-tools` pack | `psxrecomp_cli.py`, `tools/`, prebuilt `psxrecomp-game` (`scripts/package_psxrecomp_tools.sh`) |
| Toolchain pack | `bin/cmake` (+ ninja/compiler); smoke: `scripts/package_toolchain_smoke_linux.sh` |
| Game sources | `game.toml`, seeds, `CMakeLists.txt`, `psxrecomp/`, `recomp-ui/` |

RetComM can drive the same CLI without the ImGui host. Direct-download zips
should embed tools + a real (or smoke) toolchain under `toolchain/` and document
Python 3.

## Commands

```bash
python psxrecomp/psxrecomp_cli.py verify-disc \
  --config game.toml --disc path/to/dump.bin [--json-progress]

python psxrecomp/psxrecomp_cli.py generate \
  --config game.toml --project-root . --disc path/to/dump.iso \
  [--skip-hash-check] [--force-prepare] [--json-progress]

python psxrecomp/psxrecomp_cli.py rebuild \
  --config game.toml --project-root . \
  --build-dir build-release --target psx-runtime \
  --exe-basename Bomberman_Party_Edition_Recompiled \
  [--disc path/to/game.cue] [--no-pgo] [--force-pgo] [--json-progress]

python psxrecomp/psxrecomp_cli.py pgo-train \
  --config game.toml --build-dir build-release \
  --exe-basename Bomberman_Party_Edition_Recompiled \
  [--disc …] [--train-secs 120] [--train-runs 3] [--json-progress]
```

`generate` normalizes the dump via `tools/prepare_disc.py` when needed, then
runs `psxrecomp-game --config game.toml` into `[recompiler] out_dir`.

`rebuild` runs cmake. If `[pgo] enabled = true` (and `--no-pgo` not set), it
instruments, trains (`scenario` / timed boot), then rebuilds with profiles.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | success |
| 1 | runtime / generation / build failure |
| 2 | usage / argument error |
| 3 | disc verification failure |

## JSONL progress (`--json-progress`)

Stdout is reserved for one JSON object per line. Useful events:

| `event` | Notes |
|---------|--------|
| `phase` | `verify`, `prepare_disc`, `emit`, `build`, `pgo_*`, `done` |
| `disc` | digests after verification |
| `log` | mirrored tool chatter |
| `result` | final payload |
| `error` | `message`, `code` |

## Portable recomp-ui host (`host/`)

Compile:

- `host/psxrecomp_codegen_host.c`
- `host/psxrecomp_codegen_host.h`

Fill a `PsxrecompCodegenHostConfig`, then:

```c
psxrecomp_codegen_host_apply(&gi, &my_cfg);

/* after recomp_launcher_run_window: */
if (lr == RECOMP_LAUNCHER_RESULT_RELAUNCH)
    psxrecomp_codegen_host_relaunch_or_exit(disc_path);
```

### Modular `[pgo]` (game.toml) — opt-in

PGO runs only when the title sets `enabled = true`. Framework defaults when
enabled: **60s × 2 runs**, `mute_host_audio = true`, `hide_video = true`.
Games may lengthen trains in their own `game.toml`.

```toml
[pgo]
enabled = true                 # required to opt in
train_secs = 60                # optional override
train_runs = 2                 # optional override
mute_host_audio = true         # default: discard SDL output (SPU still runs)
hide_video = true              # default: --headless (no on-screen FMV)
scenario = "boot_timed"        # title-authored workload hint
```

Omit the section or set `enabled = false` to skip. During train the CLI/UI show
a **WARNING** — do not cancel the process (or close a visible window if
`hide_video = false`).

`hide_video` keeps guest MDEC/FMV decode in the profile but shows nothing on
screen (avoids abrasive/flickering FMV). Use `--pgo-video` only when you need
host present paths in the profile.

### Env overrides

| Env | Role |
|-----|------|
| `PSXRECOMP_PROJECT_ROOT` / game-specific | project root |
| `PSXRECOMP_BUILD_DIR` / game-specific | cmake build dir |
| `PSXRECOMP_FORCE_SETUP` / game-specific | force setup wizard |
| `PSXRECOMP_GAME` | path to `psxrecomp-game` binary |
| `PSX_HOST_MUTE=1` | discard host SDL audio (SPU still runs; set by PGO train) |
| `PSX_HEADLESS=1` | no SDL window (set by PGO train when `hide_video`) |
| `SDL_VIDEODRIVER=dummy` | set by default with headless train |
| `PYTHON` / `CMAKE` | tool overrides |
