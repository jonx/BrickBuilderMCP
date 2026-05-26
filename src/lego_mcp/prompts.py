"""MCP prompt templates.

These are invokable from the user's MCP client (e.g. /lego-mcp/build in
Claude Desktop). They encode best practices for using the LegoMCP toolchain
so users don't have to remember the workflow.

Conventions every prompt reminds Claude about:
- LDraw coordinates: -Y is up, 1 stud = 20 LDU, 1 brick = 24 LDU tall.
- Bricks are oriented with the long axis along +X (a "Brick 2x4" is 80x40).
- Iterate: build a small section, render, validate, fix, continue.
- For models of any real size: use subassemblies and the build_wall / build_floor
  helpers instead of placing every brick individually.
"""

from __future__ import annotations


COORDS_BLURB = """\
**LDraw coordinate convention**:
- Right-handed, with **-Y as up** (a brick stacked on top is at y = lower.y - 24).
- 1 stud width = 20 LDU. 1 plate height = 8 LDU. 1 brick height = 24 LDU.
- Part origins are at the geometric center of the bottom face.
- A "Brick A x B" has its longer axis along +X. So a 2x4 brick is 80 LDU (X) x 40 LDU (Z) x 24 LDU tall.

**Rotations** are named, not matrices: identity, rot90y, rot180y, rot270y, rot90x, rot90z.
"""

WORKFLOW_BLURB = """\
**Working loop** (do this each time you add 5-50 parts):
1. `add_part` / helpers like `build_wall`.
2. `validate_model` — fix any collisions/unknowns the report flags.
3. `render_model` — look at the result; check proportions, alignment, colors.
   Use `render_model(color_mode="instance")` or `"row"` when debugging brick layout.
4. If wrong: `undo` or `restore_checkpoint`. If right: `save_checkpoint` and continue.

For models above ~100 parts, decompose into **subassemblies**:
- `set_current_subassembly("left_tower")`, build that component, then switch back to `main`.
- Use `clone_subassembly(src, dst, x_offset, y_offset, z_offset)` to repeat a component.
- Use `mirror_subassembly(src, dst, axis, plane_offset)` for bilateral symmetry (e.g. cathedral facades).
"""

TECHNIQUES_BLURB = """\
**Well-known LEGO techniques you should use, not reinvent**:

- **Running bond (stretcher)**: each brick row offset by half a brick from the row below — distributes load, looks like real masonry. Use `build_wall(..., bond="running")`.
- **Bonded rectilinear perimeters**: use `build_perimeter(points=[...])` for footprints from images/plans/models. Use `build_room(...)` only as the rectangle shortcut. Corners alternate direction by row; use `palette=["3001"]` when you specifically want 2x4-only walls and the dimensions fit.
- **Openings and arches**: use `build_wall_with_openings(...)` for straight walls with rectangular, round-arch, or lancet spans. Keep opening edges on the stud grid and leave at least two studs of pier between adjacent openings.
- **Roofs**: use `build_stepped_gable_roof(...)` for nave/house roofs and `build_stepped_pyramid_roof(...)` for towers. The default parts are connector-aware, so validation can prove the roof is supported.
- **English bond**: alternating header (short side facing out) and stretcher rows. Use for thick, sturdy walls.
- **Plates instead of bricks**: 3 plates = 1 brick height. Use plates where you need fine height control or where the model will be picked up (plates lock).
- **Tiles for smooth tops**: when a surface should look finished (floors, roofs, road).
- **Jumper plates** (1x2 with one stud): allow half-stud horizontal offsets.
- **SNOT (Studs Not On Top)**: use brackets (parts like 99206, 30179) to attach bricks sideways. Essential for textured walls and any non-rectangular surface.
- **MILS baseplate modules**: 64-stud (8x8) modular units for landscaping.
- **Reference / mirror**: build one half, then `mirror_subassembly`. Cathedrals, castles, vehicles often have symmetry.
- **Assembly ports**: after building a module, use `analyze_assembly_ports(subassembly)` to see exposed studs/receivers, and `find_subassembly_connections(movable, target)` before moving or cloning large modules.
- **Human build order**: after a target model validates, use `plan_build_sequence(...)` for paged step-by-step instructions or `next_build_step(...)` to advance one placeable piece at a time.
- **Color coding subassemblies**: use vivid colors during the build to keep mechanisms visually distinct, then re-color before final render.
"""


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def register_resources(mcp) -> None:
    """Attach LegoMCP resources to a FastMCP instance. Resources are read-only
    pieces of data the LLM can fetch; complements the action-taking tools."""

    @mcp.resource("lego://techniques", mime_type="text/markdown")
    def techniques_resource() -> str:
        return "# LEGO building techniques\n\n" + TECHNIQUES_BLURB

    @mcp.resource("lego://coords", mime_type="text/markdown")
    def coords_resource() -> str:
        return "# LDraw coordinate convention\n\n" + COORDS_BLURB

    @mcp.resource("lego://workflow", mime_type="text/markdown")
    def workflow_resource() -> str:
        return "# Recommended build workflow\n\n" + WORKFLOW_BLURB

    @mcp.resource("lego://model/current", mime_type="application/json")
    def current_model_resource() -> str:
        import json
        from lego_mcp import server
        return json.dumps({
            "model": server.STATE.name,
            "current_subassembly": server.STATE.current_subassembly,
            "parts_total": len(server.STATE.parts),
            "subassemblies": sorted({p.subassembly for p in server.STATE.parts.values()}),
            "notes": list(server.STATE.notes.keys()),
        })


def register_prompts(mcp) -> None:
    """Attach all LegoMCP prompts to a FastMCP instance."""

    @mcp.prompt(
        name="build",
        title="Build a LEGO model",
        description="Start a fresh LegoMCP session with a build goal.",
    )
    def build(goal: str, scale: str = "minifig") -> list[dict]:
        """Begin a build session.

        Args:
            goal: What to build (e.g. "a small red house with a blue roof").
            scale: minifig | micro | display. Influences part choice & footprint.
        """
        return [_user(f"""\
You're now driving the LegoMCP server. Build target: **{goal}** at **{scale}** scale.

{COORDS_BLURB}

{WORKFLOW_BLURB}

{TECHNIQUES_BLURB}

Begin by:
1. `create_model("{goal[:30].strip().replace(' ', '_')}")`
2. Sketch the overall structure (a footprint and a list of subassemblies) before placing any brick.
3. Place a baseplate (3811 for 32x32, 3857 for 16x16) and confirm with a render.
4. Build out one subassembly at a time, validating + rendering after each.

If you're stuck or the render looks wrong, use `undo` or `restore_checkpoint`. Don't push through obviously broken intermediate states.
""")]

    @mcp.prompt(
        name="from_plans",
        title="Build from architectural plans",
        description="Build a complex model (cathedral, vehicle, etc.) from uploaded plans or photos.",
    )
    def from_plans(subject: str) -> list[dict]:
        """Multi-modal build from plans.

        Args:
            subject: What the plans show (e.g. "Strasbourg Cathedral").
        """
        return [_user(f"""\
You'll be building **{subject}** in LEGO from architectural plans the user uploads.

Before placing any brick:
1. Look at every uploaded image. Identify the major masses (towers, nave, transept, dome, etc.).
2. Estimate dimensions in studs. Pick a scale: how many studs equal one floor / one bay?
   Quote your estimates back in chat so the user can correct them ("the spire is ~30 studs tall").
3. Decompose into subassemblies. List them. Get user buy-in before you start placing parts.

{COORDS_BLURB}

{WORKFLOW_BLURB}

{TECHNIQUES_BLURB}

For this build specifically:
- Use the full LDraw library (`search_parts` will find arches, columns, windows, ornaments).
- Bilateral symmetry is your friend — build half, mirror it.
- Render OFTEN. After every subassembly is finished. After every major join.
- Save a checkpoint before each subassembly attempt — easy rollback if a section goes sideways.
""")]

    @mcp.prompt(
        name="from_image",
        title="Build from a reference image",
        description="Build a LEGO replica from an uploaded photo or rendering.",
    )
    def from_image() -> list[dict]:
        return [_user(f"""\
You'll be building from a reference image the user uploads.

1. Look at the image. Identify the subject and its overall proportions.
2. Estimate a scale (how many studs wide is the build?).
3. State your plan in chat before building. The user can correct color / scale / interpretation.

{COORDS_BLURB}

{WORKFLOW_BLURB}

After each render, compare to the reference image and adjust. Don't claim it's "done" until the silhouette and key features visibly match.
""")]

    @mcp.prompt(
        name="rescue",
        title="Improve or repair an existing model",
        description="Load a .mpd / .ldr file and clean it up: fix collisions, add missing detail, improve structure.",
    )
    def rescue(path: str) -> list[dict]:
        return [_user(f"""\
A user has handed you an existing model at `{path}`. Your job is to improve it.

1. `import_ldr("{path}")` to load it.
2. `list_parts()` to see what's there. `render_model()` to see the shape.
3. `validate_model()` — fix any reported collisions or unknown parts FIRST.
4. Then propose improvements (additional detail, color cleanup, structural reinforcement).
   State your plan in chat before making changes. The user may have reasons for what's there.
5. `save_checkpoint("before_improvements")` before you start changing things.

{COORDS_BLURB}

{TECHNIQUES_BLURB}
""")]

    @mcp.prompt(
        name="techniques",
        title="LEGO building techniques cheat sheet",
        description="Print a reference of known LEGO building techniques.",
    )
    def techniques() -> list[dict]:
        return [_user(f"""\
Here are well-known LEGO building techniques relevant to this toolchain. Apply them rather than reinventing geometry.

{TECHNIQUES_BLURB}

You also have these LegoMCP helpers (call them by name with no arguments to see signatures):
- `build_wall(x0, z0, x1, z1, height, color, bond)`
- `build_perimeter(points, height_rows, color, thickness_studs, palette)`
- `build_wall_with_openings(start_x, start_z, end_x, end_z, height_rows, openings)`
- `build_stepped_gable_roof(x_min, z_min, x_max, z_max, eave_y, ridge_axis)`
- `build_stepped_pyramid_roof(x_min, z_min, x_max, z_max, eave_y)`
- `build_floor(x_min, z_min, x_max, z_max, y, color, part)`
- `build_room(x_min, z_min, x_max, z_max, height_rows, color)`
- `clone_subassembly(src, dst, x_offset, y_offset, z_offset)`
- `mirror_subassembly(src, dst, axis, plane_offset)`
- `analyze_assembly_ports(subassembly)`
- `find_subassembly_connections(movable, target)`
- `plan_build_sequence(subassembly, max_steps, start_after)`
- `next_build_step(subassembly, built_count)`
""")]
