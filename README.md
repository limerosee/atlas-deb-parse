# ATLAS SteamOS Updater

[Русская версия](README_RU.md)

A purpose-built update compatibility helper for ATLAS on SteamOS. It catches
the `.deb` update opened by ATLAS, validates the package, and installs its
allow-listed payload inside a selected Fedora Distrobox (`atlas` by default).

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
- Fedora Distrobox container (`atlas` is the default name; `--container`
  selects another one)
- Python 3 in the host and container
- Fedora packages `git`, `binutils`, `zstd`, `libcap`, `gtk3`,
  `webkit2gtk4.1`, and `libayatana-appindicator-gtk3` in the container
- `xdg-mime` on the host

## Install

### Create the Distrobox

Check whether the target container already exists:

```bash
distrobox list
```

If `atlas` is not listed, create a Fedora container on the SteamOS host. This
recommended form also prepares an isolated network namespace, the TUN device,
and the capabilities needed to test ATLAS tunnel mode:

```bash
distrobox create --name atlas --image registry.fedoraproject.org/fedora:latest --unshare-netns --additional-flags "--cap-add=NET_ADMIN --cap-add=NET_RAW --device=/dev/net/tun"
```

Enter it once so Distrobox can finish its initial setup:

```bash
distrobox enter atlas
```

Exit back to SteamOS with `exit`, then verify Fedora and the TUN device:

```bash
distrobox enter atlas -- sh -lc 'cat /etc/fedora-release; test -c /dev/net/tun && echo "TUN device available"'
```

Do not run `distrobox create` again for an existing name. If an old container
was created without the required network flags, create a new one under another
name such as `atlas-new`, then consistently pass
`--container atlas-new` to `install.sh`. Distrobox shares the user's home
directory by default, but packages and programs installed inside one container
do not automatically appear in another container.

Install the container dependencies first:

```bash
distrobox enter atlas -- sudo dnf install -y git binutils zstd libcap gtk3 webkit2gtk4.1 libayatana-appindicator-gtk3
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
./install.sh --container atlas --auto-update
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

The installer options have separate purposes:

- `--container NAME` selects and remembers the Distrobox used by ATLAS;
- `--install-atlas` performs the initial ATLAS bootstrap and opens the numbered
  package chooser when no path follows it;
- `--install-dependencies` repairs only the Fedora runtime dependencies;
- `--auto-update` enables updating this updater/parser after the user accepts
  an ATLAS update; it never silently installs an ATLAS package;
- `--no-auto-update` disables only that updater/parser self-update.

Display the command summary without changing the installation:

```bash
./install.sh --help
```

Initial bootstrap with `--install-atlas` automatically installs the known
Fedora runtime equivalents of ATLAS's Debian dependencies. To install or repair
only these dependencies in an existing container without reinstalling ATLAS:

```bash
./install.sh --container atlas --install-dependencies
```

The installer records the previous `.deb` MIME handler and makes
`atlas-steamos-updater.desktop` the handler for Debian packages.

When ATLAS downloads an update and opens it, a terminal appears. The updater
shows the package version and SHA-256, then asks `Would you like to update
ATLAS? [Y/N]`. After a successful installation it asks `Would you like to open
ATLAS now? [Y/N]`; answering `Y` launches ATLAS immediately. Each prompt
requires an explicit `Y` or `N`. The updater closes a running ATLAS only with
confirmation, requests the container sudo password, installs the update, and
restores capabilities.

The regular update flow refuses the same SHA-256 and refuses a version that is
equal to or older than the version recorded for that container. This is
separate from initial bootstrap, whose package chooser has no hard-coded
version or checksum allowlist.

Packages outside ATLAS's own update directory are rejected.

## Test in a separate Distrobox

Create a separate Fedora container on the SteamOS host:

```bash
distrobox create --name atlas-updater-test --image registry.fedoraproject.org/fedora:latest --unshare-netns --additional-flags "--cap-add=NET_ADMIN --cap-add=NET_RAW --device=/dev/net/tun"
distrobox enter atlas-updater-test -- sudo dnf install -y git binutils zstd libcap gtk3 webkit2gtk4.1 libayatana-appindicator-gtk3
```

The isolated network namespace and runtime flags are required only when this
container will also test ATLAS tunnel mode; they keep that test separate from
the host network. Package installation/update testing does not itself create a
tunnel.

Place one or more ATLAS Debian packages directly in `$HOME/Downloads`, then run
the interactive bootstrap:

```bash
cd "$HOME/atlas-steamos-updater"
./install.sh --container atlas-updater-test --auto-update --install-atlas
```

The script scans `Downloads` and its subdirectories (including folders such as
`Telegram Desktop`), silently rejects unrelated packages, and lists every
package that passes the ATLAS validator with its version, filename, and
calculated SHA-256. Enter
the displayed number and press **Enter**, or enter `N` and press **Enter** to
cancel. The filename and safe version may differ, but the filename must end in
`.deb`. The validator still requires package `atlas`, architecture `amd64`, and
the allow-listed payload. Symlinks are rejected. Bootstrap also refuses to
replace an ATLAS binary already present in that container.
If an ATLAS-named package is rejected, the scanner prints its exact validation
error instead of hiding the reason.

The menu is generated automatically: if four valid packages are found, it
shows choices `1` through `4`. There is no hard-coded filename, version, or
SHA-256 allowlist. The checksum is calculated from each file and displayed so
the user can identify the exact package. Safety still comes from verifying the
internal Debian metadata, `atlas` package identity, `amd64` architecture,
per-file MD5 metadata, and the strict ATLAS payload allowlist.

You can bypass the menu by explicitly supplying a package stored anywhere
under your home directory:

```bash
./install.sh --container atlas-updater-test --auto-update --install-atlas "$HOME/Downloads/your-atlas-package.deb"
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
To keep a successfully tested container as the main ATLAS target, run the
installer once with that container name, for example:

```bash
./install.sh --container atlas-updater-nettest --auto-update
```

This changes the updater target without deleting or renaming either Distrobox.

## ATLAS connection troubleshooting

Packages may contain either `/usr/bin/atlas` (older releases) or
`/usr/bin/atlas-preview`. Both names and their corresponding PNG icons are
accepted; an update relaunches the executable included in that package.
For either version, launch with:

```bash
distrobox enter atlas -- sh -c 'if test -x /usr/bin/atlas-preview; then exec /usr/bin/atlas-preview; else exec /usr/bin/atlas; fi'
```

The installer can validate, install, back up, and update ATLAS files, but it
does not modify ATLAS's connection-selection logic or generated tunnel
configuration. A message equivalent to `Could not select working connection
parameters` is an ATLAS runtime/network error, not evidence that package
installation failed.

A useful privacy-safe diagnosis is:

```bash
distrobox enter atlas-updater-nettest -- pgrep -a -x atlas-preview
distrobox enter atlas-updater-nettest -- pgrep -a -x atlas-network
distrobox enter atlas-updater-nettest -- pgrep -a -x sing-box-awg
distrobox enter atlas-updater-nettest -- getcap /usr/lib/ATLAS/resources/atlas-network /usr/lib/ATLAS/resources/sing-box-awg
distrobox enter atlas-updater-nettest -- ip -6 route show default
distrobox enter atlas-updater-nettest -- curl -6 --connect-timeout 10 -I https://example.com
```

Expected capabilities are `cap_net_admin,cap_net_raw=ep` on both network
engines. If `atlas-network` briefly becomes a zombie with exit status zero,
the probe completed rather than crashed; if `sing-box-awg` and a TUN interface
never appear, ATLAS rejected all probed connection combinations before tunnel
startup. Missing native IPv6, blocked transports, the current Wi-Fi/router,
the ISP, or unavailable remote endpoints can cause this. Trying a different
network with working IPv6 is a useful isolation test.

The sing-box 1.15 warning that the TUN `stack` option is deprecated is not the
same error: the option still works in 1.15 but is scheduled for removal in
1.17. ATLAS must eventually update the configuration it generates. Do not edit
generated runtime JSON as an installer workaround; ATLAS may overwrite it.

Before sharing diagnostics, remove repeated entries and redact public IP
addresses, server hostnames, account identifiers, access links, tokens, and
subscription data. Never share `account.json`, cookies, full runtime
configuration files, or other credentials. Process names, exit status,
capability output, and the short error text are normally sufficient.

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

For a non-default container, the state filename is
`atlas-<container-name>.json`.

## Uninstall

Run on the SteamOS host:

```bash
cd "$HOME/atlas-steamos-updater"
./uninstall.sh
```

This restores the previously recorded MIME handler when possible. Backups and
state are intentionally retained.
