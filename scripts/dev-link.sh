#!/usr/bin/env bash
# Dev-loop helper for codex-council plugin authors.
#
# Three steps, all idempotent:
#   1. Symlink ~/.claude/plugins/cache/codex-council/codex-council/<version>/
#      to this repo's plugins/codex-council/ so edits are live at runtime.
#   2. Rewrite ~/.claude/plugins/installed_plugins.json so the harness's
#      installPath+version point at the symlinked version. Without this
#      step, the harness keeps loading whichever version it originally
#      registered (typically whatever first `claude plugins install`
#      recorded), ignoring newer sibling versions that dev-link creates.
#   3. Prune stale sibling entries in the cache dir for any other
#      version — real directories from prior `claude plugins install`
#      or symlinks from prior dev-link runs — so the cache doesn't
#      accumulate orphaned entries after version bumps.
#
# Re-run after any version bump in plugin.json, any `claude plugins update`,
# or any cache wipe. Restart Claude Code once afterward so the session
# rebinds; SKILL.md in-session caching behavior is not documented, so
# SKILL edits may still need a session restart.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PLUGIN_DIR="$REPO_ROOT/plugins/codex-council"
MANIFEST="$PLUGIN_DIR/.claude-plugin/plugin.json"

if [[ ! -f "$MANIFEST" ]]; then
    echo "dev-link: manifest not found at $MANIFEST — not a codex-council repo?" >&2
    exit 1
fi

VERSION=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$MANIFEST")
if [[ -z "$VERSION" ]]; then
    echo "dev-link: could not read version from $MANIFEST" >&2
    exit 1
fi

CACHE_ROOT="$HOME/.claude/plugins/cache/codex-council/codex-council"
CACHE_DIR="$CACHE_ROOT/$VERSION"
INSTALLED_MANIFEST="$HOME/.claude/plugins/installed_plugins.json"
PLUGIN_KEY="codex-council@codex-council"

# Guard: never touch a path outside the expected cache root prefix.
case "$CACHE_DIR" in
    "$HOME/.claude/plugins/cache/codex-council/codex-council/"*) ;;
    *)
        echo "dev-link: refusing to touch unexpected path $CACHE_DIR" >&2
        exit 1
        ;;
esac

# --- Step 1: symlink the versioned cache dir to the repo ---

mkdir -p "$CACHE_ROOT"

if [[ -L "$CACHE_DIR" ]]; then
    CURRENT_TARGET="$(readlink "$CACHE_DIR")"
    if [[ "$CURRENT_TARGET" == "$PLUGIN_DIR" ]]; then
        echo "dev-link: symlink already current: $CACHE_DIR -> $PLUGIN_DIR"
    else
        echo "dev-link: replacing stale symlink (was -> $CURRENT_TARGET)"
        rm "$CACHE_DIR"
        ln -s "$PLUGIN_DIR" "$CACHE_DIR"
        echo "dev-link: linked $CACHE_DIR -> $PLUGIN_DIR"
    fi
elif [[ -d "$CACHE_DIR" ]]; then
    echo "dev-link: removing existing cache directory at $CACHE_DIR"
    rm -rf "$CACHE_DIR"
    ln -s "$PLUGIN_DIR" "$CACHE_DIR"
    echo "dev-link: linked $CACHE_DIR -> $PLUGIN_DIR"
else
    ln -s "$PLUGIN_DIR" "$CACHE_DIR"
    echo "dev-link: linked $CACHE_DIR -> $PLUGIN_DIR"
fi

# --- Step 2: rewrite installed_plugins.json so the harness loads this version ---

if [[ ! -f "$INSTALLED_MANIFEST" ]]; then
    echo "dev-link: $INSTALLED_MANIFEST not found — skipping manifest update."
    echo "dev-link: (the symlink alone is not enough; run 'claude plugins install codex-council@codex-council' first, then re-run this script)"
    exit 0
fi

python3 - "$INSTALLED_MANIFEST" "$PLUGIN_KEY" "$VERSION" "$CACHE_DIR" <<'PYEOF'
import json
import os
import sys
from datetime import datetime, timezone

path, plugin_key, new_version, new_install_path = sys.argv[1:5]

with open(path) as f:
    data = json.load(f)

plugins = data.get("plugins") or {}
entries = plugins.get(plugin_key)
if not entries:
    print(
        f"dev-link: plugin '{plugin_key}' not found in installed_plugins.json — "
        f"run 'claude plugins install {plugin_key}' once, then re-run dev-link.",
        file=sys.stderr,
    )
    sys.exit(0)

now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
changed = 0
for entry in entries:
    old_version = entry.get("version")
    old_path = entry.get("installPath")
    if old_version == new_version and old_path == new_install_path:
        continue
    entry["installPath"] = new_install_path
    entry["version"] = new_version
    entry["lastUpdated"] = now
    changed += 1
    print(
        f"dev-link: manifest: {plugin_key}: "
        f"{old_version}@{old_path} -> {new_version}@{new_install_path}",
        file=sys.stderr,
    )

if changed == 0:
    print("dev-link: installed_plugins.json already current.", file=sys.stderr)
    sys.exit(0)

tmp = path + ".dev-link.tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2)
os.replace(tmp, path)
print("dev-link: installed_plugins.json updated.", file=sys.stderr)
PYEOF

# --- Step 3: prune stale cache entries for other versions ---
find "$CACHE_ROOT" -mindepth 1 -maxdepth 1 ! -name "$VERSION" -print0 \
    | while IFS= read -r -d '' stale; do
        case "$stale" in
            "$HOME/.claude/plugins/cache/codex-council/codex-council/"*)
                rm -rf "$stale"
                echo "dev-link: pruned stale cache entry $stale"
                ;;
        esac
    done

echo "dev-link: done. Restart Claude Code so the session rebinds."
