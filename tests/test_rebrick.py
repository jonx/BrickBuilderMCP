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
