# Context staging

Read this reference when assembling `context.md`: what belongs in it, how to
order it for a long session, and fail-closed recipes for extracting files,
diffs, or diagnostic output from disk. The launch itself stays in `SKILL.md`;
these recipes only create `ABS_RUNDIR/context.md`.

## Contents

- What goes into context.md
- Extraction rules
- The fail-closed skeleton
- Tracked changes
- Changes plus relevant untracked files
- Artifact plus a question
- Diagnostic transcript

## What goes into context.md

Context comes from one or both sources:

- **Claude-composed context** — prose written with the Write tool when you
  already understand the situation and Codex does not need raw source.
  Preserve every materially relevant fact, decision, uncertainty, and artifact
  reference; remove only irrelevant or duplicated material.
- **Shell-extracted context** — raw artifacts from disk, using the recipes
  below.

Build a decision-complete working set, not a transcript dump. The script has
no byte ceiling on `context.md`, stdin, role fields, or the composed prompt,
and it never truncates them; relevance selection is your job. For a long
session, assemble context in this order:

1. **Problem, project, trajectory, and immediate objective:** what the user
   is solving or implementing, the result needed now, the current
   branch/worktree/runtime state, and whether the work is goal-directed,
   blocked, or exploratory.
2. **In-flight work:** files, modules, features, drafts, datasets, queries,
   experiments, deployments, tests, and research being changed or validated.
3. **Active problems and hypotheses:** bugs, errors, symptoms, regressions,
   security or performance failures, failing commands, attempted fixes,
   working theories, and the evidence for or against them.
4. **Recent working context at high fidelity:** recent user constraints,
   decisions, actions, outputs, and artifacts that led to the current state.
   Keep exact wording or raw material when details matter.
5. **Current primary evidence:** files, diffs, diagnostics, data, sources, or
   command output verified live rather than recalled.
6. **Older durable context as a faithful summary:** decisions, rejected
   approaches and why, invariants, preferences, earlier evidence, and
   dependencies that still constrain the work. Include an old fact whenever
   removing it could change the recommendation; age alone is never a reason
   to drop it.
7. **Unknowns, assumptions, and provenance:** separate verified current state
   from summaries and inference; name known unknowns, likely blind spots,
   missing evidence, possibly wrong assumptions, and what observation would
   resolve each.

Leave out superseded state, conversational repetition, stale intermediate
output, and unrelated history. If the host conversation was compacted, treat
the compaction summary as an index, re-check live state, and carry forward
the older details that still matter. Do not compress merely to fit this
plugin; there is no plugin size budget. The model, provider, OS, and memory
still have real limits: if one is hit, keep the staged material and surface
the actual downstream error.

Common scopes:

- **Project context** — what the codebase is: purpose, architecture, key
  modules, conventions, direction, and constraints. For evaluating the
  project as a whole.
- **Live problem-solving and implementation map** — what the user is doing
  now, in-flight artifacts, observed bugs and hypotheses, what has been
  tried, unknowns, blockers, ownership, and the next decision.
- **Session retrospective** — what this session did: goal, files touched,
  decisions, open questions, and branch state.

Mark uncertainty explicitly, and verify state live (for example
`git status --short --branch`) instead of recalling it. Never write an empty
context file; when there is nothing to extract, write a self-contained
question to `ABS_RUNDIR/context.md` instead.

## Extraction rules

Use the exact private `ABS_RUNDIR` printed by the single `mktemp -d` call in
`SKILL.md`. Each recipe must run inside one Bash invocation: shell options and
variables do not persist across Claude Code Bash calls, so every recipe
re-assigns its own paths. Placeholder discipline: paste concrete values for
every `<angle-bracket>` placeholder and for the literal `ABS_RUNDIR` prefix
before running — never leave an undefined `$file`-style variable from an
earlier tool call in the command.

## The fail-closed skeleton

Every extraction uses this exact shape. It pre-cleans both the final file and
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
self-contained question to `context.md` instead (see above). A bare
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
