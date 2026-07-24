# Bomberman Party Edition — bring-up notes

## Working

- Disc prepare (`tools/prepare_disc.py`) → MODE2/2352 + packed ISO root + EXE
- Recompiler generate → 41 shards / ~3600 dispatch entries from prologue+JAL seeds
- Release runtime builds with `MAX_PLAYERS 5` (`PSX_MAX_PLAYERS=5`)
- LLE BIOS boot reaches SCEA license screen (GPU drawing; glitched PS logo)
- After license, disc LoadExe can place EXE bytes at `0x80010000` (SONYLOGO…)
- Multitap deferred until game entry (BIOS boot uses normal pads)
- Framework pins: `psxrecomp` `feat/max-players-5`, nested `recomp-net`
  `feat/max-slots-5` (`RNET_MAX_SLOTS=5`), `recomp-ui` `feat/netplay-5p`

## 5P / netplay contract

| Knob | Value |
|------|--------|
| CMake | `MAX_PLAYERS 5` on `psxrecomp_add_runtime_target` |
| `game.toml` | `players = 5` (lobby `max_slots`) |
| MotK / default | omit `MAX_PLAYERS` → 2 |

Hardware map when multitap is on (`slot_count >= 3` or offline after game
start with `players >= 3`): port1 SCPH-1070 pads A–D = P1–P4, port2 = P5.

## Open

- **Title/menu not reached yet.** LLE sticks after license / early EXE handoff
  (PC often `0` / `0x5C4`, GP0 flat). Needs further seed/miss iteration and
  CD/boot diagnosis.
- **HLE boot-skip** loads EXE into RAM but does not enable display / run game
  code cleanly — prefer `PSX_BIOS_HLE=0` for bring-up until that path is fixed.
- Full 5P netplay needs matching lobby-server build (`MAX_SLOTS=5`) and live
  multi-client validation (host-relay transport is in recomp-net; unit tests pass).
- `psxrecomp` feature branch push to `TechnicallyComputers/psxrecomp` may need
  auth (local tip `20cfcc1` on `feat/max-players-5`).
- Boxart / launcher assets not added yet.
