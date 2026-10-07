#!/usr/bin/env bash
set -euo pipefail
container=atlas
settings="$HOME/.local/state/atlas-steamos-updater/container"
if [[ -f "$settings" ]]; then IFS= read -r container < "$settings"; fi
[[ "$container" =~ ^[A-Za-z0-9_.-]+$ ]] || { echo 'Invalid ATLAS container name' >&2; exit 2; }
exec distrobox enter "$container" -- sh -c 'if test -x /usr/bin/atlas-preview; then exec /usr/bin/atlas-preview "$@"; elif test -x /usr/bin/atlas; then exec /usr/bin/atlas "$@"; else echo "ATLAS is not installed in this container" >&2; exit 127; fi' sh "$@"
