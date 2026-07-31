#!/usr/bin/env bash
# Local PGO via psxrecomp_cli (intros + OPENING.STR). Not used by CI.
# Usage: from repo root, DISPLAY=:0 ./scripts/pgo_bpe_intro.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="${BPE_BUILD_DIR:-${ROOT}/build-release}"
DISC="${PGO_DISC:-bpe/Bomberman Party Edition.cue}"
export DISPLAY="${DISPLAY:-:0}"
export PSX_BIOS_HLE="${PSX_BIOS_HLE:-0}"

cd "$ROOT"
exec python3 psxrecomp/psxrecomp_cli.py pgo-train \
  --config game.toml \
  --project-root "$ROOT" \
  --build-dir "$BUILD" \
  --target psx-runtime \
  --exe-basename Bomberman_Party_Edition_Recompiled \
  --disc "$DISC" \
  --train-secs "${PGO_TRAIN_SECS:-60}" \
  --train-runs "${PGO_TRAIN_RUNS:-2}"
