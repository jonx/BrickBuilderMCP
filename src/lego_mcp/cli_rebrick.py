"""`lego-mcp rebrick <file>` — consolidate 1x1 voxel models into bonded brickwork.

Wraps import_ldr + consolidate_bricks + export_ldr so any .ldr/.mpd can be
re-bricked from the shell. Prints the structural before/after so you can see
the pile -> single-rigid-body transition.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def rebrick_cmd(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="lego-mcp rebrick",
        description=("Merge 1x1-brick voxel constructions into bonded standard "
                     "brickwork (same shape and colors, one rigid body, fewer "
                     "parts)."))
    p.add_argument("input", help="Path to an .ldr / .mpd model file.")
    p.add_argument("-o", "--output", metavar="FILE",
                   help="Output path. Default: <input-stem>_rebricked.ldr next to input.")
    args = p.parse_args(argv)

    from lego_mcp import server
    input_path = Path(args.input).expanduser()
    if not input_path.is_file():
        print(f"error: no such file: {input_path}", file=sys.stderr)
        return 2
    out_path = (Path(args.output).expanduser() if args.output
                else input_path.with_name(input_path.stem + "_rebricked.ldr"))

    server._ensure_library_loaded()
    server.import_ldr(str(input_path))
    r = server.consolidate_bricks()
    out_path.write_text(server.emit_ldr(server.STATE))

    print(f"parts:        {r['parts_before']} -> {r['parts_after']}")
    print(f"rigid bodies: {r['rigid_bodies_before']} -> {r['rigid_bodies_after']}")
    print(f"spanning:     {r['spanning_ratio_after']:.0%} of elevated bricks rest on 2+ supports")
    print(f"wrote {out_path}")
    return 0
