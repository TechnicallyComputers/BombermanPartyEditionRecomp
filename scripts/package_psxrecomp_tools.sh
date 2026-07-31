#!/usr/bin/env bash
# Package a redistributable psxrecomp tools tree for setup hosts / RetComM.
# Does not include disc images, BIOS dumps, or game generated/ C.
#
# Usage:
#   scripts/package_psxrecomp_tools.sh [psxrecomp-root] [os-tag] [out-dir]
# Example:
#   ./scripts/package_psxrecomp_tools.sh . linux-x64
#
# Expects a built recompiler binary under recompiler/build/psxrecomp-game[.exe].
# Writes: <out-dir>/psxrecomp-tools-<os-tag>.zip

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_ROOT="$(cd "$SCRIPT_DIR/../psxrecomp" && pwd)"
PSX_ROOT="${1:-$DEFAULT_ROOT}"
OS_TAG="${2:-linux-x64}"
OUT="${3:-"$DEFAULT_ROOT/dist/packs"}"

if [[ ! -f "$PSX_ROOT/psxrecomp_cli.py" ]]; then
  echo "usage: $0 [psxrecomp-root] [os-tag] [out-dir]" >&2
  echo "  psxrecomp-root must contain psxrecomp_cli.py" >&2
  exit 2
fi

PSX_ROOT="$(cd "$PSX_ROOT" && pwd)"
STAGE="$OUT/stage-psxrecomp-tools-$OS_TAG"
ZIP_NAME="psxrecomp-tools-${OS_TAG}.zip"

find_tool_bin() {
  local name="$1"
  local cand
  for cand in \
    "$PSX_ROOT/recompiler/build/${name}" \
    "$PSX_ROOT/recompiler/build/${name}.exe" \
    "$PSX_ROOT/recompiler/build/Release/${name}.exe"
  do
    if [[ -f "$cand" ]]; then
      echo "$cand"
      return 0
    fi
  done
  return 1
}

GAME_BIN="$(find_tool_bin psxrecomp-game || true)"
BIOS_BIN="$(find_tool_bin psxrecomp-bios || true)"
if [[ -z "$GAME_BIN" ]]; then
  echo "error: psxrecomp-game not found under $PSX_ROOT/recompiler/build" >&2
  echo "  cmake -S recompiler -B recompiler/build -G Ninja -DCMAKE_BUILD_TYPE=Release" >&2
  echo "  cmake --build recompiler/build --target psxrecomp-game psxrecomp-bios" >&2
  exit 1
fi
if [[ -z "$BIOS_BIN" ]]; then
  echo "error: psxrecomp-bios not found under $PSX_ROOT/recompiler/build" >&2
  echo "  (required so setup hosts can regenerate OpenBIOS locally)" >&2
  exit 1
fi

rm -rf "$STAGE"
mkdir -p "$STAGE/recompiler/build" "$STAGE/docs" "$OUT"

copy_tree() {
  local src="$1" dest="$2"
  if [[ -e "$src" ]]; then
    mkdir -p "$(dirname "$dest")"
    cp -a "$src" "$dest"
  fi
}

copy_tree "$PSX_ROOT/psxrecomp_cli.py" "$STAGE/psxrecomp_cli.py"
copy_tree "$PSX_ROOT/tools" "$STAGE/tools"
copy_tree "$PSX_ROOT/docs/LOCAL_CODEGEN_SDK.md" "$STAGE/docs/LOCAL_CODEGEN_SDK.md"
copy_tree "$PSX_ROOT/README.md" "$STAGE/README.md"

# Keep recompiler sources out of the slim tools pack; ship binaries only.
# Full game source zips still include the recompiler tree for from-source builds.
cp -a "$GAME_BIN" "$STAGE/recompiler/build/$(basename "$GAME_BIN")"
cp -a "$BIOS_BIN" "$STAGE/recompiler/build/$(basename "$BIOS_BIN")"
chmod +x "$STAGE/recompiler/build/$(basename "$GAME_BIN")" 2>/dev/null || true
chmod +x "$STAGE/recompiler/build/$(basename "$BIOS_BIN")" 2>/dev/null || true

# Drop caches / VCS / tests from tools.
find "$STAGE" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -type d -name 'tests' -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -type d -name '.git' -prune -exec rm -rf {} + 2>/dev/null || true

cat >"$STAGE/retcomm-sdk.json" <<'EOF'
{
  "cli": "psxrecomp_cli.py",
  "id": "psxrecomp-tools",
  "game_bin": "recompiler/build/psxrecomp-game",
  "bios_bin": "recompiler/build/psxrecomp-bios"
}
EOF

cat >"$STAGE/README.retcomm.md" <<EOF
# psxrecomp-tools ($OS_TAG)

Headless verify-disc / generate / rebuild SDK for setup hosts and RetComM.

Point RetComM at this directory with \`RETCOMM_SDK_DIR\`, or merge under a game
project as \`psxrecomp/\` (cli + tools + recompiler/build/{psxrecomp-game,
psxrecomp-bios}).

Requires Python 3 on the host. Never ship user discs, BIOS dumps, or
\`generated/\` game/BIOS C in this pack — first-run generate emits those locally.
EOF

rm -f "$OUT/$ZIP_NAME"
( cd "$STAGE" && zip -qr "$OUT/$ZIP_NAME" . )
echo "Wrote $OUT/$ZIP_NAME"
echo "Smoke: PSXRECOMP_GAME=$STAGE/recompiler/build/$(basename "$GAME_BIN") python3 $STAGE/psxrecomp_cli.py --help"
