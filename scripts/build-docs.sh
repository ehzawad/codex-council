#!/usr/bin/env bash
# Build docs/codex-council.pdf: README, DESIGN, the skill (SKILL.md), and
# its references in one PDF, with every diagram drawn as vector graphics.
#
#   scripts/build-docs.sh
#
# scripts/docs_html.py lays the documents out as one HTML page. It renders
# each ```mermaid block to SVG through mermaid.ink (retrying a 5xx answer
# or a network failure) and inlines the SVG, so Chrome prints the diagrams
# as vector paths and text; no SVG or PNG is written anywhere, and the
# repository holds no image files. The PDF works away from this checkout:
# links between these documents become in-document links, every other
# repository link becomes its GitHub URL, and the headings become PDF
# bookmarks. The build refuses a PDF that holds any raster image or a link
# that only works on this machine. Needs uv (Markdown to HTML with
# Python-Markdown), network access to mermaid.ink, and Google Chrome or
# Chromium (headless --print-to-pdf). Set CHROME to the browser binary when
# it is not found on its own.
set -euo pipefail

root=$(cd -- "$(dirname -- "$0")/.." && pwd)
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

if [ "$#" -ne 0 ]; then
  echo "usage: $0" >&2
  exit 2
fi

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

html="$work/codex-council.html"
uvx --quiet --from markdown python3 "$root/scripts/docs_html.py" \
  "$root" "$html" "${docs[@]}"

# Printed in the temporary directory; docs/ gets it only once it passes.
printed="$work/codex-council.pdf"
"$(find_chrome)" --headless=new --disable-gpu --no-pdf-header-footer \
  --generate-pdf-document-outline --print-to-pdf="$printed" "file://$html" \
  2>/dev/null
[ -s "$printed" ] || { echo "build-docs: no PDF was written" >&2; exit 1; }

# Report what the PDF holds, and refuse one with a raster image (every
# diagram must stay vector) or with links that only work on this machine.
python3 - "$printed" <<'PY'
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
print(f"printed {pages} pages, {images} raster images, "
      f"{internal} in-document links, {len(uris)} web links, "
      f"{bookmarks} bookmarks")
if images:
    sys.exit("build-docs: the PDF holds raster images; diagrams must be "
             "inline SVG")
if local:
    sys.exit("build-docs: links that only work on this machine: "
             + ", ".join(local))
if not bookmarks:
    sys.exit("build-docs: the PDF has no bookmarks")
PY
mv -f "$printed" "$pdf"
echo "wrote $pdf ($(wc -c <"$pdf" | tr -d ' ') bytes)"
