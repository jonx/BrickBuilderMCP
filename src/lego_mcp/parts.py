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
    """A part definition. Size is in LDU. Origin is center-bottom."""
    part_id: str
    name: str
    width: int       # +X extent, LDU
    depth: int       # +Z extent, LDU
    height: int      # height upward (toward -Y), LDU


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


def _parse_dat_header(path: Path) -> tuple[str, tuple[float, float, float, float, float, float]] | None:
    """Read a .dat file's description and compute an AABB from its primitive vertices.

    Returns (name, (minx, miny, minz, maxx, maxy, maxz)) or None if unparseable.
    AABBs from primitives only (line types 2/3/4); subfile references (type 1) are ignored.
    Good enough for built-in-style AABB collision in the common case.
    """
    try:
        text = path.read_text(encoding="latin-1", errors="ignore")
    except OSError:
        return None
    name = ""
    minp = [float("inf")] * 3
    maxp = [float("-inf")] * 3
    has_any = False
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if not name and s.startswith("0 "):
            name = s[2:].strip()
        head = s.split(maxsplit=1)[0]
        if head in ("2", "3", "4", "5"):
            parts = s.split()
            # Line types 2..5: type colour x1 y1 z1 x2 y2 z2 ...
            try:
                coords = list(map(float, parts[2:]))
            except ValueError:
                continue
            for i in range(0, len(coords), 3):
                if i + 2 >= len(coords):
                    break
                x, y, z = coords[i], coords[i + 1], coords[i + 2]
                if x < minp[0]: minp[0] = x
                if y < minp[1]: minp[1] = y
                if z < minp[2]: minp[2] = z
                if x > maxp[0]: maxp[0] = x
                if y > maxp[1]: maxp[1] = y
                if z > maxp[2]: maxp[2] = z
                has_any = True
    if not has_any:
        return None
    return name, (minp[0], minp[1], minp[2], maxp[0], maxp[1], maxp[2])


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
                    pid: Part(part_id=pid, name=p["name"], width=p["width"],
                              depth=p["depth"], height=p["height"])
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
        # Convert AABB extents to width/depth/height (rounded to nearest LDU).
        # -Y is up so "height" is the upward extent (away from -Y).
        index[part_id] = Part(
            part_id=part_id,
            name=name,
            width=max(1, int(round(maxx - minx))),
            depth=max(1, int(round(maxz - minz))),
            height=max(1, int(round(maxy - miny))),
        )
    # Always include the built-in entries — they're vetted.
    for pid, p in BUILTIN_PARTS.items():
        index.setdefault(pid, p)

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(
        {pid: {"name": p.name, "width": p.width, "depth": p.depth, "height": p.height}
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
