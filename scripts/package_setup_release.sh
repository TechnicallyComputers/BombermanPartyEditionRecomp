#!/usr/bin/env bash
# Stage a BPE *setup host* zip: GUI generate & rebuild without shipping game C.
#
# Usage:
#   scripts/package_setup_release.sh <build-dir> <artifact-tag> [recompiler-build-dir]
# Example:
#   scripts/package_setup_release.sh build-ci linux-x64 build-recompiler
#
# Writes: dist/bpe-<VERSION>-<artifact-tag>.zip
#
# Contents (no disc / BIOS / generated game C):
#   Bomberman_Party_Edition_Recompiled[.exe]  — setup host
#   assets/, game.toml, VERSION, CMakeLists.txt, codegen_setup.*, seeds/
#   psxrecomp/   — runtime sources + cli + prebuilt psxrecomp-game
#   recomp-ui/   — launcher UI sources (needed to rebuild)
#   toolchain/   — optional; set BPE_TOOLCHAIN_DIR to a pack root to embed

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="${1:-}"
ARTIFACT_TAG="${2:-}"
RECOMPILER_BUILD="${3:-}"
RUNTIME_BIN_DIR="${BPE_RUNTIME_BIN_DIR:-/usr/x86_64-w64-mingw32/bin}"

if [[ -z "${BUILD_DIR}" || -z "${ARTIFACT_TAG}" ]]; then
  echo "usage: $0 <build-dir> <artifact-tag> [recompiler-build-dir]" >&2
  exit 2
fi

# Prefer CI/manual override, else repo VERSION pin.
VERSION="${BPE_RELEASE_VERSION:-}"
if [[ -z "${VERSION}" ]]; then
  VERSION="$(tr -d '[:space:]' < "${ROOT}/VERSION")"
fi
VERSION="$(printf '%s' "${VERSION}" | tr -d '[:space:]')"
VERSION="${VERSION#v}"
if [[ -z "${VERSION}" ]]; then
  echo "VERSION empty (set BPE_RELEASE_VERSION or write VERSION file)" >&2
  exit 1
fi
# Keep staged tree / lobby pin consistent with the package name.
printf '%s\n' "${VERSION}" >"${ROOT}/VERSION"

BUILD_DIR="$(cd "${BUILD_DIR}" && pwd)"
DIST="${ROOT}/dist"
STAGE="${DIST}/stage-setup-${ARTIFACT_TAG}"
ZIP_NAME="bpe-${VERSION}-${ARTIFACT_TAG}.zip"

rm -rf "${STAGE}"
mkdir -p "${STAGE}" "${DIST}"
rm -f "${DIST}/${ZIP_NAME}"

EXE=""
for cand in \
  "${BUILD_DIR}/Bomberman_Party_Edition_Recompiled" \
  "${BUILD_DIR}/Bomberman_Party_Edition_Recompiled.exe" \
  "${BUILD_DIR}/Release/Bomberman_Party_Edition_Recompiled.exe"
do
  if [[ -f "${cand}" ]]; then
    EXE="${cand}"
    break
  fi
done
if [[ -z "${EXE}" ]]; then
  echo "error: setup host executable not found under ${BUILD_DIR}" >&2
  ls -la "${BUILD_DIR}" >&2 || true
  exit 1
fi

cp -a "${EXE}" "${STAGE}/"
EXE_BASENAME="$(basename "${EXE}")"
EXE_DIR="$(dirname "${EXE}")"

if [[ ! -d "${EXE_DIR}/assets/fonts" || ! -d "${EXE_DIR}/assets/img" ]]; then
  echo "error: ${EXE_DIR}/assets/{fonts,img} missing — rebuild psx-runtime" >&2
  exit 1
fi
mkdir -p "${STAGE}/assets"
cp -a "${EXE_DIR}/assets/fonts" "${STAGE}/assets/"
cp -a "${EXE_DIR}/assets/img" "${STAGE}/assets/"
if [[ ! -f "${STAGE}/assets/img/boxart.tga" && -f "${ROOT}/launcher_assets/img/boxart.tga" ]]; then
  cp -a "${ROOT}/launcher_assets/img/boxart.tga" "${STAGE}/assets/img/boxart.tga"
fi

# Game project sources needed for local generate + cmake rebuild.
copy_proj() {
  local rel="$1"
  if [[ -e "${ROOT}/${rel}" ]]; then
    mkdir -p "$(dirname "${STAGE}/${rel}")"
    cp -a "${ROOT}/${rel}" "${STAGE}/${rel}"
  else
    echo "error: missing ${rel}" >&2
    exit 1
  fi
}

copy_proj "CMakeLists.txt"
copy_proj "game.toml"
copy_proj "VERSION"
copy_proj "codegen_setup.c"
copy_proj "codegen_setup.h"
copy_proj "seeds"
copy_proj "launcher_assets"
copy_proj "DISC.md"
copy_proj "README.md"

# Framework + UI (drop git metadata / build trees / generated BIOS artifacts).
copy_tree_filtered() {
  local src="$1" dest="$2"
  shift 2
  mkdir -p "${dest}"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a "$@" "${src}/" "${dest}/"
  else
    # MSYS / minimal hosts: full copy then prune.
    cp -a "${src}/." "${dest}/"
    local pat
    for pat in "$@"; do
      case "${pat}" in
        --exclude) continue ;;
        .git|recompiler/build|generated|__pycache__|build|build-*)
          find "${dest}" -name "${pat}" -prune -exec rm -rf {} + 2>/dev/null || true
          ;;
      esac
    done
    rm -rf "${dest}/.git" "${dest}/recompiler/build" "${dest}/generated" 2>/dev/null || true
    find "${dest}" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
    find "${dest}" -type d \( -name 'build' -o -name 'build-*' \) -prune -exec rm -rf {} + 2>/dev/null || true
  fi
}

copy_tree_filtered "${ROOT}/psxrecomp" "${STAGE}/psxrecomp" \
  --exclude '.git' \
  --exclude 'recompiler/build' \
  --exclude 'generated' \
  --exclude '__pycache__' \
  --exclude 'build' \
  --exclude 'build-*'

copy_tree_filtered "${ROOT}/recomp-ui" "${STAGE}/recomp-ui" \
  --exclude '.git' \
  --exclude 'build' \
  --exclude '__pycache__'

# Prebuilt recompiler binary into the staged tree.
GAME_BIN=""
SEARCH_ROOTS=()
if [[ -n "${RECOMPILER_BUILD}" ]]; then
  SEARCH_ROOTS+=("$(cd "${RECOMPILER_BUILD}" && pwd)")
fi
SEARCH_ROOTS+=(
  "${ROOT}/psxrecomp/recompiler/build"
  "${ROOT}/build-recompiler"
)
for dir in "${SEARCH_ROOTS[@]}"; do
  for cand in \
    "${dir}/psxrecomp-game" \
    "${dir}/psxrecomp-game.exe" \
    "${dir}/Release/psxrecomp-game.exe"
  do
    if [[ -f "${cand}" ]]; then
      GAME_BIN="${cand}"
      break 2
    fi
  done
done
if [[ -z "${GAME_BIN}" ]]; then
  echo "error: psxrecomp-game not found (pass recompiler-build-dir)" >&2
  exit 1
fi
mkdir -p "${STAGE}/psxrecomp/recompiler/build"
cp -a "${GAME_BIN}" \
  "${STAGE}/psxrecomp/recompiler/build/$(basename "${GAME_BIN}")"
chmod +x "${STAGE}/psxrecomp/recompiler/build/$(basename "${GAME_BIN}")" 2>/dev/null || true

# Optional bundled toolchain (cmake/ninja/compiler pack root).
if [[ -n "${BPE_TOOLCHAIN_DIR:-}" && -d "${BPE_TOOLCHAIN_DIR}" ]]; then
  mkdir -p "${STAGE}/toolchain"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete "${BPE_TOOLCHAIN_DIR}/" "${STAGE}/toolchain/"
  else
    cp -a "${BPE_TOOLCHAIN_DIR}/." "${STAGE}/toolchain/"
  fi
  echo "bundled toolchain from ${BPE_TOOLCHAIN_DIR}"
fi

# Never ship game generated C or disc/BIOS.
rm -rf "${STAGE}/generated" "${STAGE}/bpe"
rm -f "${STAGE}/psxrecomp/bios/SCPH1001.BIN" \
      "${STAGE}/psxrecomp/bios/SCPH1001.bin" 2>/dev/null || true

# Windows MinGW DLL bundling (same heuristic as package_release.sh).
if [[ "${EXE_BASENAME}" == *.exe ]]; then
  if command -v x86_64-w64-mingw32-objdump >/dev/null 2>&1; then
    OBJDUMP=x86_64-w64-mingw32-objdump
  elif command -v objdump >/dev/null 2>&1; then
    OBJDUMP=objdump
  else
    OBJDUMP=""
  fi
  if [[ -n "${OBJDUMP}" ]]; then
    mapfile -t needed < <(
      "${OBJDUMP}" -p "${STAGE}/${EXE_BASENAME}" 2>/dev/null \
        | awk '/DLL Name:/{print $3}' \
        | grep -viE '^(KERNEL32|USER32|GDI32|ADVAPI32|SHELL32|OLE32|OLEAUT32|WS2_32|WINMM|IMM32|SETUPAPI|VERSION|OPENGL32|COMCTL32|COMDLG32|RPCRT4|SHLWAPI|CRYPT32|BCRYPT|IPHLPAPI|NSI|DNSAPI|MSVCRT|UCRTBASE|VCRUNTIME|API-MS-).*\.DLL$' \
        | sort -u || true
    )
    for dll in "${needed[@]:-}"; do
      [[ -n "${dll}" ]] || continue
      for src in "${EXE_DIR}/${dll}" "${BUILD_DIR}/${dll}" "${RUNTIME_BIN_DIR}/${dll}"; do
        if [[ -f "${src}" ]]; then
          cp -f "${src}" "${STAGE}/"
          echo "bundled ${dll}"
          break
        fi
      done
    done
  fi
fi

cat >"${STAGE}/README-SETUP.txt" <<EOF
Bomberman Party Edition Recompiled ${VERSION} — setup host
Platform: ${ARTIFACT_TAG}

This package does NOT include recompiled game C, a BIOS, or a disc image.

1. Install Python 3 (for psxrecomp_cli.py).
2. Ensure a C/C++ toolchain is on PATH, or use ./toolchain (if bundled):
     . ./toolchain/env.sh          # Linux/macOS
3. Run ${EXE_BASENAME}
4. Pick your legally owned Bomberman Party Edition disc.
5. Click Generate & rebuild — sources land in generated/, then cmake builds
   the playable binary under build-release/ and relaunches it.

RetComM can drive the same flow via psxrecomp/psxrecomp_cli.py
(see psxrecomp/docs/LOCAL_CODEGEN_SDK.md).
EOF

(
  cd "${STAGE}"
  if command -v zip >/dev/null 2>&1; then
    zip -r -q "${DIST}/${ZIP_NAME}" .
  else
    echo "error: zip not found" >&2
    exit 1
  fi
)

echo "Wrote ${DIST}/${ZIP_NAME}"
du -h "${DIST}/${ZIP_NAME}"
