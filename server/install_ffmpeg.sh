#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${ROOT_DIR}/udp_realtime/bin"
TARGET="${BIN_DIR}/ffmpeg"
TMP_DIR="$(mktemp -d)"
ARCH="$(uname -m)"

cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

mkdir -p "${BIN_DIR}"

if [[ -x "${TARGET}" ]]; then
  echo "[ok] ffmpeg already installed: ${TARGET}"
  "${TARGET}" -version | head -1
  exit 0
fi

case "${ARCH}" in
  x86_64|amd64)
    URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
    ;;
  aarch64|arm64)
    URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz"
    ;;
  *)
    echo "[error] unsupported architecture: ${ARCH}" >&2
    exit 1
    ;;
esac

echo "[download] ${URL}"
curl -fL --retry 3 --retry-delay 2 -o "${TMP_DIR}/ffmpeg.tar.xz" "${URL}"
tar -xJf "${TMP_DIR}/ffmpeg.tar.xz" -C "${TMP_DIR}"
FFMPEG_SRC="$(find "${TMP_DIR}" -maxdepth 2 -type f -name ffmpeg | head -1)"
if [[ -z "${FFMPEG_SRC}" ]]; then
  echo "[error] ffmpeg binary not found in archive" >&2
  exit 1
fi

install -m 0755 "${FFMPEG_SRC}" "${TARGET}"
echo "[ok] installed ${TARGET}"
"${TARGET}" -version | head -1
