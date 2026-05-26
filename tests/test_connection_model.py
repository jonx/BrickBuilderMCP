"""Tests for the typed connector model + connection graph."""

from __future__ import annotations

import pytest

from lego_mcp import server
from lego_mcp.connectors import (
    Connector,
    ConnectorType,
    SUPPORTED_DEFINITIONS,
    definition_for,
    world_connectors,
)
from lego_mcp.connection_graph import (
    build_graph,
    find_floating_and_unanchored,
)


def test_3010_top_studs_local():
    """Brick 1x4 (3010): 4 top studs along +X, at local y=-24."""
    d = definition_for("3010")
    assert d is not None
    tops = [c for c in d.connectors if c.type == ConnectorType.STUD_TOP]
    assert len(tops) == 4
    xs = sorted(c.x for c in tops)
    assert xs == [-30.0, -10.0, 10.0, 30.0]
    assert all(c.y == -24.0 for c in tops)
    assert all(c.z == 0.0 for c in tops)


def test_3010_bottom_receivers_local():
    """Brick 1x4: 4 bottom receivers, same X grid, at local y=0."""
    d = definition_for("3010")
    recv = [c for c in d.connectors if c.type == ConnectorType.STUD_RECEIVER_BOTTOM]
    assert len(recv) == 4
    xs = sorted(c.x for c in recv)
    assert xs == [-30.0, -10.0, 10.0, 30.0]
    assert all(c.y == 0.0 for c in recv)


def test_3010_rotated_90y_world_connectors():
    """After rot90y, the long axis swaps from +X to +Z; studs should now be at varied Z."""
    d = definition_for("3010")
    wcs = world_connectors("inst1", d, 0.0, 0.0, 0.0, rotation="rot90y")
    tops = [w for w in wcs if w.type == ConnectorType.STUD_TOP]
    # After rot90y, the studs that were at x ∈ {-30..30} are now at z ∈ {-30..30},
    # with x = 0 for all.
    xs = sorted(round(w.x, 1) for w in tops)
    zs = sorted(round(w.z, 1) for w in tops)
    assert xs == [0.0, 0.0, 0.0, 0.0]
    assert zs == [-30.0, -10.0, 10.0, 30.0]


def test_brick_stack_full_overlap_connects():
    """1x4 brick on a 1x4 brick at the same XZ — top stud Y matches bottom receiver Y."""
    server.create_model()
    a = server.add_part("3010", "red", 0, 0, 0)["instance_id"]
    b = server.add_part("3010", "blue", 0, -24, 0)["instance_id"]
    graph, edges = build_graph(server.STATE.parts)
    assert b in graph[a] and a in graph[b]
    assert len(edges) >= 1


def test_brick_stack_two_stud_offset_connects():
    """1x4 brick offset by 40 LDU (2 studs) along its long axis still has 2 studs
    mating (running-bond half-overlap). 40 LDU is TWO studs, not half a stud."""
    server.create_model()
    server.add_part("3010", "red", 0, 0, 0)
    server.add_part("3010", "blue", 40, -24, 0)
    graph, edges = build_graph(server.STATE.parts)
    assert len(edges) == 1  # the two parts share one edge in the graph


def test_brick_stack_one_stud_offset_connects():
    """1x4 brick offset by 20 LDU (1 stud) still has 3 studs mating."""
    server.create_model()
    server.add_part("3010", "red", 0, 0, 0)
    server.add_part("3010", "blue", 20, -24, 0)
    graph, edges = build_graph(server.STATE.parts)
    assert len(edges) == 1


def test_brick_offset_by_half_stud_invalid():
    """Half-stud offset (10 LDU) puts the upper brick's receivers at off-grid
    positions; no top stud lines up exactly under any receiver -> no edge."""
    server.create_model()
    server.add_part("3010", "red", 0, 0, 0)
    server.add_part("3010", "blue", 10, -24, 0)
    graph, edges = build_graph(server.STATE.parts)
    assert len(edges) == 0


def test_floating_brick_detected():
    """A brick in mid-air with no neighbors and not grounded -> floating."""
    server.create_model()
    server.add_part("3010", "red", 0, -200, 0)
    _, _, _, floating, unanchored = find_floating_and_unanchored(server.STATE.parts)
    assert len(floating) == 1


def test_island_two_bricks_unanchored():
    """Two stacked bricks high in the air: connected to each other but the
    island doesn't reach the ground."""
    server.create_model()
    server.add_part("3010", "red", 0, -200, 0)
    server.add_part("3010", "blue", 0, -224, 0)
    _, _, _, floating, unanchored = find_floating_and_unanchored(server.STATE.parts)
    assert floating == set()
    assert len(unanchored) == 2


def test_grid_misalignment_reported_by_validate():
    """Off-grid placement triggers invalid_grid_alignment error."""
    server.create_model()
    server.add_part("3010", "red", 15, 0, 0)  # 15 is off the half-stud (10) grid
    r = server.validate_model()
    assert r["summary"]["grid_alignment_errors"] == 1
    grid_errs = [e for e in r["errors"] if e["type"] == "invalid_grid_alignment"]
    assert grid_errs
    assert "Move part" in grid_errs[0]["suggestion"]
