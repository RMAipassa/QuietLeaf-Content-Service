"""Render our simple SVG paths into the multi-size ICO fallback using Pillow.

Run from any directory: python scripts/generate_favicon.py
The SVG is the source artwork, matching the Android book-and-leaf launcher.
"""

import re
from pathlib import Path
from xml.etree import ElementTree

from PIL import Image, ImageDraw


def generate():
    web = Path(__file__).resolve().parents[1] / "quietleaf_service" / "web"
    svg = ElementTree.parse(web / "favicon.svg").getroot()
    scale = 8
    image = Image.new("RGBA", (108 * scale, 108 * scale))
    draw = ImageDraw.Draw(image)
    for path in svg.findall("{http://www.w3.org/2000/svg}path"):
        tokens = iter(re.findall(r"[MLQZ]|-?\d+(?:\.\d+)?", path.attrib["d"]))
        points = []
        for command in tokens:
            if command in {"M", "L"}:
                points.append((float(next(tokens)), float(next(tokens))))
            elif command == "Q":
                start = points[-1]
                control = (float(next(tokens)), float(next(tokens)))
                end = (float(next(tokens)), float(next(tokens)))
                for step in range(1, 65):
                    t = step / 64
                    points.append(tuple((1 - t) ** 2 * a + 2 * (1 - t) * t * b + t ** 2 * c
                                        for a, b, c in zip(start, control, end)))
            elif command == "Z":
                points.append(points[0])
            else:
                raise ValueError(f"Unsupported SVG command: {command}")
        points = [(x * scale, y * scale) for x, y in points]
        if path.get("fill", "none") != "none":
            draw.polygon(points, fill=path.attrib["fill"])
        if "stroke" in path.attrib:
            draw.line(points, fill=path.attrib["stroke"],
                      width=round(float(path.get("stroke-width", "1")) * scale))
    image.save(web / "favicon.ico", format="ICO", sizes=[(n, n) for n in (16, 32, 48, 64)])


if __name__ == "__main__":
    generate()
