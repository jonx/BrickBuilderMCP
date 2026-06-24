"""Bill of materials: turn a placed model into an orderable parts list.

Aggregates part instances into lots — one row per (part_id, color) — with
counts, human-readable part + color names, and exporters for plain text, CSV,
JSON, and a BrickLink Wanted List XML you can upload to order the pieces.

Pure module: no server import. Callers pass an iterable of objects exposing
`.part_id` and `.color` (an LDraw colour id) plus the part index used to
resolve names.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, asdict
from typing import Any, Iterable

from lego_mcp.parts import COLOR_BY_ID


# LDraw colour id -> BrickLink colour id, for the colours we are confident
# about (RGB-matched against BrickLink's palette). Ambiguous colours are left
# out on purpose: `to_bricklink_xml` reports them as warnings rather than
# guessing and producing a wrong order. Extend as mappings are verified.
LDRAW_TO_BRICKLINK_COLOR: dict[int, int] = {
    0: 11,    # Black
    1: 7,     # Blue
    2: 6,     # Green
    3: 39,    # Dark Turquoise
    4: 5,     # Red
    7: 9,     # Light Gray (legacy)
    8: 10,    # Dark Gray (legacy)
    9: 62,    # Light Blue
    10: 36,   # Bright Green
    14: 3,    # Yellow
    15: 1,    # White
    19: 2,    # Tan
    22: 24,   # Purple
    25: 4,    # Orange
    26: 71,   # Magenta
    27: 34,   # Lime
    28: 69,   # Dark Tan
    29: 23,   # Pink
    47: 12,   # Trans-Clear
    71: 86,   # Light Bluish Gray
    72: 85,   # Dark Bluish Gray
}


@dataclass(frozen=True)
class Lot:
    """One orderable line: `quantity` of `part_id` in `color_id`."""
    part_id: str
    part_name: str
    color_id: int
    color_name: str
    quantity: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def color_name(color_id: int) -> str:
    entry = COLOR_BY_ID.get(color_id)
    if entry is None:
        return f"color_{color_id}"
    return entry[0].replace("_", " ")


def aggregate(parts: Iterable[Any], part_index: dict[str, Any]) -> list[Lot]:
    """Collapse part instances into lots, sorted most-needed first.

    Sort key: quantity desc, then part_id, then color id — deterministic so
    the same model always yields the same list.
    """
    counts: dict[tuple[str, int], int] = {}
    for inst in parts:
        key = (inst.part_id, int(inst.color))
        counts[key] = counts.get(key, 0) + 1

    lots: list[Lot] = []
    for (part_id, color_id), qty in counts.items():
        part = part_index.get(part_id)
        name = part.name.strip() if part is not None else f"(unknown part {part_id})"
        lots.append(Lot(part_id=part_id, part_name=name, color_id=color_id,
                        color_name=color_name(color_id), quantity=qty))
    lots.sort(key=lambda l: (-l.quantity, l.part_id, l.color_id))
    return lots


def summary(lots: list[Lot]) -> dict[str, int]:
    return {
        "total_pieces": sum(l.quantity for l in lots),
        "unique_lots": len(lots),
        "distinct_part_ids": len({l.part_id for l in lots}),
    }


def format_table(lots: list[Lot], model_name: str | None = None) -> str:
    """A monospace parts-list table for humans."""
    s = summary(lots)
    head = f"Bill of materials" + (f" — {model_name}" if model_name else "")
    if not lots:
        return head + "\n(empty model — no parts)"
    qty_w = max(3, max(len(str(l.quantity)) for l in lots))
    pid_w = max(7, max(len(l.part_id) for l in lots))
    col_w = max(5, max(len(l.color_name) for l in lots))
    lines = [head, ""]
    lines.append(f"{'QTY':>{qty_w}}  {'PART':<{pid_w}}  {'COLOR':<{col_w}}  NAME")
    lines.append(f"{'-'*qty_w}  {'-'*pid_w}  {'-'*col_w}  {'-'*20}")
    for l in lots:
        lines.append(f"{l.quantity:>{qty_w}}  {l.part_id:<{pid_w}}  "
                     f"{l.color_name:<{col_w}}  {l.part_name}")
    lines.append("")
    lines.append(f"{s['total_pieces']} pieces total, {s['unique_lots']} lots "
                 f"({s['distinct_part_ids']} distinct part numbers).")
    return "\n".join(lines)


def to_csv(lots: list[Lot]) -> str:
    """CSV: quantity,part_id,color_id,color_name,part_name."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["quantity", "part_id", "color_id", "color_name", "part_name"])
    for l in lots:
        w.writerow([l.quantity, l.part_id, l.color_id, l.color_name, l.part_name])
    return buf.getvalue()


def to_bricklink_xml(lots: list[Lot]) -> tuple[str, list[str]]:
    """BrickLink Wanted List XML (upload at bricklink.com to order).

    Returns (xml, warnings). Lots whose LDraw colour has no verified
    BrickLink mapping are emitted WITHOUT a <COLOR> (BrickLink treats that as
    "any colour") and listed in `warnings` so you can set the colour by hand
    rather than ordering the wrong one.
    """
    warnings: list[str] = []
    rows = ["<INVENTORY>"]
    for l in lots:
        rows.append("  <ITEM>")
        rows.append("    <ITEMTYPE>P</ITEMTYPE>")
        rows.append(f"    <ITEMID>{html.escape(l.part_id)}</ITEMID>")
        bl = LDRAW_TO_BRICKLINK_COLOR.get(l.color_id)
        if bl is not None:
            rows.append(f"    <COLOR>{bl}</COLOR>")
        else:
            warnings.append(
                f"{l.quantity}x {l.part_id}: LDraw color {l.color_id} "
                f"({l.color_name}) has no verified BrickLink mapping — set it "
                "manually in the wanted list."
            )
            rows.append(f"    <!-- color: {html.escape(l.color_name)} "
                        "(set manually) -->")
        rows.append(f"    <MINQTY>{l.quantity}</MINQTY>")
        rows.append("  </ITEM>")
    rows.append("</INVENTORY>")
    return "\n".join(rows) + "\n", warnings
