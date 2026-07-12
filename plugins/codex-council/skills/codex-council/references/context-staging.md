# Context staging recipes

Read this reference when `context.md` should be assembled from workspace files,
diffs, or diagnostic output. The actual council launch remains the background
flow in `SKILL.md`; these recipes only create `ABS_RUNDIR/context.md`.

Use the exact private `ABS_RUNDIR` printed by the single `mktemp -d` call in
`SKILL.md`. Start every pipeline in the same Bash invocation with
`set -euo pipefail`; shell options do not persist across Claude Code Bash calls.
Do not add `|| true`: a failed extractor must not leave stale or partial context.

`git diff HEAD` includes staged and unstaged tracked changes. Use `git diff
--cached` for staged-only work or `git diff` for unstaged-only work. None of
those commands include untracked files.

## Tracked changes

```bash
set -euo pipefail
git diff HEAD > 'ABS_RUNDIR/context.md'
```

Staged only:

```bash
set -euo pipefail
git diff --cached > 'ABS_RUNDIR/context.md'
```

## Changes plus relevant untracked files

Keep binary, symlink, and encoding guards while preserving filenames safely:

```bash
set -euo pipefail
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
} > 'ABS_RUNDIR/context.md'
```

## Artifact plus a question

Write the complete relevant artifact and the decision the council should make:

```bash
set -euo pipefail
{
  printf 'Question: %s\n\n' '<what should the council decide or produce?>'
  cat <"$file"
} > 'ABS_RUNDIR/context.md'
```

## Diagnostic transcript

Preserve the failing command, status, and complete output. Append guarded source
artifacts when diagnosis depends on them.

```bash
set -euo pipefail
{
  printf 'Question: %s\n\n' '<what should the council diagnose?>'
  printf 'Command: %s\n' '<the failing command>'
  printf 'Exit status: %s\n\n' "$exit_status"
  echo 'Output:'
  cat <"$log_file"
} > 'ABS_RUNDIR/context.md'
```

`context.md` is UTF-8 text because it is sent to `codex exec` on stdin. For a
material binary, image, archive, or non-UTF-8 artifact, include its project
path, type, relevance, and any available textual diagnostics. Do not silently
exclude or blindly transcode it; roles can inspect the original from disk with
the tools supported by the active Codex installation.
