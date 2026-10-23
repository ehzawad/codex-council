#!/usr/bin/env python3
"""Generate the README diagrams as self-contained SVG files.

    python3 docs/diagrams/generate.py          # write docs/diagrams/<name>.svg
    python3 docs/diagrams/generate.py --check  # exit 1 if a committed SVG differs

Standard library only and deterministic. Text is sized from Helvetica metrics
with headroom for wider fallback fonts; text that would clip, overlap or leave
the canvas, or render below PHONE_MIN_PX in a phone's README column, stops
generation instead of drawing.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
SANS = '"Helvetica Neue",Helvetica,Arial,"Liberation Sans","Nimbus Sans",sans-serif'
MONO = 'ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace'
HEADROOM = 1.12  # wider fallbacks such as DejaVu Sans
MONO_ADVANCE = 0.62
PAD_X, PAD_Y, GAP = 12, 9, 50
HALO = 3  # backdrop padding around a label that may sit on a line
# Every diagram is WIDTH wide and is shown at that width on a desktop. A phone
# scales it to github.com's README column, where no text may fall below
# PHONE_MIN_PX. On a 390px phone that column is 324px (a 16px gutter, a 1px
# border and 16px padding on each side): 14 * 324 / 408 is about 11.1px.
WIDTH, PHONE_COLUMN, PHONE_MIN_PX = 408, 324, 11


def _advances(table: str) -> dict[str, int]:
    """Helvetica advance widths, 1/1000 em, for ASCII 32..126."""
    values = [int(v) for v in table.split()]
    assert len(values) == 95
    return {chr(32 + i): v for i, v in enumerate(values)}


REGULAR = _advances("""
278 278 355 556 556 889 667 191 333 333 389 584 278 333 278 278
556 556 556 556 556 556 556 556 556 556 278 278 584 584 584 556
1015 667 667 722 722 667 611 778 722 278 500 667 556 833 722 778
667 778 722 667 611 722 667 944 667 667 611 278 278 278 469 556
333 556 556 500 556 556 278 556 556 222 222 500 222 833 556 556
556 556 333 500 278 556 500 722 500 500 500 334 260 334 584""")
BOLD = _advances("""
278 333 474 556 556 889 722 238 333 333 389 584 278 333 278 278
556 556 556 556 556 556 556 556 556 556 333 333 584 584 584 611
975 722 722 722 722 667 611 778 722 278 556 722 611 833 722 778
667 778 722 667 611 722 667 944 667 667 611 333 278 333 584 556
333 556 611 556 611 556 333 611 611 278 278 556 278 889 611 611
611 611 389 556 333 611 556 778 556 556 500 389 280 389 584""")

# class: (font size, bold, mono, line height); every size is at least 14px
TEXT = {
    "title": (18, True, False, 24),
    "b": (14, True, False, 19),
    "t": (14, False, False, 19),
    "s": (14, False, False, 19),
    "c": (14, False, True, 19),
    "n": (14, True, False, 19),
}
LINE = 19  # the line height of every 14px class
PREFIX = {"*": "b", "_": "s", "`": "c"}  # line prefixes: bold, muted, code

# Primer-like colours; shape kinds are (fill, stroke).
LIGHT = {
    "bg": "#f6f8fa", "edge": "#d0d7de", "ink": "#1f2328", "muted": "#59636e",
    "line": "#59636e", "life": "#818b98", "plain": ("#ffffff", "#818b98"),
    "you": ("#ffffff", "#59636e"), "claude": ("#ddf4ff", "#0969da"),
    "runner": ("#fbefff", "#8250df"), "work": ("#dafbe1", "#1a7f37"),
    "store": ("#fff8c5", "#9a6700"), "warn": ("#fff1e5", "#bc4c00"),
    "stop": ("#ffebe9", "#cf222e"), "frame": ("#f6f8fa", "#818b98"),
}
DARK = {
    "bg": "#151b23", "edge": "#3d444d", "ink": "#f0f6fc", "muted": "#9198a1",
    "line": "#9198a1", "life": "#656c76", "plain": ("#0d1117", "#656c76"),
    "you": ("#0d1117", "#9198a1"), "claude": ("#0c2d6b", "#4493f8"),
    "runner": ("#271a3f", "#ab7df8"), "work": ("#0f2d1a", "#3fb950"),
    "store": ("#2e2410", "#d29922"), "warn": ("#3d1d0b", "#f0883e"),
    "stop": ("#3b1219", "#f85149"), "frame": ("#151b23", "#656c76"),
}
KINDS = ("plain", "you", "claude", "runner", "work", "store", "warn", "stop", "frame")


def _colours(p: dict) -> list[str]:
    return [
        f"text{{fill:{p['ink']}}}",
        f".s,.c,.n{{fill:{p['muted']}}}",
        f".bg{{fill:{p['bg']};stroke:{p['edge']}}}",
        f".ln{{fill:none;stroke:{p['line']}}}",
        f".hd,.badge{{fill:{p['line']}}}",
        f".life{{fill:none;stroke:{p['life']};stroke-dasharray:5 5}}",
        f".dot{{fill:{p['ink']}}}",
        f".ring{{fill:none;stroke:{p['ink']}}}",
        f".bt,.bd{{fill:{p['bg']}}}",
        *(f".k-{k}{{fill:{p[k][0]};stroke:{p[k][1]}}}" for k in KINDS),
    ]


def stylesheet() -> str:
    rules = [f"text{{font-family:{SANS}}}",
             "rect,path,polygon,polyline,circle{stroke-width:1.5}",
             ".dash{stroke-dasharray:6 4}"]
    rules += [f".{cls}{{font-size:{size}px;font-weight:{700 if bold else 400}"
              + (f";font-family:{MONO}" if mono else "") + "}"
              for cls, (size, bold, mono, _) in TEXT.items()]
    rules += _colours(LIGHT)
    rules += ["@media (prefers-color-scheme: dark){", *_colours(DARK), "}"]
    return "\n".join(rules)


def width_of(text: str, cls: str) -> float:
    size, bold, mono, _ = TEXT[cls]
    if not (text.isascii() and text.isprintable()):
        raise ValueError(f"only printable ASCII is measured: {text!r}")
    if mono:
        return len(text) * size * MONO_ADVANCE
    table = BOLD if bold else REGULAR
    return sum(table[c] for c in text) * size / 1000 * HEADROOM


def parse(spec: str) -> tuple[str, str]:
    cls = PREFIX.get(spec[:1])
    return (spec[1:], cls) if cls else (spec, "t")


def block_height(lines) -> float:
    return sum(TEXT[parse(spec)[1]][3] for spec in lines)


def num(value: float) -> str:
    value = round(value, 2)
    return str(int(value)) if value == int(value) else f"{value:.2f}".rstrip("0")


def pts(points) -> str:
    return " ".join(f"{num(x)},{num(y)}" for x, y in points)


def show(rect) -> str:
    return "(" + ", ".join(num(v) for v in rect) + ")"


def overlap(a, b) -> bool:
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def inside(a, b) -> bool:
    return b[0] <= a[0] and a[2] <= b[2] and b[1] <= a[1] and a[3] <= b[3]


def hits_diamond(rect, diamond) -> bool:
    """Whether a rectangle reaches into a diamond (cx, cy, half w, half h)."""
    cx, cy, a, b = diamond
    dx = 0 if rect[0] <= cx <= rect[2] else min(abs(rect[0] - cx), abs(rect[2] - cx))
    dy = 0 if rect[1] <= cy <= rect[3] else min(abs(rect[1] - cy), abs(rect[3] - cy))
    return dx / a + dy / b < 0.98


class LayoutError(ValueError):
    pass


class Box:
    def __init__(self, x, y, w, h):
        self.x, self.y, self.w, self.h = x, y, w, h

    left = property(lambda s: s.x)
    right = property(lambda s: s.x + s.w)
    top = property(lambda s: s.y)
    bottom = property(lambda s: s.y + s.h)
    cx = property(lambda s: s.x + s.w / 2)
    cy = property(lambda s: s.y + s.h / 2)


class Diagram:
    def __init__(self, name: str, width: int, title: str, desc: str):
        self.name, self.width, self.title, self.desc = name, width, title, desc
        self.parts: list[str] = []
        self.solids = []    # shape rects
        self.diamonds = []  # (cx, cy, half width, half height)
        self.frames = []    # containers: text sits wholly inside or outside
        self.labels = []    # (rect, halo); a halo label may sit on a line
        self.segments = []  # line rects
        self.soft = []      # lifeline rects: only a halo label may sit on one
        self.bottom = 0.0
        self.text(24, 40, title, "title")

    def fail(self, message: str):
        raise LayoutError(f"{self.name}: {message}")

    def grow(self, y: float):
        self.bottom = max(self.bottom, y)

    # ----- text -----
    def extent(self, x, y, text, cls, anchor="start"):
        """The rectangle a line of text covers, baseline y."""
        size, w = TEXT[cls][0], width_of(text, cls)
        left = {"start": x, "middle": x - w / 2, "end": x - w}[anchor]
        return (left, y - size * 0.8, left + w, y + size * 0.25)

    def text(self, x, y, text, cls="t", anchor="start", track=True, extra=""):
        size = TEXT[cls][0]
        if track:
            self.labels.append((self.extent(x, y, text, cls, anchor), False))
        classes = " ".join(c for c in (cls, extra) if c)
        attrs = f' class="{classes}"' + (f' text-anchor="{anchor}"' if anchor != "start" else "")
        self.parts.append(f'<text x="{num(x)}" y="{num(y)}"{attrs}>{escape(text)}</text>')
        self.grow(y + size * 0.25)

    def label(self, x, y, lines, anchor="start", halo=False):
        """Muted lines (unless prefixed) whose first baseline is y; returns
        the rectangle they cover. A halo label sits on a background-coloured
        backdrop that hides any line beneath it."""
        rows = []
        for spec in lines:
            text, cls = parse(spec)
            rows.append((text, "s" if cls == "t" else cls, y))
            y += TEXT[rows[-1][1]][3]
        boxes = [self.extent(x, by, text, cls, anchor) for text, cls, by in rows]
        rect = (min(b[0] for b in boxes), boxes[0][1], max(b[2] for b in boxes), boxes[-1][3])
        if halo:
            rect = (rect[0] - HALO, rect[1] - HALO, rect[2] + HALO, rect[3] + HALO)
            self.parts.append(f'<rect class="bd" x="{num(rect[0])}" y="{num(rect[1])}" '
                              f'width="{num(rect[2] - rect[0])}" '
                              f'height="{num(rect[3] - rect[1])}" rx="3"/>')
            self.labels.append((rect, True))
        for text, cls, by in rows:
            self.text(x, by, text, cls, anchor, track=not halo)
        return rect

    def lines_in(self, box: Box, lines, left_pad=0.0, align="middle", fits=None):
        """Centre lines vertically in box; each must fit between the pads."""
        y = box.cy - block_height(lines) / 2
        lo, hi = box.x + PAD_X + left_pad, box.right - PAD_X
        x = lo if align == "start" else (lo + hi) / 2
        for spec in lines:
            text, cls = parse(spec)
            size, _, _, line_h = TEXT[cls]
            w = width_of(text, cls)
            left = x if align == "start" else x - w / 2
            if left < lo - 0.01 or left + w > hi + 0.01:
                self.fail(f"{text!r} overflows its {num(box.w)}-wide shape")
            if fits and not fits((left, y + 1, left + w, y + line_h - 1)):
                self.fail(f"{text!r} leaves its shape")
            self.text(x, y + line_h / 2 + size * 0.36, text, cls, align, track=False)
            y += line_h

    # ----- shapes -----
    def solid(self, rect):
        self.solids.append(rect)
        self.grow(rect[3])

    def rect(self, cls, x, y, w, h, rx):
        self.parts.append(f'<rect class="{cls}" x="{num(x)}" y="{num(y)}" '
                          f'width="{num(w)}" height="{num(h)}" rx="{num(rx)}"/>')

    def path(self, cls, d, outline=False):
        fill = ' style="fill:none"' if outline else ""  # beats the class fill
        self.parts.append(f'<path class="{cls}"{fill} d="{d}"/>')

    def node(self, x, y, w, lines, kind="plain", shape="rect", h=None, dashed=False,
             spread=10):
        """A labelled shape; returns the box arrows attach to (a folder's body
        starts 8 below its top, right of the tab)."""
        need = 2 * PAD_Y + block_height(lines) + {"store": 14, "folder": 8}.get(shape, 0)
        h = need if h is None else h
        if h < need - 0.01:
            self.fail(f"shape at {num(x)},{num(y)} needs height {num(need)}")
        box = text_box = Box(x, y, w, h)
        cls = f"k-{kind}" + (" dash" if dashed else "")
        left_pad, align, extents = 0.0, "middle", [(x, y, x + w, y + h)]
        if shape in ("rect", "round"):
            self.rect(cls, x, y, w, h, 6 if shape == "rect" else min(14, h / 2))
        elif shape == "stack":  # several instances: two copies behind, up and right
            for off in (spread, spread / 2, 0):
                self.rect(cls, x + off, y - off, w, h, 6)
            extents = [(x, y - spread, x + w + spread, y + h)]
        elif shape == "store":  # a cylinder: a data store
            r = f"{num(w / 2)},7 0 0"
            self.path(cls, f"M{num(x)},{num(y + 7)} A{r} 1 {num(x + w)},{num(y + 7)} "
                           f"V{num(y + h - 7)} A{r} 1 {num(x)},{num(y + h - 7)} Z")
            self.path(cls, f"M{num(x)},{num(y + 7)} A{r} 0 {num(x + w)},{num(y + 7)}", True)
            text_box = Box(x, y + 14, w, h - 14)
        elif shape == "folder":
            tab = x + 70
            self.path(cls, f"M{num(x)},{num(y + h - 5)} V{num(y + 5)} Q{num(x)},{num(y)} "
                           f"{num(x + 5)},{num(y)} H{num(tab - 8)} L{num(tab)},{num(y + 8)} "
                           f"H{num(x + w - 5)} Q{num(x + w)},{num(y + 8)} {num(x + w)},{num(y + 13)} "
                           f"V{num(y + h - 5)} Q{num(x + w)},{num(y + h)} {num(x + w - 5)},{num(y + h)} "
                           f"H{num(x + 5)} Q{num(x)},{num(y + h)} {num(x)},{num(y + h - 5)} Z")
            text_box = Box(x, y + 8, w, h - 8)
            extents = [(x, y, tab, y + 8), (x, y + 8, x + w, y + h)]
        elif shape == "note":  # a UML note: folded corner
            f = x + w - 12
            self.path(cls, f"M{num(x)},{num(y)} H{num(f)} L{num(x + w)},{num(y + 12)} "
                           f"V{num(y + h)} H{num(x)} Z")
            self.path(cls, f"M{num(f)},{num(y)} V{num(y + 12)} H{num(x + w)}", True)
            align = "start"
        elif shape == "person":  # a box with a head-and-shoulders glyph
            self.rect(cls, x, y, w, h, 6)
            gx, gy = x + 24, y + h / 2
            self.parts.append(f'<circle class="{cls}" cx="{num(gx)}" cy="{num(gy - 6)}" r="5"/>')
            self.path(cls, f"M{num(gx - 9)},{num(gy + 11)} Q{num(gx - 9)},{num(gy + 1)} "
                           f"{num(gx)},{num(gy + 1)} Q{num(gx + 9)},{num(gy + 1)} "
                           f"{num(gx + 9)},{num(gy + 11)} Z")
            left_pad = 28
        else:
            self.fail(f"unknown shape {shape}")
        for extent in extents:
            self.solid(extent)
        self.lines_in(text_box, lines, left_pad, align)
        return box

    def actor(self, cx, top, name):
        """A UML actor: a stick figure with its name below."""
        self.parts.append(f'<circle class="ring" cx="{num(cx)}" cy="{num(top + 7)}" r="7"/>')
        self.path("ring", f"M{num(cx)},{num(top + 14)} V{num(top + 30)} "
                          f"M{num(cx - 11)},{num(top + 20)} H{num(cx + 11)} "
                          f"M{num(cx - 9)},{num(top + 42)} L{num(cx)},{num(top + 30)} "
                          f"L{num(cx + 9)},{num(top + 42)}")
        self.solid((cx - 12, top, cx + 12, top + 42))
        self.text(cx, top + 58, name, "b", "middle")

    def diamond(self, cx, cy, w, h, lines=()):
        """A decision or merge node, optionally with its question inside."""
        a, b = w / 2, h / 2
        self.parts.append(f'<polygon class="k-plain" points="'
                          f'{pts([(cx, cy - b), (cx + a, cy), (cx, cy + b), (cx - a, cy)])}"/>')
        self.diamonds.append((cx, cy, a, b))
        self.grow(cy + b)
        box = Box(cx - a, cy - b, w, h)
        if lines:
            self.lines_in(box, lines, fits=lambda r: all(
                abs(px - cx) / a + abs(py - cy) / b <= 1
                for px in (r[0], r[2]) for py in (r[1], r[3])))
        return box

    def frame(self, x, y, w, h, name):
        """A UML composite state, named inside its top right corner."""
        # Inserted first so it sits behind the states drawn before it.
        self.parts.insert(0, f'<rect class="k-frame" x="{num(x)}" y="{num(y)}" '
                             f'width="{num(w)}" height="{num(h)}" rx="16"/>')
        self.frames.append((x, y, x + w, y + h))
        self.text(x + w - 16, y + 22, name, "n", "end")
        self.grow(y + h)
        return Box(x, y, w, h)

    def initial(self, cx, cy):
        self.parts.append(f'<circle class="dot" cx="{num(cx)}" cy="{num(cy)}" r="8"/>')
        self.solid((cx - 8, cy - 8, cx + 8, cy + 8))

    def final(self, cx, from_y, gap=33):
        """A final node below from_y."""
        cy = from_y + gap + 11
        self.line((cx, from_y), (cx, cy - 11))
        self.parts.append(f'<circle class="ring" cx="{num(cx)}" cy="{num(cy)}" r="11"/>')
        self.parts.append(f'<circle class="dot" cx="{num(cx)}" cy="{num(cy)}" r="6.5"/>')
        self.solid((cx - 11, cy - 11, cx + 11, cy + 11))
        return cy

    def step(self, x, cy, n, lines):
        """A numbered badge beside its lines, left-aligned at x; the lines are
        centred on cy and the badge sits on the first."""
        top = cy - (len(lines) - 1) * LINE / 2
        self.parts.append(f'<circle class="badge" cx="{num(x + 10)}" cy="{num(top)}" r="10"/>')
        self.text(x + 10, top + 5, str(n), "n", "middle", track=False, extra="bt")
        self.labels.append(((x, top - 10, x + 20, top + 10), False))
        for i, text in enumerate(lines):
            self.text(x + 26, top + 5 + i * LINE, text, "t")

    # ----- lines -----
    def line(self, *points, dashed=False, head="solid", soft=False):
        """An orthogonal polyline; `head` is solid, open (a reply) or None.
        Soft lines (lifelines) skip the shape and crossing checks; only a halo
        label may sit on one."""
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            if x0 != x1 and y0 != y1:
                self.fail("lines are orthogonal")
            seg = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
            (self.soft if soft else self.segments).append(seg)
            self.grow(max(y0, y1))
        path = list(points)
        if head:
            (x0, y0), (x1, y1) = path[-2:]
            length = abs(x1 - x0) + abs(y1 - y0)
            dx, dy = (x1 - x0) / length, (y1 - y0) / length
            bx, by = x1 - 10 * dx, y1 - 10 * dy
            wing = [(bx - 5 * dy, by + 5 * dx), (x1, y1), (bx + 5 * dy, by - 5 * dx)]
            if head == "solid":
                path[-1] = (x1 - 8 * dx, y1 - 8 * dy)
        cls = "life" if soft else "ln dash" if dashed else "ln"
        self.parts.append(f'<polyline class="{cls}" points="{pts(path)}"/>')
        if head == "solid":
            self.parts.append(f'<polygon class="hd" points="{pts(wing)}"/>')
        elif head == "open":
            self.parts.append(f'<polyline class="ln" points="{pts(wing)}"/>')

    # ----- checks and output -----
    def check(self, height):
        canvas = (8, 8, self.width - 8, height - 8)
        shapes = self.solids + [(cx - a, cy - b, cx + a, cy + b)
                                for cx, cy, a, b in self.diamonds]
        labels = [rect for rect, _ in self.labels]
        for rect in shapes + labels + self.frames:
            if not inside(rect, canvas):
                self.fail(f"{show(rect)} leaves the canvas")
        for i, rect in enumerate(shapes):
            for other in shapes[i + 1:]:
                if overlap(rect, other):
                    self.fail(f"shapes {show(rect)} and {show(other)} overlap")
        for i, rect in enumerate(labels):
            for other in labels[i + 1:]:
                if overlap(rect, other):
                    self.fail(f"labels {show(rect)} and {show(other)} overlap")
            if any(overlap(rect, s) for s in self.solids) or any(
                    hits_diamond(rect, d) for d in self.diamonds):
                self.fail(f"label {show(rect)} overlaps a shape")
            if any(overlap(rect, f) and not inside(rect, f) for f in self.frames):
                self.fail(f"label {show(rect)} straddles a frame edge")
        for seg in self.segments:
            if any(overlap(seg, (s[0] + 1, s[1] + 1, s[2] - 1, s[3] - 1))
                   for s in self.solids) or any(hits_diamond(seg, d) for d in self.diamonds):
                self.fail(f"line {show(seg)} crosses a shape")
        for seg in self.segments + self.soft:
            for rect, halo in self.labels:
                if not halo and overlap(seg, rect):
                    self.fail(f"line {show(seg)} crosses label {show(rect)}")
        for h in (s for s in self.segments if s[1] == s[3]):
            for v in (s for s in self.segments if s[0] == s[2]):
                if h[0] < v[0] < h[2] and v[1] < h[1] < v[3]:
                    self.fail(f"lines {show(h)} and {show(v)} cross")

    def render(self) -> str:
        height = round(self.bottom + 24)
        self.check(height)
        smallest = min(size for size, *_ in TEXT.values()) * PHONE_COLUMN / self.width
        if smallest < PHONE_MIN_PX:
            self.fail(f"text renders at {smallest:.1f}px in a {PHONE_COLUMN}px phone column")
        w, h = num(self.width), num(height)
        return "\n".join([
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}" role="img">',
            f"<title>{escape(self.title)}</title>",
            f"<desc>{escape(self.desc)}</desc>",
            f"<style>\n{stylesheet()}\n</style>",
            f'<rect class="bg" x="0.75" y="0.75" width="{num(self.width - 1.5)}" '
            f'height="{num(height - 1.5)}" rx="14"/>',
            *self.parts,
            "</svg>",
        ]) + "\n"


# ---------- the diagrams ----------

def overview() -> Diagram:
    d = Diagram(
        "overview", WIDTH, "How a council works",
        "You ask Claude. Claude decides, within your instructions, and has the runner "
        "freeze a snapshot of your project. Each Codex worker works on its own copy of that "
        "snapshot and never changes your live files. Replies and evidence go to the run "
        "folder, which keeps them and the snapshot across restarts; Claude reads the "
        "results and answers you. Optional, experimental notes of Claude's position can "
        "be kept beside Claude; they are never required.")
    x, w, narrow = 24, 206, 180
    down, up = 48, 222  # the forward lane; the return lane, clear of the narrow shapes

    you = d.node(x, 64, w, ["*You", "_set the goal", "_and the limits"], "you", "person")
    claude = d.node(x, you.bottom + GAP, w,
                    ["*Claude", "decides, within your", "instructions"], "claude")
    snap = d.node(x, claude.bottom + GAP, narrow,
                  ["*Frozen snapshot", "*of your project", "_workers never change",
                   "_your live files"], "store", "store")
    workers = d.node(x, snap.bottom + GAP + 6, narrow - 10,
                     ["*Codex workers", "_each on its", "_own copy"], "work", "stack")
    folder = d.node(x, workers.bottom + GAP + 10, w,
                    ["*Run folder", "_holds the snapshot,", "_replies and evidence,",
                     "_even across restarts"], "store", "folder")

    def between(a, b, top=None):
        return ((a.bottom + (b.top if top is None else top)) / 2)

    d.line((down, you.bottom), (down, claude.top))
    d.step(down + 10, between(you, claude), 1, ["ask"])
    d.line((down, claude.bottom), (down, snap.top))
    d.step(down + 10, between(claude, snap), 2, ["runner freezes", "a snapshot"])
    d.line((down, snap.bottom), (down, workers.top - 10))  # the back copy's edge
    d.step(down + 10, between(snap, workers, workers.top - 10), 3, ["a copy per worker"])
    d.line((down, workers.bottom), (down, folder.top))
    d.step(down + 10, between(workers, folder), 4, ["replies and", "evidence"])
    d.line((up, folder.top + 8), (up, claude.bottom))  # the body, right of the tab
    d.step(up + 10, between(claude, folder), 5, ["results"])
    d.line((up, claude.top), (up, you.bottom))
    d.step(up + 10, between(you, claude), 6, ["answer"])

    # Level with Claude's top, so it hangs clear of the answer step above.
    note = d.node(claude.right + 18, claude.top, WIDTH - 24 - claude.right - 18,
                  ["*Optional notes", "*of Claude's", "*position", "_experimental;",
                   "_never required"], "frame", "note", dashed=True)
    d.line((claude.right, claude.cy), (note.left, claude.cy), dashed=True, head=None)
    return d


def run_lifecycle() -> Diagram:
    d = Diagram(
        "run-lifecycle", WIDTH, "Life of a run",
        "UML state machine of one run folder. Claude asks for a new run (Created). Claude "
        "writes the brief and the runner freezes your files (Snapshot taken). If the check "
        "fails, change model, effort or selection and check again, or leave the run for a "
        "new one. Once the check passes (Checked), start makes it Running. It ends Finished "
        "when the workers finish or the runner fails (--status reports done or aborted), "
        "Stopped when cancelled or shut down cleanly (interrupted), or Runner gone when "
        "the runner is killed or the power is lost (gone). A run is removed only when you "
        "prune old runs (30 days by default), whether it ended or was left unstarted.")
    spine, sw = 106, 144  # the column of states before launch
    top, lane = 64, 380   # lane: the prune of a run left unstarted

    d.initial(spine, top + 26)
    created = d.node(spine - sw / 2, top + 64, sw,
                     ["*Created", "_an empty", "_private folder"], shape="round")
    d.line((spine, top + 34), (spine, created.top))
    d.label(spine + 10, (top + 34 + created.top) / 2 + 5, ["Claude asks for a new run"])
    snap = d.node(spine - sw / 2, created.bottom + 72, sw,
                  ["*Snapshot taken", "_your files, frozen"], shape="round")
    fix = d.node(snap.right + 56, snap.top, 150,  # the gap fits "check" over "fails"
                 ["*Needs a fix", "_change model,", "_effort or selection,",
                  "_or leave it for", "_a new run"], "warn", "round")
    checked = d.node(spine - sw / 2, snap.bottom + 62, sw, ["*Checked", "_ready to start"],
                     shape="round")
    waiting = d.frame(24, top, WIDTH - 48, max(checked.bottom, fix.bottom) + 18 - top,
                      "Not started yet")

    d.line((spine, created.bottom), (spine, snap.top))
    d.label(spine + 10, created.bottom + 22,
            ["Claude writes the brief;", "the runner freezes your files"])
    gap_x = (snap.right + fix.left) / 2
    ascent, descent = TEXT["s"][0] * 0.8, TEXT["s"][0] * 0.25  # as in extent()
    d.line((snap.right, snap.cy - 8), (fix.left, snap.cy - 8))  # labels 4 clear of each
    d.label(gap_x, snap.cy - 8 - 4 - descent - LINE, ["check", "fails"], "middle")
    d.line((fix.left, snap.cy + 8), (snap.right, snap.cy + 8))
    d.label(gap_x, snap.cy + 8 + 4 + ascent, ["check", "again"], "middle")
    d.line((spine, snap.bottom), (spine, checked.top))
    d.label(spine + 10, checked.top - 12, ["check passes"])

    running = d.node(24, waiting.bottom + 64, lane - 20 - 24,
                     ["*Running", "_workers busy; replies", "_arrive in the run folder"],
                     "work", "round")
    d.line((spine, checked.bottom), (spine, running.top))
    d.label(spine + 10, (waiting.bottom + running.top) / 2 + 5, ["start"])
    d.label(lane - 10, waiting.bottom + 22, ["left unstarted:", "you prune old runs too"],
            "end")

    # Two ends side by side and the third below, between them; the code lines
    # are what --status reports.
    ended_top, sw2 = running.bottom + 66, 138
    ended_right = running.right
    finished = d.node(36, ended_top + 38, sw2, ["*Finished", "_results kept", "`done, aborted"],
                      shape="round")
    stopped = d.node(ended_right - 12 - sw2, ended_top + 38, sw2,
                     ["*Stopped", "_work so far kept", "`interrupted"], shape="round")
    mid = (finished.right + stopped.left) / 2
    gone = d.node(mid - sw2 / 2, finished.bottom + 56, sw2,
                  ["*Runner gone", "_evidence kept", "`gone"], "warn", "round")
    ended = d.frame(24, ended_top, ended_right - 24, gone.bottom + 16 - ended_top, "Ended")
    for state, event in ((finished, ["workers finish,", "or the runner fails"]),
                         (stopped, ["cancelled, or", "shut down cleanly"])):
        d.line((state.cx, running.bottom), (state.cx, state.top))
        d.label(state.cx, (running.bottom + ended_top) / 2 - 4, event, "middle", halo=True)
    d.line((mid, running.bottom), (mid, gone.top))
    d.label(mid, (finished.bottom + gone.top) / 2 - 4, ["runner killed", "or power lost"],
            "middle", halo=True)

    # Nothing is removed except by an explicit prune of old runs.
    removed_y = ended.bottom + 70
    d.line((ended.cx, ended.bottom), (ended.cx, removed_y), (lane - 11, removed_y))
    d.label(ended.cx + 10, ended.bottom + 20, ["you prune old runs", "(30 days by default)"])
    d.final(lane, waiting.bottom, removed_y - 11 - waiting.bottom)
    d.text(lane + 11, removed_y + 32, "Removed", "b", "end")
    return d


def council_sequence() -> Diagram:
    d = Diagram(
        "council-sequence", WIDTH, "One council, step by step",
        "UML sequence diagram with lifelines You, Claude, Runner and run folder, and Codex "
        "workers. You ask Claude. Claude asks the runner for a new run, and the runner "
        "creates the run folder. Claude writes the brief into it and asks the runner to "
        "snapshot the files; the runner stores the snapshot and a copy per worker. Claude "
        "checks, then starts; the runner starts each worker on its own copy; each worker "
        "replies with evidence, which the runner saves to the run folder. Claude reads the "
        "results, checks freshness with the runner, weighs the evidence, and answers you.")
    xs = [40, 118, 230, 344]
    you, claude, runner, workers = range(4)
    head_top, head_h = 70, 56
    d.actor(xs[you], head_top - 6, "You")
    d.node(xs[claude] - 40, head_top, 80, ["*Claude"], "claude", h=head_h)
    d.node(xs[runner] - 62, head_top, 124, ["*Runner", "_and run folder"], "runner", h=head_h)
    d.node(xs[workers] - 43, head_top, 86, ["*Codex", "*workers"], "work", "stack",
           h=head_h, spread=6)

    reply = True
    messages = (
        (you, claude, ["ask"]),
        (claude, runner, ["new run"]),
        (runner, runner, ["create the", "run folder"]),
        (claude, runner, ["write the brief", "into the folder"]),
        (claude, runner, ["snapshot", "the files"]),
        (runner, runner, ["store the", "snapshot", "and a copy", "per worker"]),
        (claude, runner, ["check,", "then start"]),
        (runner, workers, ["start each", "worker on its", "own copy"]),
        (workers, runner, ["replies and", "evidence"], reply),
        (runner, runner, ["save to the", "run folder"]),
        (claude, runner, ["read the", "results"]),
        (claude, runner, ["check", "freshness"]),
        (claude, claude, ["weigh the", "evidence"]),
        (claude, you, ["answer"], reply),
    )
    y, rows = head_top + head_h + 20, []
    for a, b, text, *is_reply in messages:
        arrow_y = y + 4 if a == b else y + LINE * len(text) + 4
        rows.append((a, b, text, bool(is_reply), y, arrow_y))
        y = arrow_y + (max(44, LINE * len(text) + 4) if a == b else 16)
    for i, x in enumerate(xs):
        d.line((x, head_top + head_h + (4 if i == you else 2)), (x, y + 4), head=None,
               soft=True)
    for a, b, text, is_reply, label_top, arrow_y in rows:
        if a == b:  # a self-message: a loop to the right
            x = xs[a]
            d.line((x, arrow_y), (x + 22, arrow_y), (x + 22, arrow_y + 16),
                   (x + 1, arrow_y + 16))
            rect = d.label(x + 30, arrow_y + 5, text, halo=True)
        else:
            d.line((xs[a], arrow_y), (xs[b], arrow_y), dashed=is_reply,
                   head="open" if is_reply else "solid")
            rect = d.label((xs[a] + xs[b]) / 2, label_top + 12, text, "middle", halo=True)
        # A label may cover only the lifelines its message passes over.
        if any(rect[0] < x < rect[2] and not min(a, b) < i < max(a, b)
               for i, x in enumerate(xs)):
            d.fail(f"{text[0]!r} crosses a lifeline its message does not pass")

    legend_y = y + 30
    d.line((24, legend_y), (64, legend_y))
    d.text(74, legend_y + 5, "request", "s")
    d.line((160, legend_y), (200, legend_y), dashed=True, head="open")
    d.text(210, legend_y + 5, "reply", "s")
    return d


def continuity() -> Diagram:
    d = Diagram(
        "continuity", WIDTH, "Fresh or continued thread",
        "UML activity diagram. Each worker starts a fresh Codex thread by default. Only "
        "when asked to continue, and when a thread is saved for the same project, task, "
        "session and role, does it continue that thread. If Codex no longer has the "
        "saved thread, the worker starts fresh with a warning that earlier turns are "
        "lost when repeating is safe; otherwise it stops and keeps the thread, so what "
        "it did can be checked before trying again.")
    L, R = 104, 306  # column centres: outcomes on the left, the questions on the right
    dw, dh, lw, rw = 150, 80, 150, 160  # diamonds; left and right column shapes

    d.initial(L, 72)
    ask = d.diamond(L, 150, dw, dh, ["Asked to", "continue?"])
    d.line((L, 80), (L, ask.top))
    saved = d.diamond(R, ask.cy + 140, dw, dh, ["Saved", "thread?"])
    d.line((ask.right, ask.cy), (R, ask.cy), (R, saved.top))
    d.label((ask.right + R) / 2, ask.cy - 8, ["yes"], "middle")

    merge = d.diamond(L, saved.cy, 26, 26)
    d.line((L, ask.bottom), (L, merge.top))
    d.label(L + 10, (ask.bottom + merge.top) / 2 + 5, ["*no (the default)"])
    d.line((saved.left, saved.cy), (merge.right, saved.cy))
    d.label(saved.left - 10, saved.cy - 8, ["no"], "end")
    d.label(R - 10, saved.bottom + 18, ["(for the same project,", "task, session and role)"],
            "end")
    fresh = d.node(L - lw / 2, saved.bottom + 50, lw, ["*Start a fresh", "*thread"], "work",
                   "round")
    d.line((L, merge.bottom), (L, fresh.top))
    fresh_end = d.final(L, fresh.bottom)

    cont = d.node(R - rw / 2, saved.bottom + 50, rw, ["*Continue the", "*saved thread"],
                  "work", "round")
    d.line((R, saved.bottom), (R, cont.top))
    d.label(R + 10, saved.bottom + 18, ["yes"])

    # Each outcome on the left is level with the question that leads to it.
    def beside(question, lines, kind):
        h = 2 * PAD_Y + block_height(lines)
        box = d.node(L - lw / 2, question.cy - h / 2, lw, lines, kind, "round")
        d.line((question.left, question.cy), (box.right, question.cy))
        d.label((question.left + box.right) / 2, question.cy - 8, ["yes"], "middle")
        return box, d.final(L, box.bottom)

    keep_lines = ["*Keep working", "*in that thread"]
    there = d.diamond(R, max(cont.bottom + 34 + dh / 2,
                             fresh_end + 27 + block_height(keep_lines) / 2 + PAD_Y),
                      dw, dh, ["Codex still", "has it?"])
    d.line((R, cont.bottom), (R, there.top))
    keep_end = beside(there, keep_lines, "work")[1]

    warn_lines = ["*Start fresh", "_and warn that", "_earlier turns", "_are lost"]
    safe = d.diamond(R, max(there.bottom + 56 + dh / 2,
                            keep_end + 27 + block_height(warn_lines) / 2 + PAD_Y),
                     dw, dh, ["Safe to", "repeat?"])
    d.line((R, there.bottom), (R, safe.top))
    d.label(R + 10, (there.bottom + safe.top) / 2 - 4, ["no, it is", "missing"])
    beside(safe, warn_lines, "warn")
    stop = d.node(R - rw / 2, safe.bottom + 40, rw,
                  ["*Stop this worker", "_keep the thread;", "_check what it did",
                   "_before trying again"], "stop", "round")
    d.line((R, safe.bottom), (R, stop.top))
    d.label(R + 10, safe.bottom + 18, ["no"])
    d.final(R, stop.bottom)
    return d


DIAGRAMS = (overview, run_lifecycle, council_sequence, continuity)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if a committed SVG differs from the generated one")
    args = parser.parse_args(argv)
    stale = []
    for build in DIAGRAMS:
        diagram = build()
        path, svg = HERE / f"{diagram.name}.svg", diagram.render()
        if path.exists() and path.read_text(encoding="utf-8") == svg:
            continue
        if args.check:
            stale.append(path.name)
        else:
            path.write_text(svg, encoding="utf-8", newline="\n")
    if stale:
        print(f"stale diagrams: {', '.join(stale)}; run python3 docs/diagrams/generate.py",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
