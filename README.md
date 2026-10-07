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

Install the container dependencies first:

```bash
distrobox enter atlas -- sudo dnf install -y git binutils zstd libcap
```

### First installation: clone

Run this only when `$HOME/atlas-steamos-updater` does not already exist:

```bash
distrobox enter atlas -- git clone https://github.com/limerosee/atlas-deb-parse.git "$HOME/atlas-steamos-updater"
```

Distrobox shares the home directory with SteamOS by default, so the cloned
directory is also available on the host.

### Update an existing clone

If the directory already exists, update it instead of running `git clone`
again:

```bash
distrobox enter atlas -- git -C "$HOME/atlas-steamos-updater" pull --ff-only
```

`--ff-only` protects local work by refusing to overwrite divergent commits or
uncommitted changes.

### Safely replace a broken or unrelated directory

If the existing directory is not a Git checkout or cannot be updated, preserve
it as a timestamped backup and clone a clean copy. Run these commands on the
SteamOS host:

```bash
mv "$HOME/atlas-steamos-updater" "$HOME/atlas-steamos-updater.backup-$(date +%Y%m%d-%H%M%S)"
distrobox enter atlas -- git clone https://github.com/limerosee/atlas-deb-parse.git "$HOME/atlas-steamos-updater"
```

### Install or refresh the host integration

After cloning, updating, or replacing the checkout, run the integration script
on the **SteamOS host**. Running it again replaces the previously installed
updater scripts with the current repository version:

```bash
cd "$HOME/atlas-steamos-updater"
./install.sh --auto-update
```

Do not run `install.sh` inside the container: it registers the host-side MIME
handler used when ATLAS opens a downloaded `.deb` file.

`--auto-update` is opt-in. When enabled, the updater checks this repository with
`git pull --ff-only` only after you answer `Y` to `Would you like to update
ATLAS?`. It never updates merely because the package was opened. The checkout
must be clean and its origin must be exactly this GitHub repository. A failed
self-update is logged and the already installed updater remains usable.

To disable this behavior while keeping the regular updater installed:

```bash
./install.sh --no-auto-update
```

The selected setting is preserved by later `./install.sh` runs unless you pass
one of these options again.

The installer records the previous `.deb` MIME handler and makes
`atlas-steamos-updater.desktop` the handler for Debian packages.

When ATLAS downloads an update and opens it, a terminal appears. The updater
shows the package version and SHA-256, then asks `Would you like to update
ATLAS? [Y/N]`. After a successful installation it asks `Would you like to open
ATLAS now? [Y/N]`; answering `Y` launches ATLAS immediately. Each prompt
requires an explicit `Y` or `N`. The updater closes a running ATLAS only with
confirmation, requests the container sudo password, installs the update, and
restores capabilities.

Packages outside ATLAS's own update directory are rejected.

## Test in a separate Distrobox

Create a separate Fedora container on the SteamOS host:

```bash
distrobox create --name atlas-updater-test --image registry.fedoraproject.org/fedora:latest
distrobox enter atlas-updater-test -- sudo dnf install -y git binutils zstd libcap
```

Place one or more ATLAS Debian packages directly in `$HOME/Downloads`, then run
the interactive bootstrap:

```bash
cd "$HOME/atlas-steamos-updater"
./install.sh \
  --container atlas-updater-test \
  --auto-update \
  --install-atlas
```

The script scans `Downloads` and its subdirectories (including folders such as
`Telegram Desktop`), silently rejects unrelated packages, and lists every
package that passes the ATLAS validator with its version and filename. Enter
the displayed number and press **Enter**, or enter `N` and press **Enter** to
cancel. The filename and safe version may differ, but the filename must end in
`.deb`. The validator still requires package `atlas`, architecture `amd64`, and
the allow-listed payload. Symlinks are rejected. Bootstrap also refuses to
replace an ATLAS binary already present in that container.

You can bypass the menu by explicitly supplying a package stored anywhere
under your home directory:

```bash
./install.sh \
  --container atlas-updater-test \
  --auto-update \
  --install-atlas "$HOME/Downloads/your-atlas-package.deb"
```

After a successful bootstrap, launch the test copy directly:

```bash
distrobox enter atlas-updater-test -- /usr/bin/atlas-preview
```

Later ATLAS updates use the normal MIME handler and confirmation flow. State
files are separate for each configured container. When testing is finished,
switch the handler back to the real container:

```bash
./install.sh --container atlas --auto-update
```

The container selection is preserved by later `./install.sh` runs.

## Event log

The host handler records short JSON events in:

```text
~/.local/state/atlas-steamos-updater/events.log
```

View the latest attempts on the SteamOS host with:

```bash
tail -n 50 "$HOME/.local/state/atlas-steamos-updater/events.log"
```

The events distinguish `open-request`, `validated`, `cancelled`, `failed`,
`self-update`, and `installed`. An `installed` event also records whether ATLAS
was relaunched.
The log contains the update filename, version and SHA-256 when available, but
never package contents. It rotates at 64 KiB; the previous file is retained as
`events.log.old`. Logging errors never block the updater.
If an attempted open produces no `open-request` event, the MIME handler was not
launched at all.

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
