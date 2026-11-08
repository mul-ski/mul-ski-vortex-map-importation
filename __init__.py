
bl_info = {
    "name": "Vortex Map Importer",
    "blender": (5, 2, 0),
    "version": (1, 1, 0),
    "category": "Import-Export",
}

from . import import_vrtx


def register():
    import_vrtx.register()


def unregister():
    import_vrtx.unregister()


if __name__ == "__main__":
    register()
