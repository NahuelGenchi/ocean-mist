#!/usr/bin/env bash
# One-time step for Ocean Mist's Apple Music playback engine on Ubuntu 24.04 and later.
#
# The engine is Chromium, which keeps its pages in a sandbox that needs "user namespaces". Ubuntu only lets a
# program create them when AppArmor has a profile that names the program: it has one for Chrome, Chromium,
# Firefox and Visual Studio Code, for example. The engine is downloaded by the player, so Ubuntu has none
# for it, and Chromium refuses to start without its sandbox (it never runs without it).
#
# This adds the same kind of profile Ubuntu ships for Chrome. It names ONE file, gives it that single
# permission ("userns") and changes nothing else about it (flags=(unconfined) adds and removes no other
# restriction). Anything that replaces that file could use the permission too, which is why the profile
# names only the engine's file, in your own folder.
#
# The Debian package ships the same profile for the engine's default place, so with the package installed
# the player never needs this script; it is for a copy run from a checkout, and for a data folder elsewhere.
#
#   sudo bash allow-sandbox.sh PATH_TO_THE_ENGINE_FILE     add the profile for that file
#   sudo bash allow-sandbox.sh --remove [PATH]             take it away again (without PATH: every one of Ocean Mist's)
#   bash allow-sandbox.sh --print PATH                     show the profile, change nothing (no sudo)
#
# Ocean Mist runs this itself through pkexec, after the system's own password window (the password goes
# to the system, never to Ocean Mist), so nobody has to open a terminal. The commands above do the same by hand.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin   # nothing from the caller's PATH runs as root here

PREFIX="ocean-mist-engine"
DIR="/etc/apparmor.d"

usage() {
  echo "Usage:  sudo bash $0 PATH_TO_THE_ENGINE_FILE"
  echo "        sudo bash $0 --remove [PATH]"
  echo "        bash $0 --print PATH"
  exit 2
}

# the profile's name: the same for the same file
name_for() { printf '%s-%s' "$PREFIX" "$(printf '%s' "$1" | sha1sum | cut -c1-10)"; }

# the AppArmor ABI the system's own browser profiles are written for, else the newest it has
abi_for() {
  local file line newest
  for file in chrome firefox chromium opera brave; do
    [ -f "$DIR/$file" ] || continue
    line="$(grep -m1 -E '^abi <abi/[0-9.]+>,' "$DIR/$file" || true)"
    if [ -n "$line" ]; then
      printf '%s' "$line"
      return
    fi
  done
  newest="$(ls "$DIR/abi" 2>/dev/null | grep -E '^[0-9]+\.[0-9]+$' | sort -V | tail -1 || true)"
  printf 'abi <abi/%s>,' "${newest:-4.0}"
}

profile_for() {
  local program="$1" name abi
  name="$(name_for "$program")"
  abi="$(abi_for)"
  cat <<PROFILE
# Ocean Mist's Apple Music engine: lets this one file create the sandbox Chromium keeps its pages in.
# Added by engine/allow-sandbox.sh. Take it away with:  sudo bash allow-sandbox.sh --remove
$abi
include <tunables/global>

profile $name $program flags=(unconfined) {
  userns,
  @{exec_path} mr,
}
PROFILE
}

check_program() {
  local program="$1" real
  case "$program" in
    /*) ;;
    *) echo "Give the engine's file with its whole path (it starts with /)."; exit 1 ;;
  esac
  case "$program" in
    *[!A-Za-z0-9_./+-]*) echo "The path has a character the profile cannot hold: $program"; exit 1 ;;
  esac
  # AppArmor matches the file as the kernel resolves it: a link, a "." or ".." or a double slash in the path
  # would give a profile that never applies, and a "Done" that is not true
  real="$(realpath -e -- "$program" 2>/dev/null || true)"
  if [ "$real" != "$program" ]; then
    echo "Give the engine's file by its real path (no links, no \".\" or \"..\", no double slashes): ${real:-$program}"
    exit 1
  fi
  if [ ! -f "$program" ] || [ -L "$program" ] || [ ! -x "$program" ] || [ "$(basename "$program")" != "electron" ]; then
    echo "That is not the engine's file (a program named electron, not a link): $program"
    exit 1
  fi
  if [ ! -f "$(dirname "$program")/libffmpeg.so" ]; then
    echo "That does not look like the engine's folder: $(dirname "$program")"
    exit 1
  fi
}

# put the profile in place: parsed in a hidden file the system does not load, moved into place, then loaded
install_profile() {
  local program="$1" file tmp
  file="$DIR/$(name_for "$program")"
  tmp="$(mktemp "$DIR/.$PREFIX.XXXXXX")"
  trap 'rm -f "$tmp"' EXIT
  chmod 644 "$tmp"
  profile_for "$program" > "$tmp"
  if ! apparmor_parser -Q -K "$tmp"; then
    echo "AppArmor does not accept the profile, so nothing was added."
    exit 1
  fi
  mv -f "$tmp" "$file"
  trap - EXIT
  if ! apparmor_parser -r "$file"; then
    rm -f "$file"
    echo "AppArmor could not load the profile, so nothing was kept."
    exit 1
  fi
}

main() {
  local action program file files removed
  action="${1:-}"
  case "$action" in
    --print)
      [ $# -eq 2 ] || usage
      check_program "$2"
      profile_for "$2"
      ;;
    --remove)
      if [ "$(id -u)" -ne 0 ]; then
        echo "Run this with sudo:  sudo bash $0 $*"
        exit 1
      fi
      if [ $# -ge 2 ]; then
        files=("$DIR/$(name_for "$2")")
      else
        shopt -s nullglob
        files=("$DIR/$PREFIX"-*)
      fi
      removed=0
      for file in "${files[@]}"; do
        if [ -f "$file" ]; then
          apparmor_parser -R "$file" 2>/dev/null || true
          rm -f "$file"
          removed=$((removed + 1))
        fi
      done
      echo "Removed $removed profile(s)."
      ;;
    ""|-*)
      usage
      ;;
    *)
      program="$action"
      if [ "$(id -u)" -ne 0 ]; then
        echo "Run this with sudo:  sudo bash $0 $program"
        exit 1
      fi
      check_program "$program"
      # who asked: sudo says it in SUDO_UID, pkexec (the player's password window) in PKEXEC_UID
      if [ "$(stat -c %u "$program")" != "${SUDO_UID:-${PKEXEC_UID:-0}}" ]; then
        echo "The engine's file belongs to someone else than you; no profile is added for it."
        exit 1
      fi
      if ! command -v apparmor_parser >/dev/null 2>&1 || [ ! -d "$DIR" ]; then
        echo "This system has no AppArmor, so there is nothing to allow."
        exit 0
      fi
      if [ "$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)" != "1" ]; then
        echo "This system does not restrict user namespaces, so no profile is needed."
        exit 0
      fi
      install_profile "$program"
      echo "Done: that one file may now create its sandbox."
      echo "Take it away again with:  sudo bash $0 --remove $program"
      ;;
  esac
}

# run when it is started, not when a check reads it for its functions
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
