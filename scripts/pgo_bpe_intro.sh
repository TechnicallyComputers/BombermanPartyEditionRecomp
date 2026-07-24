#!/usr/bin/env bash
# BPE intro + OPENING.STR PGO: instrument → multi-run train → rebuild with profiles.
# Usage: from repo root, DISPLAY=:0 ./scripts/pgo_bpe_intro.sh
#
# Covers company logos through the opening FMV. Prefer a windowed OpenGL run
# (not --headless) so host present/audio paths are included in the profile.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="${ROOT}/build-release"
DISC='bpe/Bomberman Party Edition.cue'
TRAIN_SECS="${PGO_TRAIN_SECS:-120}"
TRAIN_RUNS="${PGO_TRAIN_RUNS:-3}"
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-/run/user/1000/xauth_dvexyE}"
export PSX_BIOS_HLE="${PSX_BIOS_HLE:-0}"

cd "$BUILD"
cmake .. -DCMAKE_BUILD_TYPE=Release -DPSX_PGO=generate
cmake --build . --target psx-runtime -j"$(nproc)"

# Soft-stop prior runs by exe path only (never pkill -f).
for p in /proc/[0-9]*; do
  exe=$(readlink "$p/exe" 2>/dev/null) || continue
  case "$exe" in *Bomberman_Party_Edition_Recompiled*)
    kill "$(basename "$p")" 2>/dev/null || true
    ;;
  esac
done
sleep 1

# Keep existing .gcda across runs so profiles merge (wipe only on fresh generate).
rm -rf "$BUILD/pgo"
mkdir -p "$BUILD/pgo"

cd "$ROOT"
for run in $(seq 1 "$TRAIN_RUNS"); do
  echo "PGO train run $run/$TRAIN_RUNS (${TRAIN_SECS}s)..."
  ./build-release/Bomberman_Party_Edition_Recompiled \
    --no-launcher --game game.toml --disc "$DISC" \
    >/tmp/bpe_pgo_train_${run}.log 2>&1 &
  MPID=$!
  sleep "$TRAIN_SECS"
  # Soft-stop so atexit/__gcov_exit flushes .gcda (default SIGKILL does not).
  if kill -0 "$MPID" 2>/dev/null; then
    kill -TERM "$MPID" 2>/dev/null || true
    for _ in $(seq 1 30); do
      kill -0 "$MPID" 2>/dev/null || break
      sleep 1
    done
    kill -KILL "$MPID" 2>/dev/null || true
  fi
  wait "$MPID" 2>/dev/null || true
done

n_gcda=$(find "$BUILD" -name '*.gcda' 2>/dev/null | wc -l)
echo "PGO profiles: $n_gcda .gcda under $BUILD"
if [[ "$n_gcda" -lt 1 ]]; then
  echo "ERROR: no profiles written" >&2
  exit 1
fi

cd "$BUILD"
cmake .. -DCMAKE_BUILD_TYPE=Release -DPSX_PGO=use
cmake --build . --target psx-runtime -j"$(nproc)"
echo "PGO use build ready: $BUILD/Bomberman_Party_Edition_Recompiled"
