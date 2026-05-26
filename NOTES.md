# LegoMCP — Architecture and Decision Log

## Architecture at a glance

- MCP server, Python, stdio transport, FastMCP API.
- One file does the work: `server.py` holds model state + all tools.
- LDraw is the source of truth. Internal state is just a list of `(part_id, color, x, y, z, rotation)`. Export is `.ldr` / `.mpd`.
- Built-in catalog of ~30 common bricks so the server works out of the box. `lego-mcp install-library` downloads the full LDraw library (~85 MB) for users who want more.
- No GUI automation. LDraw viewers open the exported file.

## Key decisions

### Keep it in one file until it hurts
First pass is single-module on purpose. Splitting into geometry / ldraw / model / validate / server modules is what I'd do for the v2; for v1 it's premature decomposition. ~300–500 lines is fine to read top-to-bottom.

### Built-in catalog, optional full library
85 MB on first run is a friction tax. The built-in set covers basic bricks/plates/tiles/slopes — enough for the LLM to build something fun on day one. The full library is one command away when needed.

### Six canonical rotations, not raw matrices
LDraw stores arbitrary 3×3 matrices, but LEGO snaps to 90°. Exposing full SO(3) to an LLM means half the tool calls produce visually broken models. We expose six names: `identity`, `rot90y`, `rot180y`, `rot270y`, `rot90x`, `rot90z`. Add a matrix escape hatch later if needed.

### AABB collision only
Real geometry intersection means parsing primitive geometry in `.dat` files. Big project. AABB catches gross errors and is enough to keep the LLM honest. Known limitation: tiles on studs may register as colliding because AABBs include stud height.

### Coordinate convention
LDraw: right-handed, **-Y is up**, 1 brick = 20 LDU wide × 24 LDU tall. We don't flip anything for the LLM — every docstring spells it out.

## North star

The ultimate target for this toolchain is **building the Strasbourg Cathedral astronomical clock from its actual plans**, with Claude doing the design work using multimodal input (the plans are images / PDFs). That implies:

- Real LDraw library (the built-in 36 parts is just for "hello, brick" — a serious model needs the full ~20k catalog).
- Subassemblies via MPD `0 FILE` blocks (the clock has dozens of independent mechanisms — gears, jacquemarts, calendar dial — that must be authored separately and composed).
- Reference-frame tools so Claude can quote dimensions back to itself across long sessions ("the gothic spire is 12 studs across at the base, 4 at the top").
- Eventually Technic / gears / non-90° rotations.

None of these change Phase 1 *yet*, but I want the seams to exist:

- LDraw I/O will read multi-block MPD files even though we author flat for now (so we don't lock users out of complex imports).
- Rotation is a named enum today; switching to a matrix escape hatch is a single tool addition.
- `place_subassembly` will land as a "stamp this list of parts at this transform" tool before hierarchical editing — gives compositional power without restructuring state.

## With more time

- Real stud-clutch validation from `.dat` connection geometry.
- Hierarchical model state (subassembly tree) replacing the flat list.
- `render_model` shelling to LDView for photoreal renders (we ship with a built-in AABB renderer).
- Friendly color name → LDraw ID lookup (already in, ~22 colors).
- A small agent script (planner / builder / critic) driving Claude against this server.
- Persistent named projects on disk (not just MPD export).

## Things to discuss in the walkthrough

- Is 6 rotations the right ceiling, or jump straight to matrices?
- Should `validate_model` block mutations on collision, or always allow + report?
- Built-in catalog: which 30 parts? Currently leaning toward classic City-set bricks.

## Known limitations after the buildability work

- **Corner bonding in `build_room` isn't proper LEGO masonry.** The corner
  blocks are a vertical column of 2x2 bricks that only bond up/down, not
  sideways into the perpendicular walls. Real masonry alternates the bond
  per row: row 0 has the X-wall reaching the corner; row 1 the Z-wall. To
  implement: `build_room` would need to place corner bricks WITH a rotation
  alternating per row, and the inset on perpendicular walls would similarly
  need to alternate. Workaround for now: build walls + corner pieces manually
  for any structure where corner strength matters.
- **`MIN_SUPPORT_AREA = 400 LDU²` is one full stud overlap.** Real LEGO
  studs are circular (~6 LDU radius); my check uses axis-aligned XZ
  rectangles. A part with 400 LDU² of rectangular overlap definitely has a
  full stud's worth of clutch; under-400 might still have partial overlap.
  Acceptable for now.
- **No half-stud / jumper-plate support.** All placements snap to integer
  LDU positions; the LLM can use 8-LDU plate offsets vertically but
  horizontal half-stud offsets (jumper plates) aren't a first-class concept.

## Self-test findings (Phase 1)

I built a tiny house (46 parts), a gothic tower (72 parts), a 4-tower castle
(120 parts), and a cathedral facade attempt (92 parts) end-to-end through the
server's own tools. Every render is in [renders/](renders/). Bugs caught while
doing that and fixed in the same session:

- **Built-in dim convention was backwards.** A "Brick 2x4" in LDraw is 80 LDU
  along +X and 40 along +Z; my helper had the args swapped. Exports were valid
  LDraw but viewers showed them rotated 90deg from my Python renders. Fixed in
  parts.py + regression test.
- **`STATE` was rebound, not mutated.** `create_model` / `import_ldr` /
  `restore_checkpoint` replaced the module attribute, silently breaking any
  external `from server import STATE`. Now all three mutate in place.
- **Library wasn't loaded for direct use.** `PART_INDEX` was populated only in
  `run()`, so scripts that imported tools directly saw only 36 built-ins.
  Lazy-loads on first miss now.
- **Search was too strict.** `"tile 1x4"` couldn't find LDraw's `"Tile  1 x  4"`.
  Added size-pattern normalization (`1x4` <-> `1 x 4`) and token-AND matching.
- **`install-library` needed a real User-Agent.** library.ldraw.org returns 403
  to default urllib.
- **Renderer's painter sort fails on big-vs-small overlaps.** A baseplate's
  centroid is closer to camera than a small brick on top of it, so naive
  per-object painter's puts the baseplate in front. Subdividing large faces
  into ~2-stud chunks lets the per-face sort handle stacking correctly.

What I would NOT change yet, despite weak points I noticed:

- AABB collision still produces some false positives at corners (a 2x4 brick
  against a 2x2 brick at the corner). Acceptable — the LLM can read the error
  list and override.
- The built-in renderer doesn't show studs or slope geometry. Slopes render as
  AABBs (visually a brick of the same footprint). Path to fix: render slope
  surface, or shell out to LDView for high-quality output.
- Outlines bleed through occlusion when enabled. Currently disabled; the
  three-tone shading reads as 3D well enough for iteration.
