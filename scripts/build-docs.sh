#!/usr/bin/env bash
# Build docs/codex-council.pdf: README, DESIGN, the skill (SKILL.md), and
# its references in one PDF, with the diagram PNGs embedded.
#
#   scripts/build-docs.sh             build the PDF from the committed PNGs
#   scripts/build-docs.sh --diagrams  first re-render docs/diagrams/<id>.png
#                                     from <id>.mmd through mermaid.ink
#
# The PDF works away from this checkout: scripts/docs_html.py turns links
# between these documents into in-document links and every other repository
# link into its GitHub URL, and the headings become PDF bookmarks. Needs uv
# (Markdown to HTML with Python-Markdown) and Google Chrome or Chromium
# (headless --print-to-pdf); --diagrams also needs curl and network access.
# Set CHROME to the browser binary when it is not found on its own.
set -euo pipefail

root=$(cd -- "$(dirname -- "$0")/.." && pwd)
diagrams="$root/docs/diagrams"
pdf="$root/docs/codex-council.pdf"
skill="plugins/codex-council/skills/codex-council"
docs=(
  README.md
  DESIGN.md
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
    # Twice the diagram's natural width, for print, on an opaque white
    # background (mermaid.ink takes the color as hex without '#').
    width=$(grep -o 'viewBox="[^"]*"' "$work/$id.svg" | head -n 1 |
      awk -F'[" ]' '{ printf "%d", $4 + 0.5 }')
    curl -fsS --max-time 60 -A 'Mozilla/5.0' -o "$work/$id.png" \
      "https://mermaid.ink/img/$code?type=png&bgColor=FFFFFF&width=$width&scale=2"
    # Only an RGB PNG (colour type 2, no alpha channel) is opaque for sure.
    python3 - "$work/$id.png" <<'PY'
import sys
data = open(sys.argv[1], "rb").read()
if data[:8] != b"\x89PNG\r\n\x1a\n" or data[25] != 2 or b"tRNS" in data:
    sys.exit(f"build-docs: {sys.argv[1]} is not an opaque RGB PNG")
PY
    mv "$work/$id.png" "$diagrams/$id.png"
    echo "rendered docs/diagrams/$id.png"
  done
fi

html="$work/codex-council.html"
uvx --quiet --from markdown python3 "$root/scripts/docs_html.py" \
  "$root" "$html" "${docs[@]}"

"$(find_chrome)" --headless=new --disable-gpu --no-pdf-header-footer \
  --generate-pdf-document-outline --print-to-pdf="$pdf" "file://$html" \
  2>/dev/null
[ -s "$pdf" ] || { echo "build-docs: no PDF was written" >&2; exit 1; }

# Report what the PDF holds, and refuse one whose links would only work on
# this machine.
python3 - "$pdf" <<'PY'
import re
import sys

data = open(sys.argv[1], "rb").read()
pages = len(re.findall(rb"/Type\s*/Page(?![a-z])", data))
images = len(re.findall(rb"/Subtype\s*/Image", data))
uris = re.findall(rb"/URI\s*\(([^)]*)\)", data)
local = sorted({u.decode(errors="replace") for u in uris
                if not u.startswith((b"https://", b"http://"))})
internal = len(re.findall(rb"/Subtype\s*/Link[^>]*?/Dest\s*/", data))
bookmarks = len(re.findall(rb"/Title\s*[(<][^\n]*\n/Dest\s*\[", data))
print(f"wrote {sys.argv[1]}: {pages} pages, {images} images, "
      f"{internal} in-document links, {len(uris)} web links, "
      f"{bookmarks} bookmarks")
if local:
    sys.exit("build-docs: links that only work on this machine: "
             + ", ".join(local))
if not bookmarks:
    sys.exit("build-docs: the PDF has no bookmarks")
PY
