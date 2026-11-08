"""End-to-end test of the installed Vortex Map Importer.

Run with:

    blender --background --factory-startup --python dev/test_import.py

The add-on must already be installed and enabled (or at least installed as an
extension).  Writes a report to %TEMP%\vortex_map_import_test.txt.
"""

import os
import sys
import tempfile
import time
import traceback
from collections import Counter
from importlib import import_module

import bpy

ADDON = "bl_ext.user_default.vortex_map_importer"
TARGET = r"C:\Users\pc\Documents\vortex projects\naturedDiggingMapTemplate.vrtx"
MY_BALL = r"C:\Users\pc\Downloads\MY BALL.vrtx"
TRENCH = r"C:\Users\pc\Downloads\The Trench Obby.vrtx"
OUT = os.path.join(tempfile.gettempdir(), "vortex_map_import_test.txt")

failures = []
checks = 0
lines = []


def out(text=""):
    lines.append(str(text))
    print(text)


def check(label, actual, expected):
    global checks

    checks += 1

    if actual != expected:
        failures.append(f"{label}: got {actual!r}, expected {expected!r}")
        out(f"FAIL {label}: got {actual!r}, expected {expected!r}")
    else:
        out(f"ok   {label} = {actual!r}")


def close(label, actual, expected, tolerance=0.001):
    global checks

    checks += 1

    if abs(actual - expected) > tolerance:
        failures.append(f"{label}: got {actual!r}, expected {expected!r}")
        out(f"FAIL {label}: got {actual!r}, expected {expected!r}")
    else:
        out(f"ok   {label} = {actual!r}")


def reset_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()

    for collection in list(bpy.data.collections):
        if collection.name.startswith("Vortex Map"):
            bpy.data.collections.remove(collection)


def import_collection(stem):
    name = f"Vortex Map - {stem}"
    collection = bpy.data.collections.get(name)

    if collection is None:
        for candidate in bpy.data.collections:
            if candidate.name.startswith(name):
                collection = candidate
                break

    return collection


def material_images(material):
    return {
        node.image.name
        for node in material.node_tree.nodes
        if node.bl_idname == "ShaderNodeTexImage" and node.image is not None
    }


def check_textured_material(kind):
    material = bpy.data.materials.get(f"Vortex {kind}")

    if material is None:
        check(f"{kind} material exists", False, True)
        return

    images = material_images(material)
    stem = kind.lower()

    check(
        f"{kind} texture set",
        images,
        {
            f"{stem}_albedo.png",
            f"{stem}_normal.png",
            f"{stem}_orm.png",
        },
    )

    principled = next(
        node
        for node in material.node_tree.nodes
        if node.bl_idname == "ShaderNodeBsdfPrincipled"
    )

    check(f"{kind} base colour built from texture", principled.inputs["Base Color"].is_linked, True)
    check(f"{kind} normal map linked", principled.inputs["Normal"].is_linked, True)
    check(f"{kind} roughness from ORM", principled.inputs["Roughness"].is_linked, True)
    check(f"{kind} metallic from ORM", principled.inputs["Metallic"].is_linked, True)
    check(f"{kind} alpha from object colour", principled.inputs["Alpha"].is_linked, True)


def test_target():
    out("")
    out("target map:")
    start = time.monotonic()
    result = bpy.ops.import_scene.vortex_map(filepath=TARGET)
    elapsed = time.monotonic() - start

    check("target operator", result, {"FINISHED"})
    out(f"     import time: {elapsed:.2f} s")

    collection = import_collection("naturedDiggingMapTemplate")
    check("target collection exists", collection is not None, True)

    if collection is None:
        return

    bpy.context.view_layer.update()

    objects = list(collection.objects)
    check("target object count", len(objects), 9454)
    check("target all meshes", all(obj.type == "MESH" for obj in objects), True)

    meshes = {obj.data for obj in objects}
    check("target shared mesh count", len(meshes), 462)
    out(f"     shared meshes: {len(meshes)}")

    shapes = Counter(obj.get("vortex_shape") for obj in objects)
    check("target block count", shapes["Block"], 9418)
    check("target cylinder count", shapes["Cylinder"], 36)

    cylinder = next(
        (obj for obj in objects if obj.get("vortex_shape") == "Cylinder"),
        None,
    )
    check("target cylinder exists", cylinder is not None, True)

    if cylinder is not None:
        check("cylinder mesh vertices", len(cylinder.data.vertices), 194)
        check("cylinder mesh faces", len(cylinder.data.polygons), 96)
        check(
            "cylinder material",
            cylinder.material_slots[0].material.name,
            "Vortex Metal",
        )

    object_names = {obj.name for obj in objects}
    check("target original names kept", "ground" in object_names, True)

    ground = bpy.data.objects.get("ground")
    check("ground exists", ground is not None, True)

    if ground is not None:
        close("ground x", ground.location.x, 25.275)
        close("ground y", ground.location.y, -116.925)
        close("ground z", ground.location.z, -23.9)
        close("ground scale x", ground.scale.x, 160.0)
        close("ground scale y", ground.scale.y, 320.0)
        close("ground scale z", ground.scale.z, 44.0)

        # The file stores 8-bit sRGB; Blender shades in linear.
        close("ground red", ground.color[0], 0.0)
        close("ground green", ground.color[1], 0.23074007, 0.00005)
        close("ground blue", ground.color[2], 0.01520852, 0.00005)
        close("ground alpha", ground.color[3], 1.0)

        # The original sRGB values stay on the object for round-trips.
        raw = tuple(ground["object_color"])
        close("ground raw red", raw[0], 0.0)
        close("ground raw green", raw[1], 0.51764709, 0.00005)
        close("ground raw blue", raw[2], 0.12941177, 0.00005)

        check(
            "ground material",
            ground.material_slots[0].material.name,
            "Vortex Plastic",
        )

        # One texture tile per four studs: 320 studs / 4 = 80 tiles.
        uv_layer = ground.data.uv_layers[0]
        uv_max = max(max(element.uv) for element in uv_layer.data)
        close("ground UV max", uv_max, 80.0)

    barrier = bpy.data.objects.get("barrier")
    check("barrier exists", barrier is not None, True)

    if barrier is not None:
        close("barrier alpha", barrier.color[3], 0.3, 0.002)
        close("barrier grey", barrier.color[0], 0.41254264, 0.00005)
        check(
            "barrier material",
            barrier.material_slots[0].material.name,
            "Vortex Plastic Alpha",
        )

    for kind in ("Plastic", "Wood", "Metal", "Grass", "Ice"):
        check_textured_material(kind)

    smooth = bpy.data.materials.get("Vortex Smooth")
    check("smooth material exists", smooth is not None, True)

    if smooth is not None:
        check("smooth has no texture set", material_images(smooth), set())

    material_images_all = set()

    for material in bpy.data.materials:
        if material.name.startswith("Vortex "):
            material_images_all |= material_images(material)

    check("material image count", len(material_images_all), 15)

    packed = {
        image.name
        for image in bpy.data.images
        if image.name in material_images_all and image.packed_file is not None
    }
    check("all material images packed", packed, material_images_all)

    check(
        "no lights imported",
        len([obj for obj in objects if obj.type == "LIGHT"]),
        0,
    )
    check(
        "all objects in collection",
        all(obj.name in collection.objects for obj in objects),
        True,
    )

    test_export(ground)


def test_export(ground):
    out("")
    out("round-trip export:")
    vrtx = import_module(ADDON + ".vrtx")

    with open(TARGET, "rb") as handle:
        _, source_payload = vrtx.decompress(handle.read())

    unchanged = os.path.join(tempfile.gettempdir(), "vortex_map_export_unchanged.vrtx")
    edited = os.path.join(tempfile.gettempdir(), "vortex_map_export_edited.vrtx")

    for path in (unchanged, edited):
        try:
            os.remove(path)
        except OSError:
            pass

    check(
        "export operator",
        bpy.ops.export_scene.vortex_map(filepath=unchanged),
        {"FINISHED"},
    )
    check("export wrote a file", os.path.isfile(unchanged), True)

    with open(unchanged, "rb") as handle:
        _, copied_payload = vrtx.decompress(handle.read())

    check("unchanged export keeps payload", copied_payload == source_payload, True)

    written = vrtx.load(unchanged)
    check("export part count", len(written.parts), 9454)
    check("export instance count", written.instance_count, 9513)

    if ground is not None:
        original_location = ground.location.copy()
        ground.location = (30.0, -100.0, 10.0)

        check(
            "edited export operator",
            bpy.ops.export_scene.vortex_map(filepath=edited),
            {"FINISHED"},
        )

        moved = vrtx.load(edited)
        patched = next(
            part
            for part in moved.parts
            if part.transform_offset == int(ground["vrtx_offset"])
        )
        check(
            "exported source position",
            tuple(round(value, 3) for value in patched.position),
            (30.0, 10.0, 100.0),
        )
        check(
            "exported scale preserved",
            tuple(round(value, 3) for value in patched.scale),
            (160.0, 44.0, 320.0),
        )

        changed = 0
        before = {part.transform_offset: part for part in vrtx.load(TARGET).parts}

        for part in moved.parts:
            source = before[part.transform_offset]

            if (
                part.position != source.position
                or part.rotation != source.rotation
                or part.scale != source.scale
                or part.color != source.color
            ):
                changed += 1

        check("export changed only one part", changed, 1)
        ground.location = original_location

    for path in (unchanged, edited):
        try:
            os.remove(path)
        except OSError:
            pass


def test_my_ball():
    if not os.path.isfile(MY_BALL):
        out(f"SKIP missing sample: {MY_BALL}")
        return

    out("")
    out("MY BALL:")
    reset_scene()

    result = bpy.ops.import_scene.vortex_map(filepath=MY_BALL)
    check("MY BALL operator", result, {"FINISHED"})

    collection = import_collection("MY BALL")
    check("MY BALL collection exists", collection is not None, True)

    if collection is None:
        return

    objects = list(collection.objects)
    check("MY BALL object count", len(objects), 11)

    spawn = bpy.data.objects.get("SpawnLocation")
    check("MY BALL spawn exists", spawn is not None, True)

    if spawn is not None:
        close("spawn x", spawn.location.x, 0.0)
        close("spawn y", spawn.location.y, 0.0)
        close("spawn z", spawn.location.z, 0.5)
        close("spawn scale x", spawn.scale.x, 6.0)
        close("spawn scale y", spawn.scale.y, 6.0)
        close("spawn scale z", spawn.scale.z, 1.0)

    baseplate = bpy.data.objects.get("Baseplate")
    check("MY BALL baseplate exists", baseplate is not None, True)

    if baseplate is not None:
        close("baseplate y", baseplate.location.y, 0.0)
        close("baseplate z", baseplate.location.z, -2.0)
        close("baseplate scale x", baseplate.scale.x, 255.0)
        close("baseplate scale y", baseplate.scale.y, 255.0)
        close("baseplate scale z", baseplate.scale.z, 4.0)

        check("baseplate material slots", len(baseplate.material_slots), 6)

        faces = {
            tuple(round(value, 2) for value in polygon.normal):
                baseplate.data.materials[polygon.material_index].name
            for polygon in baseplate.data.polygons
        }
        check(
            "baseplate top studs",
            faces.get((0.0, 0.0, 1.0)),
            "Vortex Baseplate Smooth Studs",
        )
        check(
            "baseplate bottom inlets",
            faces.get((0.0, 0.0, -1.0)),
            "Vortex Baseplate Smooth Inlets",
        )
        check(
            "baseplate side plain",
            faces.get((1.0, 0.0, 0.0)),
            "Vortex Smooth",
        )

        uv_max = max(
            max(element.uv) for element in baseplate.data.uv_layers[0].data
        )
        close("baseplate UV max", uv_max, 63.75)

        stud_images = {
            image.name: image
            for image in bpy.data.images
            if image.name in ("stud.png", "inlet.png")
        }
        check(
            "baseplate textures loaded",
            set(stud_images),
            {"stud.png", "inlet.png"},
        )
        check(
            "baseplate textures packed",
            all(image.packed_file is not None for image in stud_images.values()),
            True,
        )


def test_trench():
    if not os.path.isfile(TRENCH):
        out(f"SKIP missing sample: {TRENCH}")
        return

    out("")
    out("The Trench Obby:")
    reset_scene()

    result = bpy.ops.import_scene.vortex_map(filepath=TRENCH)
    check("Trench operator", result, {"FINISHED"})

    collection = import_collection("The Trench Obby")
    check("Trench collection exists", collection is not None, True)

    if collection is None:
        return

    objects = list(collection.objects)
    check("Trench object count", len(objects), 609)

    shapes = Counter(obj.get("vortex_shape") for obj in objects)
    check("Trench block count", shapes["Block"], 383)
    check("Trench wedge count", shapes["Wedge"], 226)

    wedge = next(
        (obj for obj in objects if obj.get("vortex_shape") == "Wedge"),
        None,
    )
    check("Trench wedge exists", wedge is not None, True)

    if wedge is not None:
        check("wedge mesh vertices", len(wedge.data.vertices), 18)
        check("wedge mesh faces", len(wedge.data.polygons), 5)
        check(
            "wedge material",
            wedge.material_slots[0].material.name,
            "Vortex Sand",
        )

    textured = [obj for obj in objects if len(obj.data.materials) > 1]
    check("Trench face textured parts", len(textured), 21)

    face_materials = {material.name for obj in textured for material in obj.data.materials}
    check(
        "Trench studs material",
        "Vortex Baseplate Sand Studs" in face_materials,
        True,
    )
    check(
        "Trench inlets material",
        "Vortex Baseplate Sand Inlets" in face_materials,
        True,
    )
    check("Trench plain sand material", "Vortex Sand" in face_materials, True)


def main():
    bpy.ops.preferences.addon_enable(module=ADDON)
    check(
        "operator registered",
        hasattr(bpy.ops.import_scene, "vortex_map"),
        True,
    )
    reset_scene()

    test_target()
    test_my_ball()
    test_trench()

    out("")
    out("=" * 60)

    if failures:
        out(f"MAP IMPORT FAILED: {len(failures)} of {checks} checks")
        for failure in failures:
            out("  - " + failure)
        return 1

    out(f"MAP IMPORT PASSED: {checks} checks")
    return 0


try:
    code = main()
except SystemExit:
    raise
except Exception:
    traceback.print_exc()
    code = 2

with open(OUT, "w", encoding="utf-8") as handle:
    handle.write("\n".join(lines) + "\n")

print(f"WROTE {OUT} ({len(lines)} lines)")
sys.exit(code)
