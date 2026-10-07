#!/usr/bin/env python3

from __future__ import annotations

import gzip
import hashlib
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import atlas_deb_installer
from atlas_deb_installer import PackageError, inspect_package, install_package
from atlas_steamos_updater import log_event, version_key


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
            member.mode = 0o755 if name.endswith(("atlas-preview", "atlas-network", "sing-box-awg")) else 0o644
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


def make_deb(path: Path, files: dict[str, bytes] = FILES, *, package: str = "atlas", symlink: str | None = None) -> None:
    md5sums = "".join(
        f"{hashlib.md5(data, usedforsecurity=False).hexdigest()}  {name}\n"
        for name, data in files.items()
    ).encode()
    control = (
        f"Package: {package}\nVersion: 0.5.1-r2\nArchitecture: amd64\nDescription: test\n"
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
