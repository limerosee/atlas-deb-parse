#!/usr/bin/env python3

from __future__ import annotations

import gzip
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import atlas_deb_installer
from atlas_deb_installer import PackageError, inspect_package, install_package
from atlas_steamos_updater import (
    HandlerError,
    auto_update_enabled,
    configured_container,
    log_event,
    prompt,
    reject_reinstall_or_downgrade,
    self_update,
    version_key,
)


FILES = {
    "usr/bin/atlas-preview": b"#!/bin/sh\nexit 0\n",
    "usr/lib/ATLAS/resources/atlas-network": b"network",
    "usr/lib/ATLAS/resources/sing-box-awg": b"sing-box",
    "usr/share/applications/ATLAS.desktop": b"[Desktop Entry]\nName=ATLAS\n",
    "usr/share/icons/hicolor/32x32/apps/atlas-preview.png": b"png",
}


def tar_gz(files: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for name, data in files.items():
            member = tarfile.TarInfo(f"./{name}")
            member.mode = 0o755 if name.endswith(("atlas", "atlas-preview", "atlas-network", "sing-box-awg")) else 0o644
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        if symlink:
            member = tarfile.TarInfo(f"./{symlink}")
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/passwd"
            archive.addfile(member)
    return gzip.compress(raw.getvalue(), mtime=0)


def ar_archive(members: dict[str, bytes]) -> bytes:
    result = bytearray(b"!<arch>\n")
    for name, data in members.items():
        header = (
            f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{0o100644:<8o}{len(data):<10}`\n"
        ).encode("ascii")
        if len(header) != 60:
            raise AssertionError(len(header))
        result.extend(header)
        result.extend(data)
        if len(data) % 2:
            result.extend(b"\n")
    return bytes(result)


def make_deb(
    path: Path,
    files: dict[str, bytes] = FILES,
    *,
    package: str = "atlas",
    version: str = "0.5.1-r2",
    symlink: str | None = None,
) -> None:
    md5sums = "".join(
        f"{hashlib.md5(data, usedforsecurity=False).hexdigest()}  {name}\n"
        for name, data in files.items()
    ).encode()
    control = (
        f"Package: {package}\nVersion: {version}\nArchitecture: amd64\nDescription: test\n"
    ).encode()
    control_tar = tar_gz({"control": control, "md5sums": md5sums})
    data_tar = tar_gz(files, symlink=symlink)
    path.write_bytes(
        ar_archive(
            {
                "debian-binary": b"2.0\n",
                "control.tar.gz": control_tar,
                "data.tar.gz": data_tar,
            }
        )
    )


class InstallerTests(unittest.TestCase):
    def test_legacy_atlas_binary_installs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            files = dict(FILES)
            del files["usr/lib/ATLAS/resources/atlas-network"]
            files["usr/bin/atlas"] = files.pop("usr/bin/atlas-preview")
            files["usr/share/icons/hicolor/32x32/apps/atlas.png"] = files.pop(
                "usr/share/icons/hicolor/32x32/apps/atlas-preview.png"
            )
            package = base / "legacy.deb"
            make_deb(package, files, version="0.4.39-r1")
            info = inspect_package(package)
            root = base / "root"
            root.mkdir()
            install_package(info, root, base / "backups", None)
            self.assertTrue((root / "usr/bin/atlas").is_file())

    def test_rejects_package_without_either_atlas_binary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            files = dict(FILES)
            del files["usr/bin/atlas-preview"]
            package = Path(directory) / "missing.deb"
            make_deb(package, files)
            with self.assertRaisesRegex(PackageError, "executable is missing"):
                inspect_package(package)

    def test_install_help_does_not_require_writable_home(self) -> None:
        script = Path(__file__).resolve().parents[1] / "install.sh"
        result = subprocess.run(
            ["bash", str(script), "--help"],
            env={**os.environ, "HOME": "/proc/atlas-read-only-home"},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--install-atlas", result.stdout)

    def test_atlas_revision_ordering(self) -> None:
        self.assertLess(version_key("0.5.1-r2"), version_key("0.5.1-r10"))

    def test_event_log_records_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict("os.environ", {"HOME": directory}):
                log_event(
                    "validated",
                    file="ATLAS_0.5.1_r2_linux-x64.deb",
                    version="0.5.1-r2",
                )
            log_file = (
                Path(directory)
                / ".local/state/atlas-steamos-updater/events.log"
            )
            record = log_file.read_text()
            self.assertIn('"event": "validated"', record)
            self.assertIn('"version": "0.5.1-r2"', record)

    def test_event_log_failure_does_not_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / ".local/state/atlas-steamos-updater"
            state_path.parent.mkdir(parents=True)
            state_path.write_text("not a directory")
            with mock.patch.dict("os.environ", {"HOME": directory}):
                log_event("open-request", file="update.deb")

    def test_reads_container_and_auto_update_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / ".local/state/atlas-steamos-updater"
            state.mkdir(parents=True)
            (state / "container").write_text("atlas-test\n")
            (state / "auto-update").write_text("enabled\n")
            with mock.patch.dict("os.environ", {"HOME": directory}):
                self.assertEqual(configured_container(), "atlas-test")
                self.assertTrue(auto_update_enabled())

    def test_rejects_invalid_container_setting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / ".local/state/atlas-steamos-updater"
            state.mkdir(parents=True)
            (state / "container").write_text("atlas;bad\n")
            with mock.patch.dict("os.environ", {"HOME": directory}):
                with self.assertRaisesRegex(HandlerError, "invalid Distrobox"):
                    configured_container()

    def test_self_update_fast_forwards_and_refreshes_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            source = home / "atlas-steamos-updater"
            (source / "src").mkdir(parents=True)
            (source / "src/atlas_steamos_updater.py").write_text("host")
            (source / "src/atlas_deb_installer.py").write_text("installer")
            state = home / ".local/state/atlas-steamos-updater"
            state.mkdir(parents=True)
            (state / "source-path").write_text(str(source))
            completed = [
                mock.Mock(returncode=0, stdout="https://github.com/limerosee/atlas-deb-parse.git\n", stderr=""),
                mock.Mock(returncode=0, stdout="", stderr=""),
                mock.Mock(returncode=0, stdout="a" * 40 + "\n", stderr=""),
                mock.Mock(returncode=0, stdout="Updating\n", stderr=""),
                mock.Mock(returncode=0, stdout="b" * 40 + "\n", stderr=""),
            ]
            with mock.patch.dict("os.environ", {"HOME": directory}):
                with mock.patch(
                    "atlas_steamos_updater.run_in_container",
                    side_effect=completed,
                ) as run_mock:
                    with mock.patch("atlas_steamos_updater.atomic_copy") as copy_mock:
                        self_update("atlas-test")
            self.assertIn("pull", run_mock.call_args_list[3].args[0])
            self.assertEqual(run_mock.call_args_list[3].kwargs["container"], "atlas-test")
            self.assertEqual(copy_mock.call_count, 2)

    def test_prompt_accepts_explicit_y(self) -> None:
        with mock.patch("builtins.input", return_value="Y"):
            self.assertTrue(prompt("Continue?"))

    def test_prompt_accepts_explicit_n(self) -> None:
        with mock.patch("builtins.input", return_value="N"):
            self.assertFalse(prompt("Continue?"))

    def test_prompt_repeats_until_y_or_n(self) -> None:
        with mock.patch("builtins.input", side_effect=["invalid", "unknown", "y"]):
            with mock.patch("builtins.print") as print_mock:
                self.assertTrue(prompt("Continue?"))
        self.assertEqual(print_mock.call_count, 2)

    def test_prompt_accepts_enter_y_and_no(self) -> None:
        for answer in ("", "Y", " y "):
            with self.subTest(answer=answer), mock.patch("builtins.input", return_value=answer):
                self.assertTrue(prompt("Continue?"))
        for answer in ("no", "NO", "n"):
            with self.subTest(answer=answer), mock.patch("builtins.input", return_value=answer):
                self.assertFalse(prompt("Continue?"))

    def test_valid_package_and_test_root_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            package = base / "ATLAS_0.5.1_r2_linux-x64.deb"
            make_deb(package)
            info = inspect_package(package)
            self.assertEqual(info.package, "atlas")
            self.assertEqual(info.version, "0.5.1-r2")
            root = base / "root"
            backups = base / "backups"
            root.mkdir()
            backup, capabilities = install_package(info, root, backups, None)
            self.assertTrue((root / "usr/bin/atlas-preview").is_file())
            self.assertTrue(backup.is_dir())
            self.assertEqual(capabilities, [])

    def test_accepts_arbitrary_deb_filename_and_safe_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "my-atlas-bootstrap-package.deb"
            make_deb(package, version="1.20.3-r17")
            info = inspect_package(package)
            self.assertEqual(info.package, "atlas")
            self.assertEqual(info.version, "1.20.3-r17")

    def test_accepts_multiple_atlas_names_versions_and_checksums(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            variants = (
                ("ATLAS-old.deb", "0.4.39-r1", b"old"),
                ("telegram-download.deb", "0.5.1-r1", b"r1"),
                ("ATLAS_0.5.1_r2_linux-x64.deb", "0.5.1-r2", b"r2"),
                ("my-new-atlas.deb", "1.20.3-r17", b"new"),
            )
            results = []
            for filename, version, binary in variants:
                package = base / filename
                files = dict(FILES)
                files["usr/bin/atlas-preview"] = binary
                make_deb(package, files, version=version)
                results.append(inspect_package(package))

            self.assertEqual(
                [item.version for item in results],
                [item[1] for item in variants],
            )
            self.assertEqual(len({item.sha256 for item in results}), 4)

    def test_update_rejects_same_or_older_but_accepts_newer_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            state_file.write_text(
                '{"installed_version": "0.5.1-r2", "installed_sha256": "old-hash"}'
            )
            with self.assertRaisesRegex(HandlerError, "reinstall or downgrade"):
                reject_reinstall_or_downgrade(
                    {"version": "0.5.1-r1", "sha256": "different-hash"},
                    state_file,
                )
            reject_reinstall_or_downgrade(
                {"version": "0.5.1-r3", "sha256": "new-hash"},
                state_file,
            )

    def test_rejects_wrong_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "bad.deb"
            make_deb(package, package="not-atlas")
            with self.assertRaisesRegex(PackageError, "unexpected package"):
                inspect_package(package)

    def test_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            files = dict(FILES)
            files["../../escape"] = b"bad"
            package = Path(directory) / "bad.deb"
            make_deb(package, files)
            with self.assertRaisesRegex(PackageError, "unsafe payload path"):
                inspect_package(package)

    def test_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "bad.deb"
            make_deb(package, symlink="usr/lib/ATLAS/resources/evil")
            with self.assertRaisesRegex(PackageError, "links and special files"):
                inspect_package(package)

    def test_rejects_unlisted_payload_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            files = dict(FILES)
            files["usr/bin/unrelated"] = b"bad"
            package = Path(directory) / "bad.deb"
            make_deb(package, files)
            with self.assertRaisesRegex(PackageError, "not allow-listed"):
                inspect_package(package)

    def test_failed_post_install_rolls_back_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            package = base / "ATLAS_0.5.1_r2_linux-x64.deb"
            make_deb(package)
            info = inspect_package(package)
            root = base / "root"
            old_binary = root / "usr/bin/atlas-preview"
            old_binary.parent.mkdir(parents=True)
            old_binary.write_bytes(b"old-version")
            with mock.patch.object(
                atlas_deb_installer,
                "restore_capabilities",
                side_effect=PackageError("simulated capability failure"),
            ):
                with self.assertRaisesRegex(PackageError, "simulated capability failure"):
                    install_package(info, root, base / "backups", None)
            self.assertEqual(old_binary.read_bytes(), b"old-version")
            self.assertFalse((root / "usr/lib/ATLAS/resources/atlas-network").exists())


if __name__ == "__main__":
    unittest.main()
