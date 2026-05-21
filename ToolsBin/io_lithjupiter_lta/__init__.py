# SPDX-License-Identifier: GPL-3.0-or-later
# LithTech Jupiter .LTA Model Exporter for Blender 4.5+
# Based on the Jupiter LTA Schema Reference and NOLF2 content guide.

bl_info = {
    "name": "LithTech Jupiter LTA Model Exporter",
    "author": "Community",
    "version": (1, 0, 0),
    "blender": (4, 5, 0),
    "location": "File > Export > LithTech Model (.lta)",
    "description": "Export models to LithTech Jupiter engine .LTA format (NOLF2 compatible)",
    "category": "Import-Export",
}

import bpy
from bpy.props import (
    StringProperty,
    BoolProperty,
    FloatProperty,
    IntProperty,
    EnumProperty,
    CollectionProperty,
)
from bpy_extras.io_utils import ExportHelper
import os


# ---------------------------------------------------------------------------
#  Operator – Export LTA
# ---------------------------------------------------------------------------
class EXPORT_OT_lithtech_lta(bpy.types.Operator, ExportHelper):
    """Export scene to LithTech Jupiter .LTA model format"""
    bl_idname = "export_scene.lithtech_lta"
    bl_label = "Export LithTech LTA"
    bl_options = {'PRESET', 'UNDO'}

    filename_ext = ".lta"

    filter_glob: StringProperty(
        default="*.lta",
        options={'HIDDEN'},
        maxlen=255,
    )

    # --- General --------------------------------------------------------
    export_scale: FloatProperty(
        name="Scale",
        description="Scale factor applied to the exported model",
        default=1.0,
        min=0.001,
        max=10000.0,
    )

    apply_modifiers: BoolProperty(
        name="Apply Modifiers",
        description="Apply mesh modifiers before exporting",
        default=True,
    )

    selected_only: BoolProperty(
        name="Selected Only",
        description="Export only selected objects",
        default=False,
    )

    triangulate: BoolProperty(
        name="Triangulate Faces",
        description="Convert all faces to triangles (required by LithTech)",
        default=True,
    )

    coord_frame: EnumProperty(
        name="Coordinate Frame",
        description="Coordinate system for the exported model",
        items=[
            ('LH_YUP', "Left-Hand Y-Up (Default)", "LithTech default coordinate frame"),
            ('LH_ZUP', "Left-Hand Z-Up", "Left-handed Z-up"),
            ('RH_YUP', "Right-Hand Y-Up", "Right-handed Y-up"),
            ('RH_ZUP', "Right-Hand Z-Up", "Right-handed Z-up"),
        ],
        default='LH_YUP',
    )

    # --- Geometry -------------------------------------------------------
    export_meshes: BoolProperty(
        name="Export Meshes",
        description="Export mesh geometry (shapes/pieces)",
        default=True,
    )

    export_normals: BoolProperty(
        name="Export Normals",
        description="Include vertex normals in the export",
        default=True,
    )

    export_uvs: BoolProperty(
        name="Export UVs",
        description="Include UV coordinates",
        default=True,
    )

    export_vertex_colors: BoolProperty(
        name="Export Vertex Colors",
        description="Include vertex color data",
        default=False,
    )

    # --- Materials / Textures -------------------------------------------
    export_materials: BoolProperty(
        name="Export Materials",
        description="Export material data (diffuse, specular, shininess, texture indices)",
        default=True,
    )

    texture_path_prefix: StringProperty(
        name="Texture Path Prefix",
        description="Prefix prepended to texture filenames in texture-bindings (e.g. 'chars\\skins\\')",
        default="",
    )

    default_render_style: StringProperty(
        name="Default RenderStyle",
        description="Default render style path (e.g. 'RS\\default.ltb')",
        default="RS\\default.ltb",
    )

    # --- Skeleton / Armature --------------------------------------------
    export_skeleton: BoolProperty(
        name="Export Skeleton",
        description="Export armature as skeleton hierarchy",
        default=True,
    )

    export_skin_weights: BoolProperty(
        name="Export Skin Weights",
        description="Export vertex group weights as skel-deformer weightsets",
        default=True,
    )

    # --- Animations -----------------------------------------------------
    export_animations: BoolProperty(
        name="Export Animations",
        description="Export animation data (actions / NLA strips)",
        default=True,
    )

    animation_name: StringProperty(
        name="Animation Name",
        description="Name for the exported animation (leave blank to use action name)",
        default="",
    )

    anim_framerate: FloatProperty(
        name="Animation Rate (FPS)",
        description="Keyframe rate for the exported animation",
        default=30.0,
        min=1.0,
        max=120.0,
    )

    use_playback_range: BoolProperty(
        name="Use Playback Range",
        description="Export only the frames in the timeline playback range",
        default=True,
    )

    # --- Sockets --------------------------------------------------------
    export_sockets: BoolProperty(
        name="Export Sockets",
        description="Export empties named Socket_* as model sockets",
        default=True,
    )

    # --- LOD / On-load-cmds ---------------------------------------------
    export_lod: BoolProperty(
        name="Export LOD Data",
        description="Export level-of-detail parameters (set-repl-lod-original)",
        default=False,
    )

    lod_distances: StringProperty(
        name="LOD Distances",
        description="Comma-separated LOD switch distances (e.g. '250,550')",
        default="250,550",
    )

    lod_percentages: StringProperty(
        name="LOD Tri %%",
        description="Comma-separated triangle percentages to keep (e.g. '0.6,0.25')",
        default="0.6,0.25",
    )

    # --- Weight Sets (anim blending) ------------------------------------
    export_weight_sets: BoolProperty(
        name="Export Weight Sets",
        description="Export default NOLF2-style animation weight sets (Null, Upper, Lower, blink, twitch)",
        default=True,
    )

    # --- Node Flags -----------------------------------------------------
    export_node_flags: BoolProperty(
        name="Export Node Flags",
        description="Export set-node-flags (e.g. ignore translation on specific bones)",
        default=False,
    )

    node_flags_ignore_trans: StringProperty(
        name="Ignore-Translation Bones",
        description="Comma-separated bone names whose positional info should be ignored by child models",
        default="",
    )

    # --- Child Models ---------------------------------------------------
    export_child_models: BoolProperty(
        name="Export Child Model Refs",
        description="Include child-model references in on-load-cmds",
        default=False,
    )

    child_model_paths: StringProperty(
        name="Child Model Paths",
        description="Semicolon-separated relative paths to child model .lta files",
        default="",
    )

    # --- Command String -------------------------------------------------
    command_string: StringProperty(
        name="Command String",
        description="Model command string (e.g. 'ShadowEnable')",
        default="",
    )

    # --- User Dims ------------------------------------------------------
    export_user_dims: BoolProperty(
        name="Export User Dims",
        description="Export per-animation bounding box dimensions",
        default=True,
    )

    user_dims_x: FloatProperty(name="Dims X", default=24.0, min=0.0)
    user_dims_y: FloatProperty(name="Dims Y", default=53.0, min=0.0)
    user_dims_z: FloatProperty(name="Dims Z", default=24.0, min=0.0)

    # --- Global Radius --------------------------------------------------
    global_radius: FloatProperty(
        name="Global Radius",
        description="Visibility radius for the model",
        default=96.0,
        min=0.0,
    )

    # --- Interpolation --------------------------------------------------
    interp_time: IntProperty(
        name="Interpolation Time (ms)",
        description="Default transition time between animations in milliseconds",
        default=200,
        min=0,
    )

    # --- Render Priority ------------------------------------------------
    default_render_priority: IntProperty(
        name="Default Render Priority",
        description="Render priority for opaque pieces (0 = normal)",
        default=0,
        min=0,
        max=128,
    )

    # --- OBB ------------------------------------------------------------
    export_obb: BoolProperty(
        name="Export OBBs",
        description="Export oriented bounding boxes from custom properties on bones",
        default=False,
    )

    # ===================================================================
    #  UI Layout
    # ===================================================================
    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        # General
        box = layout.box()
        box.label(text="General", icon='SETTINGS')
        box.prop(self, "export_scale")
        box.prop(self, "apply_modifiers")
        box.prop(self, "selected_only")
        box.prop(self, "triangulate")
        box.prop(self, "coord_frame")

        # Geometry
        box = layout.box()
        box.label(text="Geometry", icon='MESH_DATA')
        box.prop(self, "export_meshes")
        sub = box.column()
        sub.enabled = self.export_meshes
        sub.prop(self, "export_normals")
        sub.prop(self, "export_uvs")
        sub.prop(self, "export_vertex_colors")

        # Materials
        box = layout.box()
        box.label(text="Materials & Textures", icon='MATERIAL')
        box.prop(self, "export_materials")
        sub = box.column()
        sub.enabled = self.export_materials
        sub.prop(self, "texture_path_prefix")
        sub.prop(self, "default_render_style")
        sub.prop(self, "default_render_priority")

        # Skeleton
        box = layout.box()
        box.label(text="Skeleton", icon='ARMATURE_DATA')
        box.prop(self, "export_skeleton")
        sub = box.column()
        sub.enabled = self.export_skeleton
        sub.prop(self, "export_skin_weights")

        # Animations
        box = layout.box()
        box.label(text="Animation", icon='ACTION')
        box.prop(self, "export_animations")
        sub = box.column()
        sub.enabled = self.export_animations
        sub.prop(self, "animation_name")
        sub.prop(self, "anim_framerate")
        sub.prop(self, "use_playback_range")
        sub.prop(self, "interp_time")

        # Sockets
        box = layout.box()
        box.label(text="Sockets", icon='EMPTY_ARROWS')
        box.prop(self, "export_sockets")

        # Weight Sets
        box = layout.box()
        box.label(text="Weight Sets / Anim Blending", icon='MOD_VERTEX_WEIGHT')
        box.prop(self, "export_weight_sets")

        # User Dims / Radius
        box = layout.box()
        box.label(text="User Dims & Radius", icon='PIVOT_BOUNDBOX')
        box.prop(self, "export_user_dims")
        sub = box.column()
        sub.enabled = self.export_user_dims
        sub.prop(self, "user_dims_x")
        sub.prop(self, "user_dims_y")
        sub.prop(self, "user_dims_z")
        box.prop(self, "global_radius")

        # LOD
        box = layout.box()
        box.label(text="Level of Detail", icon='MOD_DECIM')
        box.prop(self, "export_lod")
        sub = box.column()
        sub.enabled = self.export_lod
        sub.prop(self, "lod_distances")
        sub.prop(self, "lod_percentages")

        # Node Flags
        box = layout.box()
        box.label(text="Node Flags", icon='BONE_DATA')
        box.prop(self, "export_node_flags")
        sub = box.column()
        sub.enabled = self.export_node_flags
        sub.prop(self, "node_flags_ignore_trans")

        # Child Models
        box = layout.box()
        box.label(text="Child Models", icon='LINKED')
        box.prop(self, "export_child_models")
        sub = box.column()
        sub.enabled = self.export_child_models
        sub.prop(self, "child_model_paths")

        # Command String / OBB
        box = layout.box()
        box.label(text="Misc", icon='TEXT')
        box.prop(self, "command_string")
        box.prop(self, "export_obb")

    # ===================================================================
    #  Execute
    # ===================================================================
    def execute(self, context):
        from . import export_lta
        keywords = self.as_keywords(ignore=(
            "filter_glob",
            "check_existing",
        ))
        return export_lta.export(context, **keywords)


# ---------------------------------------------------------------------------
#  Menu entry
# ---------------------------------------------------------------------------
def menu_func_export(self, context):
    self.layout.operator(EXPORT_OT_lithtech_lta.bl_idname, text="LithTech Model (.lta)")


# ---------------------------------------------------------------------------
#  Registration
# ---------------------------------------------------------------------------
classes = (
    EXPORT_OT_lithtech_lta,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
