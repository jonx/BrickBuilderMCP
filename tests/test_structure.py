"""Structural cohesion: rigid bodies, weak joints, articulation, spanning.

Connectivity-to-ground alone lets a forest of independent 1x1 columns pass
validation while being thousands of separate objects. These tests pin the
cohesion layer that catches that.
"""

from __future__ import annotations

from lego_mcp import helpers, server
from lego_mcp.connection_graph import structural_analysis


def _pile_of_towers(n_towers=3, height=3):
    server.create_model("pile", include_manual=False)
    for t in range(n_towers):
        for level in range(height):
            server.add_part("3005", "red", t * 20, -24 * level, 0)


def test_pile_is_many_rigid_bodies():
    _pile_of_towers(3, 3)
    st = structural_analysis(server.STATE.parts)
    assert st["rigid_bodies"] == 3
    assert st["largest_body"] == 3
    assert st["spanning_ratio"] == 0.0


def test_pile_fails_soundness_with_fragmentation_error():
    _pile_of_towers(3, 3)
    r = server.validate_model()
    assert r["valid"]                       # geometrically fine...
    assert not r["structurally_sound"]      # ...but it is a pile
    frag = [e for e in r["errors"] if e["type"] == "fragmented_structure"]
    assert frag and "consolidate_bricks" in frag[0]["suggestion"]


def test_bonded_wall_is_one_body_and_sound():
    server.create_model("wall", include_manual=False)
    helpers.build_wall(0, 0, 320, 0, height_rows=3, bond="running")
    r = server.validate_model()
    assert r["structurally_sound"]
    assert r["structure"]["rigid_bodies"] == 1
    assert r["structure"]["spanning_ratio"] > 0.5


def test_single_stud_joint_detected():
    """A 2x4 attached by exactly one corner stud is a hinge risk."""
    server.create_model("hinge", include_manual=False)
    server.add_part("3001", "red", 0, 0, 0)
    server.add_part("3001", "blue", 60, -24, 20)   # exactly 1 stud mates
    st = structural_analysis(server.STATE.parts)
    assert st["single_stud_joints"] == 1


def test_articulation_points_in_a_chain():
    """In a vertical chain every interior brick is a single point of failure."""
    server.create_model("chain", include_manual=False)
    for level in range(4):
        server.add_part("3003", "red", 0, -24 * level, 0)
    st = structural_analysis(server.STATE.parts)
    assert st["rigid_bodies"] == 1
    assert st["articulation_parts"] == 2    # the two interior bricks


def test_edge_stud_weights():
    """find_edges reports how many studs mate per part pair."""
    from lego_mcp.connection_graph import build_graph
    server.create_model("w", include_manual=False)
    server.add_part("3001", "red", 0, 0, 0)
    server.add_part("3001", "blue", 0, -24, 0)     # full 8-stud nest
    _, edges = build_graph(server.STATE.parts)
    assert len(edges) == 1
    assert edges[0].studs == 8


def test_baseplate_unifies_bodies():
    """Two towers each on the same baseplate are ONE body (via the plate)."""
    server.create_model("bp", include_manual=False)
    server.add_part("3811", "tan", 0, 0, 0)
    for gx in (-100, 100):
        for level in (1, 2):
            server.add_part("3003", "red", gx, -4 - 24 * (level - 1), 0)
    st = structural_analysis(server.STATE.parts)
    assert st["rigid_bodies"] == 1
