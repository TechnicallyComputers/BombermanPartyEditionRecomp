#!/usr/bin/env bash
# Stage a BPE release zip next to the built runtime (no BIOS/disc).
#
# Usage:
#   scripts/package_release.sh <build-dir> <artifact-tag>
# Example:
#   scripts/package_release.sh build-linux-netplay linux-netplay
#   BPE_RUNTIME_BIN_DIR=/usr/x86_64-w64-mingw32/bin \
#     scripts/package_release.sh build-mingw-netplay windows-x64-mingw
#
# Writes: dist/bpe-<VERSION>-<artifact-tag>.zip
#
# For Windows .exe packs, copies any non-system DLL imports found next to the
# exe or under BPE_RUNTIME_BIN_DIR (default /usr/x86_64-w64-mingw32/bin).
# Fully static MinGW Release builds (PSX_STATIC_RUNTIME=ON) usually need none.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="${1:-}"
ARTIFACT_TAG="${2:-}"
RUNTIME_BIN_DIR="${BPE_RUNTIME_BIN_DIR:-/usr/x86_64-w64-mingw32/bin}"

if [[ -z "${BUILD_DIR}" || -z "${ARTIFACT_TAG}" ]]; then
  echo "usage: $0 <build-dir> <artifact-tag>" >&2
  exit 2
fi

VERSION="$(tr -d '[:space:]' < "${ROOT}/VERSION")"
if [[ -z "${VERSION}" ]]; then
  echo "VERSION file is empty" >&2
  exit 1
fi

if [[ ! -d "${BUILD_DIR}" ]]; then
  echo "error: build dir not found: ${BUILD_DIR}" >&2
  exit 1
fi

BUILD_DIR="$(cd "${BUILD_DIR}" && pwd)"
DIST="${ROOT}/dist"
STAGE="${DIST}/stage-${ARTIFACT_TAG}"
ZIP_NAME="bpe-${VERSION}-${ARTIFACT_TAG}.zip"

rm -rf "${STAGE}"
mkdir -p "${STAGE}" "${DIST}"
rm -f "${DIST}/${ZIP_NAME}"

# Exe name is derived from WINDOW_TITLE → Bomberman_Party_Edition_Recompiled
EXE=""
for cand in \
  "${BUILD_DIR}/Bomberman_Party_Edition_Recompiled" \
  "${BUILD_DIR}/Bomberman_Party_Edition_Recompiled.exe" \
  "${BUILD_DIR}/Release/Bomberman_Party_Edition_Recompiled.exe" \
  "${BUILD_DIR}/psx-runtime" \
  "${BUILD_DIR}/psx-runtime.exe"
do
  if [[ -f "${cand}" ]]; then
    EXE="${cand}"
    break
  fi
done

if [[ -z "${EXE}" ]]; then
  echo "error: runtime executable not found under ${BUILD_DIR}" >&2
  ls -la "${BUILD_DIR}" >&2 || true
  exit 1
fi

cp -a "${EXE}" "${STAGE}/"
EXE_BASENAME="$(basename "${EXE}")"
STAGE_EXE="${STAGE}/${EXE_BASENAME}"

# recomp-ui POST_BUILD stages a flat assets/fonts + assets/img next to the exe.
EXE_DIR="$(dirname "${EXE}")"
if [[ ! -d "${EXE_DIR}/assets/fonts" || ! -d "${EXE_DIR}/assets/img" ]]; then
  echo "error: ${EXE_DIR}/assets/{fonts,img} missing — rebuild psx-runtime" >&2
  exit 1
fi
mkdir -p "${STAGE}/assets"
cp -a "${EXE_DIR}/assets/fonts" "${STAGE}/assets/"
cp -a "${EXE_DIR}/assets/img" "${STAGE}/assets/"

if [[ ! -f "${STAGE}/assets/fonts/LatoLatin-Regular.ttf" ]]; then
  echo "error: assets/fonts incomplete (missing LatoLatin-Regular.ttf)" >&2
  exit 1
fi
if [[ ! -f "${STAGE}/assets/img/boxart.tga" ]]; then
  if [[ -f "${ROOT}/launcher_assets/img/boxart.tga" ]]; then
    cp -a "${ROOT}/launcher_assets/img/boxart.tga" "${STAGE}/assets/img/boxart.tga"
  else
    echo "error: assets/img/boxart.tga missing (build POST_BUILD or launcher_assets/)" >&2
    exit 1
  fi
fi

# Windows MinGW: bundle DLL deps via shared psxrecomp helper.
if [[ "${EXE_BASENAME}" == *.exe ]]; then
  BUNDLE_DLLS="${ROOT}/psxrecomp/tools/bundle_mingw_dlls.sh"
  if [[ ! -f "${BUNDLE_DLLS}" ]]; then
    echo "error: psxrecomp/tools/bundle_mingw_dlls.sh not found" >&2
    exit 1
  fi
  chmod +x "${BUNDLE_DLLS}" 2>/dev/null || true
  bash "${BUNDLE_DLLS}" \
    --soft-missing \
    --runtime-bin "${RUNTIME_BIN_DIR}" \
    --search-dir "${EXE_DIR}" \
    --search-dir "${BUILD_DIR}" \
    --exe "${STAGE_EXE}" --dest "${STAGE}" --label "${EXE_BASENAME}"
fi

cp -a "${ROOT}/game.toml" "${STAGE}/"
cp -a "${ROOT}/VERSION" "${STAGE}/"

cat > "${STAGE}/README.txt" <<EOF
Bomberman Party Edition Recompiled ${VERSION}
Platform pack: ${ARTIFACT_TAG}

This build does NOT include a PlayStation BIOS or game disc.
On first launch, select:
  - SCPH1001.BIN (BIOS)
  - Your legally obtained Bomberman Party Edition disc image (.cue/.bin)

Netplay lobbies match on game title + this VERSION string.
Up to 5 players (multitap / netplay slots). Online lobbies need ICE
(libjuice) — this pack is built with RNET_ENABLE_ICE=ON.
EOF

(
  cd "${STAGE}"
  if command -v zip >/dev/null 2>&1; then
    zip -r -q "${DIST}/${ZIP_NAME}" .
  else
    echo "error: zip not found; install zip to package releases" >&2
    exit 1
  fi
)

rm -rf "${STAGE}"
echo "Wrote ${DIST}/${ZIP_NAME}"
sha256sum "${DIST}/${ZIP_NAME}" 2>/dev/null || true
