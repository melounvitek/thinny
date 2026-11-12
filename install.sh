#!/bin/sh
# hermes-manager bootstrap: packages, the tool itself, then `hermes-manager install`.
# Safe to rerun: it repairs and never updates Hermes or removes data.
set -eu

LIB=/usr/local/lib/hermes-manager
SITE=/etc/hermes-manager/site.toml
SRC=$(cd "$(dirname "$0")" && pwd)

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
. /etc/os-release
[ "${ID:-}" = debian ] && [ "${VERSION_ID:-}" = 13 ] || echo "warning: tested on Debian 13 only" >&2

echo "Installing packages"
apt-get update -q >/dev/null
DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
	podman uidmap passt fuse-overlayfs git curl python3 nftables >/dev/null

echo "Installing hermes-manager into $LIB"
mkdir -p "$LIB"
rm -rf "$LIB/hermes_manager" "$LIB/files"
cp -r "$SRC/hermes_manager" "$SRC/files" "$SRC/hermes-manager" "$SRC/site.example.toml" "$LIB/"
find "$LIB" -name __pycache__ -prune -exec rm -rf {} +
chmod 755 "$LIB/hermes-manager"
ln -sfn "$LIB/hermes-manager" /usr/local/sbin/hermes-manager

if [ ! -f "$SITE" ]; then
	install -d -m 755 /etc/hermes-manager
	install -m 644 "$SRC/site.example.toml" "$SITE"
	echo
	echo "Edit $SITE (model, LAN address, ...), then run this script again."
	exit 0
fi

exec /usr/local/sbin/hermes-manager install
