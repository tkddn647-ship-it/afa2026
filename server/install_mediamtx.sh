#!/usr/bin/env bash
# Download MediaMTX binary for RTSP relay (Pi publish → server record).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${ROOT_DIR}/udp_realtime/bin"
TARGET="${BIN_DIR}/mediamtx"
CFG_DIR="${ROOT_DIR}/udp_realtime/mediamtx"
TMP_DIR="$(mktemp -d)"
ARCH="$(uname -m)"
VERSION="${MEDIAMTX_VERSION:-v1.11.3}"

cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

mkdir -p "${BIN_DIR}" "${CFG_DIR}"

if [[ -x "${TARGET}" ]]; then
  echo "[ok] mediamtx already installed: ${TARGET}"
  "${TARGET}" --help 2>&1 | head -2 || true
  exit 0
fi

case "${ARCH}" in
  x86_64|amd64)
    ASSET="mediamtx_${VERSION}_linux_amd64.tar.gz"
    ;;
  aarch64|arm64)
    ASSET="mediamtx_${VERSION}_linux_arm64v8.tar.gz"
    ;;
  armv7l|armhf)
    ASSET="mediamtx_${VERSION}_linux_armv7.tar.gz"
    ;;
  *)
    echo "[error] unsupported architecture: ${ARCH}" >&2
    exit 1
    ;;
esac

URL="https://github.com/bluenviron/mediamtx/releases/download/${VERSION}/${ASSET}"
echo "[download] ${URL}"
curl -fL --retry 3 --retry-delay 2 -o "${TMP_DIR}/mediamtx.tgz" "${URL}"
tar -xzf "${TMP_DIR}/mediamtx.tgz" -C "${TMP_DIR}"

BIN_SRC="$(find "${TMP_DIR}" -maxdepth 2 -type f -name mediamtx | head -1)"
if [[ -z "${BIN_SRC}" ]]; then
  echo "[error] mediamtx binary not found in archive" >&2
  exit 1
fi

install -m 0755 "${BIN_SRC}" "${TARGET}"
if [[ ! -f "${CFG_DIR}/mediamtx.yml" ]]; then
  cat >"${CFG_DIR}/mediamtx.yml" <<'EOF'
logLevel: warn
rtsp: yes
rtspAddress: :8554
rtpAddress: :8004
rtcpAddress: :8005
paths:
  cam:
    source: publisher
EOF
fi

echo "[ok] installed ${TARGET}"
echo "[hint] start with: ${ROOT_DIR}/start_servers.sh"
