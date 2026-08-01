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
# Contents (no disc / retail BIOS / generated game C):
#   Bomberman_Party_Edition_Recompiled[.exe]  — setup host
#   assets/, game.toml, VERSION, CMakeLists.txt, codegen_setup.*, host/, seeds/
#   psxrecomp/   — runtime + CLI + OpenBIOS profiles + psxrecomp-game/bios
#   recomp-ui/   — launcher UI sources (needed to rebuild)
#   toolchain/   — optional; set BPE_TOOLCHAIN_DIR to a pack root to embed
#
# Same zip is used for standalone wizard installs and RetComM build/update.

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
copy_proj "host"
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

# Overlay SDK surface (CLI / prepare_disc / progress helpers) when present.
# CI copies psxrecomp-sdk into psxrecomp/ before calling this script; this
# covers local packaging from a clean submodule checkout.
if [[ -d "${ROOT}/psxrecomp-sdk" ]]; then
  if command -v rsync >/dev/null 2>&1; then
    rsync -a "${ROOT}/psxrecomp-sdk/" "${STAGE}/psxrecomp/"
  else
    cp -a "${ROOT}/psxrecomp-sdk/." "${STAGE}/psxrecomp/"
  fi
fi

# Prebuilt recompiler binaries (game emit + OpenBIOS regen).
find_tool_bin() {
  local name="$1"
  local dir cand
  for dir in "${SEARCH_ROOTS[@]}"; do
    for cand in \
      "${dir}/${name}" \
      "${dir}/${name}.exe" \
      "${dir}/Release/${name}.exe"
    do
      if [[ -f "${cand}" ]]; then
        echo "${cand}"
        return 0
      fi
    done
  done
  return 1
}

SEARCH_ROOTS=()
if [[ -n "${RECOMPILER_BUILD}" ]]; then
  SEARCH_ROOTS+=("$(cd "${RECOMPILER_BUILD}" && pwd)")
fi
SEARCH_ROOTS+=(
  "${ROOT}/psxrecomp/recompiler/build"
  "${ROOT}/build-recompiler"
)

GAME_BIN="$(find_tool_bin psxrecomp-game || true)"
BIOS_BIN="$(find_tool_bin psxrecomp-bios || true)"
if [[ -z "${GAME_BIN}" ]]; then
  echo "error: psxrecomp-game not found (pass recompiler-build-dir)" >&2
  exit 1
fi
if [[ -z "${BIOS_BIN}" ]]; then
  echo "error: psxrecomp-bios not found (required for OpenBIOS regen in wizard)" >&2
  exit 1
fi
mkdir -p "${STAGE}/psxrecomp/recompiler/build"
cp -a "${GAME_BIN}" \
  "${STAGE}/psxrecomp/recompiler/build/$(basename "${GAME_BIN}")"
cp -a "${BIOS_BIN}" \
  "${STAGE}/psxrecomp/recompiler/build/$(basename "${BIOS_BIN}")"
chmod +x "${STAGE}/psxrecomp/recompiler/build/$(basename "${GAME_BIN}")" 2>/dev/null || true
chmod +x "${STAGE}/psxrecomp/recompiler/build/$(basename "${BIOS_BIN}")" 2>/dev/null || true

# RetComM / CLI marker (same fields as the slim tools pack).
cat >"${STAGE}/psxrecomp/retcomm-sdk.json" <<'EOF'
{
  "cli": "psxrecomp_cli.py",
  "id": "psxrecomp-tools",
  "game_bin": "recompiler/build/psxrecomp-game",
  "bios_bin": "recompiler/build/psxrecomp-bios"
}
EOF

# OpenBIOS profiles required for first-run generate without a retail dump.
for f in OpenBIOS.toml openbios.bin OpenBIOS.LICENSE SCPH1001.toml; do
  if [[ ! -f "${STAGE}/psxrecomp/bios/${f}" ]]; then
    echo "error: missing psxrecomp/bios/${f} in staged tree" >&2
    exit 1
  fi
done
if [[ ! -f "${STAGE}/psxrecomp/psxrecomp_cli.py" ]]; then
  echo "error: missing psxrecomp/psxrecomp_cli.py (overlay psxrecomp-sdk before pack)" >&2
  exit 1
fi

# Bundled portable toolchain (cmake/ninja/compiler pack root).
# CI sets BPE_TOOLCHAIN_DIR from retcomm-toolchains so the zip is self-building.
if [[ -n "${BPE_TOOLCHAIN_DIR:-}" && -d "${BPE_TOOLCHAIN_DIR}" ]]; then
  if [[ ! -d "${BPE_TOOLCHAIN_DIR}/bin" ]]; then
    echo "error: BPE_TOOLCHAIN_DIR missing bin/: ${BPE_TOOLCHAIN_DIR}" >&2
    exit 1
  fi
  mkdir -p "${STAGE}/toolchain"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete "${BPE_TOOLCHAIN_DIR}/" "${STAGE}/toolchain/"
  else
    cp -a "${BPE_TOOLCHAIN_DIR}/." "${STAGE}/toolchain/"
  fi
  echo "bundled toolchain from ${BPE_TOOLCHAIN_DIR}"
else
  echo "warning: BPE_TOOLCHAIN_DIR unset — zip will need system cmake/ninja" >&2
fi

# Never ship game generated C or retail BIOS dumps.
rm -rf "${STAGE}/generated" "${STAGE}/bpe"
rm -f "${STAGE}/psxrecomp/bios/SCPH1001.BIN" \
      "${STAGE}/psxrecomp/bios/SCPH1001.bin" 2>/dev/null || true

# Windows MinGW: copy imported non-system DLLs next to an exe.
# Emitters are MSYS2 GCC builds (libstdc++/libgcc); the portable llvm-mingw
# toolchain does NOT provide those — they must ship beside the .exe.
bundle_mingw_dlls_into() {
  local exe="$1"
  local dest_dir="$2"
  local label="${3:-$(basename "${exe}")}"
  local objdump=""
  local dll src key
  local -a needed=()
  local -a unique=()
  local -A seen=()

  if [[ ! -f "${exe}" ]]; then
    echo "error: cannot bundle DLLs; missing ${exe}" >&2
    exit 1
  fi
  mkdir -p "${dest_dir}"

  if command -v x86_64-w64-mingw32-objdump >/dev/null 2>&1; then
    objdump="x86_64-w64-mingw32-objdump"
  elif command -v objdump >/dev/null 2>&1; then
    objdump="objdump"
  else
    echo "error: no objdump; cannot bundle MinGW DLLs for ${label}" >&2
    exit 1
  fi

  mapfile -t needed < <(
    "${objdump}" -p "${exe}" 2>/dev/null \
      | awk '/DLL Name:/{print $3}' \
      | grep -viE '^(KERNEL32|USER32|GDI32|ADVAPI32|SHELL32|OLE32|OLEAUT32|WS2_32|WINMM|IMM32|SETUPAPI|VERSION|OPENGL32|COMCTL32|COMDLG32|RPCRT4|SHLWAPI|CRYPT32|BCRYPT|IPHLPAPI|NSI|DNSAPI|MSVCRT|UCRTBASE|VCRUNTIME|API-MS-).*\.DLL$' \
      | sort -u || true
  )

  # Always probe common MinGW runtimes (objdump can miss unusual PE layouts).
  needed+=(
    SDL2.dll
    zlib1.dll
    libgcc_s_seh-1.dll
    libstdc++-6.dll
    libwinpthread-1.dll
    libssp-0.dll
  )

  for dll in "${needed[@]}"; do
    [[ -n "${dll}" ]] || continue
    key="$(printf '%s' "${dll}" | tr '[:upper:]' '[:lower:]')"
    if [[ -n "${seen[$key]:-}" ]]; then
      continue
    fi
    seen[$key]=1
    unique+=("${dll}")
  done

  for dll in "${unique[@]}"; do
    # Skip names the exe does not actually import.
    if ! "${objdump}" -p "${exe}" 2>/dev/null | grep -qi "DLL Name:[[:space:]]*${dll}"; then
      continue
    fi
    src=""
    for cand in \
        "$(dirname "${exe}")/${dll}" \
        "${EXE_DIR}/${dll}" \
        "${BUILD_DIR}/${dll}" \
        "${RUNTIME_BIN_DIR}/${dll}" \
        "/mingw64/bin/${dll}" \
        "/usr/x86_64-w64-mingw32/bin/${dll}"
    do
      if [[ -f "${cand}" ]]; then
        src="${cand}"
        break
      fi
    done
    if [[ -z "${src}" ]]; then
      echo "error: required DLL missing for ${label}: ${dll}" >&2
      echo "  looked in $(dirname "${exe}"), ${EXE_DIR}, ${BUILD_DIR}," \
           "${RUNTIME_BIN_DIR}, /mingw64/bin" >&2
      exit 1
    fi
    cp -f "${src}" "${dest_dir}/"
    echo "bundled ${dll} → ${dest_dir#${STAGE}/}/ (${label})"
  done
}

if [[ "${EXE_BASENAME}" == *.exe ]]; then
  bundle_mingw_dlls_into "${STAGE}/${EXE_BASENAME}" "${STAGE}" "${EXE_BASENAME}"
  for emitter in \
      "${STAGE}/psxrecomp/recompiler/build/psxrecomp-game.exe" \
      "${STAGE}/psxrecomp/recompiler/build/psxrecomp-bios.exe"
  do
    if [[ -f "${emitter}" ]]; then
      bundle_mingw_dlls_into \
        "${emitter}" \
        "$(dirname "${emitter}")" \
        "$(basename "${emitter}")"
    fi
  done
  # Emitters are required on Windows setup zips — fail closed if absent.
  for emitter in psxrecomp-game.exe psxrecomp-bios.exe; do
    if [[ ! -f "${STAGE}/psxrecomp/recompiler/build/${emitter}" ]]; then
      echo "error: missing ${emitter} in staged tree" >&2
      exit 1
    fi
    for dll in libgcc_s_seh-1.dll libstdc++-6.dll; do
      if [[ ! -f "${STAGE}/psxrecomp/recompiler/build/${dll}" ]]; then
        echo "error: ${emitter} staged without ${dll}" >&2
        exit 1
      fi
    done
  done
fi

cat >"${STAGE}/README-SETUP.txt" <<EOF
Bomberman Party Edition Recompiled ${VERSION} — setup package
Platform: ${ARTIFACT_TAG}

One zip for first install and updates. Does NOT include disc images, retail
BIOS dumps, or pre-generated game C. Emitters (psxrecomp-game / psxrecomp-bios)
and the CLI are inside psxrecomp/. A portable cmake/clang pack is under
toolchain/ (removed automatically after a successful Generate & rebuild).

Standalone:
1. Install Python 3.
2. Run ${EXE_BASENAME} (uses ./toolchain when present; else system cmake).
3. Provide your legally owned Bomberman Party Edition disc (and optional
   retail SCPH-1001 BIOS; otherwise OpenBIOS is regenerated locally).
4. Follow the Generate & rebuild wizard.

RetComM uses this same zip: it promotes tools + toolchain into shared caches,
prunes per-title copies after build, and preserves saves/user config.
EOF

# Normalize mtimes to "now" so extractors do not see CI clocks ahead of users
# (Ninja "build.ninja still dirty" loops). The CLI also clamps on rebuild.
find "${STAGE}" -exec touch -c {} + 2>/dev/null || find "${STAGE}" -exec touch {} +

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
