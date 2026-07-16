from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.ttLib import TTCollection


SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)


class Typeface:
    def __init__(self, path: str, regular_index: int, bold_index: int) -> None:
        collection = TTCollection(path)
        self.regular = collection.fonts[regular_index]
        self.bold = collection.fonts[bold_index]

    def choose(self, bold: bool):
        return self.bold if bold else self.regular


TYPEFACES = {
    "sans": Typeface("/System/Library/Fonts/Helvetica.ttc", 0, 1),
    "mono": Typeface("/System/Library/Fonts/Menlo.ttc", 0, 1),
}


def number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def outline_text(element: ET.Element) -> ET.Element:
    value = element.text or ""
    attributes = element.attrib
    font_size = float(attributes["font-size"].removesuffix("px"))
    is_mono = "Menlo" in attributes.get("font-family", "")
    weight = attributes.get("font-weight", "400")
    is_bold = weight in {"bold", "600", "700", "800", "900"}
    font = TYPEFACES["mono" if is_mono else "sans"].choose(is_bold)
    glyph_set = font.getGlyphSet()
    cmap = font.getBestCmap()
    metrics = font["hmtx"].metrics
    scale = font_size / font["head"].unitsPerEm

    glyphs: list[tuple[str, int]] = []
    total_advance = 0
    for character in value:
        glyph_name = cmap.get(ord(character), ".notdef")
        advance = metrics[glyph_name][0]
        glyphs.append((glyph_name, advance))
        total_advance += advance

    x = float(attributes.get("x", "0"))
    y = float(attributes.get("y", "0"))
    anchor = attributes.get("text-anchor", "start")
    if anchor == "middle":
        x -= total_advance * scale / 2
    elif anchor == "end":
        x -= total_advance * scale

    group = ET.Element(
        f"{{{SVG_NS}}}g",
        {
            "fill": attributes.get("fill", "#000000"),
            "transform": (
                f"translate({number(x)} {number(y)}) "
                f"scale({number(scale)} {number(-scale)})"
            ),
        },
    )
    cursor = 0
    for glyph_name, advance in glyphs:
        pen = SVGPathPen(glyph_set)
        glyph_set[glyph_name].draw(pen)
        path_data = pen.getCommands()
        if path_data:
            path = ET.SubElement(group, f"{{{SVG_NS}}}path", {"d": path_data})
            if cursor:
                path.set("transform", f"translate({cursor} 0)")
        cursor += advance
    return group


def process(path: Path) -> None:
    tree = ET.parse(path)
    root = tree.getroot()
    text_tag = f"{{{SVG_NS}}}text"
    for parent in root.iter():
        for index, child in enumerate(list(parent)):
            if child.tag == text_tag:
                parent.remove(child)
                parent.insert(index, outline_text(child))
    tree.write(path, encoding="utf-8", xml_declaration=True)


for argument in sys.argv[1:]:
    process(Path(argument))
