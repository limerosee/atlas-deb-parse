#!/usr/bin/env bash
# Downloaded with curl; run on the SteamOS host, with terminal stdin intact.
set -euo pipefail

if [[ "${1:-}" == --help ]]; then
  echo 'Usage: bash bootstrap.sh [install.sh options]'
  echo 'Repository: $HOME/.local/share/atlas-steamos-updater/source'
  exit 0
fi
if [[ -n "${CONTAINER_ID:-}" || -e /run/.containerenv || -e /.dockerenv ]]; then
  echo 'ERROR: run this installer on the SteamOS host. Type exit first.' >&2
  exit 2
fi
command -v distrobox >/dev/null || { echo 'ERROR: distrobox is required' >&2; exit 2; }
command -v xdg-mime >/dev/null || { echo 'ERROR: xdg-mime is required on the host' >&2; exit 2; }

container=atlas
settings="$HOME/.local/state/atlas-steamos-updater/container"
if [[ -f "$settings" ]]; then IFS= read -r container < "$settings"; fi
arguments=("$@")
for ((i=0; i<${#arguments[@]}; i++)); do
  if [[ "${arguments[i]}" == --container ]]; then
    container="${arguments[i+1]:-}"
  fi
done
[[ "$container" =~ ^[A-Za-z0-9_.-]+$ ]] || { echo 'ERROR: invalid container name' >&2; exit 2; }

source_dir="$HOME/.local/share/atlas-steamos-updater/source"
origin=https://github.com/limerosee/atlas-deb-parse.git
git_in_box() { distrobox enter "$container" -- git "$@"; }
distrobox enter "$container" -- sudo dnf install -y git
if [[ -e "$source_dir" ]]; then
  [[ ! -L "$source_dir" && -d "$source_dir/.git" ]] || {
    echo "ERROR: source directory is not a regular Git checkout: $source_dir" >&2; exit 2;
  }
  actual_origin="$(git_in_box -C "$source_dir" remote get-url origin)"
  [[ "${actual_origin%.git}" == "${origin%.git}" ]] || { echo 'ERROR: unexpected repository origin' >&2; exit 2; }
  [[ -z "$(git_in_box -C "$source_dir" status --porcelain)" ]] || { echo 'ERROR: source has local changes; preserve or commit them before updating' >&2; exit 2; }
  git_in_box -C "$source_dir" pull --ff-only origin main
else
  mkdir -p "${source_dir%/*}"
  git_in_box clone --branch main --single-branch "$origin" "$source_dir"
fi
exec bash "$source_dir/install.sh" "${arguments[@]}"
