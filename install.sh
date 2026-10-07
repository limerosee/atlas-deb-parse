#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: ./install.sh [--container NAME] [--auto-update|--no-auto-update] [--install-dependencies] [--install-atlas [PACKAGE.deb]]"
}

# Help must be safe even when the user's home directory is unavailable or
# read-only.  Do not create state directories before handling it.
for argument in "$@"; do
  case "$argument" in
    -h|--help)
      usage
      exit 0
      ;;
  esac
done

if [[ -n "${CONTAINER_ID:-}" || -e /run/.containerenv || -e /.dockerenv ]]; then
  echo "ERROR: run install.sh on the SteamOS host. Type exit, then run it again." >&2
  exit 2
fi
if ! command -v xdg-mime >/dev/null; then
  echo "ERROR: xdg-mime is required on the SteamOS host" >&2
  exit 2
fi

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
atlas_package=""
scan_downloads=false
install_dependencies=false

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
    --install-atlas)
      install_dependencies=true
      if (($# >= 2)) && [[ "$2" != --* ]]; then
        atlas_package="$2"
        shift 2
      else
        scan_downloads=true
        shift
      fi
      ;;
    --install-dependencies)
      install_dependencies=true
      shift
      ;;
    -h|--help)
      usage
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
install -m 0755 "$project_dir/src/atlas-launch.sh" "$bin_dir/atlas-launch"
install -m 0644 "$project_dir/share/atlas-distrobox.desktop" "$applications_dir/atlas-distrobox.desktop"
launcher_path="$bin_dir/atlas-launch"
escaped_launcher="${launcher_path//&/\\&}"
escaped_launcher="${escaped_launcher//|/\\|}"
sed -i "s|@LAUNCHER@|$escaped_launcher|" "$applications_dir/atlas-distrobox.desktop"
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

if [[ "$install_dependencies" == true ]]; then
  echo "Installing ATLAS runtime dependencies in Distrobox '$container'..."
  distrobox enter "$container" -- sudo dnf install -y \
    git \
    binutils \
    zstd \
    libcap \
    gtk3 \
    webkit2gtk4.1 \
    libayatana-appindicator-gtk3
fi

if [[ -n "$atlas_package" || "$scan_downloads" == true ]]; then
  if distrobox enter "$container" -- sh -c 'test -e /usr/bin/atlas-preview || test -e /usr/bin/atlas'; then
    echo "ERROR: ATLAS is already installed in Distrobox '$container'; use the normal updater" >&2
    exit 2
  fi

  if [[ "$scan_downloads" == true ]]; then
    downloads_dir="$HOME/Downloads"
    if [[ ! -d "$downloads_dir" ]]; then
      echo "ERROR: Downloads directory does not exist: $downloads_dir" >&2
      exit 2
    fi
    atlas_candidates=()
    atlas_versions=()
    atlas_hashes=()
    rejected_names=()
    rejected_reasons=()
    while IFS= read -r -d '' candidate; do
      if details="$(distrobox enter "$container" -- \
        python3 "$libexec_dir/atlas_deb_installer.py" --inspect --json "$candidate" \
        2>&1)"; then
        version="$(printf '%s' "$details" | python3 -c \
          'import json, sys; print(json.load(sys.stdin)["version"])')"
        sha256="$(printf '%s' "$details" | python3 -c \
          'import json, sys; print(json.load(sys.stdin)["sha256"])')"
        atlas_candidates+=("$candidate")
        atlas_versions+=("$version")
        atlas_hashes+=("$sha256")
      elif [[ "${candidate##*/}" == *[Aa][Tt][Ll][Aa][Ss]* ]]; then
        rejected_names+=("${candidate##*/}")
        rejected_reasons+=("${details:-validator returned no details}")
      fi
    done < <(find "$downloads_dir" -type f -iname '*.deb' -print0 2>/dev/null | sort -z)

    if ((${#atlas_candidates[@]} == 0)); then
      echo "ERROR: no valid ATLAS .deb packages were found in $downloads_dir" >&2
      if ((${#rejected_names[@]})); then
        echo "ATLAS-named packages were rejected:" >&2
        for index in "${!rejected_names[@]}"; do
          printf '  - %s: %s\n' \
            "${rejected_names[$index]}" \
            "${rejected_reasons[$index]}" >&2
        done
      fi
      exit 2
    fi

    echo "Valid ATLAS packages found in Downloads:"
    for index in "${!atlas_candidates[@]}"; do
      printf '  %d) Version %s — %s\n' \
        "$((index + 1))" \
        "${atlas_versions[$index]}" \
        "${atlas_candidates[$index]##*/}"
      printf '     SHA-256: %s\n' "${atlas_hashes[$index]}"
    done
    while true; do
      printf 'Install package 1? [Y/no; Enter = Y] Or enter another package number: '
      if ! IFS= read -r choice; then
        echo
        echo "Cancelled."
        exit 0
      fi
      choice="${choice#"${choice%%[![:space:]]*}"}"
      choice="${choice%"${choice##*[![:space:]]}"}"
      if [[ -z "$choice" || "${choice,,}" == y ]]; then
        choice=1
      fi
      if [[ "${choice,,}" == no || "${choice,,}" == n ]]; then
        echo "Cancelled."
        exit 0
      fi
      if [[ "$choice" =~ ^[0-9]{1,6}$ ]]; then
        choice_number=$((10#$choice))
        if ((choice_number >= 1 && choice_number <= ${#atlas_candidates[@]})); then
          atlas_package="${atlas_candidates[$((choice_number - 1))]}"
          break
        fi
      fi
      echo "Press Enter or type Y for package 1, a listed number for another package, or no to cancel."
    done
  fi

  if [[ -L "$atlas_package" || ! -f "$atlas_package" ]]; then
    echo "ERROR: ATLAS package must be a regular, non-symlink file: $atlas_package" >&2
    exit 2
  fi
  package_path="$(readlink -f -- "$atlas_package")"
  home_path="$(readlink -f -- "$HOME")"
  case "$package_path" in
    "$home_path"/*) ;;
    *)
      echo "ERROR: ATLAS package must be stored under your home directory" >&2
      exit 2
      ;;
  esac
  if [[ "${package_path,,}" != *.deb ]]; then
    echo "ERROR: ATLAS package filename must end in .deb" >&2
    exit 2
  fi

  state_name="atlas.json"
  if [[ "$container" != "atlas" ]]; then
    state_name="atlas-$container.json"
  fi
  echo "Validating and installing the initial ATLAS package in Distrobox '$container'..."
  distrobox enter "$container" -- \
    sudo python3 "$libexec_dir/atlas_deb_installer.py" \
      --install \
      --state-file "$state_dir/$state_name" \
      "$package_path"
fi

echo "Installed ATLAS SteamOS Updater."
echo "Distrobox: $container"
echo "Updater self-update: $auto_update"
echo "ATLAS .deb updates will open in a terminal and require confirmation."
