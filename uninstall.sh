#!/usr/bin/env bash
set -euo pipefail

applications_dir="$HOME/.local/share/applications"
state_dir="$HOME/.local/state/atlas-steamos-updater"
previous_vnd_file="$state_dir/previous-vnd-handler"
previous_xdeb_file="$state_dir/previous-xdeb-handler"

if [[ -s "$previous_vnd_file" ]]; then
  previous_vnd_handler="$(sed -n '1p' "$previous_vnd_file")"
  if [[ -n "$previous_vnd_handler" && "$previous_vnd_handler" != "atlas-steamos-updater.desktop" ]]; then
    xdg-mime default "$previous_vnd_handler" application/vnd.debian.binary-package
  fi
fi
if [[ -s "$previous_xdeb_file" ]]; then
  previous_xdeb_handler="$(sed -n '1p' "$previous_xdeb_file")"
  if [[ -n "$previous_xdeb_handler" && "$previous_xdeb_handler" != "atlas-steamos-updater.desktop" ]]; then
    xdg-mime default "$previous_xdeb_handler" application/x-deb
  fi
fi

rm -f \
  "$HOME/.local/bin/atlas-launch" \
  "$applications_dir/atlas-distrobox.desktop" \
  "$HOME/.local/bin/atlas-steamos-updater" \
  "$HOME/.local/libexec/atlas-steamos-updater/atlas_deb_installer.py" \
  "$applications_dir/atlas-steamos-updater.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$applications_dir"
fi

echo "Removed ATLAS SteamOS Updater. Backups and state were retained."
