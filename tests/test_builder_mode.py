"""Builder mode: target model + incremental physical placement."""

from __future__ import annotations

import pytest

from lego_mcp import server


def _stack_two_bricks() -> tuple[str, str]:
    base = server.add_part("3001", "red", 0, 0, 0)["instance_id"]
    top = server.add_part("3001", "blue", 0, -24, 0)["instance_id"]
    return base, top


def test_design_mode_auto_marks_parts_built():
    server.create_model()
    base, top = _stack_two_bricks()
    assert server.STATE.built == {base, top}
    assert not server.STATE.builder_mode


def test_start_builder_session_clears_built():
    server.create_model()
    base, top = _stack_two_bricks()
    r = server.start_builder_session()
    assert r["target_parts"] == 2
    assert server.STATE.built == set()
    assert server.STATE.builder_mode


def test_in_builder_mode_add_part_does_not_auto_build():
    server.create_model()
    server.start_builder_session()
    new_id = server.add_part("3001", "green", 100, 0, 0)["instance_id"]
    # The new part is a TARGET, but not yet built.
    assert new_id in server.STATE.parts
    assert new_id not in server.STATE.built


def test_mark_built_advances_progress():
    server.create_model()
    base, top = _stack_two_bricks()
    server.start_builder_session()
    server.mark_built(base)
    assert server.STATE.built == {base}
    server.mark_built(top)
    assert server.STATE.built == {base, top}


def test_mark_built_rejects_unknown_part():
    server.create_model()
    with pytest.raises(ValueError):
        server.mark_built("not_a_real_id")


def test_unmark_built_reverses_progress():
    server.create_model()
    base, top = _stack_two_bricks()
    server.start_builder_session()
    server.mark_built(base)
    server.unmark_built(base)
    assert base not in server.STATE.built


def test_mark_built_batch_skips_unknowns():
    server.create_model()
    base, top = _stack_two_bricks()
    server.start_builder_session()
    r = server.mark_built_batch([base, top, "missing"])
    assert r["newly_built"] == 2
    assert "missing" in r["unknown"]


def test_reset_build_progress_clears_set():
    server.create_model()
    base, top = _stack_two_bricks()
    server.start_builder_session()
    server.mark_built_batch([base, top])
    server.reset_build_progress()
    assert server.STATE.built == set()


def test_builder_status_reports_next_up_first_then_supported():
    server.create_model()
    base, top = _stack_two_bricks()
    server.start_builder_session()
    r = server.builder_status()
    assert r["total"] == 2
    assert r["built"] == 0
    # Ground-level part comes first
    assert r["next_up"]["instance_id"] == base
    assert not r["complete"]

    server.mark_built(base)
    r = server.builder_status()
    assert r["next_up"]["instance_id"] == top

    server.mark_built(top)
    r = server.builder_status()
    assert r["complete"]
    assert r["next_up"] is None


def test_next_unbuilt_step_returns_grounded_first():
    server.create_model()
    base, top = _stack_two_bricks()
    server.start_builder_session()
    r = server.next_unbuilt_step(limit=2)
    # Only the grounded part is placeable until the top brick's supporter exists.
    assert len(r["candidates"]) == 1
    assert r["candidates"][0]["instance_id"] == base
    # The top brick is reported as blocked, waiting on its supporter.
    blocked_ids = [b["instance_id"] for b in r["blocked"]]
    assert top in blocked_ids


def test_next_unbuilt_step_unblocks_after_marking_supporter():
    server.create_model()
    base, top = _stack_two_bricks()
    server.start_builder_session()
    server.mark_built(base)
    r = server.next_unbuilt_step(limit=2)
    assert any(c["instance_id"] == top for c in r["candidates"])


def test_end_builder_session_marks_all_built_by_default():
    server.create_model()
    base, top = _stack_two_bricks()
    server.start_builder_session()
    server.mark_built(base)
    server.end_builder_session()
    assert not server.STATE.builder_mode
    assert server.STATE.built == {base, top}


def test_render_progress_writes_a_png(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGO_MCP_RENDERS_DIR", str(tmp_path))
    server.create_model()
    base, top = _stack_two_bricks()
    server.start_builder_session()
    server.mark_built(base)
    result = server.render_progress(width=400, height=300)
    # Shape: [markdown_text, summary, MCPImage]
    assert len(result) == 3
    markdown, summary, image = result
    assert isinstance(markdown, str) and markdown.startswith("![")
    assert summary["ok"]
    from pathlib import Path
    p = Path(summary["path"])
    assert p.exists()
    assert p.read_bytes().startswith(b"\x89PNG")
    assert image.data.startswith(b"\x89PNG")
    assert summary["built"] == 1
    assert summary["total"] == 2
