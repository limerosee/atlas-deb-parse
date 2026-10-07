#!/usr/bin/env python3
"""Route validated ATLAS updates from SteamOS into its Distrobox."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import unquote, urlparse


APP_ID = "com.nanitnet.atlas"
CONTAINER = "atlas"


class HandlerError(RuntimeError):
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


def run_in_container(arguments: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    command = ["distrobox", "enter", CONTAINER, "--", *arguments]
    return subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def prompt(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


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

    try:
        if not shutil.which("distrobox"):
            raise HandlerError("distrobox is not installed")
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
            capture=True,
        )
        if inspect_result.returncode:
            detail = inspect_result.stderr.strip() or inspect_result.stdout.strip()
            raise HandlerError(f"package validation failed: {detail}")
        details = json.loads(inspect_result.stdout)
        state_file = (
            Path.home()
            / ".local"
            / "state"
            / "atlas-steamos-updater"
            / "atlas.json"
        )
        reject_reinstall_or_downgrade(details, state_file)
        print("Validated ATLAS update")
        print(f"  Version: {details['version']}")
        print(f"  SHA-256: {details['sha256']}")
        print(f"  Files:   {len(details['files'])}")
        print("The Debian maintainer scripts will NOT be executed.")
        if not prompt("Install this update into Distrobox 'atlas'?"):
            print("Cancelled.")
            return 0

        process_check = run_in_container(["pgrep", "-x", "atlas-preview"], capture=True)
        if process_check.returncode == 0:
            if not prompt("ATLAS is running. Stop it before updating?"):
                raise HandlerError("ATLAS must be closed before installation")
            stop_result = run_in_container(["pkill", "-TERM", "-x", "atlas-preview"])
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
            ]
        )
        if install_result.returncode:
            raise HandlerError("installation failed; see the error above")

        if not args.no_relaunch and prompt("Relaunch ATLAS now?"):
            subprocess.Popen(
                ["distrobox", "enter", CONTAINER, "--", "/usr/bin/atlas-preview"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        print("Done. You may close this terminal.")
        return 0
    except (HandlerError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        try:
            input("Press Enter to close...")
        except EOFError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
