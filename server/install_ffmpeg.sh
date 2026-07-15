#!/usr/bin/env bash
# Install a static ffmpeg binary into sever/bin/ (~40MB, no apt dependencies).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$ROOT/bin"
TARGET="$BIN_DIR/ffmpeg"

mkdir -p "$BIN_DIR"

arch="$(uname -m)"
case "$arch" in
  x86_64) ffmpeg_arch="amd64" ;;
  aarch64 | arm64) ffmpeg_arch="arm64" ;;
  *)
    echo "[error] unsupported architecture: $arch"
    exit 1
    ;;
esac

if [[ -x "$TARGET" ]]; then
  echo "[ok] ffmpeg already installed: $TARGET"
  "$TARGET" -version | head -n 1
  exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

url="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-${ffmpeg_arch}-static.tar.xz"
archive="$tmp/ffmpeg.tar.xz"

echo "[download] $url"
if command -v curl >/dev/null 2>&1; then
  curl -fL --progress-bar -o "$archive" "$url"
elif command -v wget >/dev/null 2>&1; then
  wget -O "$archive" "$url"
else
  echo "[error] curl or wget is required"
  exit 1
fi

tar -xJf "$archive" -C "$tmp"
ffmpeg_bin="$(find "$tmp" -type f -name ffmpeg -executable | head -n 1)"
if [[ -z "$ffmpeg_bin" ]]; then
  echo "[error] ffmpeg binary not found in archive"
  exit 1
fi

cp "$ffmpeg_bin" "$TARGET"
chmod +x "$TARGET"

echo "[ok] installed $TARGET"
"$TARGET" -version | head -n 1
echo "[next] restart camera_server: python camera_server.py"
