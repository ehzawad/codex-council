"""Workspace, capabilities, freshness, evidence and real-process adapter tests.

No paid inference or network. The fake CLI checks integration/argv, NOT the real
Codex sandbox. tests/test_live_workflow.py is the opt-in actual-sandbox gate.
"""

import contextlib
import copy
import dataclasses
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/codex-council/skills/codex-council/scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import council_evidence as evidence  # noqa: E402
import council_policy as policy  # noqa: E402
import council_workflow as workflow  # noqa: E402
import council_workspace as workspace  # noqa: E402
from fake_workflow_codex import apply_codex_override  # noqa: E402


@dataclasses.dataclass(frozen=True)
class RoleFixture:
    id: str = "experiment"
    label: str = "Current contribution"
    instruction: str = "Reproduce the suspected boundary failure."
    execution: dict | None = None
    workdir: str | None = None
    evidence_dir: str | None = None
    execution_overrides: tuple = ()
    task_id: str | None = None
    input_snapshot: str | None = None


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def apply_overrides(config, values):
    """Apply -c settings the way Codex does, not as Python TOML documents."""
    result = copy.deepcopy(config)
    for setting in values:
        apply_codex_override(result, setting)
    return result


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup)
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / "source"
        self.run = self.base / "run"
        self.source.mkdir(mode=0o700)
        self.run.mkdir(mode=0o700)
        self.file = self.source / "subject.py"
        self.file.write_text("value = 1\n")
        self.baseline = self.run / "baseline"

    def _cleanup(self):
        for root, dirs, files in os.walk(self.temp.name):
            for name in dirs:
                path = Path(root) / name
                if not path.is_symlink():
                    path.chmod(0o700)
            for name in files:
                path = Path(root) / name
                if not path.is_symlink() and path.is_file():
                    path.chmod(0o600)
        self.temp.cleanup()

    def capture(self, **kwargs):
        return workspace.capture(self.source, self.baseline, include=["."], **kwargs)

    def git(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                   GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                   GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
        return subprocess.run(["git", "-C", str(self.source), *args],
                              env=env, capture_output=True, check=True).stdout

    def init_git(self):
        self.git("init", "-q")
        self.git("add", "subject.py")
        self.git("commit", "-qm", "baseline")


class CaptureTests(WorkspaceCase):
    def test_copy_is_independent_and_baseline_is_read_only(self):
        snap = self.capture()
        target = self.run / "scratch"
        workspace.copy_workspace(self.baseline, target, snap)
        (target / "subject.py").write_text("value = 2\n")
        self.assertEqual(self.file.read_text(), "value = 1\n")
        self.assertEqual((self.baseline / "subject.py").read_text(), "value = 1\n")
        self.assertNotEqual(self.file.stat().st_ino, (target / "subject.py").stat().st_ino)
        self.assertEqual(stat.S_IMODE((self.baseline / "subject.py").stat().st_mode), 0o400)

    def test_git_uses_dirty_bytes_and_relevant_untracked_files(self):
        self.init_git()
        self.file.write_text("value = 2\n")
        self.git("add", "subject.py")
        self.file.write_text("value = 3\n")
        (self.source / "regression.py").write_text("assert value == 3\n")
        snap = workspace.capture(self.source, self.baseline)
        self.assertEqual((self.baseline / "subject.py").read_text(), "value = 3\n")
        self.assertIn("regression.py", snap["files"])
        self.assertIsNotNone(snap["git"]["head"])
        self.assertFalse((self.baseline / ".git").exists())
        self.assertEqual(self.git("show", ":subject.py"), b"value = 2\n")

    def test_git_deletion_is_preserved(self):
        self.init_git()
        self.file.unlink()
        snap = workspace.capture(self.source, self.baseline)
        self.assertNotIn("subject.py", snap["files"])

    def test_ignored_files_require_explicit_scope(self):
        self.init_git()
        (self.source / ".gitignore").write_text("ignored.txt\n")
        (self.source / "ignored.txt").write_text("fixture")
        snap = workspace.capture(self.source, self.baseline)
        self.assertNotIn("ignored.txt", snap["files"])
        explicit = workspace.capture(self.source, self.run / "explicit", include=["ignored.txt"])
        self.assertIn("ignored.txt", explicit["files"])

    def test_non_git_requires_explicit_scope_without_initializing_git(self):
        with self.assertRaisesRegex(workspace.WorkspaceError, "explicit include"):
            workspace.capture(self.source, self.baseline)
        snap = self.capture()
        self.assertIsNone(snap["git"])
        self.assertFalse((self.source / ".git").exists())
        self.assertFalse((self.baseline / ".git").exists())

    def test_empty_explicit_scope_supports_context_only_tasks(self):
        snap = workspace.capture(self.source, self.baseline, include=[])
        self.assertEqual(snap["files"], {})

    def test_credentials_and_agent_configuration_are_omitted(self):
        (self.source / ".env").write_text("SECRET=test-fixture")
        (self.source / ".codex").mkdir()
        (self.source / ".codex/config.toml").write_text('model="untrusted"')
        (self.source / ".env.example").write_text("TOKEN=")
        snap = self.capture()
        self.assertIn(".env", snap["omitted"])
        self.assertIn(".codex", snap["omitted"])
        self.assertIn(".env.example", snap["files"])
        self.assertFalse((self.baseline / ".env").exists())

    def test_source_symlinks_are_refused_and_never_followed(self):
        outside = self.base / "outside"
        outside.write_text("original")
        (self.source / "escape").symlink_to(outside)
        with self.assertRaises((workspace.WorkspaceError, OSError)):
            self.capture()
        self.assertEqual(outside.read_text(), "original")

    def test_symlinked_parent_in_explicit_include_is_refused(self):
        outside = self.base / "outside-dir"
        outside.mkdir()
        (outside / "value.txt").write_text("original")
        (self.source / "jump").symlink_to(outside, target_is_directory=True)
        with self.assertRaises((workspace.WorkspaceError, OSError)):
            workspace.capture(self.source, self.baseline, include=["jump/value.txt"])

    def test_hardlinked_input_becomes_an_independent_copy(self):
        outside = self.base / "outside"
        outside.write_text("original")
        os.link(outside, self.source / "hardlink")
        snap = self.capture()
        target = self.run / "scratch"
        workspace.copy_workspace(self.baseline, target, snap)
        (target / "hardlink").write_text("changed")
        self.assertEqual(outside.read_text(), "original")

    def test_fifo_is_refused_without_blocking(self):
        os.mkfifo(self.source / "pipe")
        started = time.monotonic()
        with self.assertRaises((workspace.WorkspaceError, OSError)):
            self.capture()
        self.assertLess(time.monotonic() - started, 2)

    def test_capture_detects_source_changes_during_copy(self):
        real = workspace._hash_file
        changed = False

        def changing(root, name, destination=None):
            nonlocal changed
            result = real(root, name, destination)
            if destination is not None and not changed:
                changed = True
                self.file.write_text("value = 999\n")
            return result

        with patch.object(workspace, "_hash_file", side_effect=changing):
            with self.assertRaisesRegex(workspace.WorkspaceError, "source changed"):
                self.capture()

    def test_nested_run_directory_is_refused(self):
        with self.assertRaisesRegex(workspace.WorkspaceError, "outside"):
            workspace.capture(self.source, self.source / "run" / "baseline", include=["."])

    def test_executable_bit_is_preserved(self):
        self.file.chmod(0o755)
        snap = self.capture()
        target = self.run / "scratch"
        workspace.copy_workspace(self.baseline, target, snap)
        self.assertEqual(stat.S_IMODE((target / "subject.py").stat().st_mode), 0o700)

    def test_case_collisions_are_rejected_when_filesystem_supports_them(self):
        a, b = self.source / "Case.txt", self.source / "case.txt"
        a.write_text("a")
        b.write_text("b")
        if a.stat().st_ino == b.stat().st_ino:
            self.skipTest("case-insensitive filesystem")
        with self.assertRaisesRegex(workspace.WorkspaceError, "collision"):
            self.capture()

    def test_collision_checker_covers_directories_and_components(self):
        """Pure check, independent of the host filesystem's case rules."""
        nfd = "cafe\u0301"
        refused = (
            ((), ("Foo", "foo")),                       # empty directories
            (("Foo/a.txt", "foo/b.txt"), ()),           # directory components
            (("Foo/a.txt",), ("foo",)),                 # component vs empty dir
            (("Foo",), ("foo",)),                       # file vs directory
            (("x/Foo/a", "x/FOO/b"), ()),               # deeper component
            ((f"{nfd}/a.txt", "caf\u00e9/b.txt"), ()),   # NFC vs NFD component
            (("Same.txt", "same.txt"), ()),             # complete file paths
        )
        for names, dirs in refused:
            with self.subTest(names=names, dirs=dirs):
                pair = workspace.portable_collision(names, dirs)
                self.assertIsNotNone(pair)
                self.assertNotEqual(pair[0], pair[1])
        for names, dirs in ((("Foo/a.txt", "Foo/b.txt"), ("Foo/empty",)),
                            (("a/b.txt", "ab/b.txt"), ("a/b",))):
            with self.subTest(names=names, dirs=dirs):
                self.assertIsNone(workspace.portable_collision(names, dirs))

    def test_collision_checker_uses_canonical_caseless_matching(self):
        """APFS treats these as one name; NFC then case folding did not,
        so capture failed while copying instead of refusing the pair."""
        composed, decomposed = "\u0390", "\u03aa\u0301"
        for names in ((composed, decomposed),
                      (f"{composed}/a.txt", f"{decomposed}/b.txt")):
            with self.subTest(names=names):
                self.assertIsNotNone(workspace.portable_collision(names))

    def test_inventory_refuses_directory_and_component_collisions(self):
        """The capture path refuses what the checker reports; _walk is
        stubbed so case-insensitive hosts exercise it too."""
        def walk(names, dirs):
            def fake(root, start, exclude, omitted, empty=None, special=None, *,
                     hint=""):
                if empty is not None:
                    empty.update(dirs)
                return set(names)
            return fake

        for names, dirs in (((), ("Foo", "foo")),
                            (("Foo/a.txt", "foo/b.txt"), ())):
            with self.subTest(names=names, dirs=dirs), \
                    patch.object(workspace, "_walk", walk(names, dirs)), \
                    patch.object(workspace, "_hash_file") as hashed:
                with self.assertRaisesRegex(
                        workspace.WorkspaceError,
                        "non-portable case/Unicode collision: "):
                    workspace.inventory(self.source, ["."], [])
                hashed.assert_not_called()

    def test_git_scope_component_collision_is_refused_on_any_filesystem(self):
        """The index can hold Foo/a.txt and foo/b.txt even where the
        filesystem folds case; the capture is refused either way."""
        self.init_git()
        (self.source / "Foo").mkdir()
        (self.source / "Foo/a.txt").write_text("a")
        with contextlib.suppress(FileExistsError):
            (self.source / "foo").mkdir()
        (self.source / "foo/b.txt").write_text("b")
        # Add Foo/a.txt first: with core.ignorecase, a later add would adopt
        # the case of a directory already in the index.
        self.git("add", "Foo/a.txt")
        blob = self.git("hash-object", "-w", str(self.source / "foo/b.txt"))
        self.git("update-index", "--add", "--cacheinfo",
                 f"100644,{blob.decode().strip()},foo/b.txt")
        self.assertIn(b"\tfoo/b.txt", self.git("ls-files", "--stage"))
        self.assertIn(b"\tFoo/a.txt", self.git("ls-files", "--stage"))
        with self.assertRaisesRegex(workspace.WorkspaceError,
                                    "non-portable case/Unicode collision: "):
            workspace.capture(self.source, self.baseline)
        self.assertFalse(self.baseline.exists())

    def test_git_scope_refuses_a_tracked_symlink_by_name(self):
        self.init_git()
        (self.source / "link").symlink_to("subject.py")
        self.git("add", "link")
        self.git("commit", "-qm", "link")
        with self.assertRaisesRegex(
                workspace.WorkspaceError,
                r"selected symlink is not copied: link; add it to exclude in request\.json"):
            workspace.capture(self.source, self.baseline)
        snap = workspace.capture(self.source, self.run / "excluded", exclude=["link"])
        self.assertEqual(snap["omitted"]["link"], "requested exclusion")
        self.assertIn("subject.py", snap["files"])

    def test_git_scope_refuses_a_tracked_file_replaced_by_a_directory(self):
        self.init_git()
        self.file.unlink()
        (self.source / "subject.py").mkdir()
        (self.source / "subject.py/inner.txt").write_text("inner")
        with self.assertRaisesRegex(
                workspace.WorkspaceError,
                "selected special file or directory is not copied: subject.py; "
                r"add it to exclude in request\.json"):
            workspace.capture(self.source, self.baseline)

    def test_git_scope_refuses_a_submodule_populated_empty_or_missing(self):
        self.init_git()
        (self.source / "sub").mkdir()
        self.git("-C", "sub", "init", "-q")
        (self.source / "sub/inner.txt").write_text("inner")
        self.git("-C", "sub", "add", "inner.txt")
        self.git("-C", "sub", "commit", "-qm", "inner")
        self.git("add", "sub")
        self.assertIn(b"160000 ", self.git("ls-files", "--stage", "sub"))
        refusal = ("Git submodule is not copied: sub; add it to exclude in "
                   r"request\.json, or select its files with include")
        for state in ("populated", "empty", "missing"):
            if state == "empty":
                shutil.rmtree(self.source / "sub")
                (self.source / "sub").mkdir()
            elif state == "missing":
                (self.source / "sub").rmdir()
            with self.subTest(state=state):
                with self.assertRaisesRegex(workspace.WorkspaceError, refusal):
                    workspace.capture(self.source, self.run / f"baseline-{state}")
                snap = workspace.capture(self.source, self.run / f"excluded-{state}",
                                         exclude=["sub"])
                self.assertEqual(snap["omitted"]["sub"], "requested exclusion")
                self.assertIn("subject.py", snap["files"])

    def test_git_scope_refuses_an_untracked_nested_repository(self):
        self.init_git()
        (self.source / "nested").mkdir()
        self.git("-C", "nested", "init", "-q")
        (self.source / "nested/inner.txt").write_text("inner")
        with self.assertRaisesRegex(
                workspace.WorkspaceError,
                "nested Git repository is not copied: nested/; add it to exclude in "
                r"request\.json, or select its files with include"):
            workspace.capture(self.source, self.baseline)
        snap = workspace.capture(self.source, self.run / "excluded", exclude=["nested"])
        self.assertEqual(snap["omitted"]["nested"], "requested exclusion")
        # The refusal prints "nested/"; added to exclude as printed, it works too.
        snap = workspace.capture(self.source, self.run / "as-printed", exclude=["nested/"])
        self.assertEqual(snap["omitted"]["nested"], "requested exclusion")
        self.assertEqual(workspace.freshness(snap)["state"], "unchanged")

    def test_git_scope_excluding_a_link_that_replaced_a_tracked_directory(self):
        """The refusal names the link; excluding it is enough, and the
        tracked paths below it are omitted with it, never read through it."""
        (self.source / "d").mkdir()
        (self.source / "d/x.txt").write_text("x")
        self.init_git()
        self.git("add", "d")
        self.git("commit", "-qm", "d")
        host = self.base / "host"
        host.mkdir()
        (host / "x.txt").write_text("HOST-SECRET-SENTINEL")
        shutil.rmtree(self.source / "d")
        (self.source / "d").symlink_to(host, target_is_directory=True)
        with self.assertRaisesRegex(
                workspace.WorkspaceError,
                r"selected symlink is not copied: d; add it to exclude in request\.json"):
            workspace.capture(self.source, self.baseline)
        excluded = self.run / "excluded"
        snap = workspace.capture(self.source, excluded, exclude=["d"])
        self.assertEqual(snap["omitted"], {"d": "requested exclusion",
                                           "d/x.txt": "requested exclusion"})
        self.assertEqual(set(snap["files"]), {"subject.py"})
        self.assertEqual(sorted(path.name for path in excluded.rglob("*")), ["subject.py"])
        self.assertEqual(workspace.freshness(snap)["state"], "unchanged")

    @unittest.skipIf(os.geteuid() == 0, "root reads a mode-000 directory anyway")
    def test_git_scope_refuses_an_unreadable_tracked_path_not_a_deletion(self):
        """lexists() is False on EACCES, so 1.3.3 silently dropped such a
        path as a tracked deletion."""
        (self.source / "locked").mkdir()
        (self.source / "locked/f.txt").write_text("f")
        self.init_git()
        self.git("add", "locked")
        self.git("commit", "-qm", "locked")
        (self.source / "locked").chmod(0o000)
        with self.assertRaises(PermissionError):
            workspace.capture(self.source, self.baseline)
        self.assertFalse(self.baseline.exists())

    def test_explicit_include_through_a_linked_directory_names_the_fix(self):
        """Not "[Errno 20] Not a directory: 'lnk'"; exclusions are matched
        below the include path, so the fix is the include path."""
        (self.source / "real").mkdir()
        (self.source / "real/b.txt").write_text("b")
        (self.source / "lnk").symlink_to("real", target_is_directory=True)
        for name in ("lnk/b.txt", "real/b.txt/c"):
            with self.subTest(include=name):
                with self.assertRaisesRegex(
                        workspace.WorkspaceError,
                        "selected input or a directory in its path is a symlink or not "
                        f"a directory: {name}; name it in include by a path through "
                        "real directories"):
                    workspace.capture(self.source, self.baseline, include=[name])
                self.assertFalse(self.baseline.exists())
        snap = workspace.capture(self.source, self.run / "real", include=["real/b.txt"])
        self.assertEqual(set(snap["files"]), {"real/b.txt"})

    def test_a_directory_holding_only_a_listed_link_is_not_empty(self):
        """A recorded symlink or special file counts as found, so its
        parent is not reported as an empty directory."""
        (self.source / "bin").mkdir()
        (self.source / "bin/git").symlink_to("/usr/bin/git")
        (self.source / "pipes").mkdir()
        os.mkfifo(self.source / "pipes/p")
        special = {}
        _, _, _, empty = workspace.inventory(self.source, ["."], [], special=special,
                                             portable=False)
        self.assertEqual(special, {"bin/git": "symlink", "pipes/p": "special-file"})
        self.assertEqual(empty, [])

    def test_explicit_scope_refusal_names_the_exclude_fix(self):
        (self.source / "link").symlink_to("subject.py")
        with self.assertRaisesRegex(
                workspace.WorkspaceError,
                "selected symlink or special file is not copied: link; "
                r"add it to exclude in request\.json"):
            self.capture()

    def test_selected_input_reader_names_links_and_directories(self):
        """Not '[Errno 21] Is a directory: 3' (a descriptor number) or a
        bare ELOOP/ENOTDIR."""
        (self.source / "dir").mkdir()
        (self.source / "dir/value.txt").write_text("value")
        (self.source / "link").symlink_to("subject.py")
        (self.source / "jump").symlink_to("dir", target_is_directory=True)
        for name, message in (
                ("dir", "selected input is not a regular file: dir"),
                ("link", "selected input or a directory in its path is a symlink or "
                         "not a directory: link"),
                ("jump/value.txt", "selected input or a directory in its path is a "
                                   "symlink or not a directory: jump/value.txt"),
                ("subject.py/value.txt", "selected input or a directory in its path "
                                         "is a symlink or not a directory: "
                                         r"subject\.py/value\.txt")):
            with self.subTest(name=name):
                with self.assertRaisesRegex(workspace.WorkspaceError, message):
                    with workspace.regular(self.source, name):
                        pass

    def test_created_directories_are_private_under_a_permissive_umask(self):
        (self.source / "a/b").mkdir(parents=True)
        (self.source / "a/b/c.txt").write_text("c")
        (self.source / "x/y/z").mkdir(parents=True)
        for mask in (0o022, 0o000):
            with self.subTest(umask=f"{mask:03o}"):
                run = self.base / f"run-{mask:03o}"
                run.mkdir(mode=0o700)
                previous = os.umask(mask)
                try:
                    # Before freeze, the baseline's own modes are visible.
                    with patch.object(workspace, "freeze"):
                        snap = workspace.capture(self.source, run / "baseline",
                                                 include=["."])
                    workspace.freeze(run / "baseline")
                    unfrozen = run / "unfrozen"
                    with patch.object(workspace, "freeze"):
                        workspace.capture(self.source, unfrozen, include=["."])
                    target = run / "scratch"
                    workspace.copy_workspace(run / "baseline", target, snap)
                finally:
                    os.umask(previous)
                for root in (unfrozen, target):
                    for directory, dirs, files in os.walk(root):
                        self.assertEqual(
                            stat.S_IMODE(os.lstat(directory).st_mode), 0o700,
                            directory)
                        for name in files:
                            path = Path(directory) / name
                            self.assertEqual(
                                stat.S_IMODE(path.lstat().st_mode), 0o600, path)
                self.assertEqual(sorted(snap["dirs"]), ["x/y/z"])

    def test_empty_directories_are_captured_copied_and_compared(self):
        (self.source / "output").mkdir()
        (self.source / "nested/leaf").mkdir(parents=True)
        (self.source / "secrets").mkdir()
        (self.source / "secrets/.env").write_text("TOKEN=x")
        snap = self.capture()
        self.assertEqual(snap["dirs"], ["nested/leaf", "output", "secrets"])
        target = self.run / "scratch"
        workspace.copy_workspace(self.baseline, target, snap)
        for name in snap["dirs"]:
            self.assertTrue((target / name).is_dir(), name)
            self.assertTrue((self.baseline / name).is_dir(), name)
        self.assertFalse((target / "secrets/.env").exists())
        self.assertEqual(workspace.freshness(snap)["state"], "unchanged")
        (self.source / "output").rmdir()
        fresh = workspace.freshness(snap)
        self.assertEqual(fresh["state"], "stale")
        self.assertIn("output/", fresh["changed_paths"])
        (target / "nested/leaf").rmdir()
        with self.assertRaisesRegex(workspace.WorkspaceError, "empty directories"):
            workspace.verify_tree(target, snap["files"], snap["dirs"])

    def test_modified_baseline_is_not_used(self):
        snap = self.capture()
        target = self.baseline / "subject.py"
        target.chmod(0o600)
        target.write_text("changed")
        with self.assertRaisesRegex(workspace.WorkspaceError, "input changed"):
            workspace.copy_workspace(self.baseline, self.run / "scratch", snap)

    def test_a_link_planted_in_a_prepared_copy_has_no_request_hint(self):
        """The link is not in the source, so editing request.json cannot
        fix it; the check keeps its own wording."""
        snap = self.capture()
        target = self.run / "copy"
        workspace.copy_workspace(self.baseline, target, snap)
        (target / "planted").symlink_to("subject.py")
        with self.assertRaises(workspace.WorkspaceError) as ctx:
            workspace.verify_tree(target, snap["files"], snap["dirs"])
        self.assertEqual(str(ctx.exception),
                         "selected symlink or special file is not copied: planted")

    def test_added_baseline_control_directory_is_detected(self):
        snap = self.capture()
        self.baseline.chmod(0o700)
        (self.baseline / ".codex").mkdir()
        with self.assertRaisesRegex(workspace.WorkspaceError, "control"):
            workspace.verify_tree(self.baseline, snap["files"], snap["dirs"])

    def test_freshness_tracks_content_additions_deletions_and_index(self):
        self.init_git()
        snap = workspace.capture(self.source, self.baseline)
        self.assertEqual(workspace.freshness(snap)["state"], "unchanged")
        self.file.write_text("new value\n")
        self.assertIn("subject.py", workspace.freshness(snap)["changed_paths"])
        self.file.write_text("value = 1\n")
        (self.source / "new.txt").write_text("new")
        self.assertIn("new.txt", workspace.freshness(snap)["changed_paths"])
        (self.source / "new.txt").unlink()
        self.git("rm", "--cached", "subject.py")
        self.assertTrue(workspace.freshness(snap)["git_metadata_changed"])

    def test_freshness_of_a_moved_git_source_says_the_source_is_missing(self):
        """Not "non-Git inputs require explicit include paths"."""
        self.init_git()
        snap = workspace.capture(self.source, self.baseline)
        self.source.rename(self.base / "moved")
        self.assertEqual(workspace.freshness(snap), {
            "state": "unknown",
            "error": f"WorkspaceError: source_root is missing or not a directory: "
                     f"{self.source}"})

    def test_freshness_refusals_do_not_ask_for_a_request_edit(self):
        """A prepared run cannot follow "add it to exclude in request.json"
        (its request is bound), so freshness reports the path alone."""
        self.init_git()
        git_scope = workspace.capture(self.source, self.baseline)
        explicit = workspace.capture(self.source, self.run / "explicit", include=["."])
        (self.source / "newlink").symlink_to("subject.py")
        for snap, message in (
                (git_scope, "WorkspaceError: selected symlink is not copied: newlink"),
                (explicit, "WorkspaceError: selected symlink or special file is not "
                           "copied: newlink")):
            with self.subTest(include=snap["include"]):
                self.assertEqual(workspace.freshness(snap),
                                 {"state": "unknown", "error": message})
        with self.assertRaisesRegex(workspace.WorkspaceError,
                                    r"newlink; add it to exclude in request\.json"):
            workspace.capture(self.source, self.run / "again")

    def test_freshness_binds_selected_index_entries_not_the_whole_index(self):
        self.init_git()
        (self.source / "other.txt").write_text("unrelated")
        self.git("add", "other.txt")
        self.git("commit", "-qm", "other")
        narrow = workspace.capture(self.source, self.baseline, include=["subject.py"])
        context_only = workspace.capture(self.source, self.run / "context-only", include=[])
        (self.source / "other.txt").write_text("unrelated staged edit")
        self.git("add", "other.txt")
        for snap in (narrow, context_only):
            fresh = workspace.freshness(snap)
            self.assertEqual(fresh["state"], "unchanged", fresh)
        # Staging the captured path itself still changes its binding.
        self.file.write_text("value = 2\n")
        self.git("add", "subject.py")
        self.file.write_text("value = 1\n")
        fresh = workspace.freshness(narrow)
        self.assertEqual((fresh["state"], fresh["changed_paths"]), ("stale", []))
        self.assertTrue(fresh["git_metadata_changed"])
        self.assertEqual(workspace.freshness(context_only)["state"], "unchanged")

    def test_invalid_paths(self):
        for value in ("../escape", "/absolute", "a/../b", "a\\b", "a//b", "", "./a"):
            with self.subTest(value=value), self.assertRaises(workspace.WorkspaceError):
                workspace.relative_path(value)

    def test_strict_json_and_private_reader(self):
        path = self.run / "value.json"
        path.write_text('{"a": 1, "a": 2}')
        path.chmod(0o600)
        with self.assertRaisesRegex(workspace.WorkspaceError, "duplicate"):
            workspace.read_json(path)
        for number in ("NaN", "1e999"):
            path.write_text('{"a": %s}' % number)
            with self.subTest(number=number), self.assertRaisesRegex(
                    workspace.WorkspaceError, "non-finite"):
                workspace.read_json(path)
        write_json(path, {"a": 1})
        path.chmod(0o644)
        with self.assertRaises(workspace.WorkspaceError):
            workspace.read_json(path)
        self.assertEqual(workspace.read_json(path, private=False), {"a": 1})

    def test_deeply_nested_json_is_refused_not_a_traceback(self):
        path = self.run / "request.json"
        depth = 1_000_000  # past every supported Python's parser limit
        path.write_text("[" * depth + "]" * depth)
        path.chmod(0o600)
        with self.assertRaisesRegex(workspace.WorkspaceError,
                                    "JSON nesting too deep: " + re.escape(str(path))):
            workspace.read_json(path, private=False)


class PolicyTests(unittest.TestCase):
    def test_default_is_restricted_fresh_and_required(self):
        p = policy.parse_policy()
        self.assertEqual((p.sandbox, p.network, p.external_tools, p.continuity, p.required),
                         ("read-only", False, "deny", "fresh", True))

    def test_escalation_requires_authorization(self):
        for value in ({"sandbox": "bypass"}, {"external_tools": "native"},
                      {"sandbox": "workspace-write", "network": True}):
            with self.subTest(value=value), self.assertRaisesRegex(workspace.WorkspaceError,
                                                                   "authorization"):
                policy.parse_policy(value)
        p = policy.parse_policy({"sandbox": "workspace-write", "network": True,
                                 "authorization": "User requested dependency download"})
        self.assertTrue(p.network)

    def test_unknown_and_wrong_type_values_are_refused(self):
        for value in ({"network": 1}, {"required": "yes"}, {"sandbox": "auto"},
                      {"continuity": "last"}, {"external_tools": "all"}, {"other": True}, []):
            with self.subTest(value=value), self.assertRaises(workspace.WorkspaceError):
                policy.parse_policy(value)

    def test_mcp_keys_are_bare_codex_dotted_paths(self):
        p = policy.parse_policy()
        settings = policy.config_overrides(p, ("node_repl", "fixture-2"))
        self.assertIn("mcp_servers.node_repl.enabled=false", settings)
        self.assertFalse(any('"' in s for s in settings if s.startswith("mcp_servers.")))
        config = apply_overrides({"mcp_servers": {"node_repl": {}, "fixture-2": {}}}, settings)
        self.assertEqual(set(config["mcp_servers"]), {"node_repl", "fixture-2"})
        self.assertIs(config["mcp_servers"]["node_repl"]["enabled"], False)
        self.assertIs(config["features"]["apps"], False)

    def test_every_sandboxed_policy_ignores_execpolicy_rules(self):
        for sandbox in ("read-only", "workspace-write"):
            with self.subTest(sandbox=sandbox):
                options = policy.command_options(policy.parse_policy({"sandbox": sandbox}))
                self.assertEqual(options[:3], ["--sandbox", sandbox, "--ignore-rules"])

    def test_quoted_mcp_key_would_create_a_different_server(self):
        # Why quoting is wrong: Codex keeps the quotes as part of the name.
        config = apply_overrides({"mcp_servers": {"node_repl": {}}},
                                 ['mcp_servers."node_repl".enabled=false'])
        self.assertEqual(set(config["mcp_servers"]), {"node_repl", '"node_repl"'})
        self.assertNotIn("enabled", config["mcp_servers"]["node_repl"])

    def test_unaddressable_mcp_names_fail_closed(self):
        p = policy.parse_policy()
        for name in ("server.name", 'quote"name', "a:b", "x@y", "a/b", "", "sp ace", "é"):
            with self.subTest(name=name), self.assertRaisesRegex(
                    workspace.WorkspaceError, "cannot be disabled"):
                policy.config_overrides(p, (name,))

    def test_policy_read_failure_keeps_the_sanitized_category(self):
        import council_discovery

        def failing(*_args, **_kwargs):
            raise council_discovery._DiscoveryFailure("server_exited:initialize", "other")

        with patch("shutil.which", return_value="/bin/codex"), \
                patch.object(council_discovery, "_app_server_session", failing):
            with self.assertRaisesRegex(workspace.WorkspaceError,
                                        r"\(server_exited:initialize:other\)"):
                policy._read_config("/scratch", ())

    def test_metadata_inspection_checks_effective_settings_twice(self):
        seen = []
        config = {"mcp_servers": {"fixture": {"enabled": True, "url": "SECRET-SENTINEL"}}}

        def read(root, settings):
            seen.append((root, settings))
            return apply_overrides(config, settings), None

        result = policy.inspect_policy("/scratch", policy.parse_policy(), reader=read)
        self.assertEqual(len(seen), 2)
        self.assertNotIn("SECRET-SENTINEL", json.dumps(result))
        self.assertIn("mcp_servers.fixture.enabled=false", result["overrides"])
        self.assertEqual(result["attestation"], "configuration-observed-not-exec-attested")

    def test_managed_override_and_unknown_mcp_fail_closed(self):
        p = policy.parse_policy({"sandbox": "workspace-write"})

        def managed(root, settings):
            result = apply_overrides({"mcp_servers": {}}, settings)
            result["sandbox_mode"] = "danger-full-access"
            return result, None

        with self.assertRaisesRegex(workspace.WorkspaceError, "sandbox_mode"):
            policy.inspect_policy("/scratch", p, reader=managed)
        with self.assertRaisesRegex(workspace.WorkspaceError, "MCP inventory"):
            policy.inspect_policy("/scratch", p, reader=lambda root, settings: ({}, None))

    def test_managed_requirements_that_forbid_the_request_fail_closed(self):
        p = policy.parse_policy({"sandbox": "workspace-write"})
        refused = ({"allowedSandboxModes": ["read-only"]},
                   {"allowedApprovalPolicies": ["on-request"]},
                   {"allowedWebSearchModes": ["live"]},
                   {"featureRequirements": {"memories": True}},
                   {"featureRequirements": {"tool_suggest": True}},
                   {"allowedSandboxModes": "workspace-write"}, ["unexpected"])
        for requirements in refused:
            with self.subTest(requirements=requirements), self.assertRaisesRegex(
                    workspace.WorkspaceError, "managed requirements"):
                policy.inspect_policy("/scratch", p, reader=lambda root, settings: (
                    apply_overrides({"mcp_servers": {}}, settings), requirements))
        allowed = {"allowedSandboxModes": ["read-only", "workspace-write"],
                   "allowedApprovalPolicies": ["never"], "featureRequirements": {"apps": False}}
        policy.inspect_policy("/scratch", p, reader=lambda root, settings: (
            apply_overrides({"mcp_servers": {}}, settings), allowed))

    def test_notify_memories_and_installers_are_disabled_and_observed(self):
        p = policy.parse_policy()
        settings = policy.config_overrides(p)
        for setting in ("notify=[]", "features.memories=false", "features.tool_suggest=false",
                        "features.skill_mcp_dependency_install=false",
                        "features.remote_plugin=false", "features.workspace_dependencies=false",
                        "features.image_generation=false"):
            self.assertIn(setting, settings)
        host = {"mcp_servers": {}, "notify": ["host-program", "--flag"],
                "features": {"memories": True}}
        config = apply_overrides(host, settings)
        policy.validate_observed(config, p, ())
        config["notify"] = ["host-program"]
        with self.assertRaisesRegex(workspace.WorkspaceError, "notify"):
            policy.validate_observed(config, p, ())
        native = policy.parse_policy({"external_tools": "native", "authorization": "user asked"})
        self.assertIn("features.memories=false", policy.config_overrides(native))

    def test_unknown_future_mcp_server_between_reads_is_refused(self):
        calls = 0

        def read(root, settings):
            nonlocal calls
            calls += 1
            config = apply_overrides({"mcp_servers": {}}, settings)
            if calls == 2:
                config["mcp_servers"]["new"] = {"enabled": True}
            return config, None

        with self.assertRaisesRegex(workspace.WorkspaceError, "inventory changed"):
            policy.inspect_policy("/scratch", policy.parse_policy(), reader=read)

    def test_restricts_tmp_and_additional_writable_roots(self):
        p = policy.parse_policy({"sandbox": "workspace-write"})
        config = apply_overrides({"mcp_servers": {}}, policy.config_overrides(p))
        policy.validate_observed(config, p, ())
        config["sandbox_workspace_write"]["writable_roots"] = ["/live"]
        with self.assertRaisesRegex(workspace.WorkspaceError, "writable_roots"):
            policy.validate_observed(config, p, ())


class WorkflowTests(WorkspaceCase):
    def stage(self, *, roles=None, body="Current objective and acceptance criteria.\n"):
        write_json(self.run / "request.json", {
            "schema": workflow.REQUEST_SCHEMA, "task_id": "current-investigation",
            "reason": "Resolve a consequential uncertainty", "source_root": str(self.source),
            "include": ["."],
        })
        (self.run / "context.md").write_text(body)
        roles = [RoleFixture()] if roles is None else roles
        return roles, body

    def test_prepare_binds_context_task_and_independent_workspaces(self):
        roles, body = self.stage(roles=[RoleFixture(id="a"), RoleFixture(id="b")])
        plan = workflow.prepare(str(self.run), roles, body)
        self.assertEqual(plan["task_id"], "current-investigation")
        self.assertEqual(plan["context_bytes"], len(body.encode()))
        self.assertEqual(plan["roles"]["a"]["execution"]["continuity"], "fresh")
        a = self.run / plan["roles"]["a"]["workspace"] / "subject.py"
        b = self.run / plan["roles"]["b"]["workspace"] / "subject.py"
        a.write_text("modified a")
        self.assertEqual(b.read_text(), "value = 1\n")
        self.assertEqual(self.file.read_text(), "value = 1\n")

    def test_run_control_directories_are_private_whatever_the_umask(self):
        """workspaces/, evidence/, catalogs/ and each role's subdirectories
        are 0700 even when the umask clears owner bits of a requested mode;
        a plain mkdir(mode=0o700) left them unwritable under umask 0o200."""
        roles, body = self.stage(roles=[RoleFixture(id="a")])
        previous = os.umask(0o200)
        try:
            workflow.prepare(str(self.run), roles, body)
        finally:
            os.umask(previous)
        for relative in ("workspaces", "evidence", "catalogs",
                         "evidence/a", "catalogs/a"):
            mode = stat.S_IMODE(os.lstat(self.run / relative).st_mode)
            self.assertEqual(mode, 0o700, relative)

    def test_agent_instruction_files_are_omitted_and_the_plan_says_so(self):
        names = ("AGENTS.md", "AGENTS.override.md", "CLAUDE.md", "CLAUDE.local.md",
                 "pkg/AGENTS.md", "pkg/claude.md")
        (self.source / "pkg").mkdir()
        for name in names:
            (self.source / name).write_text("Instructions.\n")
        (self.source / ".codex").mkdir()
        (self.source / ".codex/config.toml").write_text('model = "pinned"\n')
        roles, body = self.stage()
        plan = workflow.prepare(str(self.run), roles, body)
        work = self.run / plan["roles"]["experiment"]["workspace"]
        for name in names:
            self.assertFalse((work / name).exists(), name)
            self.assertFalse((self.run / "baseline" / name).exists(), name)
            self.assertNotIn(name, plan["snapshot"]["files"])
            self.assertEqual(plan["snapshot"]["omitted"][name], "agent instruction file")
        self.assertFalse((work / ".codex").exists())
        self.assertIn(".codex", plan["snapshot"]["omitted"])
        self.assertTrue(any("agent instruction files" in item and "not copied" in item
                            for item in plan["limitations"]), plan["limitations"])
        lines = workflow.plan_lines(plan, roles, 0)
        self.assertIn("AGENTS.md/CLAUDE.md instruction files are not copied", lines[1])

    def test_context_lens_or_permission_changes_require_new_capture(self):
        roles, body = self.stage()
        workflow.prepare(str(self.run), roles, body)
        for altered, text in ((roles, "new objective"),
                              ([dataclasses.replace(roles[0], instruction="different")], body),
                              ([dataclasses.replace(roles[0], execution={"sandbox": "workspace-write"})], body)):
            with self.assertRaises(workspace.WorkspaceError):
                workflow.load_plan(self.run, altered, text)

    def test_prepared_workspaces_must_not_be_reused_after_an_experiment(self):
        roles, body = self.stage()
        plan = workflow.prepare(str(self.run), roles, body)
        (self.run / plan["roles"][roles[0].id]["workspace"] / "subject.py").write_text("changed")
        with self.assertRaisesRegex(workspace.WorkspaceError, "input changed"):
            workflow.load_plan(self.run, roles, body, verify_inputs=True)

    def test_double_prepare_refuses_without_touching_inputs(self):
        roles, body = self.stage()
        workflow.prepare(str(self.run), roles, body)
        before = (self.run / "run.json").read_bytes()
        with self.assertRaises(workspace.WorkspaceError):
            workflow.prepare(str(self.run), roles, body)
        self.assertEqual(before, (self.run / "run.json").read_bytes())

    def test_an_incomplete_capture_is_refused(self):
        roles, body = self.stage()
        (self.run / "prepare.lock").touch(mode=0o600)
        with self.assertRaisesRegex(workspace.WorkspaceError, "capture did not finish"):
            workflow.load_plan(self.run, roles, body)

    def test_tampered_workspace_binding_is_refused(self):
        roles, body = self.stage()
        plan = workflow.prepare(str(self.run), roles, body)
        plan["roles"][roles[0].id]["workspace"] = str(self.source)
        write_json(self.run / "run.json", plan)
        with self.assertRaisesRegex(workspace.WorkspaceError, "deterministic private path"):
            workflow.load_plan(self.run, roles, body)

    def test_recorded_execution_cannot_change_while_author_contract_hash_stays(self):
        roles, body = self.stage()
        plan = workflow.prepare(str(self.run), roles, body)
        plan["roles"][roles[0].id]["execution"] = policy.policy_dict({
            "sandbox": "bypass", "network": True,
            "authorization": "a changed record, not the authored policy"})
        write_json(self.run / "run.json", plan)
        with patch.object(workflow, "inspect_policy") as inspect:
            with self.assertRaisesRegex(workspace.WorkspaceError, "permissions changed"):
                workflow.bind_roles(str(self.run), roles, body, at_launch=True)
        inspect.assert_not_called()

    def test_task_and_source_scope_stay_bound_to_the_request(self):
        roles, body = self.stage()
        original = workflow.prepare(str(self.run), roles, body)
        for field, value in (("task_id", "different-task"),
                             ("source_root", str(self.run)),
                             ("include", []), ("exclude", ["*"])):
            plan = copy.deepcopy(original)
            target = plan if field == "task_id" else plan["snapshot"]
            target[field] = value
            write_json(self.run / "run.json", plan)
            with self.subTest(field=field):
                with self.assertRaisesRegex(workspace.WorkspaceError, "binding differs"):
                    workflow.load_plan(self.run, roles, body)

    def test_native_external_tool_permission_is_not_an_agent_label(self):
        roles, body = self.stage(roles=[RoleFixture(label="Security reviewer",
                                                   execution={"sandbox": "bypass"})])
        with self.assertRaisesRegex(workspace.WorkspaceError, "authorization"):
            workflow.prepare(str(self.run), roles, body)

    def test_bound_role_contains_execution_cwd_and_durable_task_identity(self):
        roles, body = self.stage()
        workflow.prepare(str(self.run), roles, body)
        observed = []

        def inspect(root, p):
            observed.append(root)
            return {"overrides": list(policy.config_overrides(p)), "requested": dataclasses.asdict(p)}

        with patch.object(workflow, "inspect_policy", side_effect=inspect):
            bound, plan = workflow.bind_roles(str(self.run), roles, body, at_launch=True)
        self.assertEqual(bound[0].workdir, str(self.run / "workspaces/experiment"))
        self.assertEqual(observed, [bound[0].workdir])
        self.assertEqual(bound[0].task_id, plan["task_id"])
        self.assertTrue((self.run / "evidence/experiment/execution-policy.json").exists())

    def freshness(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = workflow.freshness_command(str(self.run))
        return code, json.loads(output.getvalue())

    def test_null_exclude_means_no_exclusions_at_every_step(self):
        roles, body = self.stage()
        request = json.loads((self.run / "request.json").read_text())
        write_json(self.run / "request.json", {**request, "exclude": None})
        plan = workflow.prepare(str(self.run), roles, body)
        self.assertEqual(plan["snapshot"]["exclude"], [])
        workflow.load_plan(self.run, roles, body, verify_inputs=True)
        code, fresh = self.freshness()
        self.assertEqual((code, fresh["state"]), (0, "unchanged"), fresh)

    def test_freshness_keeps_input_detail_when_the_context_changed(self):
        roles, body = self.stage()
        workflow.prepare(str(self.run), roles, body)
        self.file.write_text("value = 2\n")
        (self.run / "context.md").write_text("A different objective.\n")
        code, fresh = self.freshness()
        self.assertEqual(code, 1)
        self.assertEqual((fresh["state"], fresh["reason"], fresh["changed_paths"]),
                         ("stale", "staged context changed", ["subject.py"]))
        for key in ("git_metadata_changed", "omissions_changed", "scope"):
            self.assertIn(key, fresh)

    def test_unreadable_context_keeps_input_detail_and_a_stale_state(self):
        roles, body = self.stage()
        workflow.prepare(str(self.run), roles, body)
        (self.run / "context.md").unlink()
        code, fresh = self.freshness()
        self.assertEqual((code, fresh["state"], fresh["reason"], fresh["changed_paths"]),
                         (1, "unknown", "context unavailable", []))
        self.file.write_text("value = 2\n")
        code, fresh = self.freshness()
        self.assertEqual((code, fresh["state"], fresh["reason"], fresh["changed_paths"]),
                         (1, "stale", "context unavailable", ["subject.py"]))

    def test_moved_source_is_unknown_freshness_and_still_refused_elsewhere(self):
        roles, body = self.stage()
        workflow.prepare(str(self.run), roles, body)
        self.source.rename(self.base / "moved")
        code, fresh = self.freshness()
        self.assertEqual((code, fresh["state"]), (1, "unknown"), fresh)
        self.assertIn("error", fresh)
        # Every other step keeps requiring the source it captured.
        with self.assertRaises(FileNotFoundError):
            workflow.load_plan(self.run, roles, body)

    def test_a_looping_source_root_is_a_contract_error_not_a_traceback(self):
        """Python 3.12 raises RuntimeError for a symlink loop in resolve()."""
        loop = self.base / "loop"
        loop.symlink_to(loop)
        roles, body = self.stage()
        request = {**json.loads((self.run / "request.json").read_text()),
                   "source_root": str(loop)}
        write_json(self.run / "request.json", request)
        with self.assertRaises((OSError, ValueError)):
            workflow.prepare(str(self.run), roles, body)

    def test_a_source_spelled_through_a_removed_link_is_unknown_freshness(self):
        """Not "request changed after capture": request.json is unchanged,
        but the link source_root was spelled through is gone."""
        alias = self.base / "alias"
        alias.symlink_to(self.source, target_is_directory=True)
        roles, body = self.stage()
        request = {**json.loads((self.run / "request.json").read_text()),
                   "source_root": str(alias)}
        write_json(self.run / "request.json", request)
        plan = workflow.prepare(str(self.run), roles, body)
        self.assertEqual(plan["snapshot"]["source_root"], str(self.source))
        alias.unlink()  # the captured source itself is still there
        code, fresh = self.freshness()
        self.assertEqual((code, fresh["state"]), (1, "unknown"), fresh)
        self.assertEqual(fresh["error"], "WorkspaceError: source_root is missing or "
                                         f"not a directory: {alias}")
        self.source.rename(self.base / "moved")
        code, fresh = self.freshness()
        self.assertEqual((code, fresh["state"]), (1, "unknown"), fresh)
        # The rest of the request is still bound to the capture.
        write_json(self.run / "request.json", {**request, "task_id": "another-task"})
        with self.assertRaisesRegex(workspace.WorkspaceError, "request changed after capture"):
            self.freshness()

    def test_lifecycle_files_are_owner_readable_whatever_the_umask(self):
        """A umask that clears owner read made 0600 requests come out 0200."""
        roles, body = self.stage()
        previous = os.umask(0o400)
        try:
            plan = workflow.prepare(str(self.run), roles, body)
            directory = self.run / plan["roles"]["experiment"]["evidence"]
            recorder = evidence.AttemptEvidence(directory, ["codex"], "goal")
            meta = recorder.finish(returncode=0, state="exited")
            evidence.collect_artifacts(self.baseline,
                                       self.run / plan["roles"]["experiment"]["workspace"],
                                       plan["snapshot"], directory)
            target = self.run / "value.json"
            workspace.atomic_json(target, {"a": 1})
        finally:
            os.umask(previous)
        for path in (self.run / "prepare.lock", self.run / "run.json", target,
                     directory / meta["stdout_file"], directory / meta["stderr_file"],
                     directory / f"{recorder.key}.json", directory / "candidate.diff"):
            self.assertEqual(stat.S_IMODE(path.lstat().st_mode), 0o600, path)
        self.assertEqual(workspace.read_json(target), {"a": 1})
        self.assertEqual(workflow.load_plan(self.run, roles, body)["run_id"], plan["run_id"])


class EvidenceTests(WorkspaceCase):
    def test_records_raw_bytes_and_separates_tool_evidence_from_claims(self):
        directory = self.run / "evidence"
        directory.mkdir(mode=0o700)
        recorder = evidence.AttemptEvidence(directory, ["codex", "exec"], "goal")
        events = [
            {"type": "item.completed", "item": {"type": "command_execution",
              "command": "python -m unittest", "exit_code": 0, "aggregated_output": "OK\n"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Everything verified"}},
            {"type": "turn.completed"},
        ]
        raw = b"\n".join(json.dumps(event).encode() for event in events) + b"\n"
        for offset in range(0, len(raw), 7):
            recorder.feed("stdout", raw[offset:offset+7])
        recorder.feed("stderr", b"diagnostic")
        result = recorder.finish(returncode=0, state="exited", unsafe_to_replay=True)
        self.assertEqual((directory / result["stdout_file"]).read_bytes(), raw)
        self.assertEqual(result["sha256"]["stdout"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(result["commands"][0]["reported_exit_code"], 0)
        self.assertEqual(result["commands"][0]["source"], "codex-jsonl")
        self.assertNotIn("Everything verified", json.dumps(result))
        self.assertTrue(result["evidence_complete"])

    def test_partial_raw_writes_are_completed_before_claiming_complete_evidence(self):
        directory = self.run / "evidence"
        directory.mkdir(mode=0o700)
        recorder = evidence.AttemptEvidence(directory, ["codex"], "goal")
        stream = recorder.streams["stdout"]
        original_write = stream.write
        raw = b'{"type":"turn.completed"}\n'
        with patch.object(stream, "write", side_effect=lambda data: original_write(data[:3])):
            recorder.feed("stdout", raw)
        result = recorder.finish(returncode=0, state="exited")
        self.assertEqual((directory / result["stdout_file"]).read_bytes(), raw)
        self.assertTrue(result["evidence_complete"])

    def test_raw_stream_no_progress_is_an_explicit_coverage_gap(self):
        directory = self.run / "evidence"
        directory.mkdir(mode=0o700)
        recorder = evidence.AttemptEvidence(directory, ["codex"], "goal")
        stream = recorder.streams["stdout"]
        with patch.object(stream, "write", return_value=0):
            recorder.feed("stdout", b'{"type":"turn.completed"}\n')
        result = recorder.finish(returncode=0, state="exited")
        self.assertFalse(result["evidence_complete"])
        self.assertIn("stdout-write-failed", result["coverage_gaps"])

    def test_oversized_summary_records_keep_raw_logs_and_mark_gap(self):
        directory = self.run / "evidence"
        directory.mkdir(mode=0o700)
        recorder = evidence.AttemptEvidence(directory, ["codex"], "goal")
        raw = b"x" * 200 + b"\n" + b'{"type":"turn.completed"}\n'
        with patch.object(evidence, "MAX_SUMMARY_LINE", 64):
            recorder.feed("stdout", raw)
        result = recorder.finish(returncode=0, state="exited")
        self.assertFalse(result["evidence_complete"])
        self.assertTrue(result["turn_completed_observed"])
        self.assertEqual((directory / result["stdout_file"]).read_bytes(), raw)

    def test_interrupted_evidence_is_retained_and_not_complete_success(self):
        directory = self.run / "evidence"
        directory.mkdir(mode=0o700)
        recorder = evidence.AttemptEvidence(directory, ["codex"], "goal")
        recorder.feed("stdout", b'{"type":"turn.started"}\n')
        result = recorder.finish(returncode=None, state="interrupted")
        self.assertEqual(result["state"], "interrupted")
        self.assertFalse(result["turn_completed_observed"])
        self.assertIsNone(result["transport_exit"])

    def test_generated_text_and_binary_artifacts_are_inventory_not_auto_applied(self):
        snap = self.capture()
        target, directory = self.run / "scratch", self.run / "evidence"
        directory.mkdir(mode=0o700)
        workspace.copy_workspace(self.baseline, target, snap)
        (target / "subject.py").write_text("value = 2\n")
        (target / "result.bin").write_bytes(b"\x00\x01")
        result = evidence.collect_artifacts(self.baseline, target, snap, directory)
        self.assertEqual({row["path"] for row in result["files"]}, {"subject.py", "result.bin"})
        self.assertIn("+value = 2", (directory / "candidate.diff").read_text())
        self.assertEqual(self.file.read_text(), "value = 1\n")

    def test_candidate_diff_preserves_embedded_unicode_line_separators(self):
        old = 'value = "first\u2028second\x85third"\n'
        new = 'value = "first\u2028changed\x85third"\n'
        self.file.write_text(old, encoding="utf-8")
        snap = self.capture()
        target, directory = self.run / "scratch", self.run / "evidence"
        directory.mkdir(mode=0o700)
        workspace.copy_workspace(self.baseline, target, snap)
        (target / "subject.py").write_text(new, encoding="utf-8")
        evidence.collect_artifacts(self.baseline, target, snap, directory)
        apply_dir = self.run / "apply"
        apply_dir.mkdir(mode=0o700)
        (apply_dir / "subject.py").write_text(old, encoding="utf-8")
        subprocess.run(["git", "apply", str(directory / "candidate.diff")],
                       cwd=apply_dir, check=True, capture_output=True)
        self.assertEqual((apply_dir / "subject.py").read_text(encoding="utf-8"), new)
        self.assertEqual(self.file.read_text(encoding="utf-8"), old)

    def test_invalid_attempt_metadata_blocks_completeness_not_the_report(self):
        roles = [RoleFixture()]
        write_json(self.run / "request.json", {
            "schema": workflow.REQUEST_SCHEMA, "task_id": "metadata-test",
            "reason": "Check evidence integrity", "source_root": str(self.source),
            "include": ["."]})
        plan = workflow.prepare(str(self.run), roles, "Current evidence")
        (self.run / "context.md").write_text("Current evidence")
        directory = self.run / plan["roles"][roles[0].id]["evidence"]
        write_json(directory / "attempt-corrupt.json", [])
        result = evidence.write_result(self.run, plan, [])
        self.assertFalse(result["required_contributions_complete"])
        self.assertIn("experiment:attempt-metadata-unreadable", result["coverage_gaps"])
        self.assertTrue((self.run / "result.json").exists())

    def scratch(self):
        snap = self.capture()
        target, directory = self.run / "scratch", self.run / "evidence"
        directory.mkdir(mode=0o700)
        workspace.copy_workspace(self.baseline, target, snap)
        return snap, target, directory

    def test_artifact_symlink_cannot_read_host_file(self):
        """Listed by path and kind; the target is never opened or hashed."""
        host = self.base / "host-secret.txt"
        host.write_text("HOST-SECRET-SENTINEL\n")
        snap, target, directory = self.scratch()
        (target / "escape").symlink_to(host)
        result = evidence.collect_artifacts(self.baseline, target, snap, directory)
        self.assertEqual(result["files"], [{"path": "escape", "kind": "symlink",
                                            "before": None, "after": None,
                                            "text_patch": False}])
        manifest = json.dumps(result) + (directory / "candidate.diff").read_text()
        self.assertNotIn("HOST-SECRET-SENTINEL", manifest)
        self.assertNotIn(hashlib.sha256(host.read_bytes()).hexdigest(), manifest)

    def test_artifact_fifo_is_listed_as_a_special_file_without_blocking(self):
        snap, target, directory = self.scratch()
        os.mkfifo(target / "pipe")
        started = time.monotonic()
        result = evidence.collect_artifacts(self.baseline, target, snap, directory)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual([(row["path"], row["kind"]) for row in result["files"]],
                         [("pipe", "special-file")])

    def test_regular_edit_next_to_a_symlink_keeps_its_diff(self):
        (self.source / "other.txt").write_text("other\n")
        snap, target, directory = self.scratch()
        (target / "subject.py").write_text("value = 2\n")
        (target / "bin").mkdir()
        (target / "bin/git").symlink_to("/usr/bin/git")
        (target / "other.txt").unlink()
        (target / "other.txt").symlink_to(self.file)  # a captured file replaced
        result = evidence.collect_artifacts(self.baseline, target, snap, directory)
        rows = {row["path"]: row for row in result["files"]}
        self.assertEqual({name: row["kind"] for name, row in rows.items()},
                         {"bin/git": "symlink", "other.txt": "symlink",
                          "subject.py": "modified"})
        self.assertEqual(rows["other.txt"]["before"], snap["files"]["other.txt"])
        self.assertIsNone(rows["other.txt"]["after"])
        self.assertEqual(len(result["files"]), 3)
        diff = (directory / "candidate.diff").read_text()
        self.assertIn("+value = 2", diff)
        self.assertNotIn("other", diff)

    def test_case_colliding_scratch_files_do_not_break_inspection(self):
        """Scratch output is not a capture. A case-folding host can hold
        only one of the names, so there the checker reports the pair."""
        snap, target, directory = self.scratch()
        upper, lower = target / "Out.txt", target / "out.txt"
        upper.write_text("upper\n")
        lower.write_text("lower\n")
        distinct = upper.stat().st_ino != lower.stat().st_ino
        with contextlib.ExitStack() as stack:
            if not distinct:
                stack.enter_context(patch.object(
                    workspace, "portable_collision", return_value=("Out.txt", "out.txt")))
            result = evidence.collect_artifacts(self.baseline, target, snap, directory)
        self.assertEqual({row["path"] for row in result["files"]},
                         {"Out.txt", "out.txt"} if distinct else {"Out.txt"})
        self.assertIn("+lower", (directory / "candidate.diff").read_text())

    def prepared(self):
        roles = [RoleFixture()]
        write_json(self.run / "request.json", {
            "schema": workflow.REQUEST_SCHEMA, "task_id": "metadata-test",
            "reason": "Check evidence integrity", "source_root": str(self.source),
            "include": ["."]})
        plan = workflow.prepare(str(self.run), roles, "Current evidence")
        (self.run / "context.md").write_text("Current evidence")
        return plan

    def test_unreadable_attempt_metadata_is_also_a_role_coverage_gap(self):
        plan = self.prepared()
        directory = self.run / plan["roles"]["experiment"]["evidence"]
        recorder = evidence.AttemptEvidence(directory, ["codex"], "goal")
        recorder.feed("stdout", b'{"type":"turn.completed"}\n')
        recorder.finish(returncode=0, state="exited")
        write_json(directory / "attempt-corrupt.json", [])
        result = evidence.write_result(self.run, plan, [])
        role = result["roles"]["experiment"]
        self.assertEqual(len(role["attempts"]), 1)
        self.assertIn("attempt-metadata-unreadable", role["coverage_gaps"])
        self.assertIn("experiment:attempt-metadata-unreadable", result["coverage_gaps"])

    def test_context_change_keeps_input_detail_at_reconciliation(self):
        plan = self.prepared()
        self.file.write_text("value = 2\n")
        (self.run / "context.md").write_text("A different objective.")
        application = evidence.write_result(self.run, plan, [])["applicability"]
        self.assertEqual((application["state"], application["reason"],
                          application["changed_paths"]),
                         ("stale", "context changed since capture", ["subject.py"]))
        (self.run / "context.md").unlink()
        result = evidence.write_result(self.run, plan, [])
        self.assertEqual((result["applicability"]["state"], result["applicability"]["reason"],
                          result["applicability"]["changed_paths"]),
                         ("stale", "context unavailable", ["subject.py"]))
        self.assertIn("context-unavailable-at-reconciliation", result["coverage_gaps"])
        self.file.write_text("value = 1\n")
        application = evidence.write_result(self.run, plan, [])["applicability"]
        self.assertEqual((application["state"], application["changed_paths"]),
                         ("unknown", []))


if __name__ == "__main__":
    unittest.main()
