# SPDX-License-Identifier: GPL-3.0-or-later

bl_info = {
    "name": "LithTech Jupiter LTA Importer",
    "author": "",
    "version": (1, 2, 0),
    "blender": (4, 5, 0),
    "location": "File > Import > LithTech Jupiter (.lta)",
    "description": (
        "Import LithTech Jupiter LTA models, "
        "skeletons, skinned meshes, sockets and "
        "animations as named NLA strips"
    ),
    "category": "Import-Export",
}

import bpy

from bpy.props import (
    StringProperty,
    BoolProperty,
    FloatProperty,
    IntProperty,
    EnumProperty
)

from bpy_extras.io_utils import ImportHelper

from . import import_lta


class ImportLTA(
    bpy.types.Operator,
    ImportHelper
):
    bl_idname = "import_scene.lta"
    bl_label = "Import LithTech LTA"
    bl_options = {'PRESET', 'UNDO'}

    filename_ext = ".lta"

    filter_glob: StringProperty(
        default="*.lta",
        options={'HIDDEN'}
    )

    import_scale: FloatProperty(
        name="Scale",
        description="Global import scale",
        default=1.0,
        min=0.0001
    )

    coord_frame: EnumProperty(
        name="Coordinate Frame",
        description="Override coordinate frame",
        items=(
            ('AUTO', "Auto Detect", ""),
            ('LH_YUP', "LH Y-Up", ""),
            ('LH_ZUP', "LH Z-Up", ""),
            ('RH_YUP', "RH Y-Up", ""),
            ('RH_ZUP', "RH Z-Up", "")
        ),
        default='AUTO'
    )

    fps: IntProperty(
        name="Animation FPS",
        description="Convert LTA milliseconds into Blender frames",
        default=30,
        min=1,
        max=240
    )

    import_armature: BoolProperty(
        name="Import Skeleton",
        default=True
    )

    import_meshes: BoolProperty(
        name="Import Meshes",
        default=True
    )

    import_animations: BoolProperty(
        name="Import Animations",
        description=(
            "Create one Action and one NLA strip "
            "per LTA animset"
        ),
        default=True
    )

    def execute(self, context):

        kwargs=self.as_keywords(
            ignore=(
                "filter_glob",
            )
        )

        return import_lta.import_lta(
            context,
            **kwargs
        )


def menu_func_import(
        self,
        context
):
    self.layout.operator(
        ImportLTA.bl_idname,
        text="LithTech Jupiter (.lta)"
    )


classes=(
    ImportLTA,
)


def register():

    for cls in classes:
        bpy.utils.register_class(
            cls
        )

    bpy.types.TOPBAR_MT_file_import.append(
        menu_func_import
    )


def unregister():

    bpy.types.TOPBAR_MT_file_import.remove(
        menu_func_import
    )

    for cls in reversed(classes):

        bpy.utils.unregister_class(
            cls
        )


if __name__ == "__main__":
    register()