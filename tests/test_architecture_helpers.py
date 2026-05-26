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
