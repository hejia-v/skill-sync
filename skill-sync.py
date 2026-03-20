#!/usr/bin/env python3

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

try:
    from rich import box
    from rich.console import Console
    from rich.markup import escape
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ModuleNotFoundError as exc:
    box = None
    Console = None
    Panel = None
    Table = None
    Text = None
    escape = None
    RICH_IMPORT_ERROR = exc
else:
    RICH_IMPORT_ERROR = None


AGENT_WIDTH = 12
SKILL_WIDTH = 28
REASON_WIDTH = 30
STATUS_WIDTH = 7

HEADER_STYLE = "bold white on dark_blue"
STATUS_OK_STYLE = "bold black on bright_green"
STATUS_ERROR_STYLE = "bold white on dark_red"
STATUS_SKIP_STYLE = "bold black on bright_yellow"


def console_width() -> int:
    return max(160, shutil.get_terminal_size((160, 40)).columns)


STDOUT_CONSOLE = Console(highlight=False, soft_wrap=True, width=console_width()) if Console else None
STDERR_CONSOLE = Console(stderr=True, highlight=False, soft_wrap=True, width=console_width()) if Console else None


def bootstrap_fail(message: str, exit_code: int = 1) -> int:
    print(f"[ERR ] {message}", file=sys.stderr)
    return exit_code


def render(message: str) -> str:
    return escape(message) if escape else message


def info(message: str) -> None:
    if STDOUT_CONSOLE:
        STDOUT_CONSOLE.print(f"[bold cyan][INFO][/bold cyan] {render(message)}")
        return
    print(f"[INFO] {message}")


def success(message: str) -> None:
    if STDOUT_CONSOLE:
        STDOUT_CONSOLE.print(f"[bold green][ OK ][/bold green] {render(message)}")
        return
    print(f"[ OK ] {message}")


def warn(message: str) -> None:
    if STDOUT_CONSOLE:
        STDOUT_CONSOLE.print(f"[bold yellow][WARN][/bold yellow] {render(message)}")
        return
    print(f"[WARN] {message}")


def error(message: str) -> None:
    if STDERR_CONSOLE:
        STDERR_CONSOLE.print(f"[bold red][ERR ][/bold red] {render(message)}")
        return
    print(f"[ERR ] {message}", file=sys.stderr)


def fail(message: str, exit_code: int = 1) -> int:
    error(message)
    return exit_code


def shorten_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def padded_cell(text: str, width: int, style: str) -> Text | str:
    content = shorten_text(text, width).ljust(width)
    if Text is None:
        return content
    return Text(content, style=style)


@dataclass
class SkillLinkResult:
    agent: str
    skill: str
    source: Path
    target: Path
    status: str
    reason: str
    is_error: bool = False


@dataclass
class SyncStats:
    agent: str
    cleared_links: int = 0
    errors: int = 0
    results: list[SkillLinkResult] | None = None

    def __post_init__(self) -> None:
        if self.results is None:
            self.results = []


def load_config(config_path: Path) -> dict[str, list[str]]:
    if not config_path.is_file():
        raise ValueError(f"Config file not found: {config_path}")

    try:
        with config_path.open("rb") as handle:
            raw_config = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Failed to parse TOML: {config_path} ({exc})") from exc

    if not isinstance(raw_config, dict):
        raise ValueError("Config root must be a TOML table.")

    parsed: dict[str, list[str]] = {}
    for agent, section in raw_config.items():
        if not isinstance(agent, str) or not agent.strip():
            raise ValueError("Agent name must be a non-empty string.")
        if not isinstance(section, dict):
            raise ValueError(f"Section [{agent}] must be a TOML table.")

        skills = section.get("skills")
        if skills is None:
            raise ValueError(f"Section [{agent}] is missing 'skills'.")
        if not isinstance(skills, list) or not all(isinstance(item, str) for item in skills):
            raise ValueError(f"Section [{agent}].skills must be an array of strings.")

        ordered_unique_skills: list[str] = []
        seen: set[str] = set()
        for skill in skills:
            name = skill.strip()
            if not name:
                raise ValueError(f"Section [{agent}] contains an empty skill name.")
            if name in seen:
                continue
            seen.add(name)
            ordered_unique_skills.append(name)

        parsed[agent.strip()] = ordered_unique_skills

    return parsed


def is_windows() -> bool:
    return os.name == "nt"


def is_junction(path: Path) -> bool:
    if not is_windows():
        return False
    checker = getattr(os.path, "isjunction", None)
    if checker is None:
        return False
    return checker(path)


def is_link_like(path: Path) -> bool:
    return path.is_symlink() or is_junction(path)


def remove_existing_link(path: Path) -> None:
    if is_junction(path):
        path.rmdir()
        return

    path.unlink()


def create_windows_junction(source: Path, target: Path) -> None:
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(target), str(source)],
        capture_output=True,
        text=True,
        creationflags=creation_flags,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        detail = stderr or stdout or "unknown error"
        raise OSError(f"Failed to create junction: {detail}")


def create_directory_link(source: Path, target: Path) -> str:
    try:
        target.symlink_to(source, target_is_directory=True)
        return "symlink"
    except OSError as exc:
        if not is_windows():
            raise OSError(f"Failed to create symlink: {exc}") from exc

        try:
            create_windows_junction(source, target)
        except OSError as fallback_exc:
            raise OSError(
                f"Failed to create symlink ({exc}); fallback junction also failed ({fallback_exc})"
            ) from fallback_exc

        return "junction"


def ensure_directory(path: Path) -> None:
    if path.exists():
        if not path.is_dir():
            raise OSError(f"Path exists but is not a directory: {path}")
        return
    path.mkdir(parents=True, exist_ok=True)


def clear_directory_links(agent: str, target_skills_dir: Path, stats: SyncStats) -> None:
    for child in target_skills_dir.iterdir():
        if not is_link_like(child):
            continue

        try:
            remove_existing_link(child)
        except OSError as exc:
            error(f"[{agent}] failed to remove existing link {child}: {exc}")
            stats.errors += 1
            continue

        stats.cleared_links += 1
        warn(f"[{agent}] removed existing link: {child}")


def add_result(
    stats: SyncStats,
    skill: str,
    source: Path,
    target: Path,
    status: str,
    reason: str,
    is_error: bool,
) -> None:
    stats.results.append(
        SkillLinkResult(
            agent=stats.agent,
            skill=skill,
            source=source,
            target=target,
            status=status,
            reason=reason,
            is_error=is_error,
        )
    )


def sync_agent_skills(agent: str, skills: list[str], skill_home: Path, home: Path) -> SyncStats:
    stats = SyncStats(agent=agent)
    target_skills_dir = home / f".{agent}" / "skills"

    try:
        ensure_directory(target_skills_dir)
    except OSError as exc:
        error(f"[{agent}] cannot prepare target directory {target_skills_dir}: {exc}")
        stats.errors += 1
        return stats

    if not skills:
        add_result(
            stats,
            skill="-",
            source=Path("-"),
            target=target_skills_dir,
            status="SKIP",
            reason="no skills configured",
            is_error=False,
        )
        return stats

    clear_directory_links(agent, target_skills_dir, stats)
    info(f"[{agent}] syncing {len(skills)} skill(s) into {target_skills_dir}")

    for skill_name in skills:
        source_dir = skill_home / skill_name
        target_path = target_skills_dir / skill_name

        if not source_dir.is_dir():
            reason = "source skill directory not found"
            error(f"[{agent}] {reason}: {source_dir}")
            stats.errors += 1
            add_result(stats, skill_name, source_dir, target_path, "ERROR", reason, True)
            continue

        if target_path.exists() or is_link_like(target_path):
            reason = "target path already exists as a real directory or file"
            error(f"[{agent}] {reason}: {target_path}")
            stats.errors += 1
            add_result(stats, skill_name, source_dir, target_path, "ERROR", reason, True)
            continue

        try:
            link_type = create_directory_link(source_dir, target_path)
        except OSError as exc:
            reason = f"failed to create link: {exc}"
            error(f"[{agent}] {reason} for {skill_name}")
            stats.errors += 1
            add_result(stats, skill_name, source_dir, target_path, "ERROR", reason, True)
            continue

        reason = f"linked successfully via {link_type}"
        success(f"[{agent}] linked {skill_name} -> {source_dir} ({link_type})")
        add_result(stats, skill_name, source_dir, target_path, "OK", reason, False)

    return stats


def status_cell(result: SkillLinkResult) -> str:
    if result.status == "SKIP":
        return padded_cell("SKIP", STATUS_WIDTH, STATUS_SKIP_STYLE)
    if result.is_error:
        return padded_cell("ERROR", STATUS_WIDTH, STATUS_ERROR_STYLE)
    return padded_cell("OK", STATUS_WIDTH, STATUS_OK_STYLE)


def print_summary_table(stats_list: list[SyncStats]) -> None:
    if not STDOUT_CONSOLE or not Table or not box or not Panel:
        return

    total_rows = sum(len(stats.results) for stats in stats_list)
    error_rows = sum(1 for stats in stats_list for result in stats.results if result.is_error)
    skip_rows = sum(1 for stats in stats_list for result in stats.results if result.status == "SKIP")

    table = Table(
        box=box.SIMPLE_HEAVY,
        header_style=HEADER_STYLE,
        show_lines=False,
        pad_edge=True,
        expand=False,
        safe_box=True,
        row_styles=["none", "dim"],
    )
    table.add_column("Agent", style="bold cyan", no_wrap=True, overflow="crop", width=AGENT_WIDTH)
    table.add_column("Skill", style="bold", no_wrap=True, overflow="crop", width=SKILL_WIDTH)
    table.add_column("Status", justify="center", no_wrap=True, overflow="crop", width=STATUS_WIDTH)
    table.add_column("Reason", no_wrap=True, overflow="crop", width=REASON_WIDTH)

    first_group = True
    for stats in stats_list:
        if not first_group:
            table.add_section()
        first_group = False

        for result in stats.results:
            row_style = "yellow" if result.status == "SKIP" else ("red" if result.is_error else "green")
            table.add_row(
                shorten_text(result.agent, AGENT_WIDTH),
                shorten_text(result.skill, SKILL_WIDTH),
                status_cell(result),
                shorten_text(result.reason, REASON_WIDTH),
                style=row_style,
            )

    panel = Panel.fit(
        table,
        title="[bold white]Skill Sync[/bold white]",
        subtitle=f"[dim]rows={total_rows}  errors={error_rows}  skips={skip_rows}[/dim]",
        border_style="cyan",
        padding=(0, 1),
    )

    STDOUT_CONSOLE.print()
    STDOUT_CONSOLE.print(panel)
    STDOUT_CONSOLE.print()


def main() -> int:
    if RICH_IMPORT_ERROR is not None:
        return bootstrap_fail("Missing dependency 'rich'. Run 'python install.py' or 'python -m pip install rich'.")

    if len(sys.argv) != 1:
        return fail("This script does not accept command-line arguments.")

    skill_home_raw = os.environ.get("SKILL_HOME")
    if not skill_home_raw:
        return fail("SKILL_HOME is not set.")

    skill_home = Path(skill_home_raw).expanduser()
    if not skill_home.is_dir():
        return fail(f"SKILL_HOME does not point to a directory: {skill_home}")

    home = Path.home()
    config_path = home / ".mskill.toml"

    try:
        config = load_config(config_path)
    except ValueError as exc:
        return fail(str(exc))

    if not config:
        warn(f"No agent sections found in {config_path}. Nothing to do.")
        return 0

    info(f"Using SKILL_HOME={skill_home}")
    info(f"Using config={config_path}")

    stats_list: list[SyncStats] = []
    for agent, skills in config.items():
        stats_list.append(sync_agent_skills(agent, skills, skill_home, home))

    print_summary_table(stats_list)

    had_errors = any(stats.errors for stats in stats_list)
    if had_errors:
        error("Finished with errors.")
        return 1

    success("Finished successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
