#!/usr/bin/env bash
# Build build/wirezero_<version>-<rev>_arm64.deb from this repo.
# Needs: bash, python3, dpkg-deb. Pure Python, so no cross-compiler.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG=wirezero
REV="${REV:-1}"
ARCH="${ARCH:-arm64}"
MAINTAINER="${MAINTAINER:-Roger Morato <64442406+r-morato@users.noreply.github.com>}"
VERSION="$(python3 -c "import sys; sys.path.insert(0, '$ROOT'); import wirezero; print(wirezero.__version__)")"
FULLVER="${VERSION}-${REV}"
OUT="$ROOT/build"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
chmod 0755 "$STAGE"

APP="$STAGE/usr/share/APPLaunch"
LIB="$STAGE/usr/lib/$PKG"
install -d "$LIB/$PKG" "$APP/bin" "$APP/applications" "$APP/share/images" \
           "$STAGE/usr/share/doc/$PKG" "$STAGE/DEBIAN"

# Code and the root helper live in root-only system paths, not the launcher's
# app folder; the launcher only gets a tiny start script and the .desktop file.
install -m 0644 "$ROOT"/wirezero/*.py "$LIB/$PKG/"
install -m 0644 "$ROOT/packaging/run.py" "$LIB/run.py"
install -m 0755 "$ROOT/packaging/wirezero-helper" "$LIB/wirezero-helper"
install -m 0755 "$ROOT/packaging/wirezero-askpass" "$LIB/wirezero-askpass"
install -m 0755 "$ROOT/packaging/wirezero.sh" "$APP/bin/$PKG"
install -m 0644 "$ROOT/packaging/wirezero.desktop" "$APP/applications/$PKG.desktop"
install -m 0644 "$ROOT/packaging/wirezero.png" "$APP/share/images/$PKG.png"
install -m 0644 "$ROOT/LICENSE" "$STAGE/usr/share/doc/$PKG/copyright"

SIZE_KB="$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)"
cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG
Version: $FULLVER
Architecture: $ARCH
Maintainer: $MAINTAINER
Installed-Size: $SIZE_KB
Depends: python3 (>= 3.11), python3-pil, python3-evdev, sudo
Recommends: python3-picamera2, libzbar0t64 | libzbar0
Suggests: network-manager, wireguard-tools
Section: net
Homepage: https://github.com/r-morato/wirezero
Priority: optional
Description: WireGuard VPN tunnels for the CardputerZero
 WireZero imports WireGuard .conf files into NetworkManager and lets you
 connect, disconnect and inspect tunnels from the CardputerZero keyboard.
 The app never runs as root and installs no sudo or polkit rules. Changes
 that need admin rights ask for your password and go through a small helper
 that only acts on WireGuard profiles and never exposes private keys.
EOF

mkdir -p "$OUT"
DEB="$OUT/${PKG}_${FULLVER}_${ARCH}.deb"
dpkg-deb --root-owner-group -Zxz --build "$STAGE" "$DEB" >/dev/null
echo "$DEB"
