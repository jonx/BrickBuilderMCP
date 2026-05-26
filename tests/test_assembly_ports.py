"""Assembly-level exposed connector / port behavior."""

from __future__ import annotations

from lego_mcp import server
from lego_mcp.assembly_ports import analyze_ports


def test_single_brick_exposes_top_and_bottom_ports():
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)
    r = analyze_ports(server.STATE.parts)
    assert r["exposed_connector_count"] == 16
    assert r["counts_by_type"] == {
        "stud_receiver_bottom": 8,
        "stud_top": 8,
    }
    assert r["ports_total"] == 2


def test_stacked_bricks_hide_internal_connectors():
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)
    server.add_part("3001", "blue", 0, -24, 0)
    r = analyze_ports(server.STATE.parts)
    assert r["exposed_connector_count"] == 16
    assert r["counts_by_type"] == {
        "stud_receiver_bottom": 8,
        "stud_top": 8,
    }
    assert r["ports_total"] == 2
    assert sorted(round(p["center"][1], 1) for p in r["ports"]) == [-48.0, 0.0]


def test_find_and_apply_subassembly_connection_offset():
    server.create_model()
    server.set_current_subassembly("base")
    server.add_part("3001", "red", 0, 0, 0)
    server.set_current_subassembly("cap")
    server.add_part("3001", "blue", 200, -24, 0)
    server.set_current_subassembly("main")

    r = server.find_subassembly_connections("cap", "base")
    best = r["candidates"][0]
    assert best["connections"] == 8
    assert best["offset"] == [-200.0, 0.0, 0.0]

    moved = server.move_subassembly("cap", dx=-200, dy=0, dz=0)
    assert moved["moved"] == 1
    v = server.validate_model()
    assert v["summary"]["collisions"] == 0
    assert v["summary"]["floating"] == 0
    assert v["summary"]["unanchored"] == 0
