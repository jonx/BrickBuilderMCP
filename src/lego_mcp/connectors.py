"""Connection-aware model layer.

Bridges the existing `parts.Part` records (parsed from LDraw .dat files) and
the typed connector model the wall-bonding / floating-detection code
operates on.

Phase 1 only models stud-on-top and bottom stud-receivers (one receiver per
stud cell on the bottom face — an approximation; real bricks have internal
clutch tubes between studs, but a per-stud receiver grid suffices for
checking whether two parts can mate at a given position).

Y-axis convention (LDraw, -Y is up):
- Part origin is the center of its bottom face.
- Top studs sit at local y = -height (the upper face).
- Bottom receivers sit at local y = 0 (the lower face).
- For a brick at world y=0 stacked under a brick at world y=-24, the lower
  brick's top studs resolve to world Y = 0 - 24 = -24, and the upper
  brick's bottom receivers resolve to world Y = -24 + 0 = -24. Same Y.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

from lego_mcp.parts import LDU_PER_STUD, LDU_PER_PLATE, LDU_PER_BRICK, Part

PLATE_HEIGHT = LDU_PER_PLATE      # 8 LDU
BRICK_HEIGHT = LDU_PER_BRICK      # 24 LDU
STUD = LDU_PER_STUD                # 20 LDU


class ConnectorType(str, Enum):
    STUD_TOP = "stud_top"
    STUD_RECEIVER_BOTTOM = "stud_receiver_bottom"
    # Reserved for later phases (not emitted yet):
    STUD_SIDE = "stud_side"
    STUD_RECEIVER_SIDE = "stud_receiver_side"
    PIN = "pin"
    AXLE = "axle"
    CLIP = "clip"
    BAR = "bar"


@dataclass(frozen=True)
class Connector:
    """A connection point in PART-LOCAL coords.

    Use `world_position` to project to world coords given a part instance's
    rotation + translation.
    """
    type: ConnectorType
    x: float
    y: float
    z: float


@dataclass(frozen=True)
class PartDefinition:
    """A typed view of a Part used by the connection-aware model.

    Built from `parts.Part` for the supported subset; other parts use the
    underlying AABB-based path.
    """
    part_id: str
    name: str
    width_studs: int     # +X axis (LDraw long axis by convention)
    depth_studs: int     # +Z axis
    height_plates: int   # 1 = plate, 3 = brick
    connectors: tuple[Connector, ...]
    allowed_rotations: tuple[str, ...] = (
        "identity", "rot90y", "rot180y", "rot270y",
    )

    @property
    def width_ldu(self) -> int:
        return self.width_studs * STUD

    @property
    def depth_ldu(self) -> int:
        return self.depth_studs * STUD

    @property
    def height_ldu(self) -> int:
        return self.height_plates * PLATE_HEIGHT


def _stud_grid(width_studs: int, depth_studs: int, y: float
                ) -> Iterable[tuple[float, float, float]]:
    """Yield stud-grid XZ positions at the given local Y."""
    for i in range(width_studs):
        for j in range(depth_studs):
            x = -width_studs * STUD / 2 + STUD / 2 + i * STUD
            z = -depth_studs * STUD / 2 + STUD / 2 + j * STUD
            yield (x, y, z)


# The supported subset per the spec. Each entry: (part_id, width_studs,
# depth_studs, height_plates, friendly_name). LDraw "Brick AxB" by convention
# has the LONGER axis along +X.
_SUPPORTED: tuple[tuple[str, int, int, int, str], ...] = (
    ("3001", 4, 2, 3, "Brick 2x4"),
    ("3002", 3, 2, 3, "Brick 2x3"),
    ("3003", 2, 2, 3, "Brick 2x2"),
    ("3004", 2, 1, 3, "Brick 1x2"),
    ("3010", 4, 1, 3, "Brick 1x4"),
    ("3020", 4, 2, 1, "Plate 2x4"),
    ("3021", 3, 2, 1, "Plate 2x3"),
    ("3022", 2, 2, 1, "Plate 2x2"),
    ("3023", 2, 1, 1, "Plate 1x2"),
    ("3710", 4, 1, 1, "Plate 1x4"),
)


def _build_definition(pid: str, w: int, d: int, h: int, name: str) -> PartDefinition:
    height_ldu = h * PLATE_HEIGHT
    studs_top = [Connector(ConnectorType.STUD_TOP, x, y, z)
                 for x, y, z in _stud_grid(w, d, -height_ldu)]
    # Phase 1 approximation: one bottom-receiver per top stud, at local y=0
    # (the part's bottom face). Real bricks have internal clutch tubes between
    # studs and the underside is a cavity, but a stud-grid receiver model is
    # the right granularity for "does this stack on that?"
    receivers_bottom = [Connector(ConnectorType.STUD_RECEIVER_BOTTOM, x, 0.0, z)
                        for x, _, z in _stud_grid(w, d, 0.0)]
    return PartDefinition(
        part_id=pid,
        name=name,
        width_studs=w,
        depth_studs=d,
        height_plates=h,
        connectors=tuple(studs_top + receivers_bottom),
    )


SUPPORTED_DEFINITIONS: dict[str, PartDefinition] = {
    pid: _build_definition(pid, w, d, h, name)
    for (pid, w, d, h, name) in _SUPPORTED
}


def definition_for(part_id: str) -> PartDefinition | None:
    """Return a PartDefinition for the supported subset; None for other parts."""
    return SUPPORTED_DEFINITIONS.get(part_id)


# ---------------------------------------------------------------------------
# Local → world projection
# ---------------------------------------------------------------------------

# Six canonical rotations as 3x3 matrices (row-major). Mirrors server.ROTATIONS;
# duplicated here to keep this module free of server imports.
_ROTATIONS: dict[str, tuple[float, ...]] = {
    "identity": (1, 0, 0, 0, 1, 0, 0, 0, 1),
    "rot90y":   (0, 0, 1, 0, 1, 0, -1, 0, 0),
    "rot180y":  (-1, 0, 0, 0, 1, 0, 0, 0, -1),
    "rot270y":  (0, 0, -1, 0, 1, 0, 1, 0, 0),
    "rot90x":   (1, 0, 0, 0, 0, -1, 0, 1, 0),
    "rot90z":   (0, -1, 0, 1, 0, 0, 0, 0, 1),
}


def _apply_rotation(rot_name: str, p: tuple[float, float, float]
                     ) -> tuple[float, float, float]:
    m = _ROTATIONS[rot_name]
    x, y, z = p
    return (m[0]*x + m[1]*y + m[2]*z,
            m[3]*x + m[4]*y + m[5]*z,
            m[6]*x + m[7]*y + m[8]*z)


@dataclass(frozen=True)
class WorldConnector:
    """A connector in world coords. `instance_id` and `local_index` let the
    caller trace back to which part this came from."""
    type: ConnectorType
    x: float
    y: float
    z: float
    instance_id: str
    local_index: int


def world_connectors(instance_id: str, defn: PartDefinition,
                     instance_x: float, instance_y: float, instance_z: float,
                     rotation: str = "identity") -> list[WorldConnector]:
    """Project every connector to world coords given the instance's pose."""
    out: list[WorldConnector] = []
    for i, c in enumerate(defn.connectors):
        rx, ry, rz = _apply_rotation(rotation, (c.x, c.y, c.z))
        out.append(WorldConnector(
            type=c.type,
            x=rx + instance_x,
            y=ry + instance_y,
            z=rz + instance_z,
            instance_id=instance_id,
            local_index=i,
        ))
    return out
