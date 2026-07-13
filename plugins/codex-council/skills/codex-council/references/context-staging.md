# Context staging recipes

Read this reference when `context.md` should be assembled from workspace files,
diffs, or diagnostic output. The actual council launch remains the background
flow in `SKILL.md`; these recipes only create `ABS_RUNDIR/context.md`.

Use the exact private `ABS_RUNDIR` printed by the single `mktemp -d` call in
`SKILL.md`. Each recipe must run inside ONE Bash invocation: shell options and
variables do **not** persist across Claude Code Bash calls, so every recipe
re-assigns its own paths. Placeholder discipline: paste concrete values for
every `<angle-bracket>` placeholder and for the literal `ABS_RUNDIR` prefix
before running — never leave an undefined `$file`-style variable from an
earlier tool call in the command.

## The fail-closed skeleton

Every extraction uses this exact shape. It pre-cleans BOTH the final file and
the temp file, extracts into the temp file, refuses to publish empty output,
publishes atomically with `mv`, and removes both files if anything fails — so
a failed, partial, or empty extraction can never leave stale or previously
accepted context behind for the launch to pick up:

```bash
set -euo pipefail
out='ABS_RUNDIR/context.md'
tmp='ABS_RUNDIR/context.md.tmp'
rm -f "$out" "$tmp"
trap 'rc=$?; if [ "$rc" -ne 0 ]; then rm -f "$out" "$tmp"; fi; exit "$rc"' EXIT
{
  git diff HEAD
} >"$tmp"
[ -s "$tmp" ]
mv -f "$tmp" "$out"
trap - EXIT
```

Do not add `|| true` anywhere: a failed extractor must fail the recipe so the
trap removes both files. The `[ -s "$tmp" ]` guard fails the recipe when the
extractor produced nothing, so an empty success publishes nothing — write a
self-contained question to `context.md` instead (see `SKILL.md`). A bare
`tmp`-then-`mv` without the leading `rm -f` would leave an older accepted
`context.md` behind when extraction fails; the pre-clean plus the trap make
failure leave no file at all.

`git diff HEAD` includes staged and unstaged tracked changes. Use `git diff
--cached` for staged-only work or `git diff` for unstaged-only work. None of
those commands include untracked files.

## Tracked changes

The skeleton above already extracts `git diff HEAD`. Staged only — same
skeleton, different extractor:

```bash
set -euo pipefail
out='ABS_RUNDIR/context.md'
tmp='ABS_RUNDIR/context.md.tmp'
rm -f "$out" "$tmp"
trap 'rc=$?; if [ "$rc" -ne 0 ]; then rm -f "$out" "$tmp"; fi; exit "$rc"' EXIT
{
  git diff --cached
} >"$tmp"
[ -s "$tmp" ]
mv -f "$tmp" "$out"
trap - EXIT
```

## Changes plus relevant untracked files

Keep binary, symlink, and encoding guards while preserving filenames safely:

```bash
set -euo pipefail
out='ABS_RUNDIR/context.md'
tmp='ABS_RUNDIR/context.md.tmp'
rm -f "$out" "$tmp"
trap 'rc=$?; if [ "$rc" -ne 0 ]; then rm -f "$out" "$tmp"; fi; exit "$rc"' EXIT
{
  git diff HEAD
  git ls-files -z --others --exclude-standard -- |
    while IFS= read -r -d '' f; do
      [ -f "$f" ] && [ ! -L "$f" ] || continue
      mime=$(file --brief --mime -- "$f")
      case "$mime" in
        *charset=binary*)
          printf '\n=== non-text untracked artifact: %q (%s); inspect from disk if relevant ===\n' "$f" "$mime"
          continue
          ;;
        *charset=utf-8*|*charset=us-ascii*) ;;
        *)
          printf '\n=== non-UTF-8 untracked artifact: %q (%s); inspect from disk if relevant ===\n' "$f" "$mime"
          continue
          ;;
      esac
      printf '\n=== untracked file: %q ===\n' "$f"
      cat <"$f"
    done
} >"$tmp"
[ -s "$tmp" ]
mv -f "$tmp" "$out"
trap - EXIT
```

## Artifact plus a question

Write the complete relevant artifact and the decision the council should make.
Paste the artifact's literal absolute path (for example
`/Users/you/project/src/parser.py`) where the recipe shows one — never a
`$file` variable from an earlier Bash call:

```bash
set -euo pipefail
out='ABS_RUNDIR/context.md'
tmp='ABS_RUNDIR/context.md.tmp'
rm -f "$out" "$tmp"
trap 'rc=$?; if [ "$rc" -ne 0 ]; then rm -f "$out" "$tmp"; fi; exit "$rc"' EXIT
{
  printf 'Question: %s\n\n' '<what should the council decide or produce?>'
  cat <'/abs/path/to/artifact'
} >"$tmp"
[ -s "$tmp" ]
mv -f "$tmp" "$out"
trap - EXIT
```

## Diagnostic transcript

Preserve the failing command, status, and complete output. Paste the literal
exit status (for example `1`) and the literal absolute log path (for example
`ABS_RUNDIR/build.log`) — never `$exit_status` or `$log_file` variables from
earlier tool calls:

```bash
set -euo pipefail
out='ABS_RUNDIR/context.md'
tmp='ABS_RUNDIR/context.md.tmp'
rm -f "$out" "$tmp"
trap 'rc=$?; if [ "$rc" -ne 0 ]; then rm -f "$out" "$tmp"; fi; exit "$rc"' EXIT
{
  printf 'Question: %s\n\n' '<what should the council diagnose?>'
  printf 'Command: %s\n' '<the failing command>'
  printf 'Exit status: %s\n\n' '<pasted exit status, e.g. 1>'
  echo 'Output:'
  cat <'/abs/path/to/command.log'
} >"$tmp"
[ -s "$tmp" ]
mv -f "$tmp" "$out"
trap - EXIT
```

`context.md` is UTF-8 text because it is sent to `codex exec` on stdin. For a
material binary, image, archive, or non-UTF-8 artifact, include its project
path, type, relevance, and any available textual diagnostics. Do not silently
exclude or blindly transcode it; roles can inspect the original from disk with
the tools supported by the active Codex installation.
