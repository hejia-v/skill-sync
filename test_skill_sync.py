import importlib.util
import os
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("skill-sync.py")
SPEC = importlib.util.spec_from_file_location("skill_sync_module", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Unable to load module from {MODULE_PATH}")
SKILL_SYNC = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SKILL_SYNC
SPEC.loader.exec_module(SKILL_SYNC)


class SkillSyncMainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parent / f"test-artifacts-{uuid.uuid4().hex}"
        self.root.mkdir()
        self.skill_home = self.root / "skill-home"
        self.skill_home.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def run_main(self, argv: list[str], home: Path) -> int:
        with (
            patch.object(SKILL_SYNC, "RICH_IMPORT_ERROR", None),
            patch.object(SKILL_SYNC.Path, "home", return_value=home),
            patch.object(sys, "argv", argv),
            patch.dict(os.environ, {"SKILL_HOME": str(self.skill_home)}, clear=False),
        ):
            return SKILL_SYNC.main()

    def write_config(self, root: Path, content: str) -> None:
        (root / ".mskill.toml").write_text(content, encoding="utf-8")

    def create_skill(self, name: str, inject_content: str | None = None) -> Path:
        skill_dir = self.skill_home / name
        skill_dir.mkdir()
        if inject_content is not None:
            (skill_dir / "agent-inject.md").write_text(inject_content, encoding="utf-8")
        return skill_dir

    def test_local_mode_skips_when_config_missing(self) -> None:
        project_root = self.root / "project"
        home_root = self.root / "home"
        project_root.mkdir()
        home_root.mkdir()

        exit_code = self.run_main(["skill-sync.py", str(project_root)], home_root)

        self.assertEqual(exit_code, 0)
        self.assertFalse((project_root / ".codex").exists())
        self.assertFalse((home_root / ".codex").exists())

    def test_local_mode_writes_links_under_target_path(self) -> None:
        project_root = self.root / "project"
        home_root = self.root / "home"
        foo_skill = self.create_skill("foo")
        bar_skill = self.create_skill("bar")

        project_root.mkdir()
        home_root.mkdir()
        self.write_config(
            project_root,
            '[codex]\nskills = ["foo"]\n[claude]\nskills = ["bar"]\n[agents]\nskills = []\n',
        )

        def fake_create_link(source: Path, target: Path) -> str:
            target.mkdir()
            return "symlink"

        with patch.object(SKILL_SYNC, "create_directory_link", side_effect=fake_create_link) as link_mock:
            exit_code = self.run_main(["skill-sync.py", str(project_root)], home_root)

        codex_target = project_root / ".codex" / "skills" / "foo"
        claude_target = project_root / ".claude" / "skills" / "bar"
        self.assertEqual(exit_code, 0)
        self.assertTrue(codex_target.exists())
        self.assertTrue(claude_target.exists())
        link_mock.assert_any_call(foo_skill, codex_target)
        link_mock.assert_any_call(bar_skill, claude_target)
        self.assertFalse((home_root / ".codex").exists())
        self.assertFalse((home_root / ".claude").exists())

    def test_global_mode_still_uses_home_root(self) -> None:
        home_root = self.root / "home"
        foo_skill = self.create_skill("foo")

        home_root.mkdir()
        self.write_config(home_root, '[codex]\nskills = ["foo"]\n')

        def fake_create_link(source: Path, target: Path) -> str:
            target.mkdir()
            return "symlink"

        with patch.object(SKILL_SYNC, "create_directory_link", side_effect=fake_create_link) as link_mock:
            exit_code = self.run_main(["skill-sync.py"], home_root)

        codex_target = home_root / ".codex" / "skills" / "foo"
        self.assertEqual(exit_code, 0)
        self.assertTrue(codex_target.exists())
        link_mock.assert_called_once_with(foo_skill, codex_target)

    def test_local_mode_rejects_missing_target_path(self) -> None:
        home_root = self.root / "home"
        home_root.mkdir()

        exit_code = self.run_main(["skill-sync.py", str(self.root / "missing")], home_root)

        self.assertEqual(exit_code, 1)

    def test_local_mode_injects_linked_skills_into_agents_and_claude_docs(self) -> None:
        project_root = self.root / "project"
        home_root = self.root / "home"
        foo_skill = self.create_skill("foo", "foo guidance\n")
        bar_skill = self.create_skill("bar", "bar guidance\nsecond line\n")

        project_root.mkdir()
        home_root.mkdir()
        self.write_config(project_root, '[codex]\nskills = ["foo"]\n[claude]\nskills = ["bar"]\n')

        def fake_create_link(source: Path, target: Path) -> str:
            target.mkdir()
            return "symlink"

        with patch.object(SKILL_SYNC, "create_directory_link", side_effect=fake_create_link) as link_mock:
            exit_code = self.run_main(["skill-sync.py", str(project_root)], home_root)

        expected_doc = (
            "<!-- foo:start -->\n"
            "foo guidance\n"
            "<!-- foo:end -->\n"
            "\n"
            "<!-- bar:start -->\n"
            "bar guidance\n"
            "second line\n"
            "<!-- bar:end -->\n"
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual((project_root / "AGENTS.md").read_text(encoding="utf-8"), expected_doc)
        self.assertEqual((project_root / "CLAUDE.md").read_text(encoding="utf-8"), expected_doc)
        link_mock.assert_any_call(foo_skill, project_root / ".codex" / "skills" / "foo")
        link_mock.assert_any_call(bar_skill, project_root / ".claude" / "skills" / "bar")

    def test_local_mode_updates_non_reserved_blocks_and_preserves_reserved_blocks(self) -> None:
        project_root = self.root / "project"
        home_root = self.root / "home"
        foo_skill = self.create_skill("foo", "updated foo\n")
        gitnexus_skill = self.create_skill("gitnexus", "should not replace\n")

        project_root.mkdir()
        home_root.mkdir()
        self.write_config(
            project_root,
            'reserved_inject_blocks = ["gitnexus"]\n[codex]\nskills = ["gitnexus", "foo"]\n',
        )

        original_doc = (
            "# Header\n"
            "\n"
            "<!-- gitnexus:start -->\n"
            "keep me\n"
            "<!-- gitnexus:end -->\n"
            "\n"
            "<!-- foo:start -->\n"
            "outdated foo\n"
            "<!-- foo:end -->\n"
            "\n"
            "<!-- stale:start -->\n"
            "remove me\n"
            "<!-- stale:end -->\n"
        )
        (project_root / "AGENTS.md").write_text(original_doc, encoding="utf-8")
        (project_root / "CLAUDE.md").write_text(original_doc, encoding="utf-8")

        def fake_create_link(source: Path, target: Path) -> str:
            target.mkdir()
            return "symlink"

        with patch.object(SKILL_SYNC, "create_directory_link", side_effect=fake_create_link) as link_mock:
            exit_code = self.run_main(["skill-sync.py", str(project_root)], home_root)

        expected_doc = (
            "# Header\n"
            "\n"
            "<!-- gitnexus:start -->\n"
            "keep me\n"
            "<!-- gitnexus:end -->\n"
            "\n"
            "<!-- foo:start -->\n"
            "updated foo\n"
            "<!-- foo:end -->\n"
            "\n"
            "<!-- stale:start -->\n"
            "remove me\n"
            "<!-- stale:end -->\n"
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual((project_root / "AGENTS.md").read_text(encoding="utf-8"), expected_doc)
        self.assertEqual((project_root / "CLAUDE.md").read_text(encoding="utf-8"), expected_doc)
        link_mock.assert_any_call(gitnexus_skill, project_root / ".codex" / "skills" / "gitnexus")
        link_mock.assert_any_call(foo_skill, project_root / ".codex" / "skills" / "foo")

    def test_global_mode_does_not_write_agent_docs(self) -> None:
        home_root = self.root / "home"
        self.create_skill("foo", "foo guidance\n")

        home_root.mkdir()
        self.write_config(home_root, '[codex]\nskills = ["foo"]\n')

        def fake_create_link(source: Path, target: Path) -> str:
            target.mkdir()
            return "symlink"

        with patch.object(SKILL_SYNC, "create_directory_link", side_effect=fake_create_link):
            exit_code = self.run_main(["skill-sync.py"], home_root)

        self.assertEqual(exit_code, 0)
        self.assertFalse((home_root / "AGENTS.md").exists())
        self.assertFalse((home_root / "CLAUDE.md").exists())

    def test_local_mode_fails_when_existing_document_has_malformed_block(self) -> None:
        project_root = self.root / "project"
        home_root = self.root / "home"
        self.create_skill("foo", "foo guidance\n")

        project_root.mkdir()
        home_root.mkdir()
        self.write_config(project_root, '[codex]\nskills = ["foo"]\n')
        (project_root / "AGENTS.md").write_text("<!-- foo:start -->\nmissing end\n", encoding="utf-8")

        def fake_create_link(source: Path, target: Path) -> str:
            target.mkdir()
            return "symlink"

        with patch.object(SKILL_SYNC, "create_directory_link", side_effect=fake_create_link):
            exit_code = self.run_main(["skill-sync.py", str(project_root)], home_root)

        self.assertEqual(exit_code, 1)
        self.assertEqual((project_root / "AGENTS.md").read_text(encoding="utf-8"), "<!-- foo:start -->\nmissing end\n")
        self.assertTrue((project_root / "CLAUDE.md").exists())


class LoadConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parent / f"test-artifacts-{uuid.uuid4().hex}"
        self.root.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_load_config_parses_reserved_inject_blocks(self) -> None:
        config_path = self.root / ".mskill.toml"
        config_path.write_text(
            'reserved_inject_blocks = ["gitnexus", "gitnexus", ""]\n[codex]\nskills = ["foo"]\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "reserved_inject_blocks contains an empty string."):
            SKILL_SYNC.load_config(config_path)

        config_path.write_text(
            'reserved_inject_blocks = ["gitnexus", "gitnexus"]\n[codex]\nskills = ["foo", "foo"]\n',
            encoding="utf-8",
        )
        config = SKILL_SYNC.load_config(config_path)

        self.assertEqual(config.reserved_inject_blocks, ["gitnexus"])
        self.assertEqual(config.agents, {"codex": ["foo"]})


class CreateDirectoryLinkTests(unittest.TestCase):
    def test_windows_prefers_mklink_directory_symlink_before_python_symlink(self) -> None:
        source = Path(r"C:\skills\foo")
        target = Path(r"C:\project\.codex\skills\foo")

        with (
            patch.object(SKILL_SYNC, "is_windows", return_value=True),
            patch.object(SKILL_SYNC, "create_windows_directory_symlink") as windows_symlink_mock,
            patch.object(SKILL_SYNC, "create_directory_symlink") as symlink_mock,
        ):
            link_type = SKILL_SYNC.create_directory_link(source, target)

        self.assertEqual(link_type, "symlink")
        windows_symlink_mock.assert_called_once_with(source, target)
        symlink_mock.assert_not_called()

    def test_windows_falls_back_to_python_symlink_when_mklink_d_fails(self) -> None:
        source = Path(r"C:\skills\foo")
        target = Path(r"C:\project\.codex\skills\foo")

        with (
            patch.object(SKILL_SYNC, "is_windows", return_value=True),
            patch.object(
                SKILL_SYNC,
                "create_windows_directory_symlink",
                side_effect=OSError("mklink /D failed"),
            ) as windows_symlink_mock,
            patch.object(SKILL_SYNC, "create_directory_symlink") as symlink_mock,
        ):
            link_type = SKILL_SYNC.create_directory_link(source, target)

        self.assertEqual(link_type, "symlink")
        windows_symlink_mock.assert_called_once_with(source, target)
        symlink_mock.assert_called_once_with(source, target)

    def test_create_windows_directory_symlink_uses_mklink_d(self) -> None:
        source = Path(r"C:\skills\foo")
        target = Path(r"C:\project\.codex\skills\foo")

        completed = subprocess.CompletedProcess(
            args=["cmd", "/c", "mklink", "/D", str(target), str(source)],
            returncode=0,
            stdout="",
            stderr="",
        )

        with patch.object(SKILL_SYNC.subprocess, "run", return_value=completed) as run_mock:
            SKILL_SYNC.create_windows_directory_symlink(source, target)

        run_mock.assert_called_once()
        command = run_mock.call_args.args[0]
        self.assertEqual(command, ["cmd", "/c", "mklink", "/D", str(target), str(source)])


if __name__ == "__main__":
    unittest.main()
