"""Render the documentation set as one printable HTML page.

usage: docs_html.py ROOT OUT.html DOC.md...   (run by scripts/build-docs.sh)

Each document becomes one section, and each heading gets a GitHub-style id
prefixed with its document's id, so the same heading in two documents never
collides. Links are rewritten so the PDF works away from this checkout: a
link to a document in the set, or to a heading in one, becomes an
in-document link; a link to a diagram PNG the set embeds jumps to that
figure; any other repository path becomes its GitHub URL. Each embedded
diagram and its caption become one figure that is never split across pages,
shown at the diagram's natural size (its PNG holds PNG_SCALE times that).
A link to a missing path, outside the repository, or to a heading that does
not exist stops the build.
"""

import html
import os
import re
import struct
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

REPO_URL = "https://github.com/ehzawad/codex-council"
BRANCH = "main"
# docs/diagrams/<id>.png is rendered at this multiple of the natural size.
PNG_SCALE = 2
# An A4 page's side margin, its printable width, and the tallest a figure
# may be; a larger diagram is scaled down to fit that box.
SIDE_MARGIN_MM = 14
CONTENT_WIDTH_MM = 210 - 2 * SIDE_MARGIN_MM
FIGURE_MAX_HEIGHT_MM = 235

STYLE = f"""
@page {{ size: A4; margin: 16mm {SIDE_MARGIN_MM}mm; }}
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
img {{ max-width: 100%; }}
figure {{ margin: 1em 0; break-inside: avoid; }}
figure img {{ display: block; margin: 0 auto 0.5em;
             max-height: {FIGURE_MAX_HEIGHT_MM}mm; }}
figcaption {{ font-size: 9.5pt; }}
.doc {{ break-before: page; }}
.doc:first-child {{ break-before: auto; }}
"""

HEADING = re.compile(r"<h([1-6])>(.*?)</h\1>", re.S)
FIGURE = re.compile(
    r'<p>(<img [^>]*?src="([^"]+)"[^>]*?/?>)</p>\s*<p><em>(.*?)</em></p>',
    re.S)
HREF = re.compile(r'(<a [^>]*?href=")([^"]*)(")')
SRC = re.compile(r'(<img [^>]*?src=")([^"]*)(")')


def fail(message):
    sys.exit(f"docs_html: {message}")


def github_slug(text):
    """The anchor GitHub gives a heading with this (rendered) text."""
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip().lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def png_width(path):
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        fail(f"{path} is not a PNG")
    return struct.unpack(">I", head[16:20])[0]


class DocSet:
    def __init__(self, root, docs):
        self.root = Path(root).resolve()
        self.docs = {}  # repo path -> document id
        for doc in docs:
            doc_id = Path(doc).stem.lower()
            if doc_id in self.docs.values():
                fail(f"two documents share the id {doc_id!r}")
            self.docs[doc] = doc_id
        self.figures = {}  # repo path of a PNG -> id of its first figure

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
        """One document's section, with heading ids and figures."""
        # Imported here so the link rules can be used without it.
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

        def figure(match):
            image, src, caption = match.groups()
            target = self.repo_path(doc, src)
            ident = ""
            if target not in self.figures:
                self.figures[target] = f"fig-{Path(target).stem}"
                ident = f' id="{self.figures[target]}"'
            # A max-width, not a width, so max-height still keeps the
            # aspect ratio.
            width = png_width(self.root / target) // PNG_SCALE
            image = image.replace(
                "<img ", f'<img style="max-width: min(100%, {width}px)" ', 1)
            return (f"<figure{ident}>{image}"
                    f"<figcaption><em>{caption}</em></figcaption></figure>")

        body = FIGURE.sub(figure, HEADING.sub(heading, body))
        return f'<section class="doc" id="{doc_id}">\n{body}\n</section>'

    def link(self, doc, href):
        parts = urlsplit(html.unescape(href))
        if parts.scheme or parts.netloc:
            return href
        target = self.repo_path(doc, parts.path) if parts.path else doc
        fragment = parts.fragment
        if target in self.docs:
            doc_id = self.docs[target]
            return "#" + (f"{doc_id}--{fragment}" if fragment else doc_id)
        if target in self.figures and not fragment:
            return "#" + self.figures[target]
        kind = "tree" if (self.root / target).is_dir() else "blob"
        url = f"{REPO_URL}/{kind}/{BRANCH}/{target}"
        return url + (f"#{fragment}" if fragment else "")

    def render(self):
        sections = {doc: self.convert(doc) for doc in self.docs}
        out = []
        for doc, section in sections.items():
            section = HREF.sub(
                lambda m, d=doc: m.group(1) + html.escape(self.link(
                    d, m.group(2))) + m.group(3), section)
            section = SRC.sub(
                lambda m, d=doc: m.group(1) + (self.root / self.repo_path(
                    d, m.group(2))).as_uri() + m.group(3), section)
            out.append(section)
        page = "\n".join(out)
        ids = set(re.findall(r'\bid="([^"]+)"', page))
        missing = sorted({anchor for anchor in re.findall(r'href="#([^"]*)"',
                                                          page)
                          if anchor not in ids})
        if missing:
            fail(f"links to missing headings: {', '.join(missing)}")
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
