"""Content-addressed, copy-backed experiment inputs; no agent or Git writes.

A capture is observed-stable, not an atomic snapshot of arbitrary external
writers. Pause overlapping writers during capture. Symlinks, special files, and
case/Unicode collisions among selected files, empty directories, and directory
components are refused. Every directory a capture or copy creates is 0700
whatever the process umask. Copies never share writable inodes with the
source, and Git metadata/configuration is never borrowed by an experiment.
"""

from __future__ import annotations

import contextlib
import errno
import fnmatch
import glob
import hashlib
import json
import math
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
# Agent instruction files Codex or Claude Code read from a working tree, at any
# depth; compared case-insensitively because a case-insensitive filesystem
# resolves agents.md as AGENTS.md.
OMIT_INSTRUCTION_FILES = frozenset({"agents.md", "agents.override.md", "claude.md",
                                    "claude.local.md"})
CHUNK = 1024 * 1024
# What O_NOFOLLOW reports for a symlink final component (ELOOP; EMLINK on
# FreeBSD; EFTYPE on NetBSD) or O_DIRECTORY for a link or non-directory
# component (ENOTDIR).
LINK_ERRNOS = frozenset(code for code in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR,
                                          getattr(errno, "EFTYPE", None))
                        if code is not None)


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


def private_mkdirs(path: Path) -> None:
    """Create `path` and any missing ancestors as 0700 directories whatever
    the process umask. Only directories created here are chmodded; an
    existing component is left as it is and must be a real directory."""
    missing: list[Path] = []
    current = Path(os.path.abspath(path))
    while not os.path.lexists(current):
        missing.append(current)
        current = current.parent
    if not stat.S_ISDIR(current.lstat().st_mode):
        raise WorkspaceError(f"not a directory: {current}")
    for directory in reversed(missing):
        os.mkdir(directory, 0o700)
        os.chmod(directory, 0o700)


def portable_collision(names: Any, dirs: Any = ()) -> tuple[str, str] | None:
    """The first pair of distinct selected paths that are equal under
    Unicode canonical caseless matching (NFD, case folding, NFD again), or
    None. Compared over complete file paths, empty directories, and every
    directory component of both, so Foo/ and foo/, or Foo/a.txt and
    foo/b.txt, collide; a file and a directory with folded-equal names
    collide too."""
    seen: dict[str, str] = {}
    for name in sorted({*names, *dirs}):
        parts = PurePosixPath(name).parts
        for depth in range(1, len(parts) + 1):
            path = "/".join(parts[:depth])
            folded = unicodedata.normalize(
                "NFD", unicodedata.normalize("NFD", path).casefold())
            other = seen.setdefault(folded, path)
            if other != path:
                return other, path
    return None


def atomic_json(path: Path, value: Any) -> None:
    """Replace one private JSON file; never follow the destination entry."""
    private_dir(path.parent)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            # A umask that clears owner bits must not leave it unreadable.
            os.fchmod(stream.fileno(), 0o600)
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
    """Strict JSON (no duplicate keys or non-finite numbers) from an owned,
    regular, non-symlink file; group/other permission bits are refused only
    when `private` is true."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    # Before fdopen, which raises on a directory and leaves the descriptor open.
    st = os.fstat(fd)
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid()
            or (private and st.st_mode & 0o077)):
        os.close(fd)
        raise WorkspaceError(f"not an owned private regular file: {path}")
    with os.fdopen(fd, "rb") as stream:
        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in items:
                if key in result:
                    raise WorkspaceError(f"duplicate JSON key: {key}")
                result[key] = value
            return result

        def constant(value: str) -> Any:
            raise WorkspaceError(f"non-finite JSON number: {value}")

        def finite(literal: str) -> float:
            value = float(literal)
            if not math.isfinite(value):
                raise WorkspaceError(f"non-finite JSON number: {literal}")
            return value

        try:
            return json.loads(stream.read().decode("utf-8"), object_pairs_hook=pairs,
                              parse_constant=constant, parse_float=finite)
        except RecursionError:
            raise WorkspaceError(f"JSON nesting too deep: {path}") from None


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
    if leaf.casefold() in OMIT_INSTRUCTION_FILES:
        return "agent instruction file"
    if (leaf in OMIT_FILES or (leaf.startswith(".env.")
                              and leaf not in (".env.example", ".env.sample"))
            or leaf.endswith((".pem", ".key", ".p12", ".pfx"))):
        return "potential credential file"
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in exclude):
        return "requested exclusion"
    return None


def _exclude_fix(name: str) -> str:
    """The request.json fix for refused `name`. Exclude patterns are
    shell-style, so a name with glob metacharacters ([id].js) is given as
    the escaped pattern that matches it."""
    pattern = glob.escape(name)
    if pattern == name:
        return "; add it to exclude in request.json"
    return f"; add it to exclude in request.json as {json.dumps(pattern)}"


def _within(name: str, omitted: dict[str, str]) -> bool:
    """Whether `name` or a directory above it was omitted."""
    parts = PurePosixPath(name).parts
    return any("/".join(parts[:depth]) in omitted for depth in range(1, len(parts) + 1))


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
    except OSError as exc:
        if exc.errno in LINK_ERRNOS:
            raise WorkspaceError("selected input or a directory in its path is a "
                                 f"symlink or not a directory: {name}") from None
        raise
    finally:
        os.close(directory)
    # Before fdopen: fdopen of a directory raises an error naming the fd.
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise WorkspaceError(f"selected input is not a regular file: {name}")
    with os.fdopen(fd, "rb") as stream:
        yield stream


def _hash_file(root: Path, name: str, destination: Path | None = None
               ) -> tuple[dict[str, Any], tuple[int, ...]]:
    with regular(root, name) as stream:
        before = os.fstat(stream.fileno())
        hasher = hashlib.sha256()
        output = None
        try:
            if destination is not None:
                private_mkdirs(destination.parent)
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


def git_state(root: Path, include: list[str] | None, exclude: list[str],
              omitted: dict[str, str] | None = None) -> dict[str, Any] | None:
    """HEAD plus the index entries of selected paths only, never of a path
    inventory `omitted` or one below an omitted entry. Staging an
    unrelated file does not make a capture stale. Captured bytes are the
    working files; a partially staged file is captured as its working version."""
    top = _git(root, "rev-parse", "--show-toplevel")
    if top is None:
        return None
    index = _git(root, "ls-files", "--stage", "-z")
    if index is None:
        raise WorkspaceError("Git index could not be read")
    omitted = {} if omitted is None else omitted
    selected = b"\0".join(
        entry for entry in index.split(b"\0")
        if entry and _in_scope(name := os.fsdecode(entry.split(b"\t", 1)[-1]),
                               include, exclude) and not _within(name, omitted))
    head = _git(root, "rev-parse", "--verify", "HEAD")
    return {"root": os.fsdecode(top.rstrip(b"\n")),
            "head": head.decode("ascii").strip() if head is not None else None,
            "selected_index_sha256": hashlib.sha256(selected).hexdigest()}


def _walk(root: Path, start: str, exclude: list[str],
          omitted: dict[str, str], empty: set[str] | None = None,
          special: dict[str, str] | None = None, *, hints: bool = False) -> set[str]:
    """Enumerate through pinned directory descriptors, never link ancestors.
    Directories holding no selected entry are added to `empty`. A symlink or
    special file is refused (naming the exclude fix when `hints`) unless `special`
    is a dict; then it is recorded there by kind and never opened, followed
    or descended."""
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
        if special is not None:
            special[name] = "symlink" if stat.S_ISLNK(st.st_mode) else "special-file"
            return True
        raise WorkspaceError("selected symlink or special file is not copied: "
                             f"{name}{_exclude_fix(name) if hints else ''}")

    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if start == ".":
            for entry in os.listdir(directory):
                visit(directory, entry, entry)
        else:
            parts = PurePosixPath(relative_path(start)).parts
            try:
                for part in parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=directory)
                    os.close(directory)
                    directory = child
            except OSError as exc:
                if exc.errno not in LINK_ERRNOS:
                    raise
                # Exclusions are matched below this descent, so the fix is
                # the include path itself.
                fix = "; name it in include by a path through real directories"
                raise WorkspaceError("selected input or a directory in its path is a "
                                     f"symlink or not a directory: {start}"
                                     f"{fix if hints else ''}") from None
            visit(directory, parts[-1], start)
    finally:
        os.close(directory)
    return selected


def inventory(root: Path, include: list[str] | None, exclude: list[str], *,
              special: dict[str, str] | None = None, portable: bool = True,
              hints: bool = True
              ) -> tuple[dict[str, Any], dict[str, tuple[int, ...]], dict[str, str], list[str]]:
    """Select current bytes, not HEAD/index bytes. Explicit includes may include
    ignored fixtures and record empty directories; implicit Git scope honors
    Git's ignore rules and, like Git, lists files only. It refuses, by name,
    a submodule (populated, empty or missing), an untracked nested
    repository, and a symlink or special file it is not told to exclude.
    `special` is passed to _walk; `portable=False` skips the collision check
    for output that is not a capture; `hints=False` leaves the request.json
    fix out of refusals, for a run already prepared from that request."""
    omitted: dict[str, str] = {}
    empty: set[str] = set()

    def fix(name: str) -> str:
        return _exclude_fix(name) if hints else ""

    def select(name: str) -> str:
        return fix(name) and f"{fix(name)}, or select its files with include"

    if include is None:
        raw = _git(root, "ls-files", "--cached", "--others", "--exclude-standard",
                   "-z")
        if raw is None:
            raise WorkspaceError("non-Git inputs require explicit include paths")
        index = _git(root, "ls-files", "--stage", "-z")
        if index is None:
            raise WorkspaceError("Git index could not be read")
        # Each entry is "<mode> <object> <stage>\t<path>"; 160000 is a gitlink.
        gitlinks = {os.fsdecode(path) for fields, _, path in
                    (entry.partition(b"\t") for entry in index.split(b"\0"))
                    if fields.startswith(b"160000 ")}
        names = set()
        real_dirs: set[str] = set()
        # Sorted, so a listed link or nested repository is met before any
        # tracked path below it (a tracked directory replaced by a link):
        # excluding it omits those paths too, never read through it.
        for value in sorted(raw.split(b"\0")):
            if not value:
                continue
            name = os.fsdecode(value)
            # Git lists an untracked nested repository as one "name/" entry;
            # its exclusion may be spelled with the slash or without.
            nested = name.endswith("/")
            name = relative_path(name[:-1] if nested else name)
            parts = PurePosixPath(name).parts
            reason = _omission(name, exclude) or (
                _omission(f"{name}/", exclude) if nested else None)
            for depth in range(1, len(parts)):
                if reason:
                    break
                parent = "/".join(parts[:depth])
                reason = omitted.get(parent)
                if reason or parent in real_dirs:
                    continue
                if not os.path.islink(root / parent):
                    real_dirs.add(parent)
                    continue
                # A link Git does not list (an ignored one) above a tracked
                # path: refused and excluded by its own name, like a listed one.
                reason = _omission(parent, exclude)
                if not reason:
                    raise WorkspaceError(f"selected symlink is not copied: {parent}"
                                         f"{fix(parent)}")
                omitted[parent] = reason
            if reason:
                omitted[name] = reason
                continue
            if nested:
                raise WorkspaceError(f"nested Git repository is not copied: {name}/"
                                     f"{select(name)}")
            if name in gitlinks:  # populated, empty or missing
                raise WorkspaceError(f"Git submodule is not copied: {name}{select(name)}")
            try:
                st = os.lstat(root / name)
            except (FileNotFoundError, NotADirectoryError):
                continue  # tracked deletion: absence is part of this capture
            if stat.S_ISLNK(st.st_mode):
                raise WorkspaceError(f"selected symlink is not copied: {name}{fix(name)}")
            if not stat.S_ISREG(st.st_mode):
                raise WorkspaceError("selected special file or directory is not copied: "
                                     f"{name}{fix(name)}")
            names.add(name)
    else:
        names = set()
        for value in include:
            names.update(_walk(root, relative_path(value, allow_dot=True),
                               exclude, omitted, empty, special, hints=hints))
    collision = portable_collision(names, empty) if portable else None
    if collision is not None:
        raise WorkspaceError("non-portable case/Unicode collision: "
                             f"{collision[0]} and {collision[1]}")
    files, stamps = {}, {}
    for name in sorted(names):
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
    before, stamps, omitted, dirs = inventory(source, include, exclude)
    before_git = git_state(source, include, exclude, omitted)
    baseline.mkdir(mode=0o700)
    os.chmod(baseline, 0o700)
    for name in dirs:
        private_mkdirs(baseline / name)
    copied = {}
    for name, entry in before.items():
        copy, stamp = _hash_file(source, name, baseline / name)
        if copy != entry or stamp != stamps[name]:
            raise WorkspaceError(f"input changed during capture: {name}")
        copied[name] = copy
    after, after_stamps, after_omitted, after_dirs = inventory(source, include, exclude)
    after_git = git_state(source, include, exclude, after_omitted)
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
    os.chmod(target, 0o700)
    for name in snapshot["dirs"]:
        private_mkdirs(target / name)
    for name, value in snapshot["files"].items():
        if _hash_file(baseline, name, target / name)[0] != value:
            raise WorkspaceError(f"baseline changed while copying: {name}")
    verify_tree(target, snapshot["files"], snapshot["dirs"])


def freshness(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Compare selected file bytes and executable flags, empty directories,
    Git HEAD and selected index entries, and the recorded omission
    inventory with the capture. Omitted file contents and remote
    dependencies are not checked."""
    try:
        root = Path(snapshot["source_root"])
        if not root.is_dir():
            # Said plainly: a Git scope would otherwise report "non-Git
            # inputs require explicit include paths".
            raise WorkspaceError(f"source_root is missing or not a directory: {root}")
        files, _, omitted, dirs = inventory(root, snapshot["include"], snapshot["exclude"],
                                            hints=False)
        changed = sorted(name for name in set(files) | set(snapshot["files"])
                         if files.get(name) != snapshot["files"].get(name))
        changed += sorted(f"{name}/" for name in set(dirs) ^ set(snapshot["dirs"]))
        metadata_changed = git_state(root, snapshot["include"], snapshot["exclude"],
                                     omitted) != snapshot["git"]
        omissions_changed = omitted != snapshot["omitted"]
        return {"state": "stale" if changed or metadata_changed or omissions_changed
                else "unchanged", "changed_paths": changed,
                "git_metadata_changed": metadata_changed,
                "omissions_changed": omissions_changed,
                "scope": "captured inputs only; external dependencies unverified"}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"state": "unknown", "error": f"{type(exc).__name__}: {exc}"}
