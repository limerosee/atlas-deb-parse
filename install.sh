#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
bin_dir="$HOME/.local/bin"
libexec_dir="$HOME/.local/libexec/atlas-steamos-updater"
applications_dir="$HOME/.local/share/applications"
state_dir="$HOME/.local/state/atlas-steamos-updater"

mkdir -p "$bin_dir" "$libexec_dir" "$applications_dir" "$state_dir"

current_vnd_handler="$(xdg-mime query default application/vnd.debian.binary-package 2>/dev/null || true)"
current_xdeb_handler="$(xdg-mime query default application/x-deb 2>/dev/null || true)"
if [[ "$current_vnd_handler" != "atlas-steamos-updater.desktop" ]]; then
  printf '%s\n' "$current_vnd_handler" > "$state_dir/previous-vnd-handler"
fi
if [[ "$current_xdeb_handler" != "atlas-steamos-updater.desktop" ]]; then
  printf '%s\n' "$current_xdeb_handler" > "$state_dir/previous-xdeb-handler"
fi

install -m 0755 \
  "$project_dir/src/atlas_steamos_updater.py" \
  "$bin_dir/atlas-steamos-updater"
install -m 0755 \
  "$project_dir/src/atlas_deb_installer.py" \
  "$libexec_dir/atlas_deb_installer.py"
install -m 0644 \
  "$project_dir/share/atlas-steamos-updater.desktop" \
  "$applications_dir/atlas-steamos-updater.desktop"

handler_path="$bin_dir/atlas-steamos-updater"
escaped_handler="${handler_path//&/\\&}"
escaped_handler="${escaped_handler//|/\\|}"
sed -i "s|@HANDLER@|$escaped_handler|" \
  "$applications_dir/atlas-steamos-updater.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$applications_dir"
fi

xdg-mime default atlas-steamos-updater.desktop application/vnd.debian.binary-package
xdg-mime default atlas-steamos-updater.desktop application/x-deb

echo "Installed ATLAS SteamOS Updater."
echo "ATLAS .deb updates will open in a terminal and require confirmation."
