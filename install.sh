#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
bin_dir="$HOME/.local/bin"
libexec_dir="$HOME/.local/libexec/atlas-steamos-updater"
applications_dir="$HOME/.local/share/applications"
state_dir="$HOME/.local/state/atlas-steamos-updater"

mkdir -p "$bin_dir" "$libexec_dir" "$applications_dir" "$state_dir"

container="atlas"
if [[ -f "$state_dir/container" ]]; then
  container="$(sed -n '1p' "$state_dir/container")"
fi
auto_update=false
if [[ -f "$state_dir/auto-update" ]] && [[ "$(sed -n '1p' "$state_dir/auto-update")" == "enabled" ]]; then
  auto_update=true
fi

while (($#)); do
  case "$1" in
    --container)
      if (($# < 2)); then
        echo "ERROR: --container requires a Distrobox name" >&2
        exit 2
      fi
      container="$2"
      shift 2
      ;;
    --auto-update)
      auto_update=true
      shift
      ;;
    --no-auto-update)
      auto_update=false
      shift
      ;;
    -h|--help)
      echo "Usage: ./install.sh [--container NAME] [--auto-update|--no-auto-update]"
      exit 0
      ;;
    *)
      echo "ERROR: unknown option: $1" >&2
      exit 2
      ;;
  esac
done

if [[ ! "$container" =~ ^[A-Za-z0-9_.-]+$ ]]; then
  echo "ERROR: invalid Distrobox name: $container" >&2
  exit 2
fi
if [[ "$project_dir" == *$'\n'* || "$project_dir" == *$'\r'* ]]; then
  echo "ERROR: project path contains a line break" >&2
  exit 2
fi

printf '%s\n' "$container" > "$state_dir/container"
printf '%s\n' "$project_dir" > "$state_dir/source-path"
if [[ "$auto_update" == true ]]; then
  printf '%s\n' enabled > "$state_dir/auto-update"
else
  printf '%s\n' disabled > "$state_dir/auto-update"
fi
chmod 0600 "$state_dir/container" "$state_dir/source-path" "$state_dir/auto-update"

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
echo "Distrobox: $container"
echo "Updater self-update: $auto_update"
echo "ATLAS .deb updates will open in a terminal and require confirmation."
