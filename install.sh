#!/bin/sh
# hermes-manager bootstrap: packages, the tool itself, then `hermes-manager install`.
# Safe to rerun: it repairs and never updates Hermes or removes data.
# incus-base: containers only (no virtual machines). Ubuntu 24.04 has no incus-base, and its
# incus has no virtual machines either.
set -eu

LIB=/usr/local/lib/hermes-manager
SITE=/etc/hermes-manager/site.toml
SRC=$(cd "$(dirname "$0")" && pwd)

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
. /etc/os-release
case "${ID:-} ${VERSION_ID:-}" in
"debian 13" | "ubuntu 24.04" | "ubuntu 26.04") ;;
*) echo "warning: tested on Debian 13 and Ubuntu 24.04 and 26.04 only" >&2 ;;
esac

echo "Installing packages"
apt-get update -q >/dev/null
INCUS=incus-base
apt-cache show incus-base >/dev/null 2>&1 || INCUS=incus
# NEEDRESTART_SUSPEND: Ubuntu's needrestart would list the services to restart after the install.
DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 apt-get install -y -q \
	"$INCUS" btrfs-progs nftables curl python3 >/dev/null

echo "Installing hermes-manager into $LIB"
mkdir -p "$LIB"
rm -rf "$LIB/hermes_manager"
cp -r "$SRC/hermes_manager" "$SRC/hermes-manager" "$SRC/site.example.toml" "$LIB/"
# In place: every person's container mounts files/model-guard, and a replaced directory would
# leave them an empty one. Gateways load a changed model guard at their next restart.
mkdir -p "$LIB/files"
cp -r "$SRC/files/." "$LIB/files/"
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
