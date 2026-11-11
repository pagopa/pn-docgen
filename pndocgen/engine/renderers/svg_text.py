"""Conservative plain-text bounds from D2's embedded WOFF fonts, offline.

This is deliberately not a general SVG/CSS/shaping engine. Unknown text is
reported as unmeasured, never treated as a zero-size label or a passing gate.
No system fonts, browser, font download or name-length heuristics are used.
"""
from __future__ import annotations

import base64
import io
import math
import re
import unicodedata

from fontTools.ttLib import TTFont

from pndocgen.engine.renderers.svg_geometry import NS


class UnsupportedText(ValueError):
    pass


def union(boxes):
    boxes = list(boxes)
    x = min(b[0] for b in boxes)
    y = min(b[1] for b in boxes)
    return x, y, max(b[0] + b[2] for b in boxes) - x, max(b[1] + b[3] for b in boxes) - y


class TextMetrics:
    """Measure the supported D2 plain-text dialect, with a 2px paint guard.

    Browser hinting at a scaled viewport affects getBBox slightly. This guard
    is verified against Chromium on the corpus, not a guarantee for arbitrary
    font engines, zoom factors or SVG dialects.
    """

    def __init__(self, root):
        self.fonts = {}
        self.families = {}
        css = "\n".join(e.text or "" for e in root.iter(NS + "style"))
        for rule in re.finditer(r"\.((?:text)(?:-bold|-italic|-mono)?)\s*\{\s*font-family:\s*\"([^\"]+)\";\s*\}", css):
            self.families[rule[1]] = rule[2]
        for rule in re.finditer(r"@font-face\s*\{([^}]+)\}", css):
            family = re.search(r"font-family:\s*([\w-]+)\s*;", rule[1])
            data = re.search(r"data:application/font-woff;base64,([A-Za-z0-9+/=]+)", rule[1])
            if family and data:
                raw = base64.b64decode(data[1], validate=True)
                # WOFF2 and arbitrary containers need separate validation.
                if raw[:4] != b"wOFF" or len(raw) > 4_000_000:
                    continue
                self.fonts[family[1]] = TTFont(io.BytesIO(raw), recalcBBoxes=False, recalcTimestamp=False)

    def box(self, text):
        if list(text) or any(text.get(a) for a in ("transform", "dx", "dy", "rotate", "textLength", "lengthAdjust")):
            raise UnsupportedText("positioned/shaped text needs a richer measurement model")
        style = dict(re.findall(r"([\w-]+)\s*:\s*([^;]+)", text.get("style", "")))
        if set(style) - {"text-anchor", "font-size"}:
            raise UnsupportedText("unsupported inline text style")
        families = {self.families[c] for c in text.get("class", "").split() if c in self.families}
        if len(families) != 1 or next(iter(families)) not in self.fonts:
            raise UnsupportedText("missing or ambiguous embedded font")
        font = self.fonts[next(iter(families))]
        if any(tag in font for tag in ("GPOS", "GSUB", "kern", "fvar")) or "glyf" not in font:
            raise UnsupportedText("font shaping/kerning/variation requires a richer measurement model")
        size_text = style.get("font-size", "")
        if not re.fullmatch(r"\d+(?:\.\d+)?px", size_text):
            raise UnsupportedText("font size must be explicit px")
        size = float(size_text[:-2])
        x, y = float(text.get("x", "nan")), float(text.get("y", "nan"))
        if not all(math.isfinite(v) for v in (size, x, y)) or size <= 0:
            raise UnsupportedText("invalid text coordinates")
        value = text.text or ""
        # AWS identifiers and Latin labels have direct glyph mapping in D2's
        # subsets. RTL, combining marks and ligature shaping must not be guessed.
        if any(unicodedata.combining(c) or unicodedata.bidirectional(c) in {"R", "AL", "AN"}
               or (ord(c) > 255 and c not in "–—→←…") or ord(c) < 32 for c in value):
            raise UnsupportedText("text requires shaping or whitespace layout")
        cmap = font.getBestCmap() or {}
        advance, left, right = 0.0, 0.0, 0.0
        ascent, descent = font['hhea'].ascent, font['hhea'].descent
        scale = size / font["head"].unitsPerEm
        for char in value:
            if ord(char) not in cmap:
                raise UnsupportedText("glyph missing from embedded font")
            name = cmap[ord(char)]
            glyph = font["glyf"][name]
            if glyph.numberOfContours:
                left = min(left, advance + glyph.xMin)
                right = max(right, advance + glyph.xMax)
                ascent = max(ascent, glyph.yMax)
                descent = min(descent, glyph.yMin)
            advance += font["hmtx"].metrics[name][0]
        right = max(right, advance)
        anchor = style.get("text-anchor", "start")
        if anchor not in {"start", "middle", "end"}:
            raise UnsupportedText("unsupported text anchor")
        origin = x - advance * scale * {"start": 0, "middle": .5, "end": 1}[anchor]
        return (origin + left * scale - 2, y - ascent * scale - 2,
                (right - left) * scale + 4, (ascent - descent) * scale + 4)

    def measure(self, diagram):
        boxes, unknown = {}, []
        for index, text in enumerate(diagram.tree.getroot().iter(NS + "text")):
            try:
                boxes[text] = self.box(text)
            except (UnsupportedText, ValueError, KeyError) as exc:
                unknown.append({"text_index": index, "reason": str(exc)})
        return boxes, unknown
