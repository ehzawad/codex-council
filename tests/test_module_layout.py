"""Module-layout tests for the codex-council runner.

codex_council.py is the only entry point; it imports the sibling
council_*.py modules from its own directory. These tests pin that the
siblings resolve even when Python leaves the script's directory off
sys.path (python3 -P, PYTHONSAFEPATH=1), that a run caches no bytecode in
the installed scripts directory, that the import graph is acyclic and
layered, and that module-level state has exactly one owner.

Run from repo root:
    python3 -m unittest discover -s tests -p 'test_*.py'
"""

import ast
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__),
    "..",
    "plugins", "codex-council", "skills", "codex-council", "scripts",
))
sys.path.insert(0, SCRIPTS_DIR)

SCRIPT = os.path.join(SCRIPTS_DIR, "codex_council.py")
RUNNER_MODULES = (
    "codex_council", "council_common", "council_discovery",
    "council_selection", "council_failures", "council_liveness",
)
# The siblings each module may import (dependencies point one way).
ALLOWED_IMPORTS = {
    "council_common": set(),
    "council_discovery": {"council_common"},
    "council_selection": {"council_common", "council_discovery"},
    "council_failures": {"council_common"},
    "council_liveness": {"council_common"},
    "codex_council": {
        "council_common", "council_discovery", "council_selection",
        "council_failures", "council_liveness",
    },
}


def _child_env(**extra):
    """The test environment without anything that would put the scripts
    directory on sys.path for the child."""
    env = {key: value for key, value in os.environ.items()
           if key not in ("PYTHONPATH", "PYTHONSAFEPATH")}
    env.update(extra)
    return env


class SafePathEntryTests(unittest.TestCase):
    """python3 -P and PYTHONSAFEPATH=1 omit the script's directory from
    sys.path; the entry script must still import its sibling modules. Each
    run reaches council_common code (the private-directory gate), not just
    argparse."""

    def _run(self, *flags, **env):
        with tempfile.TemporaryDirectory() as cwd:
            missing = os.path.join(cwd, "missing-run-dir")
            proc = subprocess.run(
                [sys.executable, *flags, SCRIPT, "--check-staging-dir",
                 missing],
                cwd=cwd, env=_child_env(**env), capture_output=True,
                text=True, timeout=60,
            )
        return proc, missing

    def _assert_siblings_resolved(self, proc, missing):
        self.assertNotIn("ModuleNotFoundError", proc.stderr)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn(
            f"--check-staging-dir: {missing!r} does not exist.", proc.stderr)

    def test_pythonsafepath_env_still_resolves_siblings(self):
        self._assert_siblings_resolved(*self._run(PYTHONSAFEPATH="1"))

    def test_dash_p_flag_still_resolves_siblings(self):
        self._assert_siblings_resolved(*self._run("-P"))


# Runs the entry script the way `python3 codex_council.py --help` does and
# records every module first imported while bytecode writes are off.
_RECORD_IMPORTS_WHILE_OFF = """
import json
import runpy
import sys

script, report = sys.argv[1:]
started_off = sys.dont_write_bytecode
imported_while_off = []


class _Recorder:
    @staticmethod
    def find_spec(name, path=None, target=None):
        if sys.dont_write_bytecode:
            imported_while_off.append(name)
        return None


sys.meta_path.insert(0, _Recorder)
sys.argv = [script, "--help"]
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit:
    pass
with open(report, "w", encoding="utf-8") as f:
    json.dump({"started_off": started_off,
               "ended_off": sys.dont_write_bytecode,
               "imported_while_off": imported_while_off}, f)
"""


class BytecodeCacheTests(unittest.TestCase):
    """A run writes nothing into the installed plugin's scripts directory:
    Python never caches bytecode for the script it runs, and the entry
    imports its siblings with bytecode writes off, restoring the
    interpreter's setting afterwards. Each run uses a private
    copy of the runner modules and Python's default caching."""

    def _copy_runner(self, root):
        scripts = os.path.join(root, "scripts")
        os.mkdir(scripts)
        for name in RUNNER_MODULES:
            shutil.copy(os.path.join(SCRIPTS_DIR, f"{name}.py"), scripts)
        return scripts

    def _env(self):
        env = _child_env()
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        env.pop("PYTHONPYCACHEPREFIX", None)
        return env

    def test_a_run_writes_nothing_into_the_scripts_directory(self):
        with tempfile.TemporaryDirectory() as root:
            scripts = self._copy_runner(root)
            missing = os.path.join(root, "missing-run-dir")
            proc = subprocess.run(
                [sys.executable, os.path.join(scripts, "codex_council.py"),
                 "--check-staging-dir", missing],
                cwd=root, env=self._env(), capture_output=True, text=True,
                timeout=60,
            )
            # The run reached council_common code, so every sibling loaded.
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn(
                f"--check-staging-dir: {missing!r} does not exist.",
                proc.stderr)
            self.assertEqual(sorted(os.listdir(scripts)),
                             sorted(f"{name}.py" for name in RUNNER_MODULES))

    def _record_imports(self, *flags, **env):
        """Run the entry under the recorder; return what it recorded."""
        with tempfile.TemporaryDirectory() as root:
            scripts = self._copy_runner(root)
            report = os.path.join(root, "report.json")
            proc = subprocess.run(
                [sys.executable, *flags, "-c", _RECORD_IMPORTS_WHILE_OFF,
                 os.path.join(scripts, "codex_council.py"), report],
                cwd=root, env={**self._env(), **env}, capture_output=True,
                text=True, timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(report, encoding="utf-8") as f:
                return json.load(f)

    def test_siblings_import_with_bytecode_writes_off_then_restored(self):
        recorded = self._record_imports()
        siblings = [name for name in RUNNER_MODULES if name != "codex_council"]
        self.assertFalse(recorded["started_off"])
        self.assertFalse(recorded["ended_off"])
        self.assertLessEqual(set(siblings), set(recorded["imported_while_off"]))

    def test_bytecode_writes_the_user_turned_off_stay_off(self):
        """The entry restores the interpreter's own setting rather than
        turning writes back on: `python3 -B` and PYTHONDONTWRITEBYTECODE=1
        still write no bytecode for the rest of the process."""
        for flags, env in ((("-B",), {}),
                           ((), {"PYTHONDONTWRITEBYTECODE": "1"})):
            with self.subTest(flags=flags, env=env):
                recorded = self._record_imports(*flags, **env)
                self.assertTrue(recorded["started_off"])
                self.assertTrue(recorded["ended_off"])


class ModuleImportGraphTests(unittest.TestCase):
    def _sibling_imports(self, name):
        """The runner modules `name` imports, from every import statement."""
        with open(os.path.join(SCRIPTS_DIR, f"{name}.py"),
                  encoding="utf-8") as f:
            tree = ast.parse(f.read())
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                found.add(node.module)
        return found & set(RUNNER_MODULES)

    def test_runner_modules_are_every_script(self):
        names = sorted(name[:-3] for name in os.listdir(SCRIPTS_DIR)
                       if name.endswith(".py"))
        self.assertEqual(names, sorted(RUNNER_MODULES))

    def test_import_graph_is_acyclic_and_layered(self):
        graph = {name: self._sibling_imports(name) for name in RUNNER_MODULES}
        for name, deps in graph.items():
            with self.subTest(module=name):
                self.assertLessEqual(deps, ALLOWED_IMPORTS[name])
        # Nothing imports the entry point: run as __main__, a second copy
        # would duplicate its module state.
        self.assertFalse(any("codex_council" in deps
                             for deps in graph.values()))
        remaining = {name: set(deps) for name, deps in graph.items()}
        while remaining:
            ready = [name for name, deps in remaining.items() if not deps]
            self.assertTrue(ready, f"import cycle among {sorted(remaining)}")
            for name in ready:
                del remaining[name]
            for deps in remaining.values():
                deps.difference_update(ready)

    def test_each_module_imports_first_in_a_fresh_interpreter(self):
        for name in RUNNER_MODULES:
            with self.subTest(module=name), \
                    tempfile.TemporaryDirectory() as cwd:
                # Imported directly, a module would cache its bytecode in
                # the scripts directory; keep that directory clean.
                proc = subprocess.run(
                    [sys.executable, "-c", f"import {name}"], cwd=cwd,
                    env=_child_env(PYTHONPATH=SCRIPTS_DIR,
                                   PYTHONDONTWRITEBYTECODE="1"),
                    capture_output=True, text=True, timeout=60,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)


class ModuleStateTests(unittest.TestCase):
    """Module-level state lives in exactly one module; siblings and tests
    reach it through that owner."""

    def test_mutable_state_has_one_owner(self):
        modules = {name: importlib.import_module(name)
                   for name in RUNNER_MODULES}
        for state, owner in (
            ("_diagnostics", "council_common"),
            ("_roles_recovery_text", "council_common"),
            ("_project_root_cache", "council_common"),
            ("_RUN", "codex_council"),
            ("STATE_DIR", "codex_council"),
        ):
            with self.subTest(state=state):
                holders = [name for name, module in modules.items()
                           if state in vars(module)]
                self.assertEqual(holders, [owner])

    def test_shared_helpers_are_the_owners_objects(self):
        import codex_council
        import council_common
        import council_discovery
        import council_selection
        # One cached project root (one git call) for state keys, workers,
        # and discovery; one diagnostics sink for every stderr write.
        self.assertIs(codex_council._project_root,
                      council_common._project_root)
        self.assertIs(council_discovery._project_root,
                      council_common._project_root)
        self.assertIs(codex_council._diag, council_common._diag)
        # Role lives beside the resolver that reads it; the entry, which
        # no sibling may import, builds roles from that one class.
        self.assertIs(codex_council.Role, council_selection.Role)
        # One UTC timestamp format for snapshots, selection, and state.
        for module in (codex_council, council_discovery, council_selection):
            self.assertIs(module._utc_iso, council_common._utc_iso)


if __name__ == "__main__":
    unittest.main()
