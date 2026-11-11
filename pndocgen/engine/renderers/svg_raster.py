"""Rasterize the final D2 SVG with pinned resvg and its own embedded fonts."""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
import struct
import tempfile
import xml.etree.ElementTree as ET
import zlib

from pndocgen.engine.renderers.assets import SVG_NOTICE_ID
from pndocgen.engine.renderers.svg_geometry import NS
from pndocgen.engine.renderers.svg_text import TextMetrics, UnsupportedText

RESVG_PY_VERSION = "0.5.0"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def raster_backend():
    try:
        import resvg_py
    except ImportError as exc:
        raise RuntimeError("PNG requires the optional renderer: pip install 'pn-docgen[raster]'") from exc
    if resvg_py.__version__ != RESVG_PY_VERSION:
        raise RuntimeError(f"PNG requires resvg-py=={RESVG_PY_VERSION}; found {resvg_py.__version__}")
    return resvg_py


def _text_chunk(key: str, value: str) -> bytes:
    data = key.encode("ascii") + b"\0" + value.encode("latin-1")
    payload = b"tEXt" + data
    return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload))


def rasterize_svg(source: Path, output: Path) -> Path:
    """Write a new PNG; never modify the canonical SVG or load system fonts.

    D2 uses CSS aliases for embedded WOFF subsets. resvg needs local SFNT fonts:
    decode each subset and give its temporary font the corresponding alias.
    The original SVG, font data and icon artwork remain untouched.
    """
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing render: {output}")
    backend = raster_backend()
    svg = source.read_bytes()
    root = ET.fromstring(svg)
    viewbox = root.get("viewBox", "").replace(",", " ").split()
    dimensions = []
    for index, attribute in enumerate(("width", "height")):
        fallback = viewbox[index + 2] if len(viewbox) == 4 else "nan"
        value = float(root.get(attribute, fallback).removesuffix("px"))
        if not math.isfinite(value) or value <= 0:
            raise ValueError("PNG requires explicit positive SVG pixel dimensions")
        dimensions.append(math.ceil(value))
    if dimensions[0] * dimensions[1] > 40_000_000:
        raise ValueError("PNG exceeds the 40 megapixel raster limit; use SVG")
    metrics = TextMetrics(root)
    for text in root.iter(NS + "text"):
        try:
            metrics.box(text)
        except (UnsupportedText, ValueError, KeyError) as exc:
            raise ValueError(f"Cannot rasterize text with an unverified embedded font: {exc}") from exc
    for image in root.iter(NS + "image"):
        href = image.get("href", image.get("{http://www.w3.org/1999/xlink}href", ""))
        if not href.startswith("data:image/png;base64,"):
            raise ValueError("PNG requires self-contained SVG image data")
    with tempfile.TemporaryDirectory(prefix=".raster-fonts-", dir=output.parent) as directory:
        fonts = []
        for index, (family, font) in enumerate(sorted(metrics.fonts.items())):
            font.flavor = None
            for record in font["name"].names:
                if record.nameID in (1, 3, 4, 6, 16):
                    record.string = family.encode(record.getEncoding())
            path = Path(directory) / f"font-{index}.ttf"
            font.save(path)
            fonts.append(str(path))
        png = backend.svg_to_bytes(svg_string=svg.decode("utf-8"),
                                   skip_system_fonts=True, font_files=fonts,
                                   width=dimensions[0], height=dimensions[1],
                                   resources_dir=directory)
    if not png.startswith(PNG_SIGNATURE) or png[12:16] != b"IHDR":
        raise RuntimeError("Rasterizer did not return a PNG")
    metadata = _text_chunk("Software", f"pn-docgen; resvg-py {backend.__version__}; resvg {backend.__resvg_version__}")
    metadata += _text_chunk("Source-SVG-SHA256", hashlib.sha256(svg).hexdigest())
    notice = root.find(f"{NS}metadata[@id='{SVG_NOTICE_ID}']")
    if notice is not None and notice.text:
        metadata += _text_chunk("Description", notice.text)
    # Insert deterministic metadata after IHDR; no timestamps or local paths.
    png = png[:33] + metadata + png[33:]
    with output.open("xb") as stream:
        stream.write(png)
    return output
