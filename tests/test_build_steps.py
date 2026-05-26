from lego_mcp import server


def test_plan_build_sequence_orders_stack_bottom_up():
    server.create_model()
    base = server.add_part("3001", "red", 0, 0, 0)["instance_id"]
    top = server.add_part("3001", "blue", 0, -24, 0)["instance_id"]

    r = server.plan_build_sequence(max_steps=10)

    assert r["ok"]
    assert [s["instance_id"] for s in r["steps"]] == [base, top]
    assert r["steps"][0]["supports"][0]["instance_id"] == "ground"
    assert r["steps"][1]["supports"][0]["instance_id"] == base


def test_next_build_step_pages_through_sequence():
    server.create_model()
    first = server.add_part("3001", "red", 0, 0, 0)["instance_id"]
    second = server.add_part("3001", "blue", 0, -24, 0)["instance_id"]

    assert server.next_build_step(built_count=0)["next"]["instance_id"] == first
    assert server.next_build_step(built_count=1)["next"]["instance_id"] == second
    done = server.next_build_step(built_count=2)
    assert done["next"] is None
    assert done["complete"]


def test_plan_build_sequence_reports_unsupported_target_piece():
    server.create_model()
    unsupported = server.add_part("3001", "red", 0, -200, 0)["instance_id"]

    r = server.plan_build_sequence(max_steps=10)

    assert not r["ok"]
    assert r["sequenced"] == 0
    assert r["blocked_count"] == 1
    assert r["blocked"][0]["instance_id"] == unsupported


def test_plan_build_sequence_can_filter_subassembly():
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)
    server.set_current_subassembly("tower")
    tower_base = server.add_part("3001", "blue", 100, 0, 0)["instance_id"]
    tower_top = server.add_part("3001", "blue", 100, -24, 0)["instance_id"]

    r = server.plan_build_sequence(subassembly="tower", max_steps=10)

    assert r["ok"]
    assert [s["instance_id"] for s in r["steps"]] == [tower_base, tower_top]
