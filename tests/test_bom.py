"""Bill-of-materials aggregation + the bill_of_materials MCP tool."""

from __future__ import annotations

from lego_mcp import bom, server


def _model():
    server.create_model("bom_test", include_manual=False)
    server.add_part("3811", "tan", 0, 0, 0)            # baseplate
    server.add_part("3001", "red", 0, -4, 0)           # 2x4 red
    server.add_part("3001", "red", 80, -4, 0)          # 2x4 red
    server.add_part("3001", "blue", 0, -28, 0)         # 2x4 blue
    server.add_part("3003", "red", 0, -52, 0)          # 2x2 red


def test_aggregate_counts_and_sort():
    _model()
    lots = bom.aggregate(server.STATE.parts.values(), server.PART_INDEX)
    # Most-needed first: 2x red 2x4 leads.
    assert lots[0].part_id == "3001"
    assert lots[0].color_name == "red"
    assert lots[0].quantity == 2
    s = bom.summary(lots)
    assert s["total_pieces"] == 5
    assert s["unique_lots"] == 4          # (3811,tan)(3001,red)(3001,blue)(3003,red)
    assert s["distinct_part_ids"] == 3    # 3811, 3001, 3003


def test_quantities_match_instance_count():
    _model()
    lots = bom.aggregate(server.STATE.parts.values(), server.PART_INDEX)
    assert sum(l.quantity for l in lots) == len(server.STATE.parts)


def test_csv_has_header_and_rows():
    _model()
    lots = bom.aggregate(server.STATE.parts.values(), server.PART_INDEX)
    csv = bom.to_csv(lots)
    lines = csv.strip().splitlines()
    assert lines[0] == "quantity,part_id,color_id,color_name,part_name"
    assert len(lines) == 1 + len(lots)


def test_bricklink_xml_maps_known_colors_and_warns_on_unknown():
    server.create_model("bl", include_manual=False)
    server.add_part("3001", "red", 0, 0, 0)            # red -> BL 5
    server.add_part("3003", "brown", 0, -24, 0)        # brown -> unmapped
    lots = bom.aggregate(server.STATE.parts.values(), server.PART_INDEX)
    xml, warnings = bom.to_bricklink_xml(lots)
    assert "<COLOR>5</COLOR>" in xml          # red is mapped
    assert "<ITEMID>3001</ITEMID>" in xml
    assert any("brown" in w for w in warnings)
    assert "set manually" in xml              # unmapped color left as a comment


def test_bill_of_materials_tool():
    _model()
    r = server.bill_of_materials()
    assert r["total_pieces"] == 5
    assert r["unique_lots"] == 4
    assert r["lots"][0]["quantity"] == 2
    assert "Bill of materials" in r["text"]
    assert "bricklink_xml" not in r          # off by default


def test_bill_of_materials_tool_bricklink_and_subassembly():
    server.create_model("bl2", include_manual=False)
    server.add_part("3001", "red", 0, 0, 0)
    server.set_current_subassembly("roof")
    server.add_part("3003", "blue", 0, -24, 0)
    whole = server.bill_of_materials(bricklink=True)
    assert whole["total_pieces"] == 2
    assert "bricklink_xml" in whole
    roof = server.bill_of_materials(subassembly="roof")
    assert roof["total_pieces"] == 1
    assert roof["lots"][0]["part_id"] == "3003"


def test_empty_model_bom():
    server.create_model("empty", include_manual=False)
    lots = bom.aggregate(server.STATE.parts.values(), server.PART_INDEX)
    assert lots == []
    assert "empty model" in bom.format_table(lots)
