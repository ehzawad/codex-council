#!/usr/bin/env bash
# Build docs/codex-council.pdf: README, DESIGN, the skill (SKILL.md), and
# its references in one PDF, with the diagram PNGs embedded.
#
#   scripts/build-docs.sh             build the PDF from the committed PNGs
#   scripts/build-docs.sh --diagrams  first re-render docs/diagrams/<id>.png
#                                     from <id>.mmd through mermaid.ink
#
# Needs uvx (Markdown to HTML with Python-Markdown) and Google Chrome or
# Chromium (headless --print-to-pdf); --diagrams also needs curl and network
# access. Set CHROME to the browser binary when it is not found on its own.
set -euo pipefail

root=$(cd -- "$(dirname -- "$0")/.." && pwd)
diagrams="$root/docs/diagrams"
skill="$root/plugins/codex-council/skills/codex-council"
pdf="$root/docs/codex-council.pdf"
docs=(
  "$root/README.md"
  "$root/DESIGN.md"
  "$skill/SKILL.md"
  "$skill/references/panel-design.md"
  "$skill/references/context-staging.md"
  "$skill/references/runtime-behavior.md"
)

render=0
case "${1:-}" in
  "") ;;
  --diagrams) render=1 ;;
  *) echo "usage: $0 [--diagrams]" >&2; exit 2 ;;
esac

find_chrome() {
  local candidate
  for candidate in "${CHROME:-}" \
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
      "/Applications/Chromium.app/Contents/MacOS/Chromium" \
      google-chrome google-chrome-stable chromium chromium-browser; do
    [ -n "$candidate" ] || continue
    if [ -x "$candidate" ] || command -v "$candidate" >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  echo "build-docs: no Chrome or Chromium found; set CHROME" >&2
  return 1
}

work=$(mktemp -d "${TMPDIR:-/tmp}/codex-council-docs.XXXXXX")
trap 'rm -rf "$work"' EXIT

if [ "$render" -eq 1 ]; then
  for mmd in "$diagrams"/*.mmd; do
    id=$(basename "$mmd" .mmd)
    code=$(base64 <"$mmd" | tr -d '\n' | tr '+/' '-_' | tr -d '=')
    curl -fsS --max-time 60 -A 'Mozilla/5.0' -o "$work/$id.svg" \
      "https://mermaid.ink/svg/$code"
    if grep -q -e 'Syntax error' -e 'Parse error' "$work/$id.svg"; then
      echo "build-docs: $id.mmd does not parse" >&2
      exit 1
    fi
    # Render at twice the diagram's natural width, for print.
    width=$(grep -o 'viewBox="[^"]*"' "$work/$id.svg" | head -n 1 |
      awk -F'[" ]' '{ printf "%d", $4 + 0.5 }')
    curl -fsS --max-time 60 -A 'Mozilla/5.0' -o "$diagrams/$id.png" \
      "https://mermaid.ink/img/$code?type=png&bgColor=white&width=$width&scale=2"
    echo "rendered docs/diagrams/$id.png"
  done
fi

# Every diagram README and DESIGN embed must exist.
for image in $(grep -oh 'docs/diagrams/[a-z0-9-]*\.png' "$root/README.md" \
    "$root/DESIGN.md" | sort -u); do
  [ -f "$root/$image" ] || { echo "build-docs: missing $image" >&2; exit 1; }
done

html="$work/codex-council.html"
{
  cat <<HTML
<!doctype html>
<html><head><meta charset="utf-8">
<base href="file://$root/">
<title>codex-council</title>
<style>
@page { size: A4; margin: 16mm 14mm; }
body { font: 10.5pt/1.45 -apple-system, "Helvetica Neue", Arial, sans-serif;
       color: #111827; }
h1 { font-size: 20pt; border-bottom: 1px solid #d1d5db; }
h2 { font-size: 15pt; margin-top: 1.6em; }
h3 { font-size: 12pt; }
h1, h2, h3 { break-after: avoid; }
pre, code { font: 8.5pt/1.35 Menlo, Consolas, monospace; }
pre { background: #f3f4f6; padding: 8px; white-space: pre-wrap;
      overflow-wrap: anywhere; break-inside: avoid; }
table { border-collapse: collapse; margin: 0.8em 0; font-size: 9.5pt; }
th, td { border: 1px solid #d1d5db; padding: 3px 6px; vertical-align: top; }
img { display: block; margin: 0.6em auto; max-width: 100%;
      max-height: 200mm; break-inside: avoid; }
.doc { break-before: page; }
</style></head><body>
HTML
  for doc in "${docs[@]}"; do
    echo '<div class="doc">'
    # Drop SKILL.md's YAML frontmatter; the rest is plain Markdown.
    awk 'NR == 1 && $0 == "---" { fm = 1; next }
         fm && $0 == "---" { fm = 0; next }
         !fm' "$doc" |
      uvx --quiet --from markdown markdown_py -x extra -x sane_lists
    echo '</div>'
  done
  echo '</body></html>'
} >"$html"

"$(find_chrome)" --headless=new --disable-gpu --no-pdf-header-footer \
  --print-to-pdf="$pdf" "file://$html" 2>/dev/null
[ -s "$pdf" ] || { echo "build-docs: no PDF was written" >&2; exit 1; }

# Report what the PDF holds, so a build that lost its images shows it.
python3 - "$pdf" <<'PY'
import re
import sys

data = open(sys.argv[1], "rb").read()
pages = len(re.findall(rb"/Type\s*/Page(?![a-z])", data))
images = len(re.findall(rb"/Subtype\s*/Image", data))
print(f"wrote {sys.argv[1]}: {pages} pages, {images} images")
PY
