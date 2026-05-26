"""Part and color catalog for LegoMCP.

A small built-in inventory works out of the box. `install_library()` downloads
the full LDraw library (~85 MB) for users who want the entire catalog.

LDraw units: 1 stud = 20 LDU wide. 1 plate = 8 LDU tall. 1 brick = 24 LDU tall (= 3 plates).
LDraw axis: -Y is up. Origins for these parts are at the geometric center of the bottom face.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

LDU_PER_STUD = 20
LDU_PER_PLATE = 8
LDU_PER_BRICK = 24  # 3 plates


@dataclass(frozen=True)
class Part:
    """A part definition. Size is in LDU. Origin is center-bottom.

    `studs` is the list of (x, y, z) positions where this part has a TOP stud,
    in part-local coordinates. Empty for tiles, smooth-top parts, or when stud
    geometry couldn't be parsed.
    """
    part_id: str
    name: str
    width: int       # +X extent, LDU
    depth: int       # +Z extent, LDU
    height: int      # height upward (toward -Y), LDU
    studs: tuple[tuple[float, float, float], ...] = ()


# LDraw convention: a "Brick A x B" has the LARGER dimension along +X (width)
# and the smaller along +Z (depth). Helper args (long_studs, short_studs)
# reflect physical dimensions in studs; the human-readable name keeps the
# conventional "AxB" ordering.

def _brick(part_id: str, long_studs: int, short_studs: int, plates: int = 3,
           name: str | None = None) -> Part:
    return Part(
        part_id=part_id,
        name=name or f"Brick {short_studs}x{long_studs}",
        width=long_studs * LDU_PER_STUD,
        depth=short_studs * LDU_PER_STUD,
        height=plates * LDU_PER_PLATE,
    )


def _plate(part_id: str, long_studs: int, short_studs: int, name: str | None = None) -> Part:
    return _brick(part_id, long_studs, short_studs, plates=1,
                  name=name or f"Plate {short_studs}x{long_studs}")


def _tile(part_id: str, long_studs: int, short_studs: int, name: str | None = None) -> Part:
    return _brick(part_id, long_studs, short_studs, plates=1,
                  name=name or f"Tile {short_studs}x{long_studs}")


# Built-in catalog: ~30 common bricks/plates/tiles + a baseplate.
# Dimensions follow LDraw's actual part geometry (long axis = +X).
BUILTIN_PARTS: dict[str, Part] = {p.part_id: p for p in [
    # Bricks  (long, short)
    _brick("3005", 1, 1),
    _brick("3004", 2, 1),
    _brick("3622", 3, 1),
    _brick("3010", 4, 1),
    _brick("3009", 6, 1),
    _brick("3008", 8, 1),
    _brick("3003", 2, 2),
    _brick("3002", 3, 2),
    _brick("3001", 4, 2),
    _brick("2456", 6, 2),
    _brick("3006", 10, 2),
    # Plates
    _plate("3024", 1, 1),
    _plate("3023", 2, 1),
    _plate("3623", 3, 1),
    _plate("3710", 4, 1),
    _plate("3666", 6, 1),
    _plate("3460", 8, 1),
    _plate("4477", 10, 1),
    _plate("3022", 2, 2),
    _plate("3021", 3, 2),
    _plate("3020", 4, 2),
    _plate("3795", 6, 2),
    _plate("3034", 8, 2),
    _plate("3832", 10, 2),
    _plate("3031", 4, 4),
    _plate("3035", 8, 4),
    _plate("3036", 8, 6),
    # Tiles
    _tile("3070b", 1, 1),
    _tile("3069b", 2, 1),
    _tile("2431",  4, 1),
    _tile("3068b", 2, 2),
    # Slopes (AABB is the bounding box — the slope itself is inside)
    _brick("3040", 2, 1, name="Slope 45 1x2"),
    _brick("3039", 2, 2, name="Slope 45 2x2"),
    _brick("3037", 4, 2, name="Slope 45 2x4"),
    # Baseplates
    Part("3811", "Baseplate 32x32", 32 * LDU_PER_STUD, 32 * LDU_PER_STUD, LDU_PER_PLATE // 2),
    Part("3857", "Baseplate 16x16", 16 * LDU_PER_STUD, 16 * LDU_PER_STUD, LDU_PER_PLATE // 2),
]}


# Common LDraw colors. Names lowercased for forgiving lookup.
# (LDraw IDs are authoritative; names are convenience.)
COLORS: dict[str, tuple[int, tuple[int, int, int]]] = {
    "black":            (0,  (0x05, 0x13, 0x1d)),
    "blue":             (1,  (0x00, 0x55, 0xbf)),
    "green":            (2,  (0x25, 0x7a, 0x3e)),
    "dark_turquoise":   (3,  (0x00, 0x83, 0x8f)),
    "red":              (4,  (0xc9, 0x1a, 0x09)),
    "brown":            (6,  (0x54, 0x33, 0x24)),
    "light_gray":       (7,  (0x8a, 0x92, 0x8d)),
    "dark_gray":        (8,  (0x54, 0x5c, 0x66)),
    "light_blue":       (9,  (0xb4, 0xd2, 0xe3)),
    "bright_green":     (10, (0x4b, 0x9f, 0x4a)),
    "yellow":           (14, (0xf2, 0xcd, 0x37)),
    "white":            (15, (0xf4, 0xf4, 0xf4)),
    "tan":              (19, (0xe4, 0xcd, 0x9e)),
    "orange":           (25, (0xfe, 0x8a, 0x18)),
    "lime":             (27, (0xbb, 0xe9, 0x0b)),
    "dark_tan":         (28, (0x95, 0x82, 0x5c)),
    "magenta":          (26, (0x90, 0x1f, 0x76)),
    "pink":             (29, (0xfc, 0x97, 0xac)),
    "purple":           (22, (0x81, 0x00, 0x7b)),
    "light_bluish_gray":(71, (0xa0, 0xa5, 0xa9)),
    "dark_bluish_gray": (72, (0x6c, 0x6e, 0x68)),
    "trans_clear":      (47, (0xfc, 0xfc, 0xfc)),
}

COLOR_BY_ID: dict[int, tuple[str, tuple[int, int, int]]] = {
    cid: (name, rgb) for name, (cid, rgb) in COLORS.items()
}


def resolve_color(value: str | int) -> int:
    """Accept a name ("red") or LDraw color ID (4). Return ID."""
    if isinstance(value, int):
        return value
    s = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    if s.isdigit():
        return int(s)
    if s in COLORS:
        return COLORS[s][0]
    raise ValueError(f"Unknown color: {value!r}. Try a name like 'red' or an LDraw color ID.")


def color_rgb(color_id: int) -> tuple[int, int, int]:
    """Return RGB for known colors, else a placeholder gray."""
    entry = COLOR_BY_ID.get(color_id)
    return entry[1] if entry else (0x80, 0x80, 0x80)


# ---------------------------------------------------------------------------
# Optional full LDraw library
# ---------------------------------------------------------------------------

LDRAW_HOME = Path(os.environ.get("LDRAW_HOME", str(Path.home() / "Library" / "ldraw")))
LDRAW_URL = "https://library.ldraw.org/library/updates/complete.zip"
CACHE_FILE = Path.home() / ".cache" / "lego_mcp" / "parts_index.json"


def install_library(target: Path | None = None) -> Path:
    """Download and unpack the full LDraw library (~85 MB)."""
    dest = target or LDRAW_HOME
    dest.mkdir(parents=True, exist_ok=True)
    zip_path = dest.parent / "ldraw_complete.zip"
    print(f"Downloading LDraw library to {zip_path}\n  from {LDRAW_URL}\n  ...")
    # library.ldraw.org blocks the default urllib User-Agent.
    req = urllib.request.Request(LDRAW_URL, headers={
        "User-Agent": "LegoMCP/0.1 (+https://github.com/jknipper/LegoMCP)",
    })
    with urllib.request.urlopen(req) as resp, open(zip_path, "wb") as out:
        total = resp.headers.get("Content-Length")
        if total:
            print(f"  download size: {int(total) / 1024 / 1024:.1f} MB")
        shutil.copyfileobj(resp, out)
    print(f"Unpacking into {dest}...")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest.parent)
    zip_path.unlink(missing_ok=True)
    print(f"Done. {dest} populated. The server will index part metadata on next start.")
    return dest


_STUD_FILES = {"stud.dat", "stud2.dat", "stud3.dat", "stud4.dat"}
# stud4a, studx, etc. are "open" / "anti-stud" variants used for bottom
# connection points — we deliberately skip them when collecting TOP studs.


def _parse_dat(path: Path) -> tuple[str, list[tuple[str, str, tuple[float, ...]]], list[tuple[float, float, float]]] | None:
    """Parse a .dat file.

    Returns (name, type1_refs, prim_vertices) where:
        type1_refs: list of (filename, head_token, full_floats) — head_token is
            color, full_floats is (x,y,z, a,b,c, d,e,f, g,h,i) (12 floats).
        prim_vertices: flat list of (x,y,z) tuples from primitives (types 2..5)
            for AABB calculation.
    """
    try:
        text = path.read_text(encoding="latin-1", errors="ignore")
    except OSError:
        return None
    name = ""
    type1_refs: list[tuple[str, str, tuple[float, ...]]] = []
    prim_vertices: list[tuple[float, float, float]] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if not name and s.startswith("0 "):
            name = s[2:].strip()
            continue
        head = s.split(maxsplit=1)[0]
        if head == "1":
            tokens = s.split()
            if len(tokens) < 15:
                continue
            color_tok = tokens[1]
            try:
                values = tuple(float(t) for t in tokens[2:14])
            except ValueError:
                continue
            # Filename is everything after the 14 fixed tokens; normalize separators.
            fname = " ".join(tokens[14:]).strip().lower().replace("\\", "/")
            type1_refs.append((fname, color_tok, values))
        elif head in ("2", "3", "4", "5"):
            parts = s.split()
            try:
                coords = list(map(float, parts[2:]))
            except ValueError:
                continue
            for i in range(0, len(coords), 3):
                if i + 2 >= len(coords):
                    break
                prim_vertices.append((coords[i], coords[i + 1], coords[i + 2]))
    return name, type1_refs, prim_vertices


def _collect_vertices(filename: str, transform: tuple[float, ...], ldraw_home: Path,
                      visited: set[str], max_depth: int = 3, depth: int = 0,
                      ) -> list[tuple[float, float, float]]:
    """Recursively gather all primitive vertices in part-local coords.

    For each type-1 reference into the s/ subdir, recurse with the composed
    transform. Cycles guarded by visited set + depth limit.
    """
    if depth > max_depth or filename in visited:
        return []
    visited.add(filename)
    parts_dir = ldraw_home / "parts"
    s_dir = parts_dir / "s"
    path = parts_dir / filename
    if not path.is_file():
        path = s_dir / Path(filename).name
    if not path.is_file():
        return []
    parsed = _parse_dat(path)
    if not parsed:
        return []
    _, refs, prim_vertices = parsed

    out: list[tuple[float, float, float]] = []
    # Transform local primitives.
    for vx, vy, vz in prim_vertices:
        nx = transform[0] + transform[3]*vx + transform[4]*vy + transform[5]*vz
        ny = transform[1] + transform[6]*vx + transform[7]*vy + transform[8]*vz
        nz = transform[2] + transform[9]*vx + transform[10]*vy + transform[11]*vz
        out.append((nx, ny, nz))
    # Recurse into subparts.
    for fname, _color, values in refs:
        base = Path(fname).name
        if base in _STUD_FILES:
            continue  # studs handled separately
        # Recurse if filename looks like a subpart (in s/) or any internal ref.
        composed = (
            transform[0] + transform[3]*values[0] + transform[4]*values[1] + transform[5]*values[2],
            transform[1] + transform[6]*values[0] + transform[7]*values[1] + transform[8]*values[2],
            transform[2] + transform[9]*values[0] + transform[10]*values[1] + transform[11]*values[2],
            transform[3]*values[3] + transform[4]*values[6] + transform[5]*values[9],
            transform[3]*values[4] + transform[4]*values[7] + transform[5]*values[10],
            transform[3]*values[5] + transform[4]*values[8] + transform[5]*values[11],
            transform[6]*values[3] + transform[7]*values[6] + transform[8]*values[9],
            transform[6]*values[4] + transform[7]*values[7] + transform[8]*values[10],
            transform[6]*values[5] + transform[7]*values[8] + transform[8]*values[11],
            transform[9]*values[3] + transform[10]*values[6] + transform[11]*values[9],
            transform[9]*values[4] + transform[10]*values[7] + transform[11]*values[10],
            transform[9]*values[5] + transform[10]*values[8] + transform[11]*values[11],
        )
        out.extend(_collect_vertices(fname, composed, ldraw_home, visited, max_depth, depth + 1))
    return out


def _parse_dat_header(path: Path) -> tuple[str, tuple[float, float, float, float, float, float]] | None:
    """Read a .dat file's description and compute its AABB, recursing into subparts.

    Many LDraw parts have most of their geometry in a subpart file under s/.
    Without recursion, the parent's AABB is tiny (just the bits drawn at the
    top level). The recursive walk handles common 1-2 level nesting.
    """
    parsed = _parse_dat(path)
    if not parsed:
        return None
    name, _, _ = parsed
    # Walk recursively from this file with identity transform.
    identity = (0.0, 0.0, 0.0,  1.0, 0.0, 0.0,  0.0, 1.0, 0.0,  0.0, 0.0, 1.0)
    ldraw_home = path.parent.parent if path.parent.name == "parts" else path.parent.parent.parent
    vertices = _collect_vertices(path.name, identity, ldraw_home, visited=set())
    if not vertices:
        return None
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    zs = [v[2] for v in vertices]
    return name, (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))


def _matmul(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, ...]:
    """Compose two LDraw transforms: each is (x,y,z, a..i). Returns the same shape."""
    ax, ay, az = a[0], a[1], a[2]
    a_m = a[3:12]
    bx, by, bz = b[0], b[1], b[2]
    b_m = b[3:12]
    # World position = a_pos + a_mat @ b_pos
    nx = ax + a_m[0]*bx + a_m[1]*by + a_m[2]*bz
    ny = ay + a_m[3]*bx + a_m[4]*by + a_m[5]*bz
    nz = az + a_m[6]*bx + a_m[7]*by + a_m[8]*bz
    # New matrix = a_mat @ b_mat
    def row(i):
        return (
            a_m[i*3]*b_m[0] + a_m[i*3+1]*b_m[3] + a_m[i*3+2]*b_m[6],
            a_m[i*3]*b_m[1] + a_m[i*3+1]*b_m[4] + a_m[i*3+2]*b_m[7],
            a_m[i*3]*b_m[2] + a_m[i*3+1]*b_m[5] + a_m[i*3+2]*b_m[8],
        )
    nm = row(0) + row(1) + row(2)
    return (nx, ny, nz) + nm


def _extract_studs(part_id: str, ldraw_home: Path, max_depth: int = 5
                   ) -> list[tuple[float, float, float]]:
    """Recursively scan a part for TOP stud positions in part-local coords.

    Recurses through subparts in s/ AND stud-group primitives in p/ (like
    stug-2x2.dat which holds 4 stud.dat refs). Only keeps studs whose
    orientation matrix has e[1][1] > 0 (stud points up in -Y direction).
    Anti-studs (bottom connectors, e[1][1] < 0) are skipped.
    """
    parts_dir = ldraw_home / "parts"
    s_dir = parts_dir / "s"
    p_dir = ldraw_home / "p"
    out: list[tuple[float, float, float]] = []

    def walk(filename: str, transform: tuple[float, ...], depth: int) -> None:
        # No visited-set dedup: the same primitive (e.g. stug-2x2) can legitimately
        # be referenced from many positions in one part (a baseplate uses one stud
        # group per 2x2 cluster). max_depth bounds runaway recursion.
        if depth > max_depth:
            return
        # Resolve: try parts/, parts/s/, then p/ (for stud-group primitives).
        base = Path(filename).name
        path = parts_dir / filename
        if not path.is_file():
            path = s_dir / base
        if not path.is_file():
            path = p_dir / base
        if not path.is_file():
            return
        parsed = _parse_dat(path)
        if not parsed:
            return
        _, refs, _ = parsed
        for fname, _color, values in refs:
            base = Path(fname).name
            if base in _STUD_FILES:
                composed = _matmul(transform, values)
                # e[1][1] = composed[7]. > 0 means stud points DOWN in part
                # frame (+Y), which is UP in LDraw's -Y-is-up convention -> top stud.
                if composed[7] > 0:
                    out.append((composed[0], composed[1], composed[2]))
            else:
                # Always recurse — could be a subpart (s/), a stud-group
                # primitive (p/stug-NxN.dat), or any other internal file.
                composed = _matmul(transform, values)
                walk(fname, composed, depth + 1)

    identity = (0.0, 0.0, 0.0,  1.0, 0.0, 0.0,  0.0, 1.0, 0.0,  0.0, 0.0, 1.0)
    walk(f"{part_id}.dat", identity, 0)
    return out


def load_library_index(ldraw_home: Path | None = None) -> dict[str, Part]:
    """Scan the LDraw parts dir and produce a Part for every part file.

    Cached to ~/.cache/lego_mcp/parts_index.json. If the cache is fresher than
    the parts dir, it's loaded directly.

    Returns the BUILTIN_PARTS dict if no LDraw library is found.
    """
    home = ldraw_home or LDRAW_HOME
    parts_dir = home / "parts"
    if not parts_dir.is_dir():
        return dict(BUILTIN_PARTS)

    if CACHE_FILE.exists():
        cache_mtime = CACHE_FILE.stat().st_mtime
        parts_mtime = parts_dir.stat().st_mtime
        if cache_mtime >= parts_mtime:
            try:
                data = json.loads(CACHE_FILE.read_text())
                return {
                    pid: Part(
                        part_id=pid, name=p["name"],
                        width=p["width"], depth=p["depth"], height=p["height"],
                        studs=tuple(tuple(s) for s in p.get("studs", [])),
                    )
                    for pid, p in data.items()
                }
            except (OSError, ValueError, KeyError):
                pass

    index: dict[str, Part] = {}
    for dat in parts_dir.glob("*.dat"):
        result = _parse_dat_header(dat)
        if not result:
            continue
        name, (minx, miny, minz, maxx, maxy, maxz) = result
        part_id = dat.stem
        # Extract real top-stud positions by recursively parsing the part.
        # Skip on parse failure (no studs is also fine for tiles/baseplates).
        try:
            studs = tuple(_extract_studs(part_id, home))
        except Exception:  # noqa: BLE001
            studs = ()
        index[part_id] = Part(
            part_id=part_id,
            name=name,
            width=max(1, int(round(maxx - minx))),
            depth=max(1, int(round(maxz - minz))),
            height=max(1, int(round(maxy - miny))),
            studs=studs,
        )
    # The LDraw-parsed entries are authoritative; only fall back to built-ins
    # for parts that aren't in the library.
    for pid, p in BUILTIN_PARTS.items():
        index.setdefault(pid, p)

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(
        {pid: {"name": p.name, "width": p.width, "depth": p.depth, "height": p.height,
               "studs": [list(s) for s in p.studs]}
         for pid, p in index.items()},
    ))
    return index


_SIZE_RE = re.compile(r"(\d+)\s*[x×]\s*(\d+)(?:\s*[x×]\s*(\d+))?")


def _normalize(text: str) -> str:
    """Lowercase, collapse whitespace, and rewrite 'N x N' / 'NxN' to a single 'NxN'."""
    s = text.lower()
    s = _SIZE_RE.sub(
        lambda m: m.group(1) + "x" + m.group(2) + (("x" + m.group(3)) if m.group(3) else ""),
        s,
    )
    s = re.sub(r"\s+", " ", s).strip()
    return s


def search(index: dict[str, Part], query: str, limit: int = 20) -> list[Part]:
    """Case-insensitive search over id and name. Tolerates 'tile 1x4' / 'Tile 1 x 4'.

    A part matches if every whitespace-split token of the normalized query is
    present in the normalized name or part_id.
    """
    q = _normalize(query)
    if not q:
        return []
    tokens = q.split()
    hits = []
    for p in index.values():
        norm_name = _normalize(p.name)
        norm_id = p.part_id.lower()
        if all(tok in norm_name or tok in norm_id for tok in tokens):
            hits.append(p)
    hits.sort(key=lambda p: (len(p.name), p.part_id))
    return hits[:limit]
