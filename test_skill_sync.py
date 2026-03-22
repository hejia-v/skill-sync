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
        foo_skill = self.skill_home / "foo"
        bar_skill = self.skill_home / "bar"

        project_root.mkdir()
        home_root.mkdir()
        foo_skill.mkdir()
        bar_skill.mkdir()
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
        foo_skill = self.skill_home / "foo"

        home_root.mkdir()
        foo_skill.mkdir()
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
