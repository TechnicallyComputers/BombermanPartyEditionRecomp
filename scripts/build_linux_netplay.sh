#!/usr/bin/env bash
# Configure + build a Linux Release netplay binary (ICE on), then zip it.
#
# Usage (from repo root):
#   bash scripts/build_linux_netplay.sh
#   bash scripts/build_linux_netplay.sh --no-package
#   bash scripts/build_linux_netplay.sh --build-dir build-linux-netplay
#
# Writes:
#   <build-dir>/Bomberman_Party_Edition_Recompiled
#   dist/bpe-<VERSION>-linux-netplay.zip  (unless --no-package)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="${ROOT}/build-linux-netplay"
DO_PACKAGE=1
JOBS="$(nproc 2>/dev/null || echo 4)"
ARTIFACT_TAG="linux-netplay"

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
    --jobs)
      JOBS="${2:?}"
      shift 2
      ;;
    --artifact-tag)
      ARTIFACT_TAG="${2:?}"
      shift 2
      ;;
    -h|--help)
      sed -n '2,14p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

cd "${ROOT}"

if [[ ! -f "${ROOT}/recomp-ui/recomp_ui.cmake" ]]; then
  echo "error: recomp-ui missing — run: git submodule update --init --recursive" >&2
  exit 1
fi
if [[ ! -f "${ROOT}/psxrecomp/runtime/runtime.cmake" ]]; then
  echo "error: psxrecomp missing — run: git submodule update --init --recursive" >&2
  exit 1
fi

VERSION="$(tr -d '[:space:]' < "${ROOT}/VERSION")"
if [[ -z "${VERSION}" ]]; then
  echo "VERSION file is empty" >&2
  exit 1
fi

# Relative path ok for cmake -B; package script wants a path under ROOT.
if [[ "${BUILD_DIR}" != /* ]]; then
  BUILD_DIR="${ROOT}/${BUILD_DIR}"
fi

echo "==> configure ${BUILD_DIR} (Release, ICE on, VERSION=${VERSION})"
cmake -S "${ROOT}" -B "${BUILD_DIR}" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DRNET_ENABLE_ICE=ON \
  -DPSX_GAME_VERSION="${VERSION}"

echo "==> build psx-runtime (-j${JOBS})"
cmake --build "${BUILD_DIR}" --target psx-runtime -j"${JOBS}"

EXE="${BUILD_DIR}/Bomberman_Party_Edition_Recompiled"
if [[ ! -x "${EXE}" ]]; then
  echo "error: expected executable missing: ${EXE}" >&2
  exit 1
fi
echo "Built ${EXE}"

if [[ "${DO_PACKAGE}" -eq 1 ]]; then
  echo "==> package ${ARTIFACT_TAG}"
  bash "${ROOT}/scripts/package_release.sh" "${BUILD_DIR}" "${ARTIFACT_TAG}"
fi
