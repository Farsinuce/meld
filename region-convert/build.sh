#!/usr/bin/env bash
# Build region-convert from the vendored source and drop the binary in bin/ for Meld to find.
# Linux/macOS. Requires a Rust toolchain (https://rustup.rs).
set -euo pipefail
cd "$(dirname "$0")"

cargo build --release

# name the output by OS/arch so Meld's resolver picks the right one
os="$(uname -s)"; arch="$(uname -m)"
case "$os" in
  Linux)  plat="linux" ;;
  Darwin) plat="macos" ;;
  *)      plat="$os" ;;
esac
case "$arch" in
  x86_64|amd64) a="x86_64" ;;
  arm64|aarch64) a="arm64" ;;
  *) a="$arch" ;;
esac

mkdir -p bin
cp target/release/region_converter "bin/region_converter-${plat}-${a}"
chmod +x "bin/region_converter-${plat}-${a}"
echo "built -> bin/region_converter-${plat}-${a}"
