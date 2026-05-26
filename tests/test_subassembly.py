"""Subassembly tag system + clone/mirror + multi-block MPD I/O."""

from __future__ import annotations

import pytest

from lego_mcp import server


def test_default_subassembly_is_main():
    server.create_model()
    r = server.add_part("3001", "red", 0, 0, 0)
    assert server.STATE.parts[r["instance_id"]].subassembly == "main"


def test_set_current_subassembly_tags_subsequent_adds():
    server.create_model()
    server.set_current_subassembly("left_tower")
    a = server.add_part("3001", "red", 0, 0, 0)["instance_id"]
    server.set_current_subassembly("main")
    b = server.add_part("3001", "blue", 100, 0, 0)["instance_id"]
    assert server.STATE.parts[a].subassembly == "left_tower"
    assert server.STATE.parts[b].subassembly == "main"


def test_list_subassemblies_reports_counts():
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)        # main
    server.set_current_subassembly("spire")
    server.add_part("3001", "red", 100, 0, 0)
    server.add_part("3001", "red", 200, 0, 0)
    r = server.list_subassemblies()
    counts = {s["name"]: s["parts"] for s in r["subassemblies"]}
    assert counts == {"main": 1, "spire": 2}


def test_clone_subassembly_creates_copies_with_offset():
    server.create_model()
    server.set_current_subassembly("tower")
    server.add_part("3001", "red", 0, 0, 0)
    server.add_part("3001", "blue", 0, -24, 0)
    r = server.clone_subassembly("tower", "tower_copy", x_offset=200)
    assert r["parts"] == 2
    clones = [p for p in server.STATE.parts.values() if p.subassembly == "tower_copy"]
    assert {(p.x, p.y, p.z) for p in clones} == {(200, 0, 0), (200, -24, 0)}


def test_mirror_subassembly_flips_along_x():
    server.create_model()
    server.set_current_subassembly("left")
    server.add_part("3001", "red", 100, 0, 0)
    server.add_part("3001", "blue", 100, -24, 0)
    server.mirror_subassembly("left", "right", axis="x")
    mirrored = [p for p in server.STATE.parts.values() if p.subassembly == "right"]
    assert {p.x for p in mirrored} == {-100}


def test_mpd_multi_block_roundtrip(tmp_path):
    server.create_model("multi")
    server.add_part("3001", "red", 0, 0, 0)          # main
    server.set_current_subassembly("spire_left")
    server.add_part("3001", "blue", -200, -24, 0)
    server.set_current_subassembly("spire_right")
    server.add_part("3001", "green", 200, -24, 0)

    f = tmp_path / "out.mpd"
    server.export_mpd(str(f))

    body = f.read_text()
    # Three blocks: main, spire_left, spire_right.
    assert body.count("0 FILE ") == 3
    assert "spire_left.ldr" in body
    assert "spire_right.ldr" in body

    # Reload and confirm subassembly tags survive round-trip.
    server.import_ldr(str(f))
    subs = {p.subassembly for p in server.STATE.parts.values()}
    assert subs == {"main", "spire_left", "spire_right"}


def test_remove_subassembly_deletes_all_tagged_parts():
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)
    server.set_current_subassembly("trash")
    server.add_part("3001", "blue", 100, 0, 0)
    server.add_part("3001", "blue", 200, 0, 0)
    r = server.remove_subassembly("trash")
    assert r["removed"] == 2
    assert all(p.subassembly == "main" for p in server.STATE.parts.values())


def test_clone_subassembly_rejects_unknown_src():
    server.create_model()
    with pytest.raises(ValueError):
        server.clone_subassembly("nonexistent", "dst")
