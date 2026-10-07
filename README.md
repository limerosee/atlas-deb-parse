# ATLAS SteamOS Updater

[Русская версия](README_RU.md)

A purpose-built update compatibility helper for ATLAS on SteamOS. It catches
the `.deb` update opened by ATLAS, validates the package, and installs its
allow-listed payload inside the existing Fedora Distrobox named `atlas`.

It does **not** modify SteamOS's read-only filesystem and does not patch or
replace ATLAS's updater code.

The updater is intentionally specific to ATLAS. It is not a package manager or
a general Debian compatibility layer. This repository is independent and is
not affiliated with or endorsed by ATLAS. It contains no ATLAS source code,
binaries, assets, package archives, credentials, or user configuration.

## Security model

The updater accepts only:

- a regular, non-symlink `.deb` below
  `~/.local/share/com.nanitnet.atlas/updates`;
- Debian package `atlas`, architecture `amd64`;
- regular payload files in the known ATLAS paths under `/usr/bin`,
  `/usr/lib/ATLAS`, and the exact desktop/icon locations;
- payloads whose contents match the package's `md5sums` metadata.

It displays the full package SHA-256 and requires interactive confirmation.
It never runs `preinst`, `postinst`, `prerm`, or `postrm`. Instead, it performs
the only relevant known post-install action itself: setting and verifying
`cap_net_admin,cap_net_raw+ep` on the two network engines.

ATLAS validates the signed update manifest before placing a package in the
update directory. This updater cannot independently verify that manifest
because the public verification key and detached manifest are not saved beside
the downloaded `.deb`; its independent controls are path restriction, package
metadata, payload allow-listing, hashes, and user confirmation.

## Requirements

- SteamOS Desktop Mode
- Distrobox container named `atlas`
- Python 3 in the host and container
- Fedora packages `binutils`, `zstd`, and `libcap` in the container
- `xdg-mime` on the host

Inside the container:

```bash
sudo dnf install -y binutils zstd libcap
```

## Install

Install the container dependencies and clone the repository through the
existing Distrobox:

```bash
distrobox enter atlas -- sudo dnf install -y git binutils zstd libcap
distrobox enter atlas -- git clone https://github.com/limerosee/atlas-deb-parse.git "$HOME/atlas-steamos-updater"
```

Distrobox shares the home directory with SteamOS by default, so the cloned
directory is also available on the host. Exit the container if you entered it
interactively, then run the integration script on the **SteamOS host**:

```bash
cd "$HOME/atlas-steamos-updater"
./install.sh
```

Do not run `install.sh` inside the container: it registers the host-side MIME
handler used when ATLAS opens a downloaded `.deb` file.

The installer records the previous `.deb` MIME handler and makes
`atlas-steamos-updater.desktop` the handler for Debian packages.

When ATLAS downloads an update and opens it, a terminal appears. The updater
shows the package version and SHA-256, asks for confirmation, closes ATLAS if
you approve, requests the container sudo password, installs the update, restores
capabilities, and offers to relaunch ATLAS.

Packages outside ATLAS's own update directory are rejected.

## Backups and rollback

Before replacing anything, the container installer stores the existing files
under:

```text
/var/lib/atlas-steamos-updater/backups/
```

If installation or capability verification fails, it restores the snapshot
automatically. Successful backups are retained for manual rollback and audit.
The latest installed version/hash and backup path are recorded under:

```text
~/.local/state/atlas-steamos-updater/atlas.json
```

## Uninstall

Run on the SteamOS host:

```bash
cd "$HOME/atlas-steamos-updater"
./uninstall.sh
```

This restores the previously recorded MIME handler when possible. Backups and
state are intentionally retained.
