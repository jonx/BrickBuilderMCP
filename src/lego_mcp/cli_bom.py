"""`lego-mcp bom <file>` and `lego-mcp instructions <file>`.

Two one-shot CLI commands that operate on an LDraw .ldr/.mpd file without
spinning up the MCP server:

  bom           — the orderable parts list (table / CSV / JSON / BrickLink XML)
  instructions  — a support-respecting step-by-step build order
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load_model(path_str: str):
    """Import a model file into the server state. Returns (parts, part_index)."""
    from lego_mcp import server
    input_path = Path(path_str).expanduser()
    if not input_path.is_file():
        print(f"error: no such file: {input_path}", file=sys.stderr)
        raise SystemExit(2)
    server._ensure_library_loaded()
    server.import_ldr(str(input_path))
    return server.STATE.parts, server.PART_INDEX


# ---------------------------------------------------------------------------
# bom
# ---------------------------------------------------------------------------

def bom_cmd(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="lego-mcp bom",
        description="Generate the orderable parts list for a model file.")
    p.add_argument("input", help="Path to an .ldr / .mpd model file.")
    p.add_argument("-f", "--format", choices=("table", "csv", "json", "bricklink"),
                   default="table", help="Output format. Default: table.")
    p.add_argument("-o", "--output", metavar="FILE",
                   help="Write to FILE instead of stdout.")
    args = p.parse_args(argv)

    from lego_mcp import bom as _bom
    parts, part_index = _load_model(args.input)
    lots = _bom.aggregate(parts.values(), part_index)
    model_name = Path(args.input).stem

    if args.format == "table":
        text = _bom.format_table(lots, model_name)
    elif args.format == "csv":
        text = _bom.to_csv(lots)
    elif args.format == "json":
        text = json.dumps({"model": model_name, **_bom.summary(lots),
                           "lots": [l.as_dict() for l in lots]}, indent=2)
    else:  # bricklink
        text, warnings = _bom.to_bricklink_xml(lots)
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)

    if args.output:
        Path(args.output).expanduser().write_text(text if text.endswith("\n") else text + "\n")
        s = _bom.summary(lots)
        print(f"wrote {args.output}  ({s['total_pieces']} pieces, "
              f"{s['unique_lots']} lots)", file=sys.stderr)
    else:
        print(text)
    return 0


# ---------------------------------------------------------------------------
# instructions
# ---------------------------------------------------------------------------

def instructions_cmd(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="lego-mcp instructions",
        description="Generate a step-by-step build order for a model file.")
    p.add_argument("input", help="Path to an .ldr / .mpd model file.")
    p.add_argument("--start", type=int, default=0, metavar="N",
                   help="Skip the first N steps (paging). Default: 0.")
    p.add_argument("--max", type=int, default=None, metavar="N",
                   help="Show at most N steps. Default: all.")
    p.add_argument("--json", action="store_true",
                   help="Emit the full step payloads as JSON instead of text.")
    args = p.parse_args(argv)

    from lego_mcp import server
    from lego_mcp.build_steps import plan_build_sequence
    parts, part_index = _load_model(args.input)
    plan = plan_build_sequence(parts, part_index, server.part_aabb_world,
                               max_steps=args.max, start_after=args.start)

    if args.json:
        print(json.dumps(plan, indent=2))
        return 0 if plan["ok"] else 1

    model_name = Path(args.input).stem
    print(f"Build order — {model_name}: {plan['sequenced']} of "
          f"{plan['total_parts']} parts sequenced")
    if not plan["ok"]:
        bs = plan.get("blocked_summary") or {}
        print(f"\n⚠ {bs.get('message', 'some parts could not be sequenced')}")
    print()
    for step in plan["steps"]:
        from lego_mcp.bom import color_name
        cname = color_name(step["color"])
        part = part_index.get(step["part_id"])
        pname = part.name.strip() if part is not None else step["part_id"]
        x, y, z = step["position"]
        print(f"{step['step']:>4}. {pname} ({cname}) at "
              f"({x:g}, {y:g}, {z:g}) rot={step['rotation']}")
    if plan["ok"] and args.max is None and args.start == 0:
        print(f"\n{plan['sequenced']} steps total.")
    return 0
