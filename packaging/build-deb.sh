#!/usr/bin/env bash
# Builds Ocean Mist's Debian package from the last commit of this checkout.
#
#   packaging/build-deb.sh [OUTPUT_FOLDER]     writes OUTPUT_FOLDER/ocean-mist_VERSION_all.deb (default: dist/)
#
#   OCEAN_MIST_VERSION=0.1.0 packaging/build-deb.sh     a release build with exactly that version
#   OCEAN_MIST_KEEP_WORK=1   packaging/build-deb.sh     keep the folder the package is assembled in
#
# It needs git and dpkg-deb (Ubuntu, Debian and their relatives have both). It installs nothing and needs no root.
#
# It packs the files git has committed at HEAD, not the working folder, so an unsaved edit or a stray file never
# ends up in the package, and the same commit gives the same bytes (file times come from the commit, entries are
# sorted, everything is owned by root). Commit first.
#
# Version: packaging/VERSION names the next release. A build of any other commit is a snapshot of it, written
# VERSION~gitDATE.SHA, which Debian sorts before VERSION itself; OCEAN_MIST_VERSION overrides that for a release.
#
# What the package holds (nothing from a streaming service, nothing personal):
#   /usr/lib/ocean-mist/             the player: the Python files, ui/ and engine/
#   /usr/bin/ocean-mist              starts it
#   /usr/share/applications/, /usr/share/icons/    the menu entry and the icon, from data/
#   /usr/share/doc/ocean-mist/       copyright and changelog
#   /etc/apparmor.d/ocean-mist-engine   the playback engine's sandbox permission (a configuration file)
# The playback engine itself is not in it: the player downloads that when the user asks.
set -euo pipefail

fail() { echo "build-deb.sh: $*" >&2; exit 1; }

for tool in git dpkg-deb tar gzip md5sum sha256sum cut tr awk grep find sort sed date install mktemp cp chmod touch python3; do
  command -v "$tool" >/dev/null 2>&1 || fail "needs $tool"
done

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
git -C "$repo" rev-parse --verify -q 'HEAD^{commit}' >/dev/null || fail "$repo has no commit to build from"
mkdir -p -- "${1:-$repo/dist}"
out="$(cd -- "${1:-$repo/dist}" && pwd -P)"

epoch="$(git -C "$repo" log -1 --format=%ct HEAD)"
short="$(git -C "$repo" rev-parse --short=7 HEAD)"

work="$(mktemp -d "${TMPDIR:-/tmp}/ocean-mist-deb.XXXXXX")"
cleanup() {
  if [ -n "${OCEAN_MIST_KEEP_WORK:-}" ]; then
    echo "build-deb.sh: kept $work" >&2
  else
    rm -rf -- "$work"
  fi
}
trap cleanup EXIT
src="$work/src"
pkg="$work/pkg"
mkdir "$src" "$pkg"

# the committed files the package is made of (the top-level Python files, and these folders)
git -C "$repo" archive --format=tar HEAD -- ':(glob)*.py' ui engine data packaging | tar -x -C "$src"

base="$(tr -d '[:space:]' < "$src/packaging/VERSION")"
if [ -n "${OCEAN_MIST_VERSION:-}" ]; then
  version="$OCEAN_MIST_VERSION"
else
  version="$base~git$(date -u -d "@$epoch" +%Y%m%d).$short"
fi
[[ "$version" =~ ^[0-9][A-Za-z0-9.+~-]*$ ]] || fail "'$version' is not a Debian version"

# ---- the files, where they go on the system ----
lib="$pkg/usr/lib/ocean-mist"
install -d "$lib" "$pkg/usr/bin" "$pkg/usr/share/applications" "$pkg/usr/share/icons/hicolor/scalable/apps" \
  "$pkg/usr/share/doc/ocean-mist" "$pkg/etc/apparmor.d" "$pkg/DEBIAN"
cp "$src"/*.py "$lib/"
cp -R "$src/ui" "$src/engine" "$lib/"
cp "$src/packaging/deb/ocean-mist" "$pkg/usr/bin/ocean-mist"
sed 's|@OCEAN_MIST_DIR@|/usr/lib/ocean-mist|g' "$src/data/io.github.nahuelgenchi.OceanMist.desktop.in" \
  > "$pkg/usr/share/applications/io.github.nahuelgenchi.OceanMist.desktop"
cp "$src/data/io.github.nahuelgenchi.OceanMist.svg" "$pkg/usr/share/icons/hicolor/scalable/apps/"
cp "$src/packaging/deb/ocean-mist-engine.apparmor" "$pkg/etc/apparmor.d/ocean-mist-engine"
cp "$src/packaging/deb/copyright" "$pkg/usr/share/doc/ocean-mist/copyright"

maintainer="$(sed -n 's/^Maintainer: //p' "$src/packaging/deb/control.in")"
{
  printf 'ocean-mist (%s) unstable; urgency=medium\n\n' "$version"
  printf '  * Built from commit %s of https://github.com/NahuelGenchi/ocean-mist.\n\n' "$short"
  printf ' -- %s  %s\n' "$maintainer" "$(date -u -R -d "@$epoch")"
} | gzip -9n > "$pkg/usr/share/doc/ocean-mist/changelog.gz"

# modes do not depend on how git or the shell's umask left them
find "$pkg" -type d -exec chmod 755 {} +
find "$pkg" -type f -exec chmod 644 {} +
chmod 755 "$lib/engine/allow-sandbox.sh" "$pkg/usr/bin/ocean-mist"

# ---- what dpkg wants to know about it ----
installed_kib="$(find "$pkg" -type f -printf '%s\n' | awk '{ s += $1 } END { print int((s + 1023) / 1024) }')"
sed -e "s|@VERSION@|$version|" -e "s|@INSTALLED_SIZE@|$installed_kib|" "$src/packaging/deb/control.in" > "$pkg/DEBIAN/control"
echo /etc/apparmor.d/ocean-mist-engine > "$pkg/DEBIAN/conffiles"
cp "$src/packaging/deb/postinst" "$src/packaging/deb/prerm" "$pkg/DEBIAN/"
chmod 755 "$pkg/DEBIAN/postinst" "$pkg/DEBIAN/prerm"
chmod 644 "$pkg/DEBIAN/control" "$pkg/DEBIAN/conffiles"
# checksums of every file but the configuration files, which dpkg tracks on its own
while IFS= read -r -d '' file; do
  rel="${file#./}"
  if grep -qxF "/$rel" "$pkg/DEBIAN/conffiles"; then continue; fi
  printf '%s  %s\n' "$(md5sum < "$pkg/$rel" | cut -d' ' -f1)" "$rel"
done < <(cd "$pkg" && find . -path ./DEBIAN -prune -o -type f -print0 | LC_ALL=C sort -z) > "$pkg/DEBIAN/md5sums"
chmod 644 "$pkg/DEBIAN/md5sums"

# ---- checks that the tree is what it should be ----
[ -z "$(find "$pkg" -perm /7000 -print -quit)" ] || fail "a file has a set-user-id, set-group-id or sticky bit"
[ -z "$(find "$pkg" \( -name __pycache__ -o -name '*.pyc' \) -print -quit)" ] || fail "compiled Python files got in"
for file in player.py ui/index.html engine/helper/main.js engine/allow-sandbox.sh; do
  [ -f "$lib/$file" ] || fail "$file is missing"
done
[ -x "$lib/engine/allow-sandbox.sh" ] || fail "engine/allow-sandbox.sh is not executable"
python3 -I -c 'import sys
for name in sys.argv[1:]:
    compile(open(name, "rb").read(), name, "exec")' "$lib"/*.py || fail "a Python file does not parse"
for script in "$pkg/DEBIAN/postinst" "$pkg/DEBIAN/prerm" "$pkg/usr/bin/ocean-mist"; do
  sh -n "$script" || fail "$script does not parse"
done
bash -n "$lib/engine/allow-sandbox.sh" || fail "engine/allow-sandbox.sh does not parse"
if command -v apparmor_parser >/dev/null 2>&1; then
  apparmor_parser -Q -T -K "$pkg/etc/apparmor.d/ocean-mist-engine" || fail "AppArmor does not accept the engine's profile"
else
  echo "build-deb.sh: apparmor_parser not found, the profile was not parsed" >&2
fi
if command -v desktop-file-validate >/dev/null 2>&1; then
  desktop-file-validate "$pkg/usr/share/applications/io.github.nahuelgenchi.OceanMist.desktop" || fail "the menu entry is not valid"
fi

# ---- build it: times from the commit, entries sorted by dpkg-deb, owner root ----
find "$pkg" -exec touch -h -d "@$epoch" {} +
deb="$out/ocean-mist_${version}_all.deb"
SOURCE_DATE_EPOCH="$epoch" dpkg-deb --root-owner-group -Zxz --build "$pkg" "$deb" > /dev/null
dpkg-deb --info "$deb" > /dev/null || fail "dpkg-deb cannot read the package it just built"

echo "$deb"
echo "  version $version, $(find "$deb" -printf '%s') bytes, sha256 $(sha256sum "$deb" | cut -d' ' -f1)"
