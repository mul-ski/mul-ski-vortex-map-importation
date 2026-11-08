"""Verify the .vrtx parser against real project files.

Run with Blender's Python:

    blender --background --factory-startup --python dev/test_parser.py

or any Python that has zstandard:

    python dev/test_parser.py
"""

import math
import os
import sys
import tempfile
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import vrtx  # noqa: E402

TARGET = r"C:\Users\pc\Documents\vortex projects\naturedDiggingMapTemplate.vrtx"
SAMPLES = [
    (r"C:\Users\pc\Downloads\MY BALL.vrtx", 4, 17, 11, {"Block": 11}),
    (
        r"C:\Users\pc\Downloads\The Trench Obby.vrtx",
        5,
        630,
        609,
        {"Block": 383, "Wedge": 226},
    ),
]

failures = []
checks = 0


def check(label, actual, expected):
    global checks

    checks += 1

    if actual != expected:
        failures.append(f"{label}: got {actual!r}, expected {expected!r}")
        print(f"FAIL {label}: got {actual!r}, expected {expected!r}")
    else:
        print(f"ok   {label} = {actual!r}")


def close(label, actual, expected, tolerance=0.001):
    global checks

    checks += 1

    if abs(actual - expected) > tolerance:
        failures.append(
            f"{label}: got {actual!r}, expected {expected!r}"
        )
        print(f"FAIL {label}: got {actual!r}, expected {expected!r}")
    else:
        print(f"ok   {label} = {actual!r}")


def validate(project, label):
    check(f"{label} has instances", project.instance_count > 0, True)
    check(f"{label} parts <= instances", len(project.parts) <= project.instance_count, True)

    offsets = {part.offset for part in project.parts}
    check(f"{label} unique offsets", len(offsets), len(project.parts))

    transform_offsets = {part.transform_offset for part in project.parts}
    check(
        f"{label} unique transform offsets",
        len(transform_offsets),
        len(project.parts),
    )

    check(
        f"{label} shapes known",
        all(0 <= part.shape < len(vrtx.SHAPE_NAMES) for part in project.parts),
        True,
    )

    for part in project.parts:
        if not part.name or not part.name.isprintable():
            check(f"{label} printable name at {part.offset}", part.name, "<name>")
            break

        if not all(math.isfinite(value) for value in part.position):
            check(f"{label} finite position at {part.offset}", part.position, "<finite>")
            break

        if not all(value >= 0.0 for value in part.scale):
            check(f"{label} non-negative scale at {part.offset}", part.scale, "<non-negative>")
            break

        if not all(0.0 <= value <= 1.0 for value in part.color):
            check(f"{label} colour range at {part.offset}", part.color, "<0..1>")
            break

        length = math.sqrt(sum(value * value for value in part.rotation))
        if abs(length - 1.0) > 0.01:
            check(f"{label} unit quaternion at {part.offset}", length, 1.0)
            break

        if not 0 <= part.material < len(vrtx.MATERIAL_NAMES):
            check(f"{label} material at {part.offset}", part.material, "<known>")
            break
    else:
        check(f"{label} every part is sane", True, True)


def test_errors():
    for data in (b"", b"nope", b"VRTX", b"VRTX\x05", b"VRTX\x05not zstd"):
        try:
            vrtx.decompress(data)
        except Exception:
            check("rejects invalid data %r" % data[:8], True, True)
        else:
            check("rejects invalid data %r" % data[:8], False, True)

    try:
        vrtx.read_project(b"", 5)
    except ValueError:
        check("rejects empty payload", True, True)
    else:
        check("rejects empty payload", False, True)


def test_roundtrip():
    if not os.path.isfile(TARGET):
        return

    with open(TARGET, "rb") as handle:
        _, original = vrtx.decompress(handle.read())

    output = os.path.join(tempfile.gettempdir(), "vortex_parser_roundtrip.vrtx")

    try:
        vrtx.write_updates(TARGET, output, {})
        _, copied = vrtx.decompress(open(output, "rb").read())
        check("round-trip copy payload identical", copied == original, True)

        source = vrtx.load(TARGET)
        part = source.parts[0]
        update = (
            (1.5, 2.5, 3.5),
            (0.0, 0.0, 0.0, 1.0),
            (4.0, 5.0, 6.0),
            (0.25, 0.5, 0.75, 1.0),
        )
        count, version, total = vrtx.write_updates(
            TARGET,
            output,
            {part.transform_offset: update},
        )
        check("round-trip update count", count, 1)
        check("round-trip version", version, 5)
        check("round-trip part count", total, len(source.parts))

        written = vrtx.load(output)
        patched = next(
            item
            for item in written.parts
            if item.transform_offset == part.transform_offset
        )
        check("round-trip position", patched.position, update[0])
        check("round-trip rotation", patched.rotation, update[1])
        check("round-trip scale", patched.scale, update[2])
        check("round-trip colour", patched.color, update[3])

        # Everything except the patched part must be byte-for-byte unchanged.
        by_offset = {item.transform_offset: item for item in source.parts}
        changed = 0

        for item in written.parts:
            before = by_offset[item.transform_offset]

            if (
                item.position,
                item.rotation,
                item.scale,
                item.color,
            ) != (
                before.position,
                before.rotation,
                before.scale,
                before.color,
            ):
                changed += 1

        check("round-trip only changed one part", changed, 1)
    finally:
        try:
            os.remove(output)
        except OSError:
            pass


def main():
    if not os.path.isfile(TARGET):
        print(f"MISSING target file: {TARGET}")
        return 2

    test_errors()

    project = vrtx.load(TARGET)

    check("target version", project.version, 5)
    check(
        "target project id",
        project.project_id,
        "4f02ec6b299d7a9fa126b2e9712609c2",
    )
    check("target instances", project.instance_count, 9513)
    check("target parts", len(project.parts), 9454)
    validate(project, "target")

    ground = next(part for part in project.parts if part.name == "ground")
    close("ground source x", ground.position[0], 25.275)
    close("ground source y", ground.position[1], -23.9)
    close("ground source z", ground.position[2], 116.925)
    close("ground scale x", ground.scale[0], 160.0)
    close("ground scale y", ground.scale[1], 44.0)
    close("ground scale z", ground.scale[2], 320.0)
    check("ground material", ground.material, 1)

    from struct import unpack_from

    _, payload = vrtx.decompress(open(TARGET, "rb").read())
    check(
        "transform offset points at position",
        unpack_from("<3f", payload, ground.transform_offset),
        ground.position,
    )

    shapes = Counter(part.shape_name for part in project.parts)
    check("target block count", shapes["Block"], 9418)
    check("target cylinder count", shapes["Cylinder"], 36)
    check("target shape total", sum(shapes.values()), len(project.parts))

    barrier = next(part for part in project.parts if part.name == "barrier")
    close("barrier alpha", barrier.color[3], 0.3, 0.002)

    for path, version, instances, parts, shape_counts in SAMPLES:
        if not os.path.isfile(path):
            print(f"SKIP missing sample: {path}")
            continue

        sample = vrtx.load(path)
        label = os.path.basename(path)

        check(f"{label} version", sample.version, version)
        check(f"{label} instances", sample.instance_count, instances)
        check(f"{label} parts", len(sample.parts), parts)
        validate(sample, label)

        sample_shapes = Counter(part.shape_name for part in sample.parts)
        check(f"{label} shape counts", dict(sample_shapes), shape_counts)

    test_roundtrip()

    print("")
    print("=" * 60)

    if failures:
        print(f"PARSER FAILED: {len(failures)} of {checks} checks")
        for failure in failures:
            print("  -", failure)
        return 1

    print(f"PARSER PASSED: {checks} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
