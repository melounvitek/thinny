#!/bin/sh
# thinny bootstrap: packages, the tool itself, then `thinny setup`.
# Safe to rerun: it repairs and never updates Hermes or removes data.
# incus-base: containers only (no virtual machines). Ubuntu 24.04 has no incus-base, and its
# incus has no virtual machines either.
set -eu

LIB=/usr/local/lib/thinny
SITE=/etc/thinny/site.toml
SRC=$(cd "$(dirname "$0")" && pwd)

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
# thinny update pulls the clone and runs it as root, and git refuses a clone of another user.
[ "$(stat -c %u "$SRC")" = 0 ] || { echo "$SRC must belong to root" >&2; exit 1; }
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
	"$INCUS" btrfs-progs nftables curl git python3 >/dev/null

echo "Installing thinny into $LIB"
mkdir -p "$LIB"
rm -rf "$LIB/thinny"
cp -r "$SRC/thinny" "$SRC/bin" "$SRC/site.example.toml" "$LIB/"
# In place: every instance's container mounts files/model-guard, and a replaced directory would
# leave them an empty one. Gateways load a changed model guard at their next restart.
mkdir -p "$LIB/files"
cp -r "$SRC/files/." "$LIB/files/"
find "$LIB" -name __pycache__ -prune -exec rm -rf {} +
chmod 755 "$LIB/bin/thinny"
ln -sfn "$LIB/bin/thinny" /usr/local/sbin/thinny
echo "$SRC" >"$LIB/source" # thinny update pulls this clone
# 755 whatever the umask: any account may fetch the office certificate in it.
install -d -m 755 /etc/thinny

if [ ! -f "$SITE" ]; then
	echo
	# The one-line install pipes this script into bash, so the questions read the terminal itself.
	if (exec </dev/tty) 2>/dev/null; then
		/usr/local/sbin/thinny init </dev/tty || { echo "To try again: sh $SRC/install.sh" >&2; exit 1; }
		echo
	else
		install -m 644 "$SRC/site.example.toml" "$SITE"
		echo "Edit $SITE (model, LAN address, ...), then run: sh $SRC/install.sh"
		exit 0
	fi
fi

/usr/local/sbin/thinny setup
# The commit set up: thinny update installs only a newer one.
git -C "$SRC" rev-parse HEAD >"$LIB/commit"
