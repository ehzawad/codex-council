"""Content-addressed, copy-backed experiment inputs; no agent or Git writes.

A capture is observed-stable, not an atomic snapshot of arbitrary external
writers. Pause overlapping writers during capture. Symlinks and special files
in the selected inputs are refused. Copies never share writable inodes with the
source, and Git metadata/configuration is never borrowed by an experiment.
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

SCHEMA = "codex-council/workspace@1"
# These are omitted even when a broad include contains them. Explicitly staging
# a reviewed, sanitized fixture is preferable to copying credentials or caches.
OMIT_DIRS = frozenset({
    ".git", ".hg", ".svn", ".codex", ".claude", ".agents", ".ssh", ".aws",
    ".azure", ".gcloud", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
})
OMIT_FILES = frozenset({".env", ".netrc", ".npmrc", "id_rsa", "id_ed25519"})
CHUNK = 1024 * 1024


class WorkspaceError(ValueError):
    """Input, capture, or artifact integrity was not established."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("ascii")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def private_dir(path: str | Path) -> Path:
    path = Path(os.path.abspath(path))
    st = path.lstat()
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid()
            or st.st_mode & 0o077):
        raise WorkspaceError(f"not an owned private directory: {path}")
    return path.resolve(strict=True)


def atomic_json(path: Path, value: Any) -> None:
    """Replace one private JSON file; never follow the destination entry."""
    private_dir(path.parent)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)


def read_json(path: Path, *, private: bool = True) -> Any:
    """Strict JSON from an owned, private, regular, non-symlink file."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        st = os.fstat(stream.fileno())
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid()
                or (private and st.st_mode & 0o077)):
            raise WorkspaceError(f"not an owned private regular file: {path}")

        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in items:
                if key in result:
                    raise WorkspaceError(f"duplicate JSON key: {key}")
                result[key] = value
            return result

        def constant(value: str) -> Any:
            raise WorkspaceError(f"non-finite JSON number: {value}")

        return json.loads(stream.read().decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=constant)


def relative_path(value: str, *, allow_dot: bool = False) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise WorkspaceError("input paths must be nonempty relative strings")
    path = PurePosixPath(value)
    if value == "." and allow_dot:
        return value
    if (path.is_absolute() or ".." in path.parts or not path.parts
            or value != str(path) or "\\" in value):
        raise WorkspaceError(f"unsafe relative path: {value!r}")
    return value


def _omission(name: str, exclude: list[str]) -> str | None:
    parts = PurePosixPath(name).parts
    if any(part in OMIT_DIRS for part in parts):
        return "agent configuration, credentials, VCS metadata, or cache"
    leaf = parts[-1] if parts else ""
    if (leaf in OMIT_FILES or (leaf.startswith(".env.")
                              and leaf not in (".env.example", ".env.sample"))
            or leaf.endswith((".pem", ".key", ".p12", ".pfx"))):
        return "potential credential file"
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in exclude):
        return "requested exclusion"
    return None


def _stamp(st: os.stat_result) -> tuple[int, ...]:
    return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns,
            st.st_ctime_ns)


@contextlib.contextmanager
def regular(root: Path, name: str) -> Iterator[Any]:
    """Pin each directory component. Never traverse a substituted symlink."""
    parts = PurePosixPath(relative_path(name)).parts
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise WorkspaceError(f"selected input is not a regular file: {name}")
        yield stream


def _hash_file(root: Path, name: str, destination: Path | None = None
               ) -> tuple[dict[str, Any], tuple[int, ...]]:
    with regular(root, name) as stream:
        before = os.fstat(stream.fileno())
        hasher = hashlib.sha256()
        output = None
        try:
            if destination is not None:
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW, 0o600)
                output = os.fdopen(fd, "wb")
            size = 0
            while block := stream.read(CHUNK):
                hasher.update(block)
                size += len(block)
                if output is not None:
                    output.write(block)
            after = os.fstat(stream.fileno())
            if _stamp(before) != _stamp(after) or size != before.st_size:
                raise WorkspaceError(f"input changed while reading: {name}")
            if output is not None:
                output.flush()
                os.fsync(output.fileno())
                os.fchmod(output.fileno(), 0o700 if before.st_mode & 0o111
                          else 0o600)
        finally:
            if output is not None:
                output.close()
    return ({"sha256": hasher.hexdigest(), "bytes": size,
             "executable": bool(before.st_mode & 0o111)}, _stamp(before))


def _git(root: Path, *args: str) -> bytes | None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    try:
        proc = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
             "-c", f"core.hooksPath={os.devnull}", "-C", str(root), *args],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _in_scope(name: str, include: list[str] | None, exclude: list[str]) -> bool:
    if _omission(name, exclude):
        return False
    return include is None or any(
        item == "." or name == item or name.startswith(item + "/") for item in include)


def git_state(root: Path, include: list[str] | None, exclude: list[str]
              ) -> dict[str, Any] | None:
    """HEAD plus the index entries of selected paths only. Staging an
    unrelated file does not make a capture stale. Captured bytes are the
    working files; a partially staged file is captured as its working version."""
    top = _git(root, "rev-parse", "--show-toplevel")
    if top is None:
        return None
    index = _git(root, "ls-files", "--stage", "-z")
    if index is None:
        raise WorkspaceError("Git index could not be read")
    selected = b"\0".join(
        entry for entry in index.split(b"\0")
        if entry and _in_scope(os.fsdecode(entry.split(b"\t", 1)[-1]), include, exclude))
    head = _git(root, "rev-parse", "--verify", "HEAD")
    return {"root": os.fsdecode(top.rstrip(b"\n")),
            "head": head.decode("ascii").strip() if head is not None else None,
            "selected_index_sha256": hashlib.sha256(selected).hexdigest()}


def _walk(root: Path, start: str, exclude: list[str],
          omitted: dict[str, str], empty: set[str] | None = None) -> set[str]:
    """Enumerate through pinned directory descriptors, never link ancestors.
    Directories holding no selected entry are added to `empty`."""
    selected: set[str] = set()
    empty = set() if empty is None else empty

    def visit(parent: int, leaf: str, name: str) -> bool:
        reason = _omission(name, exclude)
        if reason:
            omitted[name] = reason
            return False
        st = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISREG(st.st_mode):
            selected.add(name)
            return True
        if stat.S_ISDIR(st.st_mode):
            child = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=parent)
            try:
                found = False
                for entry in os.listdir(child):
                    found |= visit(child, entry, f"{name}/{entry}" if name else entry)
            finally:
                os.close(child)
            if not found:
                empty.add(name)
            return True
        raise WorkspaceError(f"selected symlink or special file is not copied: {name}")

    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if start == ".":
            for entry in os.listdir(directory):
                visit(directory, entry, entry)
        else:
            parts = PurePosixPath(relative_path(start)).parts
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory)
                os.close(directory)
                directory = child
            visit(directory, parts[-1], start)
    finally:
        os.close(directory)
    return selected


def inventory(root: Path, include: list[str] | None, exclude: list[str]
              ) -> tuple[dict[str, Any], dict[str, tuple[int, ...]], dict[str, str], list[str]]:
    """Select current bytes, not HEAD/index bytes. Explicit includes may include
    ignored fixtures and record empty directories; implicit Git scope honors
    Git's ignore rules and, like Git, lists files only."""
    omitted: dict[str, str] = {}
    empty: set[str] = set()
    if include is None:
        raw = _git(root, "ls-files", "--cached", "--others", "--exclude-standard",
                   "-z")
        if raw is None:
            raise WorkspaceError("non-Git inputs require explicit include paths")
        names = set()
        for value in raw.split(b"\0"):
            if not value:
                continue
            name = relative_path(os.fsdecode(value))
            reason = _omission(name, exclude)
            if reason:
                omitted[name] = reason
                continue
            if not os.path.lexists(root / name):
                continue  # tracked deletion: absence is part of this capture
            names.add(name)
    else:
        names = set()
        for value in include:
            names.update(_walk(root, relative_path(value, allow_dot=True),
                               exclude, omitted, empty))
    portable: dict[str, str] = {}
    files, stamps = {}, {}
    for name in sorted(names):
        folded = unicodedata.normalize("NFC", name).casefold()
        if folded in portable and portable[folded] != name:
            raise WorkspaceError(f"non-portable case/Unicode collision: {name}")
        portable[folded] = name
        files[name], stamps[name] = _hash_file(root, name)
    return files, stamps, omitted, sorted(empty)


def capture(source: Path, baseline: Path, *, include: list[str] | None = None,
            exclude: list[str] | None = None) -> dict[str, Any]:
    """Create a new baseline from an observed-stable read. Never repair or
    reuse a failed capture directory; caller starts over elsewhere."""
    source = source.resolve(strict=True)
    if not source.is_dir():
        raise WorkspaceError("source_root must be a directory")
    baseline = baseline.parent.resolve(strict=False) / baseline.name
    if baseline.is_relative_to(source):
        raise WorkspaceError("run directory must be outside the source tree")
    private_dir(baseline.parent)
    exclude = [] if exclude is None else exclude
    before_git = git_state(source, include, exclude)
    before, stamps, omitted, dirs = inventory(source, include, exclude)
    baseline.mkdir(mode=0o700)
    for name in dirs:
        (baseline / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    copied = {}
    for name, entry in before.items():
        copy, stamp = _hash_file(source, name, baseline / name)
        if copy != entry or stamp != stamps[name]:
            raise WorkspaceError(f"input changed during capture: {name}")
        copied[name] = copy
    after, after_stamps, after_omitted, after_dirs = inventory(source, include, exclude)
    after_git = git_state(source, include, exclude)
    if (after != before or stamps != after_stamps or before_git != after_git
            or omitted != after_omitted or dirs != after_dirs):
        raise WorkspaceError("source changed during capture; pause writers and retry")
    snapshot = {"schema": SCHEMA, "source_root": str(source), "files": copied,
                "dirs": dirs, "include": include, "exclude": exclude, "git": before_git,
                "omitted": omitted, "consistency": "observed-stable-not-atomic"}
    snapshot["snapshot_id"] = snapshot_digest(snapshot)
    freeze(baseline)
    return snapshot


def snapshot_digest(snapshot: dict[str, Any]) -> str:
    return digest({"files": snapshot.get("files"), "dirs": snapshot.get("dirs"),
                   "git": snapshot.get("git")})


def freeze(root: Path) -> None:
    """Owner-read-only baseline is a guard against mistakes, not a security
    boundary against the same uid. Integrity is checked again before use."""
    for directory, dirs, files in os.walk(root, topdown=False, followlinks=False):
        for name in files:
            path = Path(directory) / name
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode):
                raise WorkspaceError(f"unexpected entry in baseline: {path}")
            path.chmod(0o500 if st.st_mode & 0o111 else 0o400)
        for name in dirs:
            path = Path(directory) / name
            if path.is_symlink():
                raise WorkspaceError(f"unexpected symlink in baseline: {path}")
            path.chmod(0o500)
    root.chmod(0o500)


def verify_tree(root: Path, expected: dict[str, Any], dirs: list[str]) -> None:
    """Compare all entries, including unexpected additions and empty
    directories; never follow links."""
    empty: set[str] = set()
    names = _walk(root, ".", [], {}, empty)
    if empty != set(dirs):
        raise WorkspaceError("captured empty directories changed")
    # _walk omits control paths; an experiment may have created one. For a
    # baseline verify them too rather than letting an omitted .codex appear.
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in (*dirs, *files):
            path = Path(directory) / name
            if path.is_symlink():
                raise WorkspaceError(f"unexpected symlink: {path}")
            if name in OMIT_DIRS:
                raise WorkspaceError(f"unexpected control/cache directory: {path}")
        for name in files:
            names.add((Path(directory) / name).relative_to(root).as_posix())
    if names != set(expected):
        raise WorkspaceError("captured input inventory changed")
    for name, value in expected.items():
        if _hash_file(root, name)[0] != value:
            raise WorkspaceError(f"captured input changed: {name}")


def copy_workspace(baseline: Path, target: Path, snapshot: dict[str, Any]) -> None:
    verify_tree(baseline, snapshot["files"], snapshot["dirs"])
    target.mkdir(mode=0o700)
    for name in snapshot["dirs"]:
        (target / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, value in snapshot["files"].items():
        if _hash_file(baseline, name, target / name)[0] != value:
            raise WorkspaceError(f"baseline changed while copying: {name}")
    verify_tree(target, snapshot["files"], snapshot["dirs"])


def freshness(snapshot: dict[str, Any]) -> dict[str, Any]:
    """No assertions about excluded files or remote dependencies."""
    try:
        root = Path(snapshot["source_root"])
        files, _, omitted, dirs = inventory(root, snapshot["include"], snapshot["exclude"])
        changed = sorted(name for name in set(files) | set(snapshot["files"])
                         if files.get(name) != snapshot["files"].get(name))
        changed += sorted(f"{name}/" for name in set(dirs) ^ set(snapshot["dirs"]))
        metadata_changed = git_state(root, snapshot["include"], snapshot["exclude"]) != snapshot["git"]
        omissions_changed = omitted != snapshot["omitted"]
        return {"state": "stale" if changed or metadata_changed or omissions_changed
                else "unchanged", "changed_paths": changed,
                "git_metadata_changed": metadata_changed,
                "omissions_changed": omissions_changed,
                "scope": "captured inputs only; external dependencies unverified"}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"state": "unknown", "error": f"{type(exc).__name__}: {exc}"}
