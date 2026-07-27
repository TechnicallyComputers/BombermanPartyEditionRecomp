# Launcher boot timing diagnostics

Opt-in wall-clock stamps for **time-to-launcher-window** (process start → first
UI swap). Use this when investigating a slow launcher open; it does not affect
normal play when disabled.

## Enable

```bash
export PSX_LAUNCHER_BOOT_TIMING=1
# alias also accepted by recomp-ui:
# export LNG_BOOT_TIMING=1

# Cold-ish run (close the app first). Prefer the binary you actually play:
./build/Masters_of_Teras_Kasi_Recompiled 2>boot-timing.log

# Or merge stderr+stdout:
./build/Masters_of_Teras_Kasi_Recompiled 2>&1 | tee boot-timing.log
```

Quit as soon as the launcher UI is visible (or use a smoke quit):

```bash
PSX_LAUNCHER_BOOT_TIMING=1 LNG_SMOKE_FRAMES=3 \
  ./build/Masters_of_Teras_Kasi_Recompiled 2>boot-timing.log
```

`LNG_SMOKE_FRAMES=N` makes the launcher exit after N frames (recomp-ui test hook).

## Output format

Lines go to **stderr**:

```text
[boot-timing] +   12.3 ms  total   45.6 ms  host:game_config_done
```

| Field | Meaning |
|--------|---------|
| `+ … ms` | Delta since the previous mark |
| `total … ms` | Time since the first mark (`host:main_enter`) |
| phase name | Where in the boot path we are |

## Phase map (read in order)

| Phase | What just finished |
|--------|--------------------|
| `host:main_enter` | `main()` started (anchors t0) |
| `host:crash_handlers` | Crash handlers installed |
| `host:game_config_done` | `game.toml` (+ overlay stash) loaded |
| `host:pre_overlay_worker` | About to spawn overlay-cache worker thread |
| `host:before_sdl_init` / `host:after_sdl_init` | Host `SDL_Init` |
| `host:setup_checks_done` | BIOS/disc quiet setup checks |
| `host:before_run_window` | Entering recomp-ui |
| `rui:run_window:enter` | `recomp_launcher_run_window` |
| `rui:platform_open:begin` → `…:window+gl_ready` | SDL window + GL context |
| `rui:model+binds_ready` | Launcher model + keybinds |
| `rui:backend_run:begin` | ImGui backend start |
| `rui:imgui_gl_ready` | ImGui SDL/GL backends inited |
| `rui:textures_loaded` | Boxart / pad / brand / verdict TGAs loaded |
| `rui:fonts_built` | Font atlas build (first scale apply) |
| `rui:first_swap` | **First visible UI frame** (primary KPI) |
| `host:after_run_window` | Launcher returned (Launch/Quit) |

**Primary KPI:** `total` on `rui:first_swap` = wall time until the launcher UI
is presented. Compare cold vs warm runs; page cache can cut early `host:*` a lot.

## How to interpret

1. Large gap before `rui:platform_open:window+gl_ready` → host config I/O, process
   page-in, or SDL/GL context creation.
2. Large `rui:model+binds_ready` delta → model init + binds. For PSX, disc
   identity uses `disc_verify` (not a full-BIN CRC/SHA). A multi-second spike
   here on a cartridge title usually means whole-ROM hashing.
3. Large `rui:textures_loaded` delta → TGA decode / disk for launcher art.
4. Large `rui:fonts_built` delta → ImGui font atlas (Lato / JP / emoji merges).
5. Small deltas through `rui:*` but large total at `first_swap` → sum of many
   medium costs; still use the table above to rank.

Overlay ABI/DLL init runs on a **worker** after `host:pre_overlay_worker` and
should **not** appear on the critical path to `rui:first_swap` (it is joined
only after the launcher returns, before game boot).

## Tips

- Run **twice**: first cold, second warm — if only cold is slow, blame page-in /
  disk more than GL.
- Keep `PSX_NO_LAUNCHER` unset so the GUI path is exercised.
- Rebuild after pulling recomp-ui (`ninja psx-runtime` / your usual target) so
  `launcher_boot_timing.c` is linked.
