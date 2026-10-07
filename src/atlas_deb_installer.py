#!/usr/bin/env python3
"""Validate and install an ATLAS Debian payload inside a Distrobox.

This deliberately does not execute Debian maintainer scripts.  The only
post-install action reproduced from ATLAS's postinst is restoring capabilities
on the two allow-listed network engines.
"""

from __future__ import annotations

import argparse
import bz2
import dataclasses
import datetime as dt
import gzip
import hashlib
import io
import json
import lzma
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
from typing import BinaryIO


AR_MAGIC = b"!<arch>\n"
MAX_PACKAGE_SIZE = 512 * 1024 * 1024
MAX_PAYLOAD_SIZE = 300 * 1024 * 1024
REQUIRED_FILES = {
    "usr/lib/ATLAS/resources/sing-box-awg",
    "usr/share/applications/ATLAS.desktop",
}
APP_BINARIES = {"usr/bin/atlas", "usr/bin/atlas-preview"}
CAPABILITY_FILES = (
    "usr/lib/ATLAS/resources/sing-box-awg",
    "usr/lib/ATLAS/resources/atlas-network",
)
ALLOWED_FILE_PATTERNS = (
    re.compile(r"usr/bin/(?:atlas|atlas-preview)\Z"),
    re.compile(r"usr/lib/ATLAS/resources/(?:atlas-network|sing-box-awg)\Z"),
    re.compile(r"usr/share/applications/ATLAS\.desktop\Z"),
    re.compile(
        r"usr/share/icons/hicolor/[A-Za-z0-9_.@+-]+/apps/(?:atlas|atlas-preview)\.png\Z"
    ),
)
VERSION_RE = re.compile(r"[0-9][0-9A-Za-z.+:~_-]*\Z")


class PackageError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class PayloadFile:
    path: str
    data: bytes
    mode: int


@dataclasses.dataclass(frozen=True)
class PackageInfo:
    path: Path
    sha256: str
    package: str
    version: str
    architecture: str
    files: tuple[PayloadFile, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "package": self.package,
            "version": self.version,
            "architecture": self.architecture,
            "files": [item.path for item in self.files],
        }


def read_ar(path: Path) -> dict[str, bytes]:
    if path.stat().st_size > MAX_PACKAGE_SIZE:
        raise PackageError("package exceeds the 512 MiB safety limit")

    members: dict[str, bytes] = {}
    with path.open("rb") as stream:
        if stream.read(len(AR_MAGIC)) != AR_MAGIC:
            raise PackageError("not a Debian ar archive")

        while True:
            header = stream.read(60)
            if not header:
                break
            if len(header) != 60 or header[58:60] != b"`\n":
                raise PackageError("invalid ar member header")
            try:
                size = int(header[48:58].decode("ascii").strip())
                raw_name = header[:16].decode("ascii").strip()
            except (UnicodeDecodeError, ValueError) as exc:
                raise PackageError("invalid ar metadata") from exc
            if size < 0 or size > MAX_PACKAGE_SIZE:
                raise PackageError("invalid ar member size")
            if raw_name.startswith("#1/") or raw_name in {"/", "//"}:
                raise PackageError("extended ar member names are not accepted")
            name = raw_name.removesuffix("/")
            if not name or "/" in name or name in members:
                raise PackageError("unsafe or duplicate ar member name")
            data = stream.read(size)
            if len(data) != size:
                raise PackageError("truncated ar member")
            members[name] = data
            if size % 2:
                if stream.read(1) != b"\n":
                    raise PackageError("invalid ar alignment")
    return members


def decompress_tar(name: str, data: bytes) -> bytes:
    try:
        if name.endswith(".gz"):
            return gzip.decompress(data)
        if name.endswith(".xz"):
            return lzma.decompress(data)
        if name.endswith(".bz2"):
            return bz2.decompress(data)
        if name.endswith(".zst"):
            zstd = shutil.which("zstd")
            if not zstd:
                raise PackageError("zstd is required to inspect this package")
            result = subprocess.run(
                [zstd, "--decompress", "--stdout", "--quiet"],
                input=data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if result.returncode:
                detail = result.stderr.decode("utf-8", "replace").strip()
                raise PackageError(f"zstd decompression failed: {detail}")
            return result.stdout
        if name.endswith(".tar"):
            return data
    except (OSError, EOFError, lzma.LZMAError) as exc:
        raise PackageError(f"cannot decompress {name}") from exc
    raise PackageError(f"unsupported Debian tar member: {name}")


def select_member(members: dict[str, bytes], prefix: str) -> tuple[str, bytes]:
    matches = [(name, data) for name, data in members.items() if name.startswith(prefix)]
    if len(matches) != 1:
        raise PackageError(f"expected exactly one {prefix}* member")
    return matches[0]


def normalized_tar_path(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]
    if name in {"", "."}:
        return ""
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "" in path.parts:
        raise PackageError(f"unsafe payload path: {name!r}")
    normalized = str(path)
    if normalized.startswith("/"):
        raise PackageError(f"unsafe payload path: {name!r}")
    return normalized


def read_control(tar_bytes: bytes) -> dict[str, str]:
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
            control_members = []
            for member in archive.getmembers():
                path = normalized_tar_path(member.name)
                if path == "control" and member.isfile():
                    control_members.append(member)
            if len(control_members) != 1:
                raise PackageError("control archive does not contain exactly one control file")
            stream = archive.extractfile(control_members[0])
            if stream is None:
                raise PackageError("cannot read Debian control metadata")
            text = stream.read().decode("utf-8", "strict")
    except (tarfile.TarError, UnicodeDecodeError) as exc:
        raise PackageError("invalid control archive") from exc

    fields: dict[str, str] = {}
    current = ""
    for line in text.splitlines():
        if line.startswith((" ", "\t")) and current:
            fields[current] += "\n" + line[1:]
            continue
        if ":" not in line:
            if line:
                raise PackageError("invalid Debian control line")
            continue
        key, value = line.split(":", 1)
        current = key.strip()
        fields[current] = value.strip()
    return fields


def read_md5sums(control_tar: bytes) -> dict[str, str]:
    try:
        with tarfile.open(fileobj=io.BytesIO(control_tar), mode="r:") as archive:
            matches = []
            for member in archive.getmembers():
                if normalized_tar_path(member.name) == "md5sums" and member.isfile():
                    matches.append(member)
            if len(matches) != 1:
                raise PackageError("control archive must contain md5sums")
            stream = archive.extractfile(matches[0])
            if stream is None:
                raise PackageError("cannot read md5sums")
            text = stream.read().decode("utf-8", "strict")
    except (tarfile.TarError, UnicodeDecodeError) as exc:
        raise PackageError("invalid md5sums metadata") from exc

    result: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{32})  (.+)", line)
        if not match:
            raise PackageError("invalid md5sums line")
        path = normalized_tar_path(match.group(2))
        if not path or path in result:
            raise PackageError("invalid or duplicate md5sums path")
        result[path] = match.group(1).lower()
    return result


def allowed_payload_path(path: str) -> bool:
    return any(pattern.fullmatch(path) for pattern in ALLOWED_FILE_PATTERNS)


def allowed_payload_directory(path: str) -> bool:
    fixed = {
        "usr",
        "usr/bin",
        "usr/lib",
        "usr/lib/ATLAS",
        "usr/lib/ATLAS/resources",
        "usr/share",
        "usr/share/applications",
        "usr/share/icons",
        "usr/share/icons/hicolor",
    }
    if path in fixed:
        return True
    return bool(
        re.fullmatch(r"usr/share/icons/hicolor/[A-Za-z0-9_.@+-]+(?:/apps)?", path)
    )


def read_payload(tar_bytes: bytes, expected_md5: dict[str, str]) -> tuple[PayloadFile, ...]:
    files: list[PayloadFile] = []
    total_size = 0
    seen: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
            for member in archive.getmembers():
                path = normalized_tar_path(member.name)
                if not path:
                    continue
                if member.isdir():
                    if not allowed_payload_directory(path):
                        raise PackageError(f"payload directory is not allow-listed: {path}")
                    continue
                if not member.isfile():
                    raise PackageError(f"links and special files are forbidden: {path}")
                if path in seen or not allowed_payload_path(path):
                    raise PackageError(f"payload path is not allow-listed: {path}")
                if member.size < 0 or member.size > MAX_PAYLOAD_SIZE:
                    raise PackageError(f"invalid payload size for {path}")
                total_size += member.size
                if total_size > MAX_PAYLOAD_SIZE:
                    raise PackageError("payload exceeds the 300 MiB safety limit")
                stream: BinaryIO | None = archive.extractfile(member)
                if stream is None:
                    raise PackageError(f"cannot read payload file: {path}")
                data = stream.read()
                if len(data) != member.size:
                    raise PackageError(f"truncated payload file: {path}")
                digest = hashlib.md5(data, usedforsecurity=False).hexdigest()
                if expected_md5.get(path) != digest:
                    raise PackageError(f"MD5 mismatch or missing metadata for {path}")
                files.append(PayloadFile(path=path, data=data, mode=member.mode & 0o777))
                seen.add(path)
    except tarfile.TarError as exc:
        raise PackageError("invalid data archive") from exc

    if seen != set(expected_md5):
        extra = sorted(set(expected_md5) - seen)
        raise PackageError(f"md5sums lists files absent from payload: {extra}")
    missing = REQUIRED_FILES - seen
    if not seen & APP_BINARIES:
        raise PackageError("required ATLAS executable is missing: atlas or atlas-preview")
    if missing:
        raise PackageError(f"required ATLAS files are missing: {sorted(missing)}")
    return tuple(sorted(files, key=lambda item: item.path))


def inspect_package(path: Path) -> PackageInfo:
    path = path.expanduser()
    if path.is_symlink() or not path.is_file():
        raise PackageError("package must be a regular, non-symlink file")
    path = path.resolve(strict=True)
    if path.suffix.lower() != ".deb":
        raise PackageError("package filename must end in .deb")

    package_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    members = read_ar(path)
    if members.get("debian-binary") != b"2.0\n":
        raise PackageError("unsupported Debian package format")
    control_name, control_compressed = select_member(members, "control.tar")
    data_name, data_compressed = select_member(members, "data.tar")
    control_tar = decompress_tar(control_name, control_compressed)
    data_tar = decompress_tar(data_name, data_compressed)
    metadata = read_control(control_tar)

    package = metadata.get("Package", "")
    version = metadata.get("Version", "")
    architecture = metadata.get("Architecture", "")
    if package != "atlas":
        raise PackageError(f"unexpected package name: {package!r}")
    if architecture != "amd64":
        raise PackageError(f"unexpected architecture: {architecture!r}")
    if not VERSION_RE.fullmatch(version):
        raise PackageError(f"unsafe or missing version: {version!r}")
    md5sums = read_md5sums(control_tar)
    files = read_payload(data_tar, md5sums)
    return PackageInfo(path, package_sha, package, version, architecture, files)


def ensure_within(path: Path, parent: Path) -> None:
    resolved = path.resolve(strict=True)
    allowed = parent.expanduser().resolve(strict=True)
    try:
        resolved.relative_to(allowed)
    except ValueError as exc:
        raise PackageError(f"package is outside allowed update directory: {allowed}") from exc


def target_path(root: Path, relative: str) -> Path:
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            raise PackageError(f"refusing symlink in installation path: {current}")
    return current


def backup_existing(info: PackageInfo, root: Path, backup_base: Path) -> tuple[Path, set[str]]:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_base / f"{timestamp}-{info.version}"
    snapshot = backup / "snapshot"
    snapshot.mkdir(parents=True, exist_ok=False)
    existed: set[str] = set()
    for item in info.files:
        source = target_path(root, item.path)
        if not source.exists():
            continue
        if source.is_symlink() or not source.is_file():
            raise PackageError(f"existing target is not a regular file: {source}")
        destination = snapshot / item.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        existed.add(item.path)
    metadata = {
        "created_at": timestamp,
        "incoming_version": info.version,
        "incoming_sha256": info.sha256,
        "package_path": str(info.path),
        "files_that_existed": sorted(existed),
    }
    (backup / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return backup, existed


def restore_backup(info: PackageInfo, root: Path, backup: Path, existed: set[str]) -> None:
    snapshot = backup / "snapshot"
    for item in info.files:
        destination = target_path(root, item.path)
        if item.path in existed:
            source = snapshot / item.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        elif destination.exists() and destination.is_file() and not destination.is_symlink():
            destination.unlink()


def install_files(info: PackageInfo, root: Path) -> None:
    for item in info.files:
        destination = target_path(root, item.path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.new-{os.getpid()}")
        try:
            with temporary.open("xb") as stream:
                stream.write(item.data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, item.mode)
            if os.geteuid() == 0:
                os.chown(temporary, 0, 0)
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()


def restore_capabilities(root: Path) -> list[str]:
    if root != Path("/"):
        return []
    setcap = shutil.which("setcap")
    getcap = shutil.which("getcap")
    if not setcap or not getcap:
        raise PackageError("setcap/getcap not found; install Fedora package libcap")
    installed: list[str] = []
    for relative in CAPABILITY_FILES:
        path = target_path(root, relative)
        if not path.exists():
            continue
        subprocess.run(
            [setcap, "cap_net_admin,cap_net_raw+ep", str(path)],
            check=True,
        )
        result = subprocess.run(
            [getcap, str(path)],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        if "cap_net_admin" not in result.stdout or "cap_net_raw" not in result.stdout:
            raise PackageError(f"capability verification failed for {path}")
        installed.append(result.stdout.strip())
    return installed


def write_state(path: Path | None, info: PackageInfo, backup: Path) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "installed_version": info.version,
        "installed_sha256": info.sha256,
        "installed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "backup": str(backup),
    }
    path.write_text(json.dumps(state, indent=2) + "\n")
    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")
    if sudo_uid and sudo_gid:
        os.chown(path, int(sudo_uid), int(sudo_gid))
        os.chown(path.parent, int(sudo_uid), int(sudo_gid))


def install_package(
    info: PackageInfo,
    root: Path,
    backup_base: Path,
    state_file: Path | None,
) -> tuple[Path, list[str]]:
    root = root.resolve(strict=True)
    if root == Path("/") and os.geteuid() != 0:
        raise PackageError("installation into / must run as root inside the container")
    backup_base.mkdir(parents=True, exist_ok=True)
    backup, existed = backup_existing(info, root, backup_base)
    try:
        install_files(info, root)
        capabilities = restore_capabilities(root)
        for required in REQUIRED_FILES | (APP_BINARIES & {item.path for item in info.files}):
            installed = target_path(root, required)
            if not installed.is_file() or installed.is_symlink():
                raise PackageError(f"post-install verification failed: {installed}")
        write_state(state_file, info, backup)
        return backup, capabilities
    except Exception:
        restore_backup(info, root, backup, existed)
        if root == Path("/"):
            try:
                restore_capabilities(root)
            except Exception as rollback_error:
                print(f"WARNING: rollback capability restore failed: {rollback_error}", file=sys.stderr)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--inspect", action="store_true")
    action.add_argument("--install", action="store_true")
    parser.add_argument("package", type=Path)
    parser.add_argument("--allowed-parent", type=Path)
    parser.add_argument("--root", type=Path, default=Path("/"))
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=Path("/var/lib/atlas-steamos-updater/backups"),
    )
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.allowed_parent:
            ensure_within(args.package, args.allowed_parent)
        info = inspect_package(args.package)
        if args.inspect:
            if args.json:
                print(json.dumps(info.as_dict(), indent=2))
            else:
                print(f"Package:      {info.package}")
                print(f"Version:      {info.version}")
                print(f"Architecture: {info.architecture}")
                print(f"SHA-256:      {info.sha256}")
                print(f"Payload files: {len(info.files)}")
            return 0

        backup, capabilities = install_package(
            info,
            args.root,
            args.backup_dir,
            args.state_file,
        )
        print(f"Installed ATLAS {info.version}")
        print(f"Backup: {backup}")
        for capability in capabilities:
            print(f"Capability: {capability}")
        return 0
    except (PackageError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
