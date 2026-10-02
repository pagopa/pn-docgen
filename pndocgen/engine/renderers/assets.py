"""Bundle only referenced local icons beside an L3 D2 source for relocation."""
import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path

ICON_NOTICE = Path(__file__).resolve().parents[1] / "d2/icons/aws/NOTICE.txt"
ASSET_NOTICE_NAME = "AWS-ICONS-NOTICE.txt"
SVG_NOTICE_ID = "pn-docgen-aws-icon-license"
SVG_NOTICE = (
    "AWS Architecture Icons, Amazon Web Services, via AWS Icons for PlantUML; "
    "CC BY-ND 2.0: https://creativecommons.org/licenses/by-nd/2.0/ ; "
    "source: https://github.com/awslabs/aws-icons-for-plantuml/tree/e26e2c05daf8b6bc4c764669fc2be04c314ccb8c/dist ; "
    "icons displayed without altering their source PNG bytes; no AWS endorsement implied."
)


def portable_icons(content: str, output: Path, icons) -> str:
    assets = output.parent / "assets"
    pending = {}
    for source in sorted({str(p) for p in icons if p}):
        # Match complete generated icon lines, not arbitrary labels/comments.
        token = "icon: " + source + "\n"
        if token not in content:
            continue
        data = Path(source).read_bytes()
        name = hashlib.sha256(data).hexdigest() + Path(source).suffix
        target = assets / name
        if target.exists() and target.read_bytes() != data:
            raise ValueError(f"Existing icon asset has unexpected content: {target}")
        pending[target] = data
        content = content.replace(token, f"icon: assets/{name}\n")
    if pending:
        notice = assets / ASSET_NOTICE_NAME
        notice_data = ICON_NOTICE.read_bytes()
        if notice.exists() and notice.read_bytes() != notice_data:
            raise ValueError(f"Existing icon notice has unexpected content: {notice}")
        pending[notice] = notice_data
        assets.mkdir(parents=True, exist_ok=True)
    for target, data in pending.items():
        if not target.exists():
            with target.open("xb") as stream:
                stream.write(data)
    return content


def annotate_svg_icons(svg_path: Path) -> bool:
    """Embed attribution in a standalone SVG only when it contains AWS nodes."""
    tree = ET.parse(svg_path)
    root = tree.getroot()
    ns = "{http://www.w3.org/2000/svg}"
    if not any({"aws_node", "ecs_hero"}.intersection(group.get("class", "").split())
               and next(group.iter(f"{ns}image"), None) is not None
               for group in root.iter(f"{ns}g")):
        return False
    if any(item.get("id") == SVG_NOTICE_ID for item in root.iter(f"{ns}metadata")):
        return False
    metadata = ET.Element(f"{ns}metadata", {"id": SVG_NOTICE_ID})
    metadata.text = SVG_NOTICE
    root.insert(0, metadata)
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
    tree.write(svg_path, encoding="utf-8", xml_declaration=True)
    return True
