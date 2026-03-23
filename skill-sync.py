#!/usr/bin/env python3

from __future__ import annotations

import os
import re
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

RESERVED_INJECT_BLOCKS_KEY = "reserved_inject_blocks"
AGENT_INJECT_FILENAME = "agent-inject.md"
INJECT_TARGET_FILES = ("AGENTS.md", "CLAUDE.md")
MARKER_PATTERN = re.compile(r"^<!--\s*(.+?):(start|end)\s*-->$")


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


@dataclass
class RuntimeContext:
    mode: str
    sync_root: Path
    config_path: Path


@dataclass
class SyncConfig:
    agents: dict[str, list[str]]
    reserved_inject_blocks: list[str]


@dataclass
class InjectedSkill:
    name: str
    content: str


@dataclass
class DocumentBlock:
    name: str
    body_lines: list[str]
    raw_lines: list[str]


@dataclass
class DocumentSegment:
    kind: str
    lines: list[str]
    block: DocumentBlock | None = None


def normalize_string_list(values: object, field_name: str) -> list[str]:
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise ValueError(f"{field_name} must be an array of strings.")

    ordered_unique_values: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = value.strip()
        if not name:
            raise ValueError(f"{field_name} contains an empty string.")
        if name in seen:
            continue
        seen.add(name)
        ordered_unique_values.append(name)

    return ordered_unique_values


def load_config(config_path: Path) -> SyncConfig:
    if not config_path.is_file():
        raise ValueError(f"Config file not found: {config_path}")

    try:
        with config_path.open("rb") as handle:
            raw_config = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Failed to parse TOML: {config_path} ({exc})") from exc

    if not isinstance(raw_config, dict):
        raise ValueError("Config root must be a TOML table.")

    raw_reserved_blocks = raw_config.get(RESERVED_INJECT_BLOCKS_KEY, [])
    if raw_reserved_blocks is None:
        raw_reserved_blocks = []
    reserved_inject_blocks = normalize_string_list(raw_reserved_blocks, RESERVED_INJECT_BLOCKS_KEY)

    parsed_agents: dict[str, list[str]] = {}
    for agent, section in raw_config.items():
        if agent == RESERVED_INJECT_BLOCKS_KEY:
            continue
        if not isinstance(agent, str) or not agent.strip():
            raise ValueError("Agent name must be a non-empty string.")
        if not isinstance(section, dict):
            raise ValueError(f"Section [{agent}] must be a TOML table.")

        skills = section.get("skills")
        if skills is None:
            raise ValueError(f"Section [{agent}] is missing 'skills'.")
        parsed_agents[agent.strip()] = normalize_string_list(skills, f"Section [{agent}].skills")

    return SyncConfig(agents=parsed_agents, reserved_inject_blocks=reserved_inject_blocks)


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


def create_windows_directory_symlink(source: Path, target: Path) -> None:
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/D", str(target), str(source)],
        capture_output=True,
        text=True,
        creationflags=creation_flags,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        detail = stderr or stdout or "unknown error"
        raise OSError(f"Failed to create directory symlink: {detail}")


def create_directory_symlink(source: Path, target: Path) -> None:
    try:
        target.symlink_to(source, target_is_directory=True)
    except OSError as exc:
        raise OSError(f"Failed to create symlink: {exc}") from exc


def create_directory_link(source: Path, target: Path) -> str:
    if is_windows():
        try:
            create_windows_directory_symlink(source, target)
            return "symlink"
        except OSError as windows_symlink_exc:
            try:
                create_directory_symlink(source, target)
                return "symlink"
            except OSError as exc:
                raise OSError(
                    "Failed to create directory symlink via mklink /D "
                    f"({windows_symlink_exc}); fallback symlink also failed ({exc})"
                ) from windows_symlink_exc

    create_directory_symlink(source, target)
    return "symlink"

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


def sync_agent_skills(agent: str, skills: list[str], skill_home: Path, sync_root: Path) -> SyncStats:
    stats = SyncStats(agent=agent)
    target_skills_dir = sync_root / f".{agent}" / "skills"

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


def collect_injected_skills(stats_list: list[SyncStats]) -> list[InjectedSkill]:
    injected_skills: list[InjectedSkill] = []
    seen: set[str] = set()

    for stats in stats_list:
        for result in stats.results:
            if result.status != "OK":
                continue
            if result.skill in seen:
                continue

            inject_path = result.source / AGENT_INJECT_FILENAME
            if not inject_path.is_file():
                continue

            content = inject_path.read_text(encoding="utf-8")
            injected_skills.append(InjectedSkill(name=result.skill, content=content))
            seen.add(result.skill)

    return injected_skills


def detect_newline(content: str) -> str:
    if "\r\n" in content:
        return "\r\n"
    if "\r" in content:
        return "\r"
    return "\n"


def parse_document_segments(content: str, path: Path) -> list[DocumentSegment]:
    lines = content.splitlines()
    segments: list[DocumentSegment] = []
    text_lines: list[str] = []
    index = 0

    while index < len(lines):
        line = lines[index]
        marker_match = MARKER_PATTERN.match(line)
        if marker_match is None:
            text_lines.append(line)
            index += 1
            continue

        marker_name = marker_match.group(1).strip()
        marker_kind = marker_match.group(2)
        if marker_kind == "end":
            raise ValueError(f"Unmatched end marker for '{marker_name}' in {path}")

        if text_lines:
            segments.append(DocumentSegment(kind="text", lines=text_lines.copy()))
            text_lines.clear()

        block_lines = [line]
        body_lines: list[str] = []
        index += 1

        while index < len(lines):
            current_line = lines[index]
            current_match = MARKER_PATTERN.match(current_line)
            if current_match is None:
                body_lines.append(current_line)
                block_lines.append(current_line)
                index += 1
                continue

            current_name = current_match.group(1).strip()
            current_kind = current_match.group(2)
            if current_kind == "start":
                raise ValueError(f"Nested start marker for '{current_name}' in {path}")
            if current_name != marker_name:
                raise ValueError(
                    f"Mismatched end marker for '{current_name}' while parsing '{marker_name}' in {path}"
                )

            block_lines.append(current_line)
            segments.append(
                DocumentSegment(
                    kind="block",
                    lines=block_lines,
                    block=DocumentBlock(name=marker_name, body_lines=body_lines, raw_lines=block_lines.copy()),
                )
            )
            index += 1
            break
        else:
            raise ValueError(f"Missing end marker for '{marker_name}' in {path}")

    if text_lines:
        segments.append(DocumentSegment(kind="text", lines=text_lines.copy()))

    return segments


def block_lines(name: str, content: str) -> list[str]:
    lines = content.splitlines()
    return [f"<!-- {name}:start -->", *lines, f"<!-- {name}:end -->"]


def render_document_content(
    existing_content: str,
    desired_blocks: list[InjectedSkill],
    reserved_names: set[str],
    path: Path,
) -> str:
    newline = detect_newline(existing_content)
    desired_by_name = {block.name: block for block in desired_blocks if block.name not in reserved_names}
    emitted: set[str] = set()
    output_lines: list[str] = []

    for segment in parse_document_segments(existing_content, path):
        if segment.kind == "text":
            output_lines.extend(segment.lines)
            continue

        assert segment.block is not None
        if segment.block.name in reserved_names:
            output_lines.extend(segment.block.raw_lines)
            continue

        replacement = desired_by_name.get(segment.block.name)
        if replacement is None:
            output_lines.extend(segment.block.raw_lines)
            continue

        output_lines.extend(block_lines(replacement.name, replacement.content))
        emitted.add(replacement.name)

    pending_blocks = [block for block in desired_blocks if block.name not in reserved_names and block.name not in emitted]
    for pending in pending_blocks:
        if output_lines and output_lines[-1] != "":
            output_lines.append("")
        output_lines.extend(block_lines(pending.name, pending.content))
        emitted.add(pending.name)

    while output_lines and output_lines[-1] == "":
        output_lines.pop()

    if not output_lines:
        return ""
    return newline.join(output_lines) + newline


def sync_document_injections(sync_root: Path, injected_skills: list[InjectedSkill], reserved_names: set[str]) -> int:
    errors = 0

    for filename in INJECT_TARGET_FILES:
        target_path = sync_root / filename
        existing_content = target_path.read_text(encoding="utf-8") if target_path.is_file() else ""

        try:
            new_content = render_document_content(existing_content, injected_skills, reserved_names, target_path)
            target_path.write_text(new_content, encoding="utf-8")
        except (OSError, ValueError) as exc:
            error(f"[inject] failed to sync {target_path}: {exc}")
            errors += 1
            continue

        success(f"[inject] synced {target_path}")

    return errors


def resolve_runtime_context(argv: list[str]) -> RuntimeContext:
    if len(argv) > 2:
        raise ValueError("Usage: skill-sync.py [PATH]")

    if len(argv) == 1:
        sync_root = Path.home()
        return RuntimeContext(mode="global", sync_root=sync_root, config_path=sync_root / ".mskill.toml")

    raw_path = Path(argv[1]).expanduser()
    sync_root = raw_path if raw_path.is_absolute() else (Path.cwd() / raw_path)
    sync_root = sync_root.resolve(strict=False)

    if not sync_root.exists():
        raise ValueError(f"Target path does not exist: {sync_root}")
    if not sync_root.is_dir():
        raise ValueError(f"Target path is not a directory: {sync_root}")

    return RuntimeContext(mode="local", sync_root=sync_root, config_path=sync_root / ".mskill.toml")


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

    try:
        runtime = resolve_runtime_context(sys.argv)
    except ValueError as exc:
        return fail(str(exc))

    skill_home_raw = os.environ.get("SKILL_HOME")
    if not skill_home_raw:
        return fail("SKILL_HOME is not set.")

    skill_home = Path(skill_home_raw).expanduser()
    if not skill_home.is_dir():
        return fail(f"SKILL_HOME does not point to a directory: {skill_home}")

    if runtime.mode == "local" and not runtime.config_path.is_file():
        warn(f"Config file not found under target path. Skipping: {runtime.config_path}")
        return 0

    try:
        config = load_config(runtime.config_path)
    except ValueError as exc:
        return fail(str(exc))

    if not config.agents:
        warn(f"No agent sections found in {runtime.config_path}. Nothing to do.")
        return 0

    info(f"Using SKILL_HOME={skill_home}")
    info(f"Using mode={runtime.mode}")
    info(f"Using sync_root={runtime.sync_root}")
    info(f"Using config={runtime.config_path}")
    if config.reserved_inject_blocks:
        info(f"Using reserved_inject_blocks={config.reserved_inject_blocks}")

    stats_list: list[SyncStats] = []
    for agent, skills in config.agents.items():
        stats_list.append(sync_agent_skills(agent, skills, skill_home, runtime.sync_root))

    injection_errors = 0
    if runtime.mode == "local":
        injected_skills = collect_injected_skills(stats_list)
        injection_errors = sync_document_injections(
            runtime.sync_root,
            injected_skills,
            set(config.reserved_inject_blocks),
        )

    print_summary_table(stats_list)

    had_errors = any(stats.errors for stats in stats_list) or injection_errors > 0
    if had_errors:
        error("Finished with errors.")
        return 1

    success("Finished successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
