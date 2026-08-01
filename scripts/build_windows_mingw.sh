#!/usr/bin/env bash
# Cross-compile a Windows x64 Release netplay binary from Linux (MinGW-w64),
# then zip it with launcher assets and any non-system DLL dependencies.
#
# Usage (from repo root):
#   bash scripts/build_windows_mingw.sh
#   bash scripts/build_windows_mingw.sh --no-package
#   bash scripts/build_windows_mingw.sh --build-dir build-mingw-netplay
#   bash scripts/build_windows_mingw.sh --dynamic   # ship SDL2 + libgcc DLLs
#
# Prerequisites (Arch / CachyOS):
#   pacman -S --needed mingw-w64-gcc mingw-w64-sdl2 cmake ninja zip
#
# Debian/Ubuntu (names vary by release):
#   apt install g++-mingw-w64-x86-64 cmake ninja-build zip
#   # plus a MinGW SDL2 that x86_64-w64-mingw32-pkg-config can see
#
# Writes:
#   <build-dir>/Bomberman_Party_Edition_Recompiled.exe
#   dist/bpe-<VERSION>-windows-x64-mingw.zip  (unless --no-package)
#
# Default links SDL2 + libgcc/libstdc++ statically (PSX_STATIC_RUNTIME=ON for
# MinGW Release) so the zip is mostly a single .exe + assets. Use --dynamic
# if static SDL2 fails on your distro; packaging then copies the DLLs.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="${ROOT}/build-mingw-netplay"
DO_PACKAGE=1
STATIC_RUNTIME=1
JOBS="$(nproc 2>/dev/null || echo 4)"
ARTIFACT_TAG="windows-x64-mingw"
if [[ -f "${ROOT}/psxrecomp/cmake/toolchain-mingw-w64.cmake" ]]; then
  TOOLCHAIN="${ROOT}/psxrecomp/cmake/toolchain-mingw-w64.cmake"
else
  TOOLCHAIN="${ROOT}/scripts/toolchain-mingw-w64.cmake"
fi
RUNTIME_BIN_DIR="/usr/x86_64-w64-mingw32/bin"
TRIPLE="x86_64-w64-mingw32"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --build-dir)
      BUILD_DIR="${2:?}"
      shift 2
      ;;
    --no-package)
      DO_PACKAGE=0
      shift
      ;;
    --dynamic)
      STATIC_RUNTIME=0
      shift
      ;;
    --jobs)
      JOBS="${2:?}"
      shift 2
      ;;
    --artifact-tag)
      ARTIFACT_TAG="${2:?}"
      shift 2
      ;;
    --runtime-bin-dir)
      RUNTIME_BIN_DIR="${2:?}"
      shift 2
      ;;
    --toolchain)
      TOOLCHAIN="${2:?}"
      shift 2
      ;;
    -h|--help)
      sed -n '2,28p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

cd "${ROOT}"

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "error: missing required tool: $1" >&2
    exit 1
  fi
}

need cmake
need ninja
need "${TRIPLE}-gcc"
need "${TRIPLE}-g++"
if ! command -v "${TRIPLE}-pkg-config" >/dev/null 2>&1; then
  echo "error: missing ${TRIPLE}-pkg-config (need MinGW SDL2 + pkg-config)" >&2
  exit 1
fi

if [[ ! -f "${ROOT}/recomp-ui/recomp_ui.cmake" ]]; then
  echo "error: recomp-ui missing — run: git submodule update --init --recursive" >&2
  exit 1
fi
if [[ ! -f "${ROOT}/psxrecomp/runtime/runtime.cmake" ]]; then
  echo "error: psxrecomp missing — run: git submodule update --init --recursive" >&2
  exit 1
fi
if [[ ! -f "${TOOLCHAIN}" ]]; then
  echo "error: toolchain file missing: ${TOOLCHAIN}" >&2
  exit 1
fi

VERSION="$(tr -d '[:space:]' < "${ROOT}/VERSION")"
if [[ -z "${VERSION}" ]]; then
  echo "VERSION file is empty" >&2
  exit 1
fi

if ! "${TRIPLE}-pkg-config" --exists sdl2; then
  echo "error: MinGW sdl2.pc not found via ${TRIPLE}-pkg-config" >&2
  echo "  Arch: pacman -S mingw-w64-sdl2" >&2
  exit 1
fi

if [[ "${BUILD_DIR}" != /* ]]; then
  BUILD_DIR="${ROOT}/${BUILD_DIR}"
fi

STATIC_FLAG=ON
if [[ "${STATIC_RUNTIME}" -eq 0 ]]; then
  STATIC_FLAG=OFF
fi

export PKG_CONFIG_PATH=""
export PKG_CONFIG_LIBDIR="/usr/${TRIPLE}/lib/pkgconfig"
MINGW_PKG_CONFIG="$(command -v "${TRIPLE}-pkg-config")"

echo "==> configure ${BUILD_DIR}"
echo "    Release, ICE on, PSX_STATIC_RUNTIME=${STATIC_FLAG}, VERSION=${VERSION}"
echo "    pkg-config: ${MINGW_PKG_CONFIG} (libdir=${PKG_CONFIG_LIBDIR})"
cmake -S "${ROOT}" -B "${BUILD_DIR}" -G Ninja \
  -DCMAKE_TOOLCHAIN_FILE="${TOOLCHAIN}" \
  -DCMAKE_BUILD_TYPE=Release \
  -DPKG_CONFIG_EXECUTABLE="${MINGW_PKG_CONFIG}" \
  -DRNET_ENABLE_ICE=ON \
  -DPSX_STATIC_RUNTIME="${STATIC_FLAG}" \
  -DPSX_GAME_VERSION="${VERSION}" \
  -DRNET_BUILD_EXAMPLES=OFF \
  -DRNET_BUILD_TESTS=OFF

echo "==> build psx-runtime (-j${JOBS})"
cmake --build "${BUILD_DIR}" --target psx-runtime -j"${JOBS}"

EXE="${BUILD_DIR}/Bomberman_Party_Edition_Recompiled.exe"
if [[ ! -f "${EXE}" ]]; then
  # Some generators nest under Release/
  if [[ -f "${BUILD_DIR}/Release/Bomberman_Party_Edition_Recompiled.exe" ]]; then
    EXE="${BUILD_DIR}/Release/Bomberman_Party_Edition_Recompiled.exe"
  else
    echo "error: expected executable missing: ${EXE}" >&2
    exit 1
  fi
fi
echo "Built ${EXE}"

if command -v "${TRIPLE}-objdump" >/dev/null 2>&1; then
  echo "==> non-system DLL imports (empty is ideal with PSX_STATIC_RUNTIME=ON):"
  "${TRIPLE}-objdump" -p "${EXE}" 2>/dev/null \
    | awk '/DLL Name:/{print $3}' \
    | grep -viE '^(KERNEL32|USER32|GDI32|ADVAPI32|SHELL32|OLE32|OLEAUT32|WS2_32|WINMM|IMM32|SETUPAPI|VERSION|OPENGL32|COMCTL32|COMDLG32|RPCRT4|SHLWAPI|CRYPT32|BCRYPT|IPHLPAPI|NSI|DNSAPI|MSVCRT|ucrtbase|VCRUNTIME|api-ms-).*\.dll$' \
    || true
fi

if [[ "${DO_PACKAGE}" -eq 1 ]]; then
  echo "==> package ${ARTIFACT_TAG}"
  BPE_RUNTIME_BIN_DIR="${RUNTIME_BIN_DIR}" \
    bash "${ROOT}/scripts/package_release.sh" "${BUILD_DIR}" "${ARTIFACT_TAG}"
fi
