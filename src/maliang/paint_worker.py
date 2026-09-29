"""Isolated native paint execution; input is validated JSON, never executable code."""

import json
import sys
from pathlib import Path

from .paint import parse_document
from .paint_native import render_document


def main():
    job = json.loads(Path(sys.argv[1]).read_text())
    width, height = job["width"], job["height"]
    if (
        type(width) is not int
        or type(height) is not int
        or not (32 <= width <= 4096 and 32 <= height <= 4096)
    ):
        raise ValueError("Invalid painting size")
    image = render_document(parse_document(job["source"]), width, height)
    image.save(Path(job["output"]) / "000000.png")


if __name__ == "__main__":
    main()
