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
#   sudo bash allow-sandbox.sh PATH_TO_THE_ENGINE_FILE     add the profile for that file
#   sudo bash allow-sandbox.sh --remove [PATH]             take it away again (without PATH: every one of Ocean Mist's)
#   bash allow-sandbox.sh --print PATH                     show the profile, change nothing (no sudo)
set -euo pipefail

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
  local program="$1"
  case "$program" in
    /*) ;;
    *) echo "Give the engine's file with its whole path (it starts with /)."; exit 1 ;;
  esac
  case "$program" in
    *[!A-Za-z0-9_./+-]*) echo "The path has a character the profile cannot hold: $program"; exit 1 ;;
  esac
  if [ ! -f "$program" ] || [ -L "$program" ] || [ ! -x "$program" ] || [ "$(basename "$program")" != "electron" ]; then
    echo "That is not the engine's file (a program named electron, not a link): $program"
    exit 1
  fi
  if [ ! -f "$(dirname "$program")/libffmpeg.so" ]; then
    echo "That does not look like the engine's folder: $(dirname "$program")"
    exit 1
  fi
}

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
    if [ "$(stat -c %u "$program")" != "${SUDO_UID:-0}" ]; then
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
    file="$DIR/$(name_for "$program")"
    profile_for "$program" > "$file"
    apparmor_parser -r "$file"
    echo "Done: that one file may now create its sandbox."
    echo "Take it away again with:  sudo bash $0 --remove $program"
    ;;
esac
