"""Blender importer/exporter for Vortex ``.vrtx`` map files.

Imported geometry covers blocks, wedges, cylinders and balls, plus per-face
stud/inlet textures.  Scripts, folders, lights, instances and editor metadata
from the project are ignored.

The exporter writes edited part transforms and colours back into a copy of the
source project, preserving every other byte.

Materials follow the engine's own material shader:

    base colour = albedo texture * mix(white, part colour, mask)

``mask`` only exists for Brick (white on the mortar, black on the bricks).  The
other kinds use a mask of 1, so the part colour tints the albedo everywhere.
Albedo, normal and ORM (occlusion/roughness/metallic) maps are read from the
texture set that the engine embeds for each material kind.
"""

from __future__ import annotations

import math
import os
import traceback
import zlib

import bpy
from mathutils import Matrix, Quaternion, Vector

try:
    from . import vrtx
except ImportError:  # running from the source folder
    import vrtx


# ------------------------------------------------------------------------
# Coordinate conversion
# ------------------------------------------------------------------------

# Vortex uses Y-up, Z-forward.  Blender uses Z-up, -Y-forward.
SOURCE_TO_BLENDER = Matrix((
    (1.0, 0.0, 0.0),
    (0.0, 0.0, -1.0),
    (0.0, 1.0, 0.0),
))

_BASIS_QUATERNION = SOURCE_TO_BLENDER.to_4x4().to_quaternion()

# The engine builds part meshes with one texture tile every four studs, so a
# face's UVs span its size divided by this value.
STUDS_PER_TILE = vrtx.STUDS_PER_TEXTURE_TILE


def convert_position(position):
    return Vector((position[0], -position[2], position[1]))


def convert_color(color):
    """Vortex stores 8-bit sRGB colours; Blender shades in linear."""

    return (
        srgb_to_linear(color[0]),
        srgb_to_linear(color[1]),
        srgb_to_linear(color[2]),
        color[3],
    )


def srgb_to_linear(value):
    if value <= 0.04045:
        return value / 12.92

    return ((value + 0.055) / 1.055) ** 2.4


def convert_scale(scale):
    # Dimensions are unsigned, so only the axes move.
    return Vector((scale[0], scale[2], scale[1]))


def convert_rotation(rotation):
    # PartData stores x, y, z, w; Blender's constructor wants w, x, y, z.
    source = Quaternion((rotation[3], rotation[0], rotation[1], rotation[2]))
    converted = _BASIS_QUATERNION @ source @ _BASIS_QUATERNION.inverted()

    if converted.magnitude == 0.0:
        return Quaternion()

    return converted.normalized()


def unconvert_position(location):
    """Blender position -> Vortex ``(x, y-up, z-forward)``."""

    return (location[0], location[2], -location[1])


def unconvert_scale(scale):
    return (scale[0], scale[2], scale[1])


def unconvert_rotation(rotation):
    """Blender quaternion -> PartData's ``(x, y, z, w)``."""

    basis = _BASIS_QUATERNION
    source = basis.inverted() @ rotation @ basis

    if source.magnitude == 0.0:
        source = Quaternion()

    source.normalize()
    return (source.x, source.y, source.z, source.w)


def linear_to_srgb(value):
    if value <= 0.0031308:
        return value * 12.92

    return 1.055 * value ** (1.0 / 2.4) - 0.055


def unconvert_color(color):
    return tuple(
        min(1.0, max(0.0, linear_to_srgb(color[index]))) for index in range(3)
    ) + (min(1.0, max(0.0, color[3])),)


# ------------------------------------------------------------------------
# Textures
# ------------------------------------------------------------------------

TEXTURE_KINDS = ("albedo", "normal", "orm", "mask")

# Roughness / metallic used when a kind has no ORM map.
MATERIAL_STYLE = {
    "Smooth": (0.25, 0.0),
    "Plastic": (0.45, 0.0),
    "Wood": (0.65, 0.0),
    "Metal": (0.35, 0.90),
    "Grass": (0.85, 0.0),
    "Ice": (0.12, 0.0),
    "Paint": (0.35, 0.0),
    "Brick": (0.80, 0.0),
    "Sand": (0.95, 0.0),
}

_BUNDLED_TEXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "textures")

_texture_cache = {}


def _preferences_folder():
    package = __package__

    if not package:
        return ""

    entry = bpy.context.preferences.addons.get(package)

    if entry is None:
        return ""

    return getattr(entry.preferences, "textures_dir", "") or ""


def texture_folders(extra_folder=""):
    """Folders searched for material textures, most specific first."""

    folders = []

    for folder in (
        _BUNDLED_TEXTURES,
        bpy.path.abspath(extra_folder) if extra_folder else "",
        _preferences_folder(),
    ):
        folder = folder.strip() if isinstance(folder, str) else folder

        if folder and folder not in folders:
            folders.append(folder)

    return folders


def _find_texture(folder, kind, name):
    for candidate in (
        os.path.join(folder, kind, f"{kind.lower()}_{name}.png"),
        os.path.join(folder, f"{kind.lower()}_{name}.png"),
    ):
        if os.path.isfile(candidate):
            return candidate

    return None


def resolve_textures(kind, extra_folder=""):
    """Return ``{"albedo": path, ...}`` for one material kind."""

    if kind not in vrtx.TEXTURED_MATERIALS:
        return {}

    key = (kind, tuple(texture_folders(extra_folder)))

    if key in _texture_cache:
        return _texture_cache[key]

    paths = {}

    for candidate_folder in texture_folders(extra_folder):
        found = {}

        for texture_kind in TEXTURE_KINDS:
            path = _find_texture(candidate_folder, kind, texture_kind)

            if path is not None:
                found[texture_kind] = path

        if "albedo" in found:
            paths = found
            break

    _texture_cache[key] = paths
    return paths


def load_texture(path, non_color):
    image = bpy.data.images.load(path, check_existing=True)

    try:
        image.colorspace_settings.name = "Non-Color" if non_color else "sRGB"
    except (AttributeError, TypeError):
        pass

    if image.packed_file is None:
        try:
            image.pack()
        except (RuntimeError, TypeError):
            pass

    return image


def _new_image_node(nodes, image, location):
    node = nodes.new("ShaderNodeTexImage")
    node.image = image
    node.interpolation = "Linear"
    node.location = location
    return node


def _new_mix(nodes, blend_type, location, factor=1.0):
    node = nodes.new("ShaderNodeMixRGB")
    node.blend_type = blend_type
    node.inputs["Factor"].default_value = factor
    node.location = location
    return node


def create_material(kind, blended, paths):
    """Create (or reuse) a material for one Vortex material kind."""

    suffix = " Alpha" if blended else ""
    name = f"Vortex {kind}{suffix}"

    material = bpy.data.materials.get(name)

    if material is not None:
        return material

    material = bpy.data.materials.new(name)
    material.use_nodes = True
    material.diffuse_color = (0.8, 0.8, 0.8, 1.0)

    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()

    output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (420.0, 0.0)

    principled = nodes.new("ShaderNodeBsdfPrincipled")
    principled.location = (120.0, 0.0)

    links.new(principled.outputs["BSDF"], output.inputs["Surface"])

    info = nodes.new("ShaderNodeObjectInfo")
    info.location = (-880.0, 320.0)

    albedo_path = paths.get("albedo")

    if albedo_path is not None:
        coordinates = nodes.new("ShaderNodeTexCoord")
        coordinates.location = (-1080.0, 120.0)

        albedo = _new_image_node(
            nodes,
            load_texture(albedo_path, non_color=False),
            (-880.0, 120.0),
        )
        links.new(coordinates.outputs["UV"], albedo.inputs["Vector"])

        # tint = mix(white, part colour, mask);  base = albedo * tint
        tint = _new_mix(nodes, "MIX", (-560.0, 320.0))
        tint.inputs["Color1"].default_value = (1.0, 1.0, 1.0, 1.0)
        links.new(info.outputs["Color"], tint.inputs["Color2"])

        mask_path = paths.get("mask")

        if mask_path is not None:
            mask = _new_image_node(
                nodes,
                load_texture(mask_path, non_color=True),
                (-880.0, -180.0),
            )
            links.new(coordinates.outputs["UV"], mask.inputs["Vector"])

            separate = nodes.new("ShaderNodeSeparateColor")
            separate.location = (-560.0, -180.0)
            links.new(mask.outputs["Color"], separate.inputs["Color"])
            links.new(separate.outputs["Red"], tint.inputs["Factor"])
        else:
            tint.inputs["Factor"].default_value = 1.0

        multiply = _new_mix(nodes, "MULTIPLY", (-280.0, 200.0))
        links.new(albedo.outputs["Color"], multiply.inputs["Color1"])
        links.new(tint.outputs["Color"], multiply.inputs["Color2"])
        links.new(multiply.outputs["Color"], principled.inputs["Base Color"])
    else:
        links.new(info.outputs["Color"], principled.inputs["Base Color"])

    links.new(info.outputs["Alpha"], principled.inputs["Alpha"])

    normal_path = paths.get("normal")

    if normal_path is not None:
        normal_map = nodes.new("ShaderNodeNormalMap")
        normal_map.location = (-280.0, -320.0)

        normal_texture = _new_image_node(
            nodes,
            load_texture(normal_path, non_color=True),
            (-560.0, -320.0),
        )
        links.new(
            normal_texture.outputs["Color"],
            normal_map.inputs["Color"],
        )
        links.new(
            normal_map.outputs["Normal"],
            principled.inputs["Normal"],
        )

    orm_path = paths.get("orm")

    if orm_path is not None:
        orm = _new_image_node(
            nodes,
            load_texture(orm_path, non_color=True),
            (-560.0, -560.0),
        )
        separate = nodes.new("ShaderNodeSeparateColor")
        separate.location = (-280.0, -560.0)
        links.new(orm.outputs["Color"], separate.inputs["Color"])
        links.new(separate.outputs["Green"], principled.inputs["Roughness"])
        links.new(separate.outputs["Blue"], principled.inputs["Metallic"])
    else:
        roughness, metallic = MATERIAL_STYLE.get(kind, (0.5, 0.0))
        principled.inputs["Roughness"].default_value = roughness
        principled.inputs["Metallic"].default_value = metallic

    if blended:
        _set_blended(material)

    return material


def _set_blended(material):
    try:
        material.surface_render_method = "BLENDED"
    except (AttributeError, TypeError):
        try:
            material.blend_method = "BLEND"
        except (AttributeError, TypeError):
            pass

    try:
        material.use_transparent_shadow = True
    except AttributeError:
        pass


# ------------------------------------------------------------------------
# Baseplate materials
# ------------------------------------------------------------------------

# FaceData (Front, Back, Top, Bottom, Left, Right) -> cube polygon.  The cube
# builder emits +X, -X, +Y, -Y, +Z, -Z; Bevy's forward axis is -Z, so Front
# is the -Y face after the coordinate conversion.
_FACE_TO_POLYGON = {0: 2, 1: 3, 2: 4, 3: 5, 4: 1, 5: 0}

# TextureKindData: 0 = Studs, 1 = Inlets.
_BASEPLATE_TEXTURES = {0: "Studs", 1: "Inlets"}

_baseplate_texture_cache = {}


def resolve_baseplate_texture(name, extra_folder=""):
    """Return the path of ``stud.png`` / ``inlet.png`` if one is available."""

    key = (name, tuple(texture_folders(extra_folder)))

    if key in _baseplate_texture_cache:
        return _baseplate_texture_cache[key]

    path = None

    for folder in texture_folders(extra_folder):
        for candidate in (
            os.path.join(folder, "Baseplate", name),
            os.path.join(folder, name),
        ):
            if os.path.isfile(candidate):
                path = candidate
                break

        if path is not None:
            break

    _baseplate_texture_cache[key] = path
    return path


def create_baseplate_material(kind, texture, blended, extra_folder=""):
    """Material for one baseplate face.

    The engine's stud shader is::

        tint = max(base, 0.05)
        base = mix(base, tint * texture, texture.a)

    Both bundled textures are fully opaque greyscale, so the mix reduces to a
    multiply by the tinted colour.
    """

    if texture is None:
        return create_material(kind, blended, resolve_textures(kind, extra_folder))

    texture_path = resolve_baseplate_texture(
        "stud.png" if texture == "Studs" else "inlet.png",
        extra_folder,
    )

    if texture_path is None:
        return create_material(kind, blended, resolve_textures(kind, extra_folder))

    suffix = " Alpha" if blended else ""
    name = f"Vortex Baseplate {kind} {texture}{suffix}"

    material = bpy.data.materials.get(name)

    if material is not None:
        return material

    material = bpy.data.materials.new(name)
    material.use_nodes = True
    material.diffuse_color = (0.8, 0.8, 0.8, 1.0)

    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()

    output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (420.0, 0.0)

    principled = nodes.new("ShaderNodeBsdfPrincipled")
    principled.location = (120.0, 0.0)
    principled.inputs["Roughness"].default_value = 0.45
    principled.inputs["Metallic"].default_value = 0.0
    links.new(principled.outputs["BSDF"], output.inputs["Surface"])

    info = nodes.new("ShaderNodeObjectInfo")
    info.location = (-1080.0, 320.0)

    separate = nodes.new("ShaderNodeSeparateColor")
    separate.location = (-880.0, 320.0)
    links.new(info.outputs["Color"], separate.inputs["Color"])

    floor = nodes.new("ShaderNodeCombineColor")
    floor.location = (-560.0, 320.0)

    for channel in ("Red", "Green", "Blue"):
        maximum = nodes.new("ShaderNodeMath")
        maximum.operation = "MAXIMUM"
        maximum.location = (-720.0, 320.0)
        maximum.inputs[1].default_value = 0.05
        links.new(separate.outputs[channel], maximum.inputs[0])
        links.new(maximum.outputs["Value"], floor.inputs[channel])

    texture_path = resolve_baseplate_texture(
        "stud.png" if texture == "Studs" else "inlet.png",
        extra_folder,
    )

    coordinates = nodes.new("ShaderNodeTexCoord")
    coordinates.location = (-1080.0, 0.0)

    image = _new_image_node(
        nodes,
        load_texture(texture_path, non_color=False),
        (-880.0, 0.0),
    )
    links.new(coordinates.outputs["UV"], image.inputs["Vector"])

    multiply = _new_mix(nodes, "MULTIPLY", (-280.0, 160.0))
    links.new(floor.outputs["Color"], multiply.inputs["Color1"])
    links.new(image.outputs["Color"], multiply.inputs["Color2"])
    links.new(multiply.outputs["Color"], principled.inputs["Base Color"])

    links.new(info.outputs["Alpha"], principled.inputs["Alpha"])

    if blended:
        _set_blended(material)

    return material


# ------------------------------------------------------------------------
# Geometry
# ------------------------------------------------------------------------

def _new_mesh(name, vertices, faces, uvs, materials, face_materials=None):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)

    layer = mesh.uv_layers.new(name="UVMap")

    for loop in mesh.loops:
        layer.data[loop.index].uv = uvs[loop.vertex_index]

    for material in materials:
        mesh.materials.append(material)

    if face_materials is not None:
        for polygon, index in zip(mesh.polygons, face_materials):
            polygon.material_index = index

    mesh.validate()
    mesh.update()

    return mesh


def _cube_geometry(size):
    """A unit cube whose UVs match ``size`` in studs (four studs per tile)."""

    vertices = []
    faces = []
    uvs = []

    corners = ((-1, -1), (1, -1), (1, 1), (-1, 1))

    for axis in range(3):
        u_axis = (axis + 1) % 3
        v_axis = (axis + 2) % 3
        u_size = size[u_axis] / STUDS_PER_TILE
        v_size = size[v_axis] / STUDS_PER_TILE

        for sign in (1.0, -1.0):
            base = len(vertices)
            ordered = corners if sign > 0.0 else tuple(reversed(corners))

            for du, dv in ordered:
                coordinate = [0.0, 0.0, 0.0]
                coordinate[axis] = 0.5 * sign
                coordinate[u_axis] = 0.5 * du
                coordinate[v_axis] = 0.5 * dv

                vertices.append(tuple(coordinate))
                uvs.append(
                    ((du + 1.0) * 0.5 * u_size, (dv + 1.0) * 0.5 * v_size)
                )

            faces.append((base, base + 1, base + 2, base + 3))

    return vertices, faces, uvs


def _cylinder_geometry(size):
    """Round plate whose axis is local X (the engine's cylinder axis)."""

    segments = 32
    radius = 0.5
    u_wrap = math.pi * 0.5 * (size[1] + size[2]) / STUDS_PER_TILE
    v_wrap = size[0] / STUDS_PER_TILE

    vertices = []
    faces = []
    uvs = []

    def add(coordinate, uv):
        vertices.append(coordinate)
        uvs.append(uv)
        return len(vertices) - 1

    # Side wall.
    for index in range(segments):
        angle0 = 2.0 * math.pi * index / segments
        angle1 = 2.0 * math.pi * (index + 1) / segments
        y0 = radius * math.cos(angle0)
        z0 = radius * math.sin(angle0)
        y1 = radius * math.cos(angle1)
        z1 = radius * math.sin(angle1)
        u0 = u_wrap * index / segments
        u1 = u_wrap * (index + 1) / segments

        a = add((-0.5, y0, z0), (u0, 0.0))
        b = add((-0.5, y1, z1), (u1, 0.0))
        c = add((0.5, y1, z1), (u1, v_wrap))
        d = add((0.5, y0, z0), (u0, v_wrap))
        faces.append((a, b, c, d))

    # Caps.  UVs are planar so the texture keeps the same texel density.
    for sign in (1.0, -1.0):
        centre = add(
            (0.5 * sign, 0.0, 0.0),
            (0.5, 0.5),
        )
        ring = []

        for index in range(segments):
            angle = 2.0 * math.pi * index / segments
            y = radius * math.cos(angle)
            z = radius * math.sin(angle)
            ring.append(
                add(
                    (0.5 * sign, y, z),
                    (
                        0.5 + y * size[1] / STUDS_PER_TILE,
                        0.5 + z * size[2] / STUDS_PER_TILE,
                    ),
                )
            )

        for index in range(segments):
            current = ring[index]
            following = ring[(index + 1) % segments]

            if sign > 0.0:
                faces.append((centre, current, following))
            else:
                faces.append((centre, following, current))

    return vertices, faces, uvs


def _ball_geometry(size):
    """UV sphere with texture density matched to the part's diameter."""

    rings = 16
    segments = 32
    diameter = (size[0] + size[1] + size[2]) / 3.0
    u_wrap = max(1.0, math.pi * diameter / STUDS_PER_TILE)
    v_wrap = u_wrap / 2.0
    radius = 0.5

    vertices = []
    faces = []
    uvs = []

    def add(coordinate, uv):
        vertices.append(coordinate)
        uvs.append(uv)
        return len(vertices) - 1

    def point(theta, phi):
        return (
            radius * math.cos(theta) * math.cos(phi),
            radius * math.cos(theta) * math.sin(phi),
            radius * math.sin(theta),
        )

    for ring in range(rings):
        theta0 = -math.pi / 2.0 + math.pi * ring / rings
        theta1 = -math.pi / 2.0 + math.pi * (ring + 1) / rings
        v0 = v_wrap * ring / rings
        v1 = v_wrap * (ring + 1) / rings

        for segment in range(segments):
            phi0 = 2.0 * math.pi * segment / segments
            phi1 = 2.0 * math.pi * (segment + 1) / segments
            u0 = u_wrap * segment / segments
            u1 = u_wrap * (segment + 1) / segments

            if ring == 0:
                a = add(point(theta1, phi0), (u0, v1))
                b = add(point(theta1, phi1), (u1, v1))
                c = add(point(theta0, 0.0), (u0, 0.0))
                faces.append((c, a, b))
            elif ring == rings - 1:
                a = add(point(theta0, phi0), (u0, v0))
                b = add(point(theta0, phi1), (u1, v0))
                c = add(point(theta1, 0.0), (u0, v_wrap))
                faces.append((a, b, c))
            else:
                a = add(point(theta0, phi0), (u0, v0))
                b = add(point(theta0, phi1), (u1, v0))
                c = add(point(theta1, phi1), (u1, v1))
                d = add(point(theta1, phi0), (u0, v1))
                faces.append((a, b, c, d))

    return vertices, faces, uvs


def _wedge_geometry(size):
    """Triangular prism: full height at the back (-Y), slope to the front.

    The engine's wedge orientation is not documented; this matches the usual
    Roblox-style ramp, with the vertical face at -Y and the slope descending
    toward +Y.
    """

    width, depth, height = size[0], size[1], size[2]
    slope_length = math.sqrt(depth * depth + height * height)

    vertices = []
    faces = []
    uvs = []

    def add(coordinate, uv):
        vertices.append(coordinate)
        uvs.append(uv)
        return len(vertices) - 1

    def planar(u, v, u_size, v_size):
        return (
            (u + 0.5) * u_size / STUDS_PER_TILE,
            (v + 0.5) * v_size / STUDS_PER_TILE,
        )

    # Corners (unit cube; object scale supplies the real size).
    b0 = (-0.5, -0.5, -0.5)
    b1 = (0.5, -0.5, -0.5)
    f0 = (-0.5, 0.5, -0.5)
    f1 = (0.5, 0.5, -0.5)
    t0 = (-0.5, -0.5, 0.5)
    t1 = (0.5, -0.5, 0.5)

    # Bottom (normal -Z).
    faces.append((
        add(b0, planar(b0[0], b0[1], width, depth)),
        add(f0, planar(f0[0], f0[1], width, depth)),
        add(f1, planar(f1[0], f1[1], width, depth)),
        add(b1, planar(b1[0], b1[1], width, depth)),
    ))

    # Vertical back face (normal -Y).
    faces.append((
        add(b0, planar(b0[0], b0[2], width, height)),
        add(b1, planar(b1[0], b1[2], width, height)),
        add(t1, planar(t1[0], t1[2], width, height)),
        add(t0, planar(t0[0], t0[2], width, height)),
    ))

    # Slope, UVs running down the incline.
    def slope_uv(x, y):
        return (
            (x + 0.5) * width / STUDS_PER_TILE,
            (y + 0.5) * slope_length / STUDS_PER_TILE,
        )

    faces.append((
        add(t0, slope_uv(t0[0], -0.5)),
        add(t1, slope_uv(t1[0], -0.5)),
        add(f1, slope_uv(f1[0], 0.5)),
        add(f0, slope_uv(f0[0], 0.5)),
    ))

    # Triangular sides.
    faces.append((
        add(b0, planar(b0[1], b0[2], depth, height)),
        add(t0, planar(t0[1], t0[2], depth, height)),
        add(f0, planar(f0[1], f0[2], depth, height)),
    ))
    faces.append((
        add(b1, planar(b1[1], b1[2], depth, height)),
        add(f1, planar(f1[1], f1[2], depth, height)),
        add(t1, planar(t1[1], t1[2], depth, height)),
    ))

    return vertices, faces, uvs


def build_mesh(name, size, shape, materials, face_materials=None):
    if shape == vrtx.SHAPE_CYLINDER:
        geometry = _cylinder_geometry(size)
    elif shape == vrtx.SHAPE_BALL:
        geometry = _ball_geometry(size)
    elif shape == vrtx.SHAPE_WEDGE and face_materials is None:
        geometry = _wedge_geometry(size)
    else:
        geometry = _cube_geometry(size)

    return _new_mesh(name, *geometry, materials, face_materials)


def build_cube_mesh(name, size):
    return build_mesh(name, size, vrtx.SHAPE_BLOCK, [])


def mesh_for(kind, blended, size, shape, textures, cache):
    key = (
        kind,
        blended,
        shape,
        round(size[0], 3),
        round(size[1], 3),
        round(size[2], 3),
    )

    mesh = cache.get(key)

    if mesh is not None:
        return mesh

    suffix = " Alpha" if blended else ""
    label = ""

    if shape != vrtx.SHAPE_BLOCK:
        label = f" {vrtx.SHAPE_NAMES[shape]}"

    mesh = build_mesh(
        f"Vortex Map {kind}{label}{suffix}",
        size,
        shape,
        [create_material(kind, blended, textures)],
    )
    cache[key] = mesh

    return mesh


def textured_part_mesh_for(part, extra_folder, cache):
    """Cube with one material per face (studs, inlets or the plain material)."""

    kind = part.material_name
    blended = part.color[3] < 0.999
    size = convert_scale(part.scale)
    texture_faces = dict(part.textures)

    key = (
        kind,
        blended,
        tuple(round(value, 3) for value in size),
        tuple(sorted(texture_faces.items())),
    )

    mesh = cache.get(key)

    if mesh is not None:
        return mesh

    suffix = " Alpha" if blended else ""
    materials = []
    slots = [None] * 6

    for face in range(6):
        texture_index = texture_faces.get(face)
        texture = _BASEPLATE_TEXTURES.get(texture_index)
        material = create_baseplate_material(kind, texture, blended, extra_folder)
        materials.append(material)
        slots[_FACE_TO_POLYGON[face]] = face

    mesh = build_mesh(
        f"Vortex Map {kind} Face Textures{suffix}",
        size,
        vrtx.SHAPE_BLOCK,
        materials,
        slots,
    )
    cache[key] = mesh

    return mesh


# ------------------------------------------------------------------------
# Import
# ------------------------------------------------------------------------

def import_vrtx(filepath, textures_dir=""):
    """Import every Part in ``filepath`` and return the parsed project."""

    project = vrtx.load(filepath)
    parts = project.parts

    if not parts:
        return project

    stem = os.path.splitext(os.path.basename(filepath))[0] or "Vortex Map"
    collection = bpy.data.collections.new(f"Vortex Map - {stem}")
    bpy.context.scene.collection.children.link(collection)

    texture_sets = {}

    def textures_for(kind):
        if kind not in texture_sets:
            texture_sets[kind] = resolve_textures(kind, textures_dir)
        return texture_sets[kind]

    mesh_cache = {}
    baseplate_cache = {}
    source = os.path.abspath(filepath)
    source_crc = _file_crc32(source)

    # Keep the imported map separate from the existing selection.
    for selected in bpy.context.selected_objects:
        selected.select_set(False)

    last_object = None

    for part in parts:
        kind = part.material_name
        blended = part.color[3] < 0.999
        size = convert_scale(part.scale)

        if part.textures:
            mesh = textured_part_mesh_for(part, textures_dir, baseplate_cache)
        else:
            mesh = mesh_for(
                kind,
                blended,
                size,
                part.shape,
                textures_for(kind),
                mesh_cache,
            )

        obj = bpy.data.objects.new(part.name or "Part", mesh)
        collection.objects.link(obj)

        obj.location = convert_position(part.position)
        obj.rotation_mode = "QUATERNION"
        obj.rotation_quaternion = convert_rotation(part.rotation)
        obj.scale = size

        obj.color = convert_color(part.color)
        obj["object_color"] = part.color
        obj["vortex_shape"] = part.shape_name
        obj["vrtx_source"] = source
        obj["vrtx_crc32"] = f"{source_crc:08x}"
        obj["vrtx_offset"] = part.transform_offset
        obj.visible_shadow = part.cast_shadow

        if part.part_name != part.name:
            obj["vortex_part_name"] = part.part_name

        last_object = obj

    if last_object is not None:
        last_object.select_set(True)
        bpy.context.view_layer.objects.active = last_object

    return project


# ------------------------------------------------------------------------
# Round-trip export
# ------------------------------------------------------------------------

def _file_crc32(path):
    checksum = 0

    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            checksum = zlib.crc32(chunk, checksum)

    return checksum & 0xFFFFFFFF


def _property(obj, name, default=None):
    try:
        return obj[name]
    except KeyError:
        return default


def export_scope(context):
    """Return ``(objects, source_path)`` for the map being edited."""

    active = context.active_object
    collection = None

    if active is not None and _property(active, "vrtx_offset") is not None:
        collections = getattr(active, "users_collection", ())

        if collections:
            collection = collections[0]

    objects = []

    if collection is not None:
        objects = [
            obj
            for obj in collection.objects
            if obj.type == "MESH" and _property(obj, "vrtx_offset") is not None
        ]

    if not objects:
        objects = [
            obj
            for obj in context.scene.objects
            if obj.type == "MESH" and _property(obj, "vrtx_offset") is not None
        ]

    if not objects:
        raise ValueError(
            "No imported Vortex map parts found. Import a .vrtx map first."
        )

    sources = {str(_property(obj, "vrtx_source")) for obj in objects}

    if len(sources) > 1:
        raise ValueError(
            "Parts from more than one .vrtx file are in scope. "
            "Select a part from the map you want to export."
        )

    return objects, sources.pop()


def export_vrtx(filepath, objects, crc_check=True):
    """Write the objects' transforms and colours back into their .vrtx."""

    objects = [
        obj
        for obj in objects
        if obj.type == "MESH" and _property(obj, "vrtx_offset") is not None
    ]

    if not objects:
        raise ValueError("No Vortex map parts to export")

    sources = {str(_property(obj, "vrtx_source")) for obj in objects}

    if len(sources) != 1:
        raise ValueError("Selected parts come from different .vrtx files")

    source = sources.pop()
    source_crc = _file_crc32(source)

    if crc_check:
        expected_crc = f"{source_crc:08x}"

        for obj in objects:
            stored = _property(obj, "vrtx_crc32")

            if stored is None or str(stored) != expected_crc:
                raise ValueError(
                    "The source .vrtx changed after it was imported. "
                    "Re-import the map before exporting."
                )

    # Parts left alone keep their exact on-disk values (no float noise from
    # the coordinate and colour conversions).
    project = vrtx.load(source)
    original = {part.transform_offset: part for part in project.parts}

    updates = {}

    for obj in objects:
        offset = int(_property(obj, "vrtx_offset"))
        part = original.get(offset)

        if part is None:
            raise ValueError(
                f"Part offset {offset} is not in {source}; "
                "re-import the map before exporting"
            )

        if offset in updates:
            raise ValueError(
                f"Two objects point at the same part (offset {offset})"
            )

        if obj.rotation_mode == "QUATERNION":
            rotation = obj.rotation_quaternion
        else:
            rotation = obj.rotation_euler.to_quaternion()

        position = tuple(float(value) for value in unconvert_position(obj.location))
        converted_rotation = unconvert_rotation(rotation)
        scale = tuple(abs(float(value)) for value in unconvert_scale(obj.scale))
        color = unconvert_color(obj.color)

        if not all(
            math.isfinite(value)
            for value in (*position, *converted_rotation, *scale, *color)
        ):
            raise ValueError(
                f"Object {obj.name!r} has a non-finite transform or colour"
            )

        if _same_part(part, position, converted_rotation, scale, color):
            continue

        updates[offset] = (position, converted_rotation, scale, color)

    return vrtx.write_updates(source, filepath, updates)


def _same_part(part, position, rotation, scale, color, tolerance=1e-5):
    if any(abs(a - b) > tolerance for a, b in zip(position, part.position)):
        return False

    if any(abs(a - b) > tolerance for a, b in zip(scale, part.scale)):
        return False

    if any(abs(a - b) > tolerance for a, b in zip(color, part.color)):
        return False

    # q and -q describe the same rotation.
    direct = max(abs(a - b) for a, b in zip(rotation, part.rotation))
    flipped = max(abs(a + b) for a, b in zip(rotation, part.rotation))
    return min(direct, flipped) <= tolerance


# ------------------------------------------------------------------------
# Preferences
# ------------------------------------------------------------------------

class VortexMapPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__ or "vortex_map_importer"

    textures_dir: bpy.props.StringProperty(
        name="Texture Folder",
        description=(
            "Optional folder with Vortex material textures. Used when the "
            "add-on has no bundled textures"
        ),
        subtype="DIR_PATH",
        default="",
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "textures_dir")


# ------------------------------------------------------------------------
# Operator
# ------------------------------------------------------------------------

class IMPORT_OT_vortex_map(bpy.types.Operator):
    bl_idname = "import_scene.vortex_map"
    bl_label = "Import Vortex Map"
    bl_description = "Import the 3D parts of a Vortex .vrtx project"
    bl_options = {"REGISTER", "UNDO"}

    filepath: bpy.props.StringProperty(
        name="Vortex Map File",
        subtype="FILE_PATH",
    )

    filter_glob: bpy.props.StringProperty(
        default="*.vrtx",
        options={"HIDDEN"},
    )

    def execute(self, context):
        try:
            project = import_vrtx(self.filepath)
        except Exception as error:
            traceback.print_exc()
            self.report({"ERROR"}, f"Vortex map import failed: {error}")
            return {"CANCELLED"}

        count = len(project.parts)

        if count == 0:
            self.report(
                {"WARNING"},
                "No parts found in this .vrtx file",
            )
            return {"FINISHED"}

        textured = sum(
            1
            for kind in {part.material_name for part in project.parts}
            if resolve_textures(kind)
        )

        self.report(
            {"INFO"},
            f"Imported {count} parts from .vrtx version {project.version} "
            f"({textured} textured materials)",
        )
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


# ------------------------------------------------------------------------
# Export operator
# ------------------------------------------------------------------------

class EXPORT_OT_vortex_map(bpy.types.Operator):
    bl_idname = "export_scene.vortex_map"
    bl_label = "Export Vortex Map"
    bl_description = (
        "Write part positions, rotations, sizes and colours back into a "
        ".vrtx project"
    )
    bl_options = {"REGISTER"}

    filepath: bpy.props.StringProperty(
        name="Vortex Map File",
        subtype="FILE_PATH",
    )

    filter_glob: bpy.props.StringProperty(
        default="*.vrtx",
        options={"HIDDEN"},
    )

    check_existing: bpy.props.BoolProperty(
        name="Check Existing",
        default=True,
    )

    def execute(self, context):
        try:
            objects, source = export_scope(context)
            count, version, total = export_vrtx(self.filepath, objects)
        except Exception as error:
            traceback.print_exc()
            self.report({"ERROR"}, f"Vortex map export failed: {error}")
            return {"CANCELLED"}

        if os.path.exists(source) and os.path.samefile(source, self.filepath):
            destination = "the original file"
        else:
            destination = os.path.basename(self.filepath)

        self.report(
            {"INFO"},
            f"Exported {count} of {total} parts to {destination} "
            f"(.vrtx version {version})",
        )
        return {"FINISHED"}

    def invoke(self, context, event):
        if not self.filepath:
            try:
                _, source = export_scope(context)
            except Exception:
                source = ""

            if source:
                stem, extension = os.path.splitext(source)
                self.filepath = f"{stem} (Blender){extension or '.vrtx'}"

        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


# ------------------------------------------------------------------------
# Menu
# ------------------------------------------------------------------------

def menu_func_import(self, context):
    self.layout.operator(
        IMPORT_OT_vortex_map.bl_idname,
        text="Vortex Map (.vrtx)",
    )


def menu_func_export(self, context):
    self.layout.operator(
        EXPORT_OT_vortex_map.bl_idname,
        text="Vortex Map (.vrtx)",
    )


classes = (
    VortexMapPreferences,
    IMPORT_OT_vortex_map,
    EXPORT_OT_vortex_map,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
