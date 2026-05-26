"""ModelState + undo/redo + checkpoints."""

from __future__ import annotations

import pytest

from lego_mcp import server


def test_add_remove_move_rotate():
    server.create_model()
    r = server.add_part("3001", "red", 10, 0, 20)
    iid = r["instance_id"]
    assert iid in server.STATE.parts

    server.move_part(iid, 50, 0, 60)
    inst = server.STATE.parts[iid]
    assert (inst.x, inst.y, inst.z) == (50, 0, 60)

    server.rotate_part(iid, "rot90y")
    assert server.STATE.parts[iid].rotation == "rot90y"

    server.remove_part(iid)
    assert iid not in server.STATE.parts


def test_unknown_part_raises():
    server.create_model()
    with pytest.raises(ValueError):
        server.add_part("not_a_real_id")


def test_unknown_color_raises():
    server.create_model()
    with pytest.raises(ValueError):
        server.add_part("3001", "blurple")


def test_unknown_rotation_raises():
    server.create_model()
    with pytest.raises(ValueError):
        server.add_part("3001", rotation="rot45y")


def test_undo_redo_through_mutations():
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)
    server.add_part("3001", "blue", 40, 0, 0)
    assert len(server.STATE.parts) == 2

    server.undo()
    assert len(server.STATE.parts) == 1

    server.undo()
    assert len(server.STATE.parts) == 0

    r = server.undo()  # nothing left
    assert r["ok"] is False

    server.redo()
    server.redo()
    assert len(server.STATE.parts) == 2


def test_undo_after_redo_truncates_redo_stack():
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)
    server.add_part("3001", "blue", 40, 0, 0)
    server.undo()
    # New mutation should drop the redo stack
    server.add_part("3001", "yellow", 80, 0, 0)
    r = server.redo()
    assert r["ok"] is False


def test_state_mutates_in_place_across_create_import_restore():
    """External code that holds a reference to STATE must keep seeing the
    same object across create_model / import_ldr / restore_checkpoint."""
    server.create_model("a")
    captured = server.STATE
    server.add_part("3001", "red", 0, 0, 0)

    server.create_model("b")
    assert captured is server.STATE
    assert len(captured.parts) == 0  # external ref reflects the reset

    server.add_part("3001", "blue", 0, 0, 0)
    server.save_checkpoint("c1")
    server.add_part("3001", "yellow", 40, 0, 0)
    server.restore_checkpoint("c1")
    assert captured is server.STATE
    assert len(captured.parts) == 1


def test_checkpoints():
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)
    server.save_checkpoint("a")

    server.add_part("3001", "blue", 40, 0, 0)
    server.save_checkpoint("b")

    server.add_part("3001", "yellow", 80, 0, 0)
    assert len(server.STATE.parts) == 3

    server.restore_checkpoint("a")
    assert len(server.STATE.parts) == 1

    server.restore_checkpoint("b")
    assert len(server.STATE.parts) == 2

    with pytest.raises(ValueError):
        server.restore_checkpoint("nope")

    cps = {c["name"] for c in server.list_checkpoints()["checkpoints"]}
    assert cps == {"a", "b"}
