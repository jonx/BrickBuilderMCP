"""Re-bricker: 1x1 voxel constructions -> bonded standard brickwork."""

from __future__ import annotations

from lego_mcp import server
from lego_mcp.connection_graph import structural_analysis
from lego_mcp.rebrick import extract_cells, rebrick_instances
from lego_mcp.server import PART_INDEX, part_aabb_world


def _occupancy() -> dict[tuple[int, int, int], int]:
    """(half-stud gx, quarter-plate level, half-stud gz) -> color for every
    stud-cell every part covers. Lets tests prove shape+color preservation."""
    cells: dict[tuple[int, int, int], int] = {}
    for inst in server.STATE.parts.values():
        p = PART_INDEX[inst.part_id]
        (x0, y0, z0), (x1, y1, z1) = part_aabb_world(inst, p)
        for cx in range(int(round((x0 + 10) / 20)), int(round((x1 - 10) / 20)) + 1):
            for cz in range(int(round((z0 + 10) / 20)), int(round((z1 - 10) / 20)) + 1):
                for lv in range(int(round(-y1 / 24)), int(round(-y0 / 24))):
                    cells[(cx, lv, cz)] = inst.color
    return cells


def _voxel_wall(width=8, depth=2, height=4, color="red"):
    server.create_model("vox", include_manual=False)
    for gx in range(width):
        for gz in range(depth):
            for level in range(height):
                server.add_part("3005", color, gx * 20, -24 * level, gz * 20)


def test_rebrick_fuses_pile_into_one_body():
    _voxel_wall()
    before = structural_analysis(server.STATE.parts)
    assert before["rigid_bodies"] > 1
    r = server.consolidate_bricks()
    assert r["rigid_bodies_after"] == 1
    assert r["parts_after"] < r["parts_before"]
    v = server.validate_model()
    assert v["valid"] and v["structurally_sound"]


def test_rebrick_preserves_shape_and_colors():
    server.create_model("shape", include_manual=False)
    # two colors, irregular footprint
    for gx in range(6):
        for level in range(3):
            color = "red" if (gx < 3) ^ (level % 2 == 1) else "blue"
            server.add_part("3005", color, gx * 20, -24 * level, 0)
    before = _occupancy()
    server.consolidate_bricks()
    assert _occupancy() == before


def test_rebrick_leaves_other_parts_alone():
    server.create_model("mix", include_manual=False)
    server.add_part("3811", "tan", 0, 0, 0)
    server.add_part("3001", "red", 10, -4, 10)
    for level in range(3):
        server.add_part("3005", "blue", 10, -4 - 24 * (level + 1), 10)
    r = server.consolidate_bricks()
    assert r["passthrough_parts"] == 2
    kinds = {i.part_id for i in server.STATE.parts.values()}
    assert "3811" in kinds and "3001" in kinds


def test_rebrick_handles_baseplate_lattice():
    """1x1 stacks on a baseplate live at y=-4-24k on the odd-10 XZ grid —
    the re-bricker must consolidate that lattice, not pass it through."""
    server.create_model("bp", include_manual=False)
    server.add_part("3811", "tan", 0, 0, 0)
    for gx in range(4):
        for level in range(3):
            server.add_part("3005", "red", -30 + gx * 20, -4 - 24 * level, 10)
    r = server.consolidate_bricks()
    assert r["converted_cells"] == 12
    assert r["parts_after"] < r["parts_before"]
    v = server.validate_model()
    assert v["valid"] and v["structurally_sound"]
    assert v["structure"]["rigid_bodies"] == 1


def test_rebrick_staggers_layers():
    """When a layer needs several bricks, successive layers must not tile
    identically (that would be columns of stacks). Width 12 forces at least
    one seam per layer, so some brick must span a below-seam."""
    _voxel_wall(width=12, depth=1, height=4)
    server.consolidate_bricks()
    st = structural_analysis(server.STATE.parts)
    assert st["rigid_bodies"] == 1
    assert st["spanning_ratio"] > 0


def test_extract_cells_partitions_lattices():
    class FakeInst:
        def __init__(self, pid, x, y, z):
            self.part_id, self.x, self.y, self.z = pid, x, y, z
            self.color = 4
    insts = [
        FakeInst("3005", 0, 0, 0),          # ground lattice, even grid
        FakeInst("3005", 10, -4, 10),       # baseplate lattice, odd grid
        FakeInst("3001", 0, -24, 0),        # not a 1x1 -> passthrough
        FakeInst("3005", 5, 0, 0),          # off-grid -> passthrough
    ]
    lattices, passthrough = extract_cells(insts)
    assert len(passthrough) == 2
    assert (0, 0, 0) in lattices and (10, 10, 4) in lattices


# ---------------------------------------------------------------------------
# The generic pipeline: shapes are cell sets, the engine does the bonding
# ---------------------------------------------------------------------------

def _room_with_openings():
    server.create_model("room_generic", include_manual=False)
    from lego_mcp import helpers
    return helpers.build_room(-140, -80, 140, 80, height_rows=4, color="red",
        openings={"south": [{"center": 140, "width": 40, "bottom_row": 0,
                             "height_rows": 3, "type": "door"}],
                  "north": [{"center": 140, "width": 80, "bottom_row": 1,
                             "height_rows": 2, "style": "arch"}]})


def test_engine_room_is_one_sound_body():
    """A room with a door and a window, built as cells through the engine:
    corners weave and the lintel bridges without any room-specific logic."""
    r = _room_with_openings()
    assert r["engine"] == "rebrick"
    v = server.validate_model()
    assert v["valid"] and v["structurally_sound"]
    assert v["structure"]["rigid_bodies"] == 1
    assert v["summary"]["floating"] == 0
    assert v["summary"]["collisions"] == 0


def test_engine_room_door_is_open():
    _room_with_openings()
    blocking = []
    for inst in server.STATE.parts.values():
        p = PART_INDEX[inst.part_id]
        (x0, _, z0), (x1, y1, z1) = part_aabb_world(inst, p)
        if x0 < 0 < x1 and z0 < -80 < z1 and y1 > -72:
            blocking.append(inst.instance_id)
    assert blocking == []


def test_overhang_rows_prefer_supported_bricks():
    """A wall with a 2-stud void: the row above must bridge it (no floating).
    This is the W_UNSUPPORTED scoring, not a lintel special case."""
    server.create_model("lintel", include_manual=False)
    for gx in range(8):
        if gx in (3, 4):
            continue                          # void columns at ground level
        server.add_part("3005", "red", gx * 20, 0, 0)
    for gx in range(8):                        # full row above the void
        server.add_part("3005", "red", gx * 20, -24, 0)
    server.consolidate_bricks()
    v = server.validate_model()
    assert v["valid"]
    assert v["summary"]["floating"] == 0


def test_build_volume_declarative_shapes():
    """A tower ring with a tunnel and a pyramid cap, described as regions —
    one engine call, one rigid body, tunnel genuinely open."""
    from lego_mcp import helpers
    server.create_model("vol", include_manual=False)
    server.add_part("3811", "green", 0, 0, 0)
    helpers.build_volume([
        {"shape": "ring", "x0": -120, "z0": -120, "x1": 120, "z1": 120,
         "rows": 6, "color": "light_bluish_gray"},
        {"shape": "box", "x0": -40, "z0": -140, "x1": 40, "z1": -80,
         "rows": 3, "subtract": True},
        {"shape": "pyramid", "x0": -120, "z0": -120, "x1": 120, "z1": 120,
         "rows": 5, "start_row": 6, "color": "dark_bluish_gray"},
    ])
    v = server.validate_model()
    assert v["valid"] and v["structurally_sound"]
    assert v["structure"]["rigid_bodies"] == 1
    blocking = []
    for inst in server.STATE.parts.values():
        p = PART_INDEX[inst.part_id]
        (x0, _, z0), (x1, y1, z1) = part_aabb_world(inst, p)
        if x0 < 0 < x1 and z0 < -110 < z1 and y1 > -72 and inst.part_id != "3811":
            blocking.append(inst.instance_id)
    assert blocking == []
