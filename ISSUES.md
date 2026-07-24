# Bomberman Party Edition — bring-up notes

## Working

- Disc prepare (`tools/prepare_disc.py`) → MODE2/2352 + packed ISO root + EXE
  boot patches (see below)
- Recompiler generate → ~38 shards / ~3608 dispatch entries
- Release/debug runtimes build with `MAX_PLAYERS 5` (`PSX_MAX_PLAYERS=5`)
- LLE BIOS boot reaches SCEA license, LoadExe, CRT, and enters game `main`
- Multitap deferred until game entry (BIOS boot uses normal pads)
- Framework pins: `psxrecomp` `feat/max-players-5`, nested `recomp-net`
  `feat/max-slots-5` (`RNET_MAX_SLOTS=5`), `recomp-ui` `feat/netplay-5p`

### Boot patches applied by `tools/prepare_disc.py`

The irregular source dump needs several EXE fixes (confirmed on ISO user_off=21):

| Patch | From → To | Why |
|-------|-----------|-----|
| PC0 | `0x800785D8` → `0x80078638` | Retail PC0 is a `jr $ra` leaf → SystemHalt(908) |
| CRT `jal main` | `0x80012ED8` → `0x80012F38` | Target was printf rodata, not real prologue |
| main first `jal` | `0x80078680` → `0x800786E0` | Mid-CRT (contains `jal main`) → recursion |
| main `jal` #2 | `0x8006BC08` → `0x8006BDFC` | Skip broken libetc thunk |
| clear_words `jal` | `0x8006C730` → `0x8006C790` | Mid-DMACallback infinite I_MASK loop |
| InterruptCallback `jal` | `0x8006BC38` → `0x8006BD5C` | Nop-sled mid-thunk |
| libetc table | rebuild at `0x800917D0` / slot `0x800917F0` | Dump gap LBA 283 was all-zero |
| libetc thunks | `j` to real callees | CPS + IRQ clobbered `$v0` → `jalr 0xFFFFFFFF` |
| libgpu env | zero `0x8008B4A8..B4FF`, GCB=`0x8008B508` | Dump garbage → DrawSync/ResetGraph fatals |
| GPU printf stub | `jr $ra` at `0x80064618` | Jump-table slot pointed at zero gap |
| libgpu MMIO ptrs | restore GP0/GP1/DMA at `0x8008B5F4` | Dump zeros → ResetGraph store-to-null |
| ResetGraph / libgpu `-0x60` jals | clear/GPU_cw/GPU_reset → real prologues | Mid-DrawSync / mid-epilogue → hang |
| ResetGraph / GPU_reset `j` | epilogue / continue (MotK-matched) | Self-loop at `0x80065898` / `0x80067F54` |
| SetGraphDebug jal | `0x800658AC` → `0x8006590C` | Called ResetGraph path-B → jalr `0xFFFFFFFF` |
| SetIMask jals / stub | `0x8006BD84` → `0x8006BDE4` | Stub left `I_MASK` unrestored |
| GCB table | relocate `0x8008B508` → `0x8008B570` | ResetGraph `clear_bytes` filled table with `0xFF` |
| startIntr helper jal | `0x8006BC38` → `0x8006C044` (libetc+8) | Was wrongly InterruptCallback |

## Debug-tools probe results (2026-07-23)

Port **4530**, `PSX_BIOS_HLE=0`, headless software renderer:

| Check | Result |
|-------|--------|
| Pre-fix exit | Frame ~1240 `DISPATCH FATAL` / `PC=0` (ResetGraph / SetGraphDebug) |
| After above patches | **No longer exits** — runs past frame 5000+ |
| `gpu_state` post-ResetGraph | `disabled=1`, `gp0_writes` stuck ~32083, res 256×240 |
| `i_mask` | `0` after ResetGraph (was `0xC` on license). VBlank in `i_stat` ignored |
| `$ra` while stuck | `0x800585E0` → inside `jal 0x80068A60` (Gs/GTE init → BIOS `B0:0x56`) |
| screenshot | License OK until ResetGraph; then “display disabled” |

**Current blocker:** after ResetGraph the display stays off and `I_MASK=0`, so the
game sits in early GPU/Gs init waiting on IRQs that never fire. Looks like a
frozen license / black screen (no new guest draws) even though the host FPS
counter still ticks. Next: finish libetc IRQ arming (ExitCritical vs SetIMask
ordering, remaining `6BC38` sites at `0x80070160` / `0x80070190`) and confirm
`I_MASK` non-zero + `disabled=0` + climbing `gp0_writes` toward title.

## 5P / netplay contract

| Knob | Value |
|------|--------|
| CMake | `MAX_PLAYERS 5` on `psxrecomp_add_runtime_target` |
| `game.toml` | `players = 5` (lobby `max_slots`) |
| MotK / default | omit `MAX_PLAYERS` → 2 |

Hardware map when multitap is on (`slot_count >= 3` or offline after game
start with `players >= 3`): port1 SCPH-1070 pads A–D = P1–P4, port2 = P5.

## Open

- **Title/menu not confirmed.** Continue libgpu `.data` recovery (more fields
  around `0x8008B5xx` may still be dump garbage) and/or compare Beetle on the
  same `bpe/*.bin`.
- Prefer `PSX_BIOS_HLE=0` for bring-up.
- Full 5P netplay needs matching lobby-server build (`MAX_SLOTS=5`) and live
  multi-client validation.
- Boxart / launcher assets not added yet.
