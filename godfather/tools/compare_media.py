#!/usr/bin/env python3
"""Compare two dashboard screenshots cell by cell and write them side by side, with a red frame round every terminal
cell that differs. A development tool: it needs Pillow (`pip install pillow`); Godfather itself does not.

    python3 tools/compare_media.py OLD.png NEW.png OUT.png [--columns 176] [--rows 50] [--mask-rows N]

The screenshots are screen-kit captures of a COLUMNS × ROWS terminal (tools/screens.mjs). A cell differs when at least
--pixels of its pixels move by more than --tolerance in brightness (takes differ by font smoothing); `--mask-rows N` leaves the last N terminal rows out of the verdict (the footer, whose name and
version differ on purpose) and frames them in yellow. Prints JSON: the differing cells outside the mask and inside it,
and exits 1 when any outside it differs.
"""
from __future__ import annotations

import argparse
import json
import sys
from PIL import Image, ImageChops, ImageDraw  # type: ignore[import-not-found]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("old")
    parser.add_argument("new")
    parser.add_argument("out")
    parser.add_argument("--columns", type=int, default=176)
    parser.add_argument("--rows", type=int, default=50)
    parser.add_argument("--mask-rows", type=int, default=1)
    parser.add_argument("--tolerance", type=int, default=48,
                        help="a pixel differs when a channel moves more than this (font smoothing varies by take)")
    parser.add_argument("--pixels", type=int, default=4, help="a cell differs when this many of its pixels do")
    args = parser.parse_args(argv)
    old, new = Image.open(args.old).convert("RGB"), Image.open(args.new).convert("RGB")
    if old.size != new.size:
        print(json.dumps({"error": f"sizes differ: {old.size} and {new.size}"}))
        return 2
    width, height = old.size
    cell_w, cell_h = width / args.columns, height / args.rows
    difference = ImageChops.difference(old, new).convert("L").point(lambda v: 255 if v > args.tolerance else 0)
    outside, inside = [], []
    for row in range(args.rows):
        for column in range(args.columns):
            box = (round(column * cell_w), round(row * cell_h), round((column + 1) * cell_w),
                   round((row + 1) * cell_h))
            if difference.crop(box).histogram()[255] >= args.pixels:
                (inside if row >= args.rows - args.mask_rows else outside).append((row, column))
    canvas = Image.new("RGB", (width * 2 + 24, height + 48), (128, 128, 128))
    canvas.paste(old, (0, 48))
    canvas.paste(new, (width + 24, 48))
    draw = ImageDraw.Draw(canvas)
    draw.text((8, 16), f"before: {args.old}", fill=(255, 255, 255))
    draw.text((width + 32, 16), f"after: {args.new}  ({len(outside)} cells differ outside the footer)",
              fill=(255, 255, 255))
    for cells, colour in ((outside, (255, 0, 0)), (inside, (255, 200, 0))):
        for row, column in cells:
            for offset in (0, width + 24):
                x, y = offset + round(column * cell_w), 48 + round(row * cell_h)
                draw.rectangle((x, y, x + round(cell_w), y + round(cell_h)), outline=colour)
    canvas.save(args.out)
    print(json.dumps({"outside_mask": len(outside), "inside_mask": len(inside), "out": args.out,
                      "first": outside[:10]}))
    return 1 if outside else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
