"""Persistent project save/load + reference notes."""

from __future__ import annotations

import pytest

from lego_mcp import server


def test_save_and_load_project_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PROJECTS_DIR", tmp_path)
    server.create_model("orig")
    server.add_part("3001", "red", 0, 0, 0)
    server.set_current_subassembly("spire")
    server.add_part("3001", "blue", 100, -24, 0)
    server.add_note("scale", "1 stud = 1 meter")
    server.save_project("test_project")

    # Reset and reload
    server.create_model("clobbered")
    assert len(server.STATE.parts) == 0
    server.load_project("test_project")
    assert len(server.STATE.parts) == 2
    subs = {p.subassembly for p in server.STATE.parts.values()}
    assert subs == {"main", "spire"}
    assert server.STATE.notes["scale"] == "1 stud = 1 meter"


def test_list_projects_finds_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PROJECTS_DIR", tmp_path)
    server.create_model("a")
    server.add_part("3001", "red", 0, 0, 0)
    server.save_project("alpha")
    server.create_model("b")
    server.add_part("3001", "blue", 0, 0, 0)
    server.save_project("beta")
    r = server.list_projects()
    names = {p["name"] for p in r["projects"]}
    assert {"alpha", "beta"}.issubset(names)


def test_load_unknown_project_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PROJECTS_DIR", tmp_path)
    with pytest.raises(ValueError):
        server.load_project("nonexistent")


def test_note_crud():
    server.create_model()
    server.add_note("a", "first")
    server.add_note("b", "second")
    assert server.get_note("a")["text"] == "first"
    assert len(server.list_notes()["notes"]) == 2
    server.remove_note("a")
    assert len(server.list_notes()["notes"]) == 1
    with pytest.raises(ValueError):
        server.get_note("a")
