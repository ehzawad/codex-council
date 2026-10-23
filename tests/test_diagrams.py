"""The README diagrams: generated, self-contained SVG kept outside the plugin."""
import ast
import importlib.util
import os
import re
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIAGRAMS = ROOT / "docs/diagrams"
GENERATOR = DIAGRAMS / "generate.py"
NAMES = ("overview", "run-lifecycle", "council-sequence", "continuity")
SVG = "http://www.w3.org/2000/svg"
# github.com's README column on a 390px phone: a 16px gutter, a 1px border and
# 16px padding on each side.
PHONE_COLUMN, PHONE_MIN_PX = 324, 11
TEXT_SUFFIXES = {".md", ".py", ".svg", ".json", ".yml", ".yaml", ".toml", ".txt", ".html"}
SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules"}


def svg_text(name):
    return (DIAGRAMS / f"{name}.svg").read_text(encoding="utf-8")


def load_generator():
    spec = importlib.util.spec_from_file_location("readme_diagrams", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # keep docs/ clean
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def class_font_sizes(style):
    """{class: px} for every rule in style that sets a font size."""
    sizes = {}
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", style):
        size = re.search(r"font-size:\s*([\d.]+)px", body)
        if size:
            for selector in selectors.split(","):
                selector = selector.strip()
                if not re.fullmatch(r"\.[\w-]+", selector):
                    raise AssertionError(f"font size set on {selector!r}, not one class")
                sizes[selector[1:]] = float(size.group(1))
    return sizes


class DiagramTests(unittest.TestCase):
    def test_committed_svgs_match_the_generator(self):
        run = subprocess.run([sys.executable, str(GENERATOR), "--check"], cwd=ROOT,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_generation_is_deterministic_and_standard_library_only(self):
        module = load_generator()
        self.assertEqual([build().name for build in module.DIAGRAMS], list(NAMES))
        for build in module.DIAGRAMS:
            self.assertEqual(build().render(), build().render())
        imported = set()
        for node in ast.walk(ast.parse(GENERATOR.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                imported.add(node.module.split(".")[0])
        self.assertLessEqual(imported, set(sys.stdlib_module_names) | {"__future__"})

    def test_each_svg_is_self_contained_and_themed(self):
        self.assertEqual(sorted(p.stem for p in DIAGRAMS.glob("*.svg")), sorted(NAMES))
        for name in NAMES:
            with self.subTest(diagram=name):
                text = svg_text(name)
                root = ET.fromstring(text)
                self.assertEqual(root.tag, f"{{{SVG}}}svg")
                x, y, width, height = map(float, root.get("viewBox").split())
                self.assertEqual((x, y), (0, 0))
                self.assertGreater(height, 0)
                for element in root.iter():
                    tag = element.tag.rsplit("}", 1)[-1].lower()
                    self.assertNotIn(tag, {"script", "foreignobject", "image", "use"})
                    for attr in element.attrib:
                        attr = attr.rsplit("}", 1)[-1].lower()
                        self.assertFalse(attr.endswith("href") or attr.startswith("on"), attr)
                lowered = text.lower()
                for token in ("url(", "@import", "<script", "javascript:"):
                    self.assertNotIn(token, lowered)
                self.assertEqual(re.findall(r"https?://[^\s\"'<>)]+", text), [SVG])
                # Its own rounded background, light by default, dark on request.
                background = root.find(f"{{{SVG}}}rect")
                self.assertEqual(background.get("class"), "bg")
                self.assertTrue(background.get("rx"))
                self.assertEqual(text.count("@media (prefers-color-scheme: dark)"), 1)
                # A desktop shows it at its own width; a phone scales it down.
                self.assertEqual(float(root.get("width")), width)
                self.assertLessEqual(width, 412)

    def test_text_is_legible_in_a_phone_column_without_zooming(self):
        # Scaled to fit a phone's README column, every line stays at least
        # PHONE_MIN_PX tall: font_size * PHONE_COLUMN / viewBox width.
        rendered = []  # (px, diagram, text)
        for name in NAMES:
            with self.subTest(diagram=name):
                text = svg_text(name)
                self.assertNotIn("font-size=", text)
                root = ET.fromstring(text)
                width = float(root.get("viewBox").split()[2])
                sizes = class_font_sizes(root.find(f"{{{SVG}}}style").text)
                texts = list(root.iter(f"{{{SVG}}}text"))
                self.assertTrue(texts)
                for element in texts:
                    self.assertNotIn("font", element.get("style", ""))
                    sized = [sizes[c] for c in element.get("class", "").split() if c in sizes]
                    self.assertEqual(len(sized), 1, f"{element.text!r} needs one sizing class")
                    rendered.append((sized[0] * PHONE_COLUMN / width, name, element.text))
        px, name, line = min(rendered)
        self.assertGreaterEqual(px, PHONE_MIN_PX, f"{name}: {line!r} renders at {px:.2f}px")

    def test_generator_refuses_text_too_small_for_a_phone(self):
        module = load_generator()
        self.assertEqual((module.PHONE_COLUMN, module.PHONE_MIN_PX),
                         (PHONE_COLUMN, PHONE_MIN_PX))
        module.WIDTH = 420  # the previous width: 14px text would render at 10.8px
        with self.assertRaisesRegex(module.LayoutError, "phone column"):
            module.overview().render()

    def test_diagrams_live_outside_the_installed_plugin(self):
        plugins = (ROOT / "plugins").resolve()
        self.assertFalse(DIAGRAMS.resolve().is_relative_to(plugins))
        self.assertFalse(any(plugins.rglob("*.svg")))

    def test_readme_embeds_every_diagram_with_alt_text_and_no_ascii_drawing(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        images = {path: alt for alt, path in re.findall(r"!\[([^\]]*)\]\(([^)\s]+)\)", readme)}
        for name in NAMES:
            with self.subTest(diagram=name):
                alt = images.get(f"docs/diagrams/{name}.svg")
                self.assertIsNotNone(alt, f"README does not embed {name}.svg")
                self.assertGreaterEqual(len(alt.split()), 10)
        fences = re.findall(r"(?m)^```(.*)$", readme)
        self.assertEqual(len(fences) % 2, 0)
        self.assertTrue(all(fences[0::2]), "every README code block names its language")

    def test_no_diagram_dsl_anywhere(self):
        needle = "mer" + "maid"  # split so this file does not match itself
        for directory, subdirs, files in os.walk(ROOT):
            subdirs[:] = [d for d in subdirs if d not in SKIP_DIRS]
            for file in files:
                path = Path(directory, file)
                if path.suffix in TEXT_SUFFIXES:
                    text = path.read_text(encoding="utf-8", errors="replace").lower()
                    self.assertFalse(needle in text, str(path.relative_to(ROOT)))


if __name__ == "__main__":
    unittest.main()
