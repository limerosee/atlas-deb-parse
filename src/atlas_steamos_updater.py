#!/usr/bin/env python3
"""Route validated ATLAS updates from SteamOS into its Distrobox."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import unquote, urlparse


APP_ID = "com.nanitnet.atlas"
DEFAULT_CONTAINER = "atlas"
TRUSTED_SOURCE = "https://github.com/limerosee/atlas-deb-parse"
MAX_LOG_SIZE = 64 * 1024


class HandlerError(RuntimeError):
    pass


def read_setting(name: str) -> str | None:
    path = Path.home() / ".local" / "state" / "atlas-steamos-updater" / name
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def configured_container() -> str:
    value = read_setting("container") or DEFAULT_CONTAINER
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise HandlerError("invalid Distrobox name in updater configuration")
    return value


def auto_update_enabled() -> bool:
    return read_setting("auto-update") == "enabled"


def log_event(event: str, **fields: object) -> None:
    """Append a short JSON event without ever blocking the updater."""
    try:
        state_dir = Path.home() / ".local" / "state" / "atlas-steamos-updater"
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if state_dir.is_symlink() or not state_dir.is_dir():
            return
        log_file = state_dir / "events.log"
        old_log = state_dir / "events.log.old"
        if log_file.exists():
            if log_file.is_symlink() or not log_file.is_file():
                return
            if log_file.stat().st_size >= MAX_LOG_SIZE:
                os.replace(log_file, old_log)
        record = {
            "time": dt.datetime.now(dt.timezone.utc).isoformat(),
            "event": event,
        }
        for key, value in fields.items():
            if isinstance(value, str):
                value = value[:500]
            record[key] = value
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(log_file, flags, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def file_argument(value: str) -> Path:
    parsed = urlparse(value)
    if parsed.scheme:
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise HandlerError("only local file:// URLs are accepted")
        value = unquote(parsed.path)
    path = Path(value).expanduser()
    if any(ord(character) < 32 for character in str(path)):
        raise HandlerError("control characters are forbidden in update paths")
    if path.is_symlink() or not path.is_file():
        raise HandlerError("update must be a regular, non-symlink file")
    return path.resolve(strict=True)


def ensure_update_location(path: Path) -> Path:
    allowed = (Path.home() / ".local" / "share" / APP_ID / "updates").resolve()
    try:
        path.relative_to(allowed)
    except ValueError as exc:
        raise HandlerError(f"refusing package outside ATLAS update directory: {allowed}") from exc
    return allowed


def run_in_container(
    arguments: list[str],
    *,
    container: str = DEFAULT_CONTAINER,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = ["distrobox", "enter", container, "--", *arguments]
    return subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def normalized_origin(value: str) -> str:
    return value.strip().removesuffix("/").removesuffix(".git")


def atomic_copy(source: Path, destination: Path, mode: int) -> None:
    if source.is_symlink() or not source.is_file():
        raise HandlerError(f"self-update source is not a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.new-{os.getpid()}")
    try:
        with source.open("rb") as input_stream, temporary.open("xb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def self_update(container: str) -> None:
    source_value = read_setting("source-path")
    if not source_value:
        raise HandlerError("self-update source path is not configured; run install.sh again")
    source = Path(source_value).expanduser()
    if source.is_symlink() or not source.is_dir():
        raise HandlerError(f"self-update source directory is unavailable: {source}")
    source = source.resolve(strict=True)

    origin_result = run_in_container(
        ["git", "-C", str(source), "remote", "get-url", "origin"],
        container=container,
        capture=True,
    )
    if origin_result.returncode or normalized_origin(origin_result.stdout) != TRUSTED_SOURCE:
        raise HandlerError("self-update refused: repository origin is not trusted")

    status_result = run_in_container(
        ["git", "-C", str(source), "status", "--porcelain"],
        container=container,
        capture=True,
    )
    if status_result.returncode:
        raise HandlerError("self-update could not inspect the repository")
    if status_result.stdout.strip():
        raise HandlerError("self-update refused: repository has local changes")

    old_result = run_in_container(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        container=container,
        capture=True,
    )
    pull_result = run_in_container(
        ["git", "-C", str(source), "pull", "--ff-only"],
        container=container,
        capture=True,
    )
    if old_result.returncode or pull_result.returncode:
        detail = pull_result.stderr.strip() or pull_result.stdout.strip()
        raise HandlerError(f"self-update failed: {detail or 'git pull failed'}")
    new_result = run_in_container(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        container=container,
        capture=True,
    )
    if new_result.returncode:
        raise HandlerError("self-update could not read the updated revision")

    atomic_copy(
        source / "src" / "atlas_steamos_updater.py",
        Path.home() / ".local" / "bin" / "atlas-steamos-updater",
        0o755,
    )
    atomic_copy(
        source / "src" / "atlas_deb_installer.py",
        Path.home()
        / ".local"
        / "libexec"
        / "atlas-steamos-updater"
        / "atlas_deb_installer.py",
        0o755,
    )
    old_revision = old_result.stdout.strip()
    new_revision = new_result.stdout.strip()
    log_event(
        "self-update",
        status="updated" if new_revision != old_revision else "current",
        old_revision=old_revision,
        new_revision=new_revision,
    )
    if new_revision == old_revision:
        print("Updater is already current.")
    else:
        print(f"Updater refreshed: {old_revision[:7]} -> {new_revision[:7]}")


def prompt(question: str) -> bool:
    while True:
        try:
            answer = input(f"{question} [1 = Yes / 2 = No; Y/N; да/нет] ").strip().lower()
        except EOFError:
            return False
        if answer in {"y", "yes", "да", "1", "agree", "согласен", "согласна"}:
            return True
        if answer in {"n", "no", "нет", "2", "none", "cancel", "отмена"}:
            return False
        print("Enter 1 / Y / yes / да to accept, or 2 / N / no / нет to decline, then press Enter.")


def version_key(value: str) -> tuple[tuple[int, object], ...]:
    """Provide a conservative ordering for ATLAS's numeric revision format."""
    result: list[tuple[int, object]] = []
    for token in re.findall(r"[0-9]+|[A-Za-z]+", value):
        if token.isdigit():
            result.append((1, int(token)))
        else:
            result.append((0, token.lower()))
    return tuple(result)


def reject_reinstall_or_downgrade(details: dict[str, object], state_file: Path) -> None:
    if not state_file.is_file():
        return
    try:
        state = json.loads(state_file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HandlerError(f"cannot read updater state: {state_file}") from exc
    if state.get("installed_sha256") == details.get("sha256"):
        raise HandlerError("this exact update is already installed")
    old_version = state.get("installed_version")
    new_version = details.get("version")
    if isinstance(old_version, str) and isinstance(new_version, str):
        if version_key(new_version) <= version_key(old_version):
            raise HandlerError(
                f"refusing reinstall or downgrade: installed {old_version}, incoming {new_version}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("update")
    parser.add_argument("--no-relaunch", action="store_true")
    args = parser.parse_args()
    parsed_input = urlparse(args.update)
    update_name = Path(unquote(parsed_input.path)).name or "<unknown>"
    log_event("open-request", file=update_name)

    try:
        if not shutil.which("distrobox"):
            raise HandlerError("distrobox is not installed")
        container = configured_container()
        path = file_argument(args.update)
        allowed = ensure_update_location(path)
        installer = (
            Path.home()
            / ".local"
            / "libexec"
            / "atlas-steamos-updater"
            / "atlas_deb_installer.py"
        )
        if not installer.is_file():
            raise HandlerError(f"container installer is missing: {installer}")

        inspect_result = run_in_container(
            [
                "python3",
                str(installer),
                "--inspect",
                "--json",
                "--allowed-parent",
                str(allowed),
                str(path),
            ],
            container=container,
            capture=True,
        )
        if inspect_result.returncode:
            detail = inspect_result.stderr.strip() or inspect_result.stdout.strip()
            raise HandlerError(f"package validation failed: {detail}")
        details = json.loads(inspect_result.stdout)
        log_event(
            "validated",
            file=path.name,
            version=details.get("version"),
            sha256=details.get("sha256"),
        )
        state_name = "atlas.json" if container == DEFAULT_CONTAINER else f"atlas-{container}.json"
        state_file = (
            Path.home() / ".local" / "state" / "atlas-steamos-updater" / state_name
        )
        reject_reinstall_or_downgrade(details, state_file)
        print("Validated ATLAS update")
        print(f"  Version: {details['version']}")
        print(f"  SHA-256: {details['sha256']}")
        print(f"  Files:   {len(details['files'])}")
        print("The Debian maintainer scripts will NOT be executed.")
        if not prompt("Would you like to update ATLAS?"):
            log_event("cancelled", file=path.name, version=details.get("version"))
            print("Cancelled.")
            return 0

        if auto_update_enabled():
            try:
                self_update(container)
            except HandlerError as exc:
                log_event("self-update", status="failed", error=str(exc))
                print(f"WARNING: {exc}", file=sys.stderr)
                print("Continuing with the currently installed updater.")

        process_check = run_in_container(
            ["pgrep", "-x", "atlas-preview|atlas"],
            container=container,
            capture=True,
        )
        if process_check.returncode == 0:
            if not prompt("ATLAS is running. Would you like to close it before updating?"):
                raise HandlerError("ATLAS must be closed before installation")
            stop_result = run_in_container(
                ["pkill", "-TERM", "-x", "atlas-preview|atlas"],
                container=container,
            )
            if stop_result.returncode not in {0, 1}:
                raise HandlerError("could not stop ATLAS")

        install_result = run_in_container(
            [
                "sudo",
                "python3",
                str(installer),
                "--install",
                "--allowed-parent",
                str(allowed),
                "--state-file",
                str(state_file),
                str(path),
            ],
            container=container,
        )
        if install_result.returncode:
            raise HandlerError("installation failed; see the error above")

        relaunched = False
        if not args.no_relaunch and prompt("Would you like to open ATLAS now?"):
            executable = (
                "/usr/bin/atlas-preview"
                if "usr/bin/atlas-preview" in details["files"]
                else "/usr/bin/atlas"
            )
            subprocess.Popen(
                ["distrobox", "enter", container, "--", executable],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            relaunched = True
        log_event(
            "installed",
            file=path.name,
            version=details.get("version"),
            sha256=details.get("sha256"),
            relaunched=relaunched,
        )
        print("Done. You may close this terminal.")
        return 0
    except (HandlerError, OSError, json.JSONDecodeError) as exc:
        log_event("failed", file=update_name, error=str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        try:
            input("Press Enter to close...")
        except EOFError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
