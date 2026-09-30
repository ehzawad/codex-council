"""Render the documentation set as one printable HTML page.

usage: docs_html.py ROOT OUT.html DOC.md...   (run by scripts/build-docs.sh)

Each document becomes one section, and each heading gets a GitHub-style id
prefixed with its document's id, so the same heading in two documents never
collides. Links are rewritten so the PDF works away from this checkout: a
link to a document in the set, or to a heading in one, becomes an
in-document link; any other repository path becomes its GitHub URL. A link
to a missing path, outside the repository, or to a heading that does not
exist stops the build.

Diagrams are vector graphics. Each ```mermaid block is rendered to SVG
through mermaid.ink at build time (a 5xx answer or a network failure is
retried) and inlined as a figure captioned with its section's heading; no
SVG or image file is written anywhere. Every id inside an inlined SVG is
prefixed, so several diagrams on one page cannot collide, and its labels
use the Arial metrics mermaid.ink measured them with. A figure is shown at
its natural size when it fits the page and scaled down when it does not; a
wide diagram that prints larger on a landscape page gets an A4 landscape
page of its own. A diagram that would print below MIN_PRINT_SCALE, a
mermaid.ink refusal (a syntax error), and any image in a document each stop
the build: the documentation holds no image files.
"""

import base64
import html
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlsplit

REPO_URL = "https://github.com/ehzawad/codex-council"
BRANCH = "main"

MERMAID_INK = "https://mermaid.ink/svg/"
RENDER_ATTEMPTS = 4
RENDER_TIMEOUT_SECS = 60
# The first retry waits this long; each later one waits twice as long.
RENDER_BACKOFF_SECS = 2

# An A4 page's side margin, its printable width, and the tallest a figure
# may be; a diagram that does not fit is scaled down to fit that box. A wide
# diagram may instead get an A4 landscape page with the same margins.
SIDE_MARGIN_MM = 14
CONTENT_WIDTH_MM = 210 - 2 * SIDE_MARGIN_MM
FIGURE_MAX_HEIGHT_MM = 235
WIDE_WIDTH_MM = 297 - 2 * SIDE_MARGIN_MM
# The landscape page's height, less its margins and the caption.
WIDE_MAX_HEIGHT_MM = 210 - 2 * 16 - 12
# Landscape only when it prints the diagram at least this much larger.
WIDE_GAIN = 1.1
MM_PER_PX = 25.4 / 96
# Mermaid draws 16 px labels; below this scale they print under 5.5 pt.
MIN_PRINT_SCALE = 0.45

STYLE = f"""
@page {{ size: A4; margin: 16mm {SIDE_MARGIN_MM}mm; }}
@page wide {{ size: A4 landscape; margin: 16mm {SIDE_MARGIN_MM}mm; }}
body {{ font: 10.5pt/1.45 -apple-system, "Helvetica Neue", Arial,
       sans-serif; color: #111827; background: #ffffff; }}
h1 {{ font-size: 20pt; border-bottom: 1px solid #d1d5db; }}
h2 {{ font-size: 15pt; margin-top: 1.6em; }}
h3 {{ font-size: 12pt; }}
h1, h2, h3 {{ break-after: avoid; }}
a {{ color: #1d4ed8; text-decoration: none; }}
pre, code {{ font: 8.5pt/1.35 Menlo, Consolas, monospace; }}
pre {{ background: #f3f4f6; padding: 8px; white-space: pre-wrap;
      overflow-wrap: anywhere; break-inside: avoid; }}
table {{ border-collapse: collapse; margin: 0.8em 0; font-size: 9.5pt; }}
th, td {{ border: 1px solid #d1d5db; padding: 3px 6px; vertical-align: top; }}
figure.diagram {{ margin: 1em 0; break-inside: avoid; }}
figure.diagram.wide {{ page: wide; margin: 0; }}
figure.diagram svg {{ display: block; margin: 0 auto 0.5em; }}
figure.diagram svg * {{ font-family: Arial, "Liberation Sans", Helvetica,
                        sans-serif !important; }}
figure.diagram foreignObject {{ overflow: visible; }}
figcaption {{ font-size: 9.5pt; text-align: center; }}
.doc {{ break-before: page; }}
.doc:first-child {{ break-before: auto; }}
"""

HEADING = re.compile(r"<h([1-6])>(.*?)</h\1>", re.S)
MERMAID = re.compile(r'<pre><code class="language-mermaid">(.*?)</code></pre>',
                     re.S)
PLACEHOLDER = re.compile(r"<!--diagram-(\d+)-->")
HREF = re.compile(r'(<a [^>]*?href=")([^"]*)(")')
SVG_ROOT = re.compile(r"<svg\b[^>]*>")
ID_ATTR = re.compile(r'(?<![\w:-])id="([^"]+)"')


def fail(message):
    sys.exit(f"docs_html: {message}")


def github_slug(text):
    """The anchor GitHub gives a heading with this (rendered) text."""
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip().lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def mermaid_ink_svg(source):
    """Mermaid source rendered to SVG text by mermaid.ink.

    A 5xx answer or a network failure is retried RENDER_ATTEMPTS times in
    all, with a doubling pause; any other refusal (a syntax error comes back
    as 400 with the parser's message) stops the build at once.
    """
    code = base64.urlsafe_b64encode(source.encode("utf-8")).decode("ascii")
    request = urllib.request.Request(MERMAID_INK + code.rstrip("="),
                                     headers={"User-Agent": "Mozilla/5.0"})
    delay = RENDER_BACKOFF_SECS
    for attempt in range(1, RENDER_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(
                    request, timeout=RENDER_TIMEOUT_SECS) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code < 500:
                detail = e.read().decode("utf-8", "replace").strip()
                fail(f"mermaid.ink refused a diagram (HTTP {e.code}): "
                     f"{detail}")
            problem = f"HTTP {e.code}"
        except (urllib.error.URLError, OSError) as e:
            problem = str(getattr(e, "reason", e))
        if attempt == RENDER_ATTEMPTS:
            fail(f"mermaid.ink failed {RENDER_ATTEMPTS} times; last: "
                 f"{problem}")
        print(f"docs_html: mermaid.ink {problem}; retrying in {delay} s",
              file=sys.stderr)
        time.sleep(delay)
        delay *= 2


def inline_svg(svg, prefix):
    """(svg, width, height): mermaid SVG text made safe to inline.

    Every id in it, and every reference to one, is prefixed with `prefix`;
    external stylesheet imports are dropped; the root's own width and
    max-width are removed so the figure sets the printed size. Width and
    height are the viewBox's, in CSS pixels.
    """
    svg = re.sub(r"\A\s*(?:<\?xml[^>]*\?>\s*)?", "", svg)
    root = SVG_ROOT.match(svg)
    if root is None or "Syntax error" in svg or "Parse error" in svg:
        fail("mermaid.ink did not return a rendered diagram")
    box = re.search(r'\bviewBox="([^"]+)"', root.group(0))
    root_id = ID_ATTR.search(root.group(0))
    if box is None or root_id is None:
        fail("a rendered diagram has no viewBox or id")
    width, height = (float(v) for v in box.group(1).split()[2:4])
    # The root id also scopes the SVG's stylesheet and prefixes its markers.
    svg = svg.replace(root_id.group(1), prefix)
    local = {name for name in ID_ATTR.findall(svg)
             if not name.startswith(prefix)}

    def renamed(match, template):
        name = match.group(1)
        return template.format(f"{prefix}-{name}" if name in local else name)

    svg = ID_ATTR.sub(lambda m: renamed(m, 'id="{}"'), svg)
    svg = re.sub(r"url\(#([^)\s]+)\)", lambda m: renamed(m, "url(#{})"), svg)
    svg = re.sub(r'href="#([^"]+)"', lambda m: renamed(m, 'href="#{}"'), svg)
    svg = re.sub(r"<style[^>]*>\s*@import[^<]*</style>", "", svg)
    svg = re.sub(r"@import[^;]*;", "", svg)
    root = SVG_ROOT.match(svg).group(0)
    bare = re.sub(r'\s(?:width|height|style)="[^"]*"', "", root)
    return svg.replace(root, bare, 1), width, height


def layout(width_px, height_px):
    """(wide, scale): whether a diagram of this natural size gets a
    landscape page, and the scale it prints at (never above 1)."""
    width, height = width_px * MM_PER_PX, height_px * MM_PER_PX
    tall = min(1, CONTENT_WIDTH_MM / width, FIGURE_MAX_HEIGHT_MM / height)
    wide = min(1, WIDE_WIDTH_MM / width, WIDE_MAX_HEIGHT_MM / height)
    if wide >= tall * WIDE_GAIN:
        return True, wide
    return False, tall


class DocSet:
    def __init__(self, root, docs, render=None):
        self.root = Path(root).resolve()
        self.docs = {}  # repo path -> document id
        for doc in docs:
            doc_id = Path(doc).stem.lower()
            if doc_id in self.docs.values():
                fail(f"two documents share the id {doc_id!r}")
            self.docs[doc] = doc_id
        # Mermaid source -> SVG text; mermaid.ink unless a test stands in.
        self.render_svg = render or mermaid_ink_svg
        self.diagrams = []  # (doc, caption, mermaid source)

    def repo_path(self, doc, link_path):
        """`link_path` (relative to `doc`) as a path inside the repository."""
        target = os.path.normpath(os.path.join(os.path.dirname(doc),
                                               unquote(link_path)))
        if target.startswith("..") or os.path.isabs(target):
            fail(f"{doc}: link {link_path!r} leaves the repository")
        if not (self.root / target).exists():
            fail(f"{doc}: link {link_path!r} names a missing path")
        return target

    def convert(self, doc):
        """One document's section, with heading ids and a placeholder for
        each diagram."""
        # Imported here so the link and diagram rules can be used without it.
        import markdown

        text = (self.root / doc).read_text(encoding="utf-8")
        # Drop SKILL.md's YAML frontmatter; the rest is plain Markdown.
        text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)
        body = markdown.markdown(text, extensions=["extra", "sane_lists"])
        doc_id, seen = self.docs[doc], {}

        def heading(match):
            slug = github_slug(match.group(2))
            count = seen.get(slug, 0)
            seen[slug] = count + 1
            anchor = f"{doc_id}--{slug}" + (f"-{count}" if count else "")
            return (f'<h{match.group(1)} id="{anchor}">{match.group(2)}'
                    f"</h{match.group(1)}>")

        body = self.take_diagrams(doc, HEADING.sub(heading, body))
        return f'<section class="doc" id="{doc_id}">\n{body}\n</section>'

    def take_diagrams(self, doc, body):
        """`body` (a document's HTML, heading ids added) with each mermaid
        block replaced by a placeholder for its figure, captioned with the
        heading above it; an image stops the build."""
        image = re.search(r"<img\b[^>]*>", body)
        if image:
            fail(f"{doc}: {image.group(0)} embeds an image; the docs hold "
                 "no image files, so draw it as a mermaid block")

        def diagram(match):
            titles = re.findall(r"<h[1-6] [^>]*>(.*?)</h[1-6]>",
                                body[:match.start()], re.S)
            caption = re.sub(r"<[^>]+>", "", titles[-1]) if titles else ""
            self.diagrams.append(
                (doc, caption, html.unescape(match.group(1))))
            return f"<!--diagram-{len(self.diagrams) - 1}-->"

        return MERMAID.sub(diagram, body)

    def figure(self, index):
        """The inline SVG figure for diagram `index`."""
        doc, caption, source = self.diagrams[index]
        svg, width, height = inline_svg(self.render_svg(source),
                                        f"diagram-{index}")
        wide, scale = layout(width, height)
        where = f"{doc}: the diagram under {caption!r}"
        if scale < MIN_PRINT_SCALE:
            fail(f"{where} would print at {scale:.0%} of its size, below "
                 f"{MIN_PRINT_SCALE:.0%}; split it or shorten its labels")
        print(f"docs_html: {where}: {width:.0f}x{height:.0f} px, "
              f"{'landscape' if wide else 'portrait'}, printed at "
              f"{scale:.0%}", file=sys.stderr)
        size = (f"width: {width * MM_PER_PX * scale:.1f}mm; "
                f"height: {height * MM_PER_PX * scale:.1f}mm")
        svg = svg.replace("<svg", f'<svg style="{size}"', 1)
        return (f'<figure class="diagram{" wide" if wide else ""}">{svg}'
                f"<figcaption>{caption}</figcaption></figure>")

    def link(self, doc, href):
        parts = urlsplit(html.unescape(href))
        if parts.scheme or parts.netloc:
            return href
        target = self.repo_path(doc, parts.path) if parts.path else doc
        fragment = parts.fragment
        if target in self.docs:
            doc_id = self.docs[target]
            return "#" + (f"{doc_id}--{fragment}" if fragment else doc_id)
        kind = "tree" if (self.root / target).is_dir() else "blob"
        url = f"{REPO_URL}/{kind}/{BRANCH}/{target}"
        return url + (f"#{fragment}" if fragment else "")

    def render(self):
        sections = {doc: self.convert(doc) for doc in self.docs}
        out = []
        for doc, section in sections.items():
            out.append(HREF.sub(
                lambda m, d=doc: m.group(1) + html.escape(self.link(
                    d, m.group(2))) + m.group(3), section))
        page = "\n".join(out)
        ids = set(re.findall(r'\bid="([^"]+)"', page))
        missing = sorted({anchor for anchor in re.findall(r'href="#([^"]*)"',
                                                          page)
                          if anchor not in ids})
        if missing:
            fail(f"links to missing headings: {', '.join(missing)}")
        # The SVGs go in last, after every link and anchor check.
        page = PLACEHOLDER.sub(lambda m: self.figure(int(m.group(1))), page)
        return ("<!doctype html>\n<html><head><meta charset=\"utf-8\">\n"
                "<title>codex-council</title>\n"
                f"<style>{STYLE}</style></head><body>\n{page}\n"
                "</body></html>\n")


def main(argv):
    if len(argv) < 4:
        fail("usage: docs_html.py ROOT OUT.html DOC.md...")
    root, out, docs = argv[1], argv[2], argv[3:]
    Path(out).write_text(DocSet(root, docs).render(), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv)
