# SPDX-License-Identifier: GPL-3.0-or-later
# LithTech Jupiter .LTA Model Importer for Blender 4.5+
# Companion to the .LTA exporter; reads the same dialect plus anything else
# that conforms to the Jupiter LTA Schema.

bl_info = {
    "name": "LithTech Jupiter LTA Model Importer",
    "author": "Community",
    "version": (1, 0, 0),
    "blender": (4, 5, 0),
    "location": "File > Import > LithTech Model (.lta)",
    "description": "Import models from LithTech Jupiter engine .LTA format (NOLF2 compatible)",
    "category": "Import-Export",
}

import bpy
from bpy.props import (
    StringProperty,
    BoolProperty,
    FloatProperty,
    IntProperty,
    EnumProperty,
)
from bpy_extras.io_utils import ImportHelper


# ---------------------------------------------------------------------------
#  Operator – Import LTA
# ---------------------------------------------------------------------------
class IMPORT_OT_lithtech_lta(bpy.types.Operator, ImportHelper):
    """Import a LithTech Jupiter .LTA model"""
    bl_idname = "import_scene.lithtech_lta"
    bl_label = "Import LithTech LTA"
    bl_options = {'PRESET', 'UNDO'}

    filename_ext = ".lta"

    filter_glob: StringProperty(
        default="*.lta",
        options={'HIDDEN'},
        maxlen=255,
    )

    # --- General --------------------------------------------------------
    import_scale: FloatProperty(
        name="Scale",
        description="Scale factor applied to the imported model. "
                    "Use the inverse of what you exported with",
        default=1.0,
        min=0.0001,
        max=10000.0,
    )

    coord_frame: EnumProperty(
        name="Coordinate Frame",
        description="Source coordinate system. AUTO uses (coord-frame-type) "
                    "from the file",
        items=[
            ('AUTO',   "Auto-detect",          "Read from coord-frame-type"),
            ('LH_YUP', "Left-Hand Y-Up",       "LithTech default"),
            ('LH_ZUP', "Left-Hand Z-Up",       "Left-handed Z-up"),
            ('RH_YUP', "Right-Hand Y-Up",      "Right-handed Y-up"),
            ('RH_ZUP', "Right-Hand Z-Up",      "Same as Blender (no conversion)"),
        ],
        default='AUTO',
    )

    matrix_mode: EnumProperty(
        name="Matrix Convention",
        description="How to interpret per-bone matrices. The LTA schema "
                    "default is 'global' (each matrix is the bone's "
                    "world/model-space pose). The exporter that ships with "
                    "this addon writes 'local' (parent-relative). AUTO reads "
                    "flag3 from (coord-frame-type) and falls back to global",
        items=[
            ('AUTO',   "Auto-detect",       "Read from coord-frame-type flag3"),
            ('GLOBAL', "Global (absolute)", "Each bone matrix is in model space"),
            ('LOCAL',  "Local (parent-relative)", "Each bone matrix is relative to its parent"),
        ],
        default='AUTO',
    )

    # --- Geometry -------------------------------------------------------
    import_meshes: BoolProperty(
        name="Import Meshes",
        description="Import mesh geometry (shapes / pieces)",
        default=True,
    )

    import_uvs: BoolProperty(
        name="Import UVs",
        description="Import UV coordinates",
        default=True,
    )

    import_normals: BoolProperty(
        name="Import Normals",
        description="Import custom split normals from the file",
        default=True,
    )

    import_vertex_colors: BoolProperty(
        name="Import Vertex Colors",
        description="Import vertex color attribute when present",
        default=True,
    )

    # --- Materials / Textures -------------------------------------------
    import_materials: BoolProperty(
        name="Import Materials",
        description="Create Blender materials from the LTA material data",
        default=True,
    )

    texture_search_path: StringProperty(
        name="Texture Search Path",
        description="Semicolon-separated extra directories to look in for "
                    "texture files. The folder containing the .lta is always "
                    "searched first",
        default="",
    )

    # --- Skeleton -------------------------------------------------------
    import_armature: BoolProperty(
        name="Import Skeleton",
        description="Build an armature from the (hierarchy) tree",
        default=True,
    )

    bone_length: FloatProperty(
        name="Default Bone Length",
        description="Display length for bones that have no children. "
                    "Bones with children automatically stretch to the first child",
        default=0.1,
        min=0.001,
        max=100.0,
    )

    # --- Sockets --------------------------------------------------------
    import_sockets: BoolProperty(
        name="Import Sockets",
        description="Create Empties named Socket_* for each (socket) node",
        default=True,
    )

    # --- Animation ------------------------------------------------------
    import_animations: BoolProperty(
        name="Import Animations",
        description="Import animsets as Blender actions",
        default=True,
    )

    anim_framerate: FloatProperty(
        name="Animation Rate (FPS)",
        description="Frames-per-second used to convert LTA millisecond "
                    "timestamps into Blender frame numbers. Should match what "
                    "the file was exported with",
        default=30.0,
        min=1.0,
        max=120.0,
    )

    anim_frame_offset: IntProperty(
        name="First Frame",
        description="Blender frame that corresponds to the first key of "
                    "every imported animation",
        default=1,
        min=0,
    )

    import_to_nla: BoolProperty(
        name="Animations as NLA Strips",
        description="Push every animset to its own NLA track / strip named "
                    "after the animset. When off, only the first animation "
                    "is set as the active Action and the rest are stashed",
        default=True,
    )

    # ===================================================================
    #  UI Layout
    # ===================================================================
    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        box = layout.box()
        box.label(text="General", icon='SETTINGS')
        box.prop(self, "import_scale")
        box.prop(self, "coord_frame")
        box.prop(self, "matrix_mode")

        box = layout.box()
        box.label(text="Geometry", icon='MESH_DATA')
        box.prop(self, "import_meshes")
        sub = box.column()
        sub.enabled = self.import_meshes
        sub.prop(self, "import_uvs")
        sub.prop(self, "import_normals")
        sub.prop(self, "import_vertex_colors")

        box = layout.box()
        box.label(text="Materials & Textures", icon='MATERIAL')
        box.prop(self, "import_materials")
        sub = box.column()
        sub.enabled = self.import_materials
        sub.prop(self, "texture_search_path")

        box = layout.box()
        box.label(text="Skeleton", icon='ARMATURE_DATA')
        box.prop(self, "import_armature")
        sub = box.column()
        sub.enabled = self.import_armature
        sub.prop(self, "bone_length")

        box = layout.box()
        box.label(text="Sockets", icon='EMPTY_ARROWS')
        box.prop(self, "import_sockets")

        box = layout.box()
        box.label(text="Animation", icon='ACTION')
        box.prop(self, "import_animations")
        sub = box.column()
        sub.enabled = self.import_animations
        sub.prop(self, "anim_framerate")
        sub.prop(self, "anim_frame_offset")
        sub.prop(self, "import_to_nla")

    # ===================================================================
    #  Execute
    # ===================================================================
    def execute(self, context):
        from . import import_lta
        keywords = self.as_keywords(ignore=(
            "filter_glob",
            "check_existing",
        ))
        try:
            return import_lta.import_lta(context, **keywords)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, f"LTA import failed: {exc}")
            return {'CANCELLED'}


# ---------------------------------------------------------------------------
#  Menu entry
# ---------------------------------------------------------------------------
def menu_func_import(self, context):
    self.layout.operator(IMPORT_OT_lithtech_lta.bl_idname,
                         text="LithTech Model (.lta)")


# ---------------------------------------------------------------------------
#  Registration
# ---------------------------------------------------------------------------
classes = (
    IMPORT_OT_lithtech_lta,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
