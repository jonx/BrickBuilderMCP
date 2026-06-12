"""LDraw read/write tests."""

from __future__ import annotations

from lego_mcp import server


def test_emit_then_parse_roundtrip():
    server.create_model("rt")
    a = server.add_part("3001", "red", 0, 0, 0)["instance_id"]
    server.add_part("3001", "blue", 0, -24, 0, rotation="rot90y")
    server.add_part("3024", 14, 40, 0, 0)  # yellow 1x1 plate
    text = server.emit_ldr(server.STATE)

    parsed = server.parse_ldr_text(text)
    assert len(parsed) == 3
    parts_by_color = {p.color: p for p in parsed}
    assert 4 in parts_by_color   # red
    assert 1 in parts_by_color   # blue
    assert 14 in parts_by_color  # yellow
    blue = parts_by_color[1]
    assert blue.rotation == "rot90y"
    assert blue.x == 0 and blue.y == -24 and blue.z == 0
    # a is just confirming the instance was created
    assert a == "1"


def test_mpd_wrapping():
    server.create_model("mpd")
    server.add_part("3001", "red", 0, 0, 0)
    mpd = server.emit_mpd(server.STATE)
    assert mpd.startswith("0 FILE main.ldr\n")
    assert mpd.rstrip().endswith("0 NOFILE")
    assert "3001.dat" in mpd


def test_import_flattens_multi_block_mpd(tmp_path):
    sample = (
        "0 FILE main.ldr\n"
        "1 4 0 0 0 1 0 0 0 1 0 0 0 1 3001.dat\n"
        "0 NOFILE\n"
        "0 FILE subassembly.ldr\n"
        "1 1 100 -24 0 1 0 0 0 1 0 0 0 1 3003.dat\n"
        "0 NOFILE\n"
    )
    f = tmp_path / "demo.mpd"
    f.write_text(sample)
    r = server.import_ldr(str(f))
    assert r["loaded"] == 2
    assert len(server.STATE.parts) == 2
    part_ids = {inst.part_id for inst in server.STATE.parts.values()}
    assert part_ids == {"3001", "3003"}


def test_import_nested_mpd_does_not_duplicate_referenced_blocks(tmp_path):
    sample = (
        "0 FILE main.ldr\n"
        "1 16 100 0 0 1 0 0 0 1 0 0 0 1 outer.ldr\n"
        "0 NOFILE\n"
        "0 FILE outer.ldr\n"
        "1 4 0 0 0 1 0 0 0 1 0 0 0 1 3001.dat\n"
        "1 16 0 -24 0 1 0 0 0 1 0 0 0 1 inner.ldr\n"
        "0 NOFILE\n"
        "0 FILE inner.ldr\n"
        "1 1 0 0 0 1 0 0 0 1 0 0 0 1 3003.dat\n"
        "0 NOFILE\n"
    )
    f = tmp_path / "nested.mpd"
    f.write_text(sample)

    r = server.import_ldr(str(f))

    assert r["loaded"] == 2
    assert len(server.STATE.parts) == 2
    parts_by_id = {inst.part_id: inst for inst in server.STATE.parts.values()}
    assert set(parts_by_id) == {"3001", "3003"}
    assert (parts_by_id["3001"].x, parts_by_id["3001"].y, parts_by_id["3001"].z) == (100, 0, 0)
    assert (parts_by_id["3003"].x, parts_by_id["3003"].y, parts_by_id["3003"].z) == (100, -24, 0)
