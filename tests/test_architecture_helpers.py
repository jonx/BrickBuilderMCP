from lego_mcp import helpers, server


def test_wall_with_lancet_opening_uses_glass_fill():
    server.create_model()
    r = helpers.build_wall_with_openings(
        -120,
        0,
        120,
        0,
        height_rows=8,
        color="light_bluish_gray",
        openings=[
            {
                "center": 120,
                "width": 80,
                "bottom_row": 2,
                "height_rows": 5,
                "style": "lancet",
                "fill_color": "trans_clear",
            }
        ],
    )

    assert r["ok"]
    assert r["bricks_placed"] > 0
    glass_color = server.resolve_color("trans_clear")
    glass = [p for p in server.STATE.parts.values() if p.color == glass_color]
    assert glass

    summary = server.validate_model()["summary"]
    assert summary["collisions"] == 0
    assert summary["floating"] == 0


def test_stepped_roof_narrows_by_layer():
    server.create_model()
    r = helpers.build_stepped_gable_roof(
        -160,
        -240,
        160,
        240,
        eave_y=0,
        ridge_axis="z",
        max_layers=4,
    )

    assert r["ok"]
    assert len(r["layers"]) == 4
    widths = [layer["bounds"][2] - layer["bounds"][0] for layer in r["layers"]]
    assert widths == sorted(widths, reverse=True)

    summary = server.validate_model()["summary"]
    assert summary["collisions"] == 0
    assert summary["floating"] == 0


def test_floor_and_wall_defaults_stack_without_collisions():
    server.create_model()
    helpers.build_floor(0, 0, 200, 160)
    helpers.build_wall_with_openings(
        0,
        0,
        200,
        0,
        height_rows=4,
        color="red",
        openings=[
            {"type": "door", "x_min": 40, "x_max": 80, "bottom_row": 0, "height_rows": 3},
            {
                "x_min": 120,
                "x_max": 160,
                "bottom_row": 1,
                "height_rows": 3,
                "fill_color": "trans_clear",
            },
        ],
    )

    summary = server.validate_model()["summary"]
    assert summary["collisions"] == 0
    assert summary["floating"] == 0
    assert summary["unanchored"] == 0


def test_wall_with_multiple_world_coordinate_openings_keeps_spans_separate():
    server.create_model()
    r = helpers.build_wall_with_openings(
        0,
        0,
        200,
        0,
        height_rows=5,
        color="red",
        openings=[
            {"type": "door", "x_min": 40, "x_max": 80, "bottom_row": 0, "height_rows": 4},
            {
                "x_min": 120,
                "x_max": 160,
                "bottom_row": 2,
                "height_rows": 3,
                "fill_color": "trans_clear",
            },
        ],
    )

    row0 = r["rows"][0]["segments"]
    assert not any(seg["start"] == 40 and seg["end"] == 80 for seg in row0)
    row2 = r["rows"][2]["segments"]
    assert any(seg["start"] == 120 and seg["end"] == 160 and seg["material"] == "trans_clear"
               for seg in row2)


def test_gable_roof_keeps_two_planes_until_ridge():
    server.create_model()
    r = helpers.build_stepped_gable_roof(
        -160,
        -80,
        160,
        80,
        eave_y=0,
        ridge_axis="x",
        max_layers=4,
    )

    assert [len(layer["strips"]) for layer in r["layers"]] == [2, 2, 2, 1]


def test_auto_stacked_wall_rejects_corner_collision():
    server.create_model()
    helpers.build_floor(0, 0, 200, 160)
    helpers.build_wall_with_openings(0, 0, 200, 0, height_rows=2, color="red")

    try:
        helpers.build_wall_with_openings(0, 0, 0, 160, height_rows=2, color="red")
    except ValueError as exc:
        assert "would collide" in str(exc)
    else:
        raise AssertionError("expected overlapping corner wall to be rejected")
