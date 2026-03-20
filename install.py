#!/usr/bin/env python3

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import sysconfig
from importlib.util import find_spec
from pathlib import Path


APP_NAME = "skill-sync"
PAYLOAD_DIR_NAME = ".skill-sync"
RICH_REQUIREMENT = "rich"


def is_windows() -> bool:
    return os.name == "nt"


def info(message: str) -> None:
    print(f"[INFO] {message}")


def success(message: str) -> None:
    print(f"[ OK ] {message}")


def warn(message: str) -> None:
    print(f"[WARN] {message}")


def fail(message: str, exit_code: int = 1) -> int:
    print(f"[ERR ] {message}", file=sys.stderr)
    return exit_code


def has_rich() -> bool:
    return find_spec("rich") is not None


def install_rich() -> None:
    if has_rich():
        return

    info("Installing Python dependency: rich")
    command = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    if sys.prefix == getattr(sys, "base_prefix", sys.prefix):
        command.append("--user")
    command.append(RICH_REQUIREMENT)

    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip() or "unknown pip error"
        raise RuntimeError(f"Failed to install '{RICH_REQUIREMENT}': {details}")

    if not has_rich():
        raise RuntimeError(f"'{RICH_REQUIREMENT}' still cannot be imported after installation.")


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def path_entries() -> list[Path]:
    raw_entries = os.environ.get("PATH", "").split(os.pathsep)
    entries: list[Path] = []
    seen: set[str] = set()

    for raw in raw_entries:
        if not raw:
            continue
        entry = Path(raw).expanduser()
        key = os.path.normcase(str(entry))
        if key in seen:
            continue
        seen.add(key)
        entries.append(entry)

    return entries


def is_writable_directory(path: Path) -> bool:
    return path.is_dir() and os.access(path, os.W_OK)


def preferred_fallback_bin_dir(home: Path) -> Path:
    scheme = "nt_user" if is_windows() else "posix_user"
    scripts_path = sysconfig.get_path("scripts", scheme=scheme)
    if scripts_path:
        return Path(scripts_path)
    return home / "bin" if is_windows() else home / ".local" / "bin"


def choose_launcher_dir(home: Path) -> tuple[Path, bool]:
    writable_entries: list[Path] = []
    for entry in path_entries():
        if not is_writable_directory(entry):
            continue
        writable_entries.append(entry)
        try:
            entry.relative_to(home)
        except ValueError:
            continue
        return entry, True

    if writable_entries:
        return writable_entries[0], True

    fallback = preferred_fallback_bin_dir(home)
    ensure_directory(fallback)
    in_path = any(
        os.path.normcase(str(candidate)) == os.path.normcase(str(fallback))
        for candidate in path_entries()
    )
    return fallback, in_path


def launcher_path(launcher_dir: Path) -> Path:
    suffix = ".cmd" if is_windows() else ""
    return launcher_dir / f"{APP_NAME}{suffix}"


def payload_paths(home: Path) -> tuple[Path, Path]:
    install_dir = home / PAYLOAD_DIR_NAME
    return install_dir, install_dir / f"{APP_NAME}.py"


def source_script() -> Path:
    path = Path(__file__).resolve().parent / f"{APP_NAME}.py"
    if not path.is_file():
        raise FileNotFoundError(f"Source script not found: {path}")
    return path


def write_launcher(launcher: Path, payload_script: Path) -> None:
    python_executable = Path(sys.executable).resolve()

    if is_windows():
        content = "\r\n".join(
            [
                "@echo off",
                f'"{python_executable}" "{payload_script}" %*',
                "",
            ]
        )
        launcher.write_text(content, encoding="utf-8", newline="")
        return

    content = "\n".join(
        [
            "#!/bin/sh",
            f'exec "{python_executable}" "{payload_script}" "$@"',
            "",
        ]
    )
    launcher.write_text(content, encoding="utf-8")
    current_mode = launcher.stat().st_mode
    launcher.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def install() -> int:
    if len(sys.argv) != 1:
        return fail("This installer does not accept command-line arguments.")

    try:
        install_rich()
    except RuntimeError as exc:
        return fail(str(exc))

    home = Path.home()
    launcher_dir, launcher_in_path = choose_launcher_dir(home)
    install_dir, payload_script = payload_paths(home)

    ensure_directory(install_dir)
    ensure_directory(launcher_dir)

    source = source_script()
    shutil.copy2(source, payload_script)
    launcher = launcher_path(launcher_dir)
    write_launcher(launcher, payload_script)

    success(f"Installed payload: {payload_script}")
    success(f"Installed command: {launcher}")

    if launcher_in_path:
        success(f"Command '{APP_NAME}' is ready to use.")
    else:
        warn(f"{launcher_dir} is not currently on PATH.")
        warn(f"Add it to PATH to run '{APP_NAME}' from any directory.")

    return 0


if __name__ == "__main__":
    sys.exit(install())
