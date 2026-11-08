# Vortex Map Importer

A small, standalone Blender add-on that imports the **3D map** from a Vortex
`.vrtx` project file, and writes edited transforms and colours back.

It is separate from the avatar/animation add-on: it only reads the block parts
that make up a place and ignores scripts, folders, lights, instances and editor
metadata.

Targets **Blender 5.2+** (Blender's bundled Python includes `zstandard`).

## Use

1. Install and enable the add-on.
2. **File → Import → Vortex Map (.vrtx)**
3. Pick a `.vrtx` file.

Every part is recreated with its original position, rotation, size, colour and
shape, in a new collection named after the file.

## Shapes

The importer reads the engine's `PartShapeData` tag and builds the matching
mesh:

| Shape | Geometry |
| --- | --- |
| Block | Cube (six faces) |
| Wedge | Triangular prism (vertical face at the back, slope to the front) |
| Cylinder | 32-sided round plate; the part's local X axis is its axis |
| Ball | 32×16 UV sphere |

`CornerWedge` and `Mesh` fall back to a cube: no sample uses them and their
exact bounds are not documented. The wedge ramp direction is the usual
Roblox-style orientation; it is the only shape whose direction is a
best-effort guess.

## Materials

The add-on ships with the game's 25 material maps (albedo, normal and ORM for
each kind, plus the Brick mask) and rebuilds the engine's material shader, so
imported maps look like they do in Vortex Studio:

```
base colour = albedo × mix(white, part colour, mask)
```

- Part colours are stored as 8-bit sRGB, so they are converted to linear before
  shading. The original sRGB tuple stays on each object as `object_color`.
- ORM maps are split into roughness (green) and metallic (blue); red (ambient
  occlusion) is unused by the engine.
- Brick uses `brick_mask.png` to keep the mortar white while tinting the bricks.
- Smooth has no texture set, so it uses the part colour directly.
- UVs tile once every four studs, matching the game's stud atlas.

### Face textures

Parts may store per-face textures. The add-on reads them and uses the engine's
own `stud.png` and `inlet.png` (4×4 stud grids) on studded and inlet faces,
tinted by the part colour:

```
base colour = max(part colour, 0.05) × texture
```

The other faces keep the part's normal material (Smooth, Sand, …), so a sand
block with a studded top still has a textured sand body. `MY BALL`'s baseplate
uses studs on top and inlets underneath; 21 sand parts in `The Trench Obby`
also carry face textures.

To use different textures, set **Texture Folder** in the add-on preferences.
Both layouts are accepted:

```
<folder>/<Kind>/<kind>_albedo.png   (bundled layout)
<folder>/<kind>_albedo.png
```

Bundled textures are searched first, then the preferences folder.

## What is imported

- Every `Part` instance, including spawn locations and baseplates.
- Position, rotation and size.
- Block, wedge, cylinder and ball shapes.
- Per-part RGBA colour (linear in `obj.color`, original sRGB in
  `obj["object_color"]`).
- Vortex material kind (Smooth, Plastic, Wood, Metal, Grass, Ice, Paint, Brick,
  Sand), with its texture set.
- Per-face stud/inlet textures (baseplates and other textured parts).
- Shadow casting.

Meshes are shared per material kind, transparency, shape and size, so a
9,454-part map still uses 462 meshes instead of one per part.

## Exporting changes

After editing an imported map in Blender, use **File → Export → Vortex Map
(.vrtx)**. The exporter writes each object's local position, rotation, scale
and colour back into the original `PartData` records:

- Everything else in the project is copied byte for byte: scripts, folders,
  lights, instance records, names, materials, flags and textures.
- Parts that were not changed keep their exact on-disk values.
- The chosen file is validated by re-reading it before the export finishes.
- The source `.vrtx` is fingerprinted when it is imported. If the file changes
  on disk afterwards, the export stops and asks you to re-import, so stale
  byte offsets cannot corrupt another map.
- Save to a new name (the dialog suggests `<name> (Blender).vrtx`) if you want
  to keep the original.

Edit parts with object-level transforms (move/rotate/scale) and colours; the
exporter reads those object values. Applying a transform to the mesh data makes
the object transform neutral, so the applied values would be exported instead.

The exporter updates existing parts only. Adding or deleting Blender objects
does not add or remove parts from the project; deleted parts simply keep their
original values. Duplicating a part makes two objects point at the same
record, so the exporter stops rather than guessing which one wins.

## What is not imported

- Scripts and attributes.
- Folders/models and the instance hierarchy.
- Point/spot lights.
- User-uploaded decals (`textures` on parts).
- Corner-wedge and custom mesh shapes (they become cubes).

## Development

Build the extension zip:

```
blender --command extension build ^
  --source-dir "C:\Users\pc\Documents\vortex-map-importer" ^
  --output-dir "C:\Users\pc\Documents\vortex-map-importer\dist"
```

Install the built zip:

```
blender --background --command extension install-file ^
  -r user_default ^
  -e "C:\Users\pc\Documents\vortex-map-importer\dist\vortex_map_importer-1.2.0.zip"
```

Tests live in `dev/` and are not shipped:

- `dev/test_parser.py` reads the real sample files, checks the decoded parts,
  shapes and transform offsets, and exercises a byte-level round-trip. Run it
  with Blender's Python or any Python that has `zstandard`.
- `dev/test_import.py` imports the map through the installed add-on and
  checks the objects, transforms, linear colours, shared meshes, shape meshes,
  baseplate materials, texture sets, UV scale and the exporter.

## Version 1.2.0

- Block, wedge, cylinder and ball shapes; 36 cylinders in the sample map are
  no longer cubes.
- Face textures: studded tops and inlet undersides using the game's own
  512×512 textures, on baseplates and any other part that stores them.
- Round-trip exporter for positions, rotations, sizes and colours, preserving
  everything else in the project.

## Version 1.1.0

- Materials now use the game's own albedo, normal and ORM maps, with the
  engine's tint/mask shader, so maps match Vortex Studio instead of flat
  colours.
- Part colours are converted from sRGB to linear.
- Meshes and UVs are built per part size (one texture tile per four studs).
- Bundled texture set (about 5.8 MB), overridable in preferences.

## Version 1.0.0

- First release: reads `VRTX` v4/v5, inflates the zstd payload, extracts every
  Part and rebuilds it as a coloured cube.
