"""Read Vortex ``.vrtx`` project files.

A ``.vrtx`` file is::

    b"VRTX" + version byte + zstd stream

The decompressed payload is a bincode-encoded ``ProjectDataV2``:

    Option<String> project_id
    Vec<InstanceData> instances
    LightingData lighting

Only ``Part`` instances contain geometry.  Their data is stored inline as a
``PartData`` value, so this module can pull the blocks out without having to
understand scripts, folders, lights or the editor UI.

The instance records are not fully documented by Vortex, so parts are found
by their on-disk signature instead of decoding every unrelated instance.  The
signature is deliberately strict (printable names, a valid parent index, a
unit quaternion, an in-range colour and a known material) which keeps random
bytes inside scripts from being mistaken for geometry.
"""

from __future__ import annotations

import io
import math
import struct
from dataclasses import dataclass


MAGIC = b"VRTX"

# The game writes version 4 and 5 files.  Later versions are accepted as long
# as the zstd stream and ProjectDataV2 header still look right.
MAX_VERSION = 32

# MaterialKindData, in the order the engine declares (and therefore serialises)
# it.  The names are the engine's own UI strings.
MATERIAL_NAMES = (
    "Smooth",
    "Plastic",
    "Wood",
    "Metal",
    "Grass",
    "Ice",
    "Paint",
    "Brick",
    "Sand",
)

# Every kind except "Smooth" has an embedded texture set stored as
# ``materials/<Kind>/<kind>_{albedo,normal,orm}.png`` (and ``brick_mask.png``).
TEXTURED_MATERIALS = frozenset(MATERIAL_NAMES[1:])

# PartShapeData, in engine order.  CornerWedge and Mesh are reserved: no
# sample uses them yet, so the importer falls back to a block for those.
SHAPE_NAMES = (
    "Block",
    "Wedge",
    "CornerWedge",
    "Cylinder",
    "Ball",
    "Mesh",
)

SHAPE_BLOCK = 0
SHAPE_WEDGE = 1
SHAPE_CORNER_WEDGE = 2
SHAPE_CYLINDER = 3
SHAPE_BALL = 4
SHAPE_MESH = 5

# PointLightData is colour (4 x f32), intensity (f32) and range (f32).
# SpotLightData adds angle and face (2 x f32).
_POINT_LIGHT_SIZE = 24
_SPOT_LIGHT_SIZE = 32

# The engine's stud atlas packs 4x4 studs into one tile, so mesh UVs are
# measured in tiles of four studs.
STUDS_PER_TEXTURE_TILE = 4.0

_PART_MARKER = b"\x02\x00\x00\x00"
_MAX_NAME = 1024
_MAX_INSTANCES = 2_000_000


@dataclass(slots=True)
class VrtxPart:
    """One block part from a ``.vrtx`` file."""

    offset: int
    name: str
    part_name: str
    position: tuple[float, float, float]
    rotation: tuple[float, float, float, float]
    scale: tuple[float, float, float]
    color: tuple[float, float, float, float]
    material: int
    cast_shadow: bool = True
    anchored: bool = True
    can_collide: bool = True
    spawn_location: bool = False
    baseplate: bool = False
    custom_appearance: bool = False
    truss: bool = False
    textures: tuple[tuple[int, int], ...] = ()
    shape: int = SHAPE_BLOCK
    # Byte offset of the position/rotation/scale/colour block, used when
    # writing edits back into the original project.
    transform_offset: int = 0

    @property
    def material_name(self) -> str:
        if 0 <= self.material < len(MATERIAL_NAMES):
            return MATERIAL_NAMES[self.material]
        return "Plastic"

    @property
    def shape_name(self) -> str:
        if 0 <= self.shape < len(SHAPE_NAMES):
            return SHAPE_NAMES[self.shape]
        return "Block"


@dataclass(slots=True)
class VrtxProject:
    version: int
    project_id: str | None
    instance_count: int
    parts: list[VrtxPart]


def decompress(data: bytes) -> tuple[int, bytes]:
    """Split the header off a ``.vrtx`` file and inflate its payload."""

    if len(data) < 6 or data[:4] != MAGIC:
        raise ValueError("Not a Vortex .vrtx file (missing VRTX header)")

    version = data[4]

    if version < 1 or version > MAX_VERSION:
        raise ValueError(f"Unsupported .vrtx version {version}")

    try:
        import zstandard
    except ImportError as error:  # pragma: no cover - Blender 5.2 bundles it
        raise RuntimeError(
            "The zstandard module is required to read .vrtx files"
        ) from error

    try:
        with zstandard.ZstdDecompressor().stream_reader(
            io.BytesIO(data[5:])
        ) as reader:
            payload = reader.read()
    except zstandard.ZstdError as error:
        raise ValueError(f"Could not decompress .vrtx payload: {error}") from error

    return version, payload


def _read_string(data: bytes, offset: int) -> tuple[str, int]:
    if offset + 8 > len(data):
        raise ValueError("Truncated string length")

    length = struct.unpack_from("<Q", data, offset)[0]

    if length > _MAX_NAME:
        raise ValueError("String is too long")

    start = offset + 8
    end = start + length

    if end > len(data):
        raise ValueError("Truncated string")

    return data[start:end].decode("utf-8", "replace"), end


def _good_name(name: str) -> bool:
    return bool(name) and len(name) <= 200 and name.isprintable()


def _parse_part(
    data: bytes,
    offset: int,
    instance_count: int,
) -> VrtxPart | None:
    """Decode one Part record, or return ``None`` if this is not one."""

    cursor = offset + 4

    try:
        name, cursor = _read_string(data, cursor)
        if not _good_name(name):
            return None

        # parent: Option<u64> instance index
        parent_tag = data[cursor]
        cursor += 1

        if parent_tag == 0:
            pass
        elif parent_tag == 1:
            if cursor + 8 > len(data):
                return None
            parent = struct.unpack_from("<Q", data, cursor)[0]
            cursor += 8
            if parent >= instance_count:
                return None
        else:
            return None

        # part: Option<PartData>
        if data[cursor] != 1:
            return None
        cursor += 1

        part_name, cursor = _read_string(data, cursor)
        if not _good_name(part_name):
            return None

        # position, rotation, scale, colour, material
        fields = struct.unpack_from("<10f", data, cursor)
        color = struct.unpack_from("<4f", data, cursor + 40)
        material = struct.unpack_from("<I", data, cursor + 56)[0]
    except (IndexError, struct.error, ValueError):
        return None

    if not all(math.isfinite(value) for value in fields):
        return None
    if not all(math.isfinite(value) for value in color):
        return None

    position = fields[0:3]
    rotation = fields[3:7]
    scale = fields[7:10]

    quaternion_length = math.sqrt(sum(value * value for value in rotation))
    if not 0.5 <= quaternion_length <= 1.5:
        return None

    if any(abs(value) > 1_000_000.0 for value in position):
        return None
    if any(value < 0.0 or value > 1_000_000.0 for value in scale):
        return None
    if any(value < -0.001 or value > 1.001 for value in color):
        return None
    if material > 64:
        return None

    # Flags and textures.  They sit after the fixed PartData fields.  A group
    # payload is rare and has an unknown length, so leave the defaults alone
    # when one is present rather than guessing.
    cast_shadow = True
    anchored = True
    can_collide = True
    spawn_location = False
    baseplate = False
    custom_appearance = False
    truss = False
    textures: tuple[tuple[int, int], ...] = ()
    shape = SHAPE_BLOCK

    tail = cursor + 60

    if tail < len(data) and data[tail] == 0:
        flags = data[tail + 1:tail + 8]

        if len(flags) == 7:
            (
                cast_shadow,
                anchored,
                can_collide,
                spawn_location,
                baseplate,
                custom_appearance,
                truss,
            ) = (bool(value) for value in flags)

        texture_offset = tail + 8
        texture_end = None

        if texture_offset + 8 <= len(data):
            texture_count = struct.unpack_from("<Q", data, texture_offset)[0]

            if 0 <= texture_count <= 64:
                entries = []
                entry_offset = texture_offset + 8
                end = entry_offset + texture_count * 8

                if end <= len(data):
                    for _ in range(texture_count):
                        kind, value = struct.unpack_from("<II", data, entry_offset)
                        entries.append((kind, value))
                        entry_offset += 8
                    textures = tuple(entries)
                    texture_end = end

        # After the textures come two Option light payloads and the shape tag.
        # Parts without lights (the common case) are just two zero tags and a
        # u32 shape.
        if texture_end is not None:
            after = texture_end
            point_tag = data[after] if after < len(data) else 2

            if point_tag == 0:
                after += 1
            elif point_tag == 1:
                after += 1 + _POINT_LIGHT_SIZE
            else:
                after = None

            if after is not None:
                spot_tag = data[after] if after < len(data) else 2

                if spot_tag == 0:
                    after += 1
                elif spot_tag == 1:
                    after += 1 + _SPOT_LIGHT_SIZE
                else:
                    after = None

            if after is not None and after + 4 <= len(data):
                value = struct.unpack_from("<I", data, after)[0]

                if value < len(SHAPE_NAMES):
                    shape = value

    return VrtxPart(
        offset=offset,
        name=name,
        part_name=part_name,
        position=position,
        rotation=rotation,
        scale=scale,
        color=color,
        material=material,
        cast_shadow=cast_shadow,
        anchored=anchored,
        can_collide=can_collide,
        spawn_location=spawn_location,
        baseplate=baseplate,
        custom_appearance=custom_appearance,
        truss=truss,
        textures=textures,
        shape=shape,
        transform_offset=cursor,
    )


def read_project(payload: bytes, version: int) -> VrtxProject:
    """Decode the ProjectDataV2 header and collect every Part."""

    cursor = 0

    if not payload:
        raise ValueError("Empty .vrtx payload")

    project_tag = payload[cursor]
    cursor += 1

    if project_tag == 0:
        project_id = None
    elif project_tag == 1:
        project_id, cursor = _read_string(payload, cursor)
    else:
        raise ValueError("Not a Vortex ProjectDataV2 payload")

    if cursor + 8 > len(payload):
        raise ValueError("Truncated instance count")

    instance_count = struct.unpack_from("<Q", payload, cursor)[0]
    cursor += 8

    if instance_count > _MAX_INSTANCES:
        raise ValueError(f"Implausible instance count {instance_count}")

    parts = []
    start = cursor

    while True:
        offset = payload.find(_PART_MARKER, start)

        if offset < 0 or offset + 76 > len(payload):
            break

        start = offset + 4
        part = _parse_part(payload, offset, instance_count)

        if part is not None:
            parts.append(part)

    return VrtxProject(
        version=version,
        project_id=project_id,
        instance_count=instance_count,
        parts=parts,
    )


def load(path: str) -> VrtxProject:
    """Read a ``.vrtx`` file from disk."""

    with open(path, "rb") as handle:
        data = handle.read()

    version, payload = decompress(data)
    return read_project(payload, version)


def load_bytes(data: bytes) -> VrtxProject:
    """Read a ``.vrtx`` file already held in memory."""

    version, payload = decompress(data)
    return read_project(payload, version)


def compress(version: int, payload: bytes) -> bytes:
    """Wrap a ProjectDataV2 payload back into a ``.vrtx`` file."""

    try:
        import zstandard
    except ImportError as error:  # pragma: no cover - Blender 5.2 bundles it
        raise RuntimeError(
            "The zstandard module is required to write .vrtx files"
        ) from error

    if version < 1 or version > MAX_VERSION:
        raise ValueError(f"Unsupported .vrtx version {version}")

    return MAGIC + bytes((version,)) + zstandard.ZstdCompressor().compress(payload)


def write_updates(source_path: str, output_path: str, updates: dict) -> tuple:
    """Copy ``source_path`` and patch part transforms and colours.

    ``updates`` maps a part's ``transform_offset`` to
    ``(position, rotation, scale, color)`` in the file's own coordinate
    system.  Everything else in the project (scripts, folders, lights,
    instance records) is copied byte for byte, so unedited data survives.

    The result is re-read and compared before returning, which catches a
    stale offset from a file that changed after it was imported.
    """

    with open(source_path, "rb") as handle:
        raw = handle.read()

    version, payload = decompress(raw)
    project = read_project(payload, version)
    known = {part.transform_offset: part for part in project.parts}

    buffer = bytearray(payload)

    for offset, values in updates.items():
        if offset not in known:
            raise ValueError(
                f"Part offset {offset} is not in {source_path}; "
                "re-import the map before exporting"
            )

        position, rotation, scale, color = values

        if len(position) != 3 or len(rotation) != 4 or len(scale) != 3:
            raise ValueError("Malformed transform update")

        if not all(
            math.isfinite(value)
            for value in (*position, *rotation, *scale, *color)
        ):
            raise ValueError("Transform update contains a non-finite value")

        struct.pack_into("<10f", buffer, offset, *position, *rotation, *scale)
        struct.pack_into("<4f", buffer, offset + 40, *color)

    data = compress(version, bytes(buffer))

    with open(output_path, "wb") as handle:
        handle.write(data)

    # Re-read the written file and make sure every patch landed.
    with open(output_path, "rb") as handle:
        check_version, check_payload = decompress(handle.read())

    if check_version != version:
        raise ValueError("Version byte changed while writing")

    check = read_project(check_payload, check_version)
    written = {part.transform_offset: part for part in check.parts}

    for offset, values in updates.items():
        part = written.get(offset)

        if part is None:
            raise ValueError(f"Patched part at {offset} vanished after writing")

        expected = (*values[0], *values[1], *values[2], *values[3])
        actual = (*part.position, *part.rotation, *part.scale, *part.color)

        if any(
            abs(a - b) > 1e-4 + 1e-6 * max(abs(a), abs(b))
            for a, b in zip(actual, expected)
        ):
            raise ValueError(f"Part at {offset} did not round-trip correctly")

    return len(updates), version, len(check.parts)
