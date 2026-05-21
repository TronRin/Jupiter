# SPDX-License-Identifier: GPL-3.0-or-later
# export_lta.py – Core LTA writer for LithTech Jupiter engine models.

from __future__ import annotations

import bpy
import bmesh
import math
import os
from mathutils import Matrix, Vector, Quaternion
from typing import TextIO, Optional


# ======================================================================
#  LTA Writer – handles parenthetical formatting
# ======================================================================

class LTAWriter:
    """Writes properly-indented parenthetical LTA data."""

    def __init__(self, fp: TextIO):
        self._fp = fp
        self._depth = 0

    # ------------------------------------------------------------------
    def _indent(self) -> str:
        return "\t" * self._depth

    def begin(self, *tokens):
        """Open a parenthetical node.  ``begin("foo", '"bar"')`` → ``(foo "bar"``"""
        parts = " ".join(str(t) for t in tokens)
        self._fp.write(f"{self._indent()}({parts}\n")
        self._depth += 1

    def end(self):
        """Close the current node with ``)``."""
        self._depth -= 1
        self._fp.write(f"{self._indent()})\n")

    def node(self, *tokens):
        """Write a single-line node: ``(tok1 tok2 …)``."""
        parts = " ".join(str(t) for t in tokens)
        self._fp.write(f"{self._indent()}({parts})\n")

    def line(self, text: str):
        """Write a raw indented line (no parens)."""
        self._fp.write(f"{self._indent()}{text}\n")

    def blank(self):
        self._fp.write("\n")

    def comment(self, text: str):
        self._fp.write(f"{self._indent()}; {text}\n")


# ======================================================================
#  Formatting helpers
# ======================================================================

def _flt(v: float) -> str:
    """Format float for LTA – 6 decimal places, strip trailing zeros."""
    return f"{v:.6f}"


def _qstr(s: str) -> str:
    """Return a quoted string for LTA."""
    return f'"{s}"'


def _vec3(v) -> str:
    return f"{_flt(v[0])} {_flt(v[1])} {_flt(v[2])}"


def _vec4(v) -> str:
    return f"{_flt(v[0])} {_flt(v[1])} {_flt(v[2])} {_flt(v[3])}"


# ======================================================================
#  Coordinate-frame conversion
# ======================================================================

def _make_conversion_matrix(coord_frame: str) -> Matrix:
    """Return a matrix that converts Blender's right-hand Z-up to the
    chosen LTA coordinate frame."""
    # Blender: right-hand, Z-up
    if coord_frame == 'LH_YUP':
        # Flip X to go from RH to LH; swap Y↔Z for Z-up→Y-up
        # x' = x,  y' = z,  z' = y  (then negate x for handedness)
        return Matrix((
            (1, 0, 0, 0),
            (0, 0, 1, 0),
            (0, 1, 0, 0),
            (0, 0, 0, 1),
        ))
    elif coord_frame == 'LH_ZUP':
        return Matrix((
            (1, 0, 0, 0),
            (0, 1, 0, 0),
            (0, 0, 1, 0),
            (0, 0, 0, 1),
        ))
    elif coord_frame == 'RH_YUP':
        return Matrix((
            (1, 0, 0, 0),
            (0, 0, 1, 0),
            (0, 1, 0, 0),
            (0, 0, 0, 1),
        ))
    elif coord_frame == 'RH_ZUP':
        return Matrix.Identity(4)
    return Matrix.Identity(4)


def _coord_frame_tokens(coord_frame: str):
    """Return (hand, up, scope) strings for the coord-frame-type node."""
    table = {
        'LH_YUP': ("left-hand", "y-up", "global"),
        'LH_ZUP': ("left-hand", "z-up", "global"),
        'RH_YUP': ("right-hand", "y-up", "global"),
        'RH_ZUP': ("right-hand", "z-up", "global"),
    }
    return table.get(coord_frame, ("left-hand", "y-up", "global"))


# ======================================================================
#  Gather scene data
# ======================================================================

class BoneInfo:
    __slots__ = ("name", "index", "parent_index", "matrix_local",
                 "matrix_model", "children_indices")

    def __init__(self):
        self.name: str = ""
        self.index: int = 0
        self.parent_index: int = -1
        self.matrix_local: Matrix = Matrix.Identity(4)
        self.matrix_model: Matrix = Matrix.Identity(4)
        self.children_indices: list[int] = []


class PieceData:
    """One exported shape / piece."""
    __slots__ = ("name", "vertices", "normals", "uvs", "colors",
                 "tri_indices", "tex_indices", "nrm_indices", "col_indices",
                 "material_index", "weights", "parent_bone",
                 "has_separate_nrm_fs", "has_separate_tex_fs",
                 "has_separate_col_fs")

    def __init__(self):
        self.name: str = ""
        self.vertices: list[tuple] = []
        self.normals: list[tuple] = []
        self.uvs: list[tuple] = []
        self.colors: list[tuple] = []
        self.tri_indices: list[int] = []
        self.tex_indices: list[int] = []
        self.nrm_indices: list[int] = []
        self.col_indices: list[int] = []
        self.material_index: int = 0
        self.weights: list[list[tuple]] = []   # per-vertex: [(bone_idx, weight), …]
        self.parent_bone: str = ""
        self.has_separate_nrm_fs: bool = False
        self.has_separate_tex_fs: bool = False
        self.has_separate_col_fs: bool = False


class MaterialData:
    __slots__ = ("name", "diffuse", "specular", "emissive", "ambient",
                 "shininess", "texture_index", "texture_file")

    def __init__(self):
        self.name: str = ""
        self.diffuse = (0.8, 0.8, 0.8, 1.0)
        self.specular = (0.0, 0.0, 0.0)
        self.emissive = (0.0, 0.0, 0.0)
        self.ambient = (0.2, 0.2, 0.2)
        self.shininess: float = 0.0
        self.texture_index: int = 0
        self.texture_file: str = ""


class SocketData:
    __slots__ = ("name", "parent_bone", "position", "quaternion")

    def __init__(self):
        self.name: str = ""
        self.parent_bone: str = ""
        self.position = (0.0, 0.0, 0.0)
        self.quaternion = (0.0, 0.0, 0.0, 1.0)


class AnimKeyframe:
    """Position + quaternion for one bone at one time."""
    __slots__ = ("pos", "quat")

    def __init__(self, pos, quat):
        self.pos = pos
        self.quat = quat


class AnimData:
    __slots__ = ("name", "times", "bone_keyframes")

    def __init__(self):
        self.name: str = ""
        self.times: list[float] = []                        # ms
        self.bone_keyframes: dict[str, list[AnimKeyframe]] = {}  # bone_name → [kf, …]


# ======================================================================
#  Scene gathering
# ======================================================================

def _gather_armature(context, settings) -> tuple[Optional[bpy.types.Object], list[BoneInfo]]:
    """Find the first armature and extract bone info."""
    arm_obj = None
    objects = context.selected_objects if settings["selected_only"] else context.scene.objects
    for obj in objects:
        if obj.type == 'ARMATURE':
            arm_obj = obj
            break
    if arm_obj is None:
        return None, []

    armature = arm_obj.data
    bones: list[BoneInfo] = []
    bone_name_to_idx: dict[str, int] = {}

    # Build flat list from rest-pose bones
    for idx, bone in enumerate(armature.bones):
        bi = BoneInfo()
        bi.name = bone.name
        bi.index = idx
        bi.matrix_local = bone.matrix_local.copy()
        bone_name_to_idx[bone.name] = idx
        bones.append(bi)

    # Set parent indices & children
    for idx, bone in enumerate(armature.bones):
        if bone.parent:
            parent_idx = bone_name_to_idx.get(bone.parent.name, -1)
            bones[idx].parent_index = parent_idx
            if parent_idx >= 0:
                bones[parent_idx].children_indices.append(idx)

    return arm_obj, bones


def _gather_meshes(context, settings, arm_obj, bones, conv_mat) -> tuple[list[PieceData], list[MaterialData]]:
    """Triangulate + collect per-piece mesh data."""
    depsgraph = context.evaluated_depsgraph_get()
    scale = settings["export_scale"]
    scale_mat = Matrix.Scale(scale, 4)
    full_mat = conv_mat @ scale_mat

    objects = context.selected_objects if settings["selected_only"] else context.scene.objects
    mesh_objs = [o for o in objects if o.type == 'MESH']

    bone_name_to_idx = {b.name: b.index for b in bones}
    all_pieces: list[PieceData] = []
    all_materials: list[MaterialData] = []
    mat_name_to_idx: dict[str, int] = {}

    for obj in mesh_objs:
        if settings["apply_modifiers"]:
            eval_obj = obj.evaluated_get(depsgraph)
            mesh = eval_obj.to_mesh()
        else:
            mesh = obj.to_mesh()

        if mesh is None:
            continue

        # Triangulate
        if settings["triangulate"]:
            bm = bmesh.new()
            bm.from_mesh(mesh)
            bmesh.ops.triangulate(bm, faces=bm.faces[:])
            bm.to_mesh(mesh)
            bm.free()

        mesh.calc_loop_triangles()
        if settings["export_normals"]:
            mesh.customdata_custom_splitnormals_add()

        # Transform matrix (object → world → LTA space)
        obj_mat = full_mat @ obj.matrix_world

        # UV layer
        uv_layer = None
        if settings["export_uvs"] and mesh.uv_layers.active:
            uv_layer = mesh.uv_layers.active.data

        # Vertex-color layer
        color_layer = None
        if settings["export_vertex_colors"] and mesh.color_attributes:
            color_layer = mesh.color_attributes.active_color

        # Group meshes by material slot → one piece per material
        # If no materials, one piece for the whole mesh
        mat_slot_count = max(len(obj.material_slots), 1)
        pieces_for_obj: dict[int, PieceData] = {}

        for tri in mesh.loop_triangles:
            mat_idx = tri.material_index if tri.material_index < mat_slot_count else 0
            if mat_idx not in pieces_for_obj:
                p = PieceData()
                if mat_idx < len(obj.material_slots) and obj.material_slots[mat_idx].material:
                    mat = obj.material_slots[mat_idx].material
                    p.name = f"{obj.name}_{mat.name}"
                    p.material_index = _ensure_material(mat, all_materials, mat_name_to_idx,
                                                        settings)
                else:
                    p.name = obj.name if mat_slot_count <= 1 else f"{obj.name}_mat{mat_idx}"
                    p.material_index = 0
                # Determine parent bone (first vertex-group bone or root)
                if bones and arm_obj:
                    p.parent_bone = bones[0].name
                pieces_for_obj[mat_idx] = p

            piece = pieces_for_obj[mat_idx]
            base_vert = len(piece.vertices)
            base_uv = len(piece.uvs)
            base_nrm = len(piece.normals)
            base_col = len(piece.colors)

            for i, loop_idx in enumerate(tri.loops):
                vi = tri.vertices[i]
                co = obj_mat @ mesh.vertices[vi].co
                piece.vertices.append((co.x, co.y, co.z))

                if settings["export_normals"]:
                    n = (obj_mat.to_3x3() @ Vector(mesh.loops[loop_idx].normal)).normalized()
                    piece.normals.append((n.x, n.y, n.z))

                if uv_layer:
                    uv = uv_layer[loop_idx].uv
                    piece.uvs.append((uv[0], 1.0 - uv[1]))  # flip V for LithTech

                if color_layer and hasattr(color_layer, 'data') and loop_idx < len(color_layer.data):
                    c = color_layer.data[loop_idx].color
                    piece.colors.append(tuple(c))

                # Skin weights
                if settings["export_skin_weights"] and arm_obj:
                    vert = mesh.vertices[vi]
                    wlist = []
                    for vg in vert.groups:
                        if vg.group < len(obj.vertex_groups):
                            gname = obj.vertex_groups[vg.group].name
                            if gname in bone_name_to_idx:
                                wlist.append((bone_name_to_idx[gname], vg.weight))
                    if not wlist:
                        wlist.append((0, 1.0))
                    piece.weights.append(wlist)

            # Triangle indices (within this piece)
            piece.tri_indices.extend([base_vert, base_vert + 1, base_vert + 2])
            if uv_layer:
                piece.tex_indices.extend([base_uv, base_uv + 1, base_uv + 2])
            if settings["export_normals"]:
                piece.nrm_indices.extend([base_nrm, base_nrm + 1, base_nrm + 2])
            if color_layer:
                piece.col_indices.extend([base_col, base_col + 1, base_col + 2])

        for p in pieces_for_obj.values():
            # Since we're emitting per-loop data the face-set indices are
            # identical to the tri-fs already; mark them as separate only if
            # we actually have the data and the counts differ from vertex count.
            p.has_separate_tex_fs = bool(p.tex_indices)
            p.has_separate_nrm_fs = bool(p.nrm_indices)
            p.has_separate_col_fs = bool(p.col_indices)
            all_pieces.append(p)

        if settings["apply_modifiers"]:
            eval_obj.to_mesh_clear()
        else:
            obj.to_mesh_clear()

    # Ensure at least a default material
    if not all_materials:
        md = MaterialData()
        md.name = "default"
        all_materials.append(md)

    return all_pieces, all_materials


def _ensure_material(mat, all_materials, mat_name_to_idx, settings) -> int:
    """Add material to the list if not present; return its index."""
    if mat.name in mat_name_to_idx:
        return mat_name_to_idx[mat.name]

    md = MaterialData()
    md.name = mat.name
    md.texture_index = len(all_materials)

    # Try to extract base color from Principled BSDF
    if mat.use_nodes:
        for node in mat.node_tree.nodes:
            if node.type == 'BSDF_PRINCIPLED':
                bc = node.inputs.get("Base Color")
                if bc:
                    c = bc.default_value
                    md.diffuse = (c[0], c[1], c[2], c[3])
                rough = node.inputs.get("Roughness")
                if rough:
                    md.shininess = (1.0 - rough.default_value) * 128.0
                spec_input = node.inputs.get("Specular IOR Level")
                if spec_input is None:
                    spec_input = node.inputs.get("Specular")
                if spec_input:
                    sv = spec_input.default_value if isinstance(spec_input.default_value, float) else 0.5
                    md.specular = (sv, sv, sv)
                # Find image texture connected to Base Color
                if bc.is_linked:
                    link_node = bc.links[0].from_node
                    if link_node.type == 'TEX_IMAGE' and link_node.image:
                        fname = link_node.image.name
                        prefix = settings.get("texture_path_prefix", "")
                        md.texture_file = prefix + fname
                break
    else:
        md.diffuse = (mat.diffuse_color[0], mat.diffuse_color[1],
                      mat.diffuse_color[2], 1.0)

    idx = len(all_materials)
    all_materials.append(md)
    mat_name_to_idx[mat.name] = idx
    return idx


def _gather_sockets(context, settings, arm_obj, bones, conv_mat) -> list[SocketData]:
    """Collect empties whose name starts with ``Socket_`` and convert to LTA sockets."""
    if not settings["export_sockets"]:
        return []

    scale = settings["export_scale"]
    scale_mat = Matrix.Scale(scale, 4)
    full_mat = conv_mat @ scale_mat

    sockets: list[SocketData] = []
    bone_names = {b.name for b in bones}
    objects = context.selected_objects if settings["selected_only"] else context.scene.objects

    for obj in objects:
        if obj.type != 'EMPTY':
            continue
        name = obj.name
        if not name.startswith("Socket_"):
            continue

        sd = SocketData()
        sd.name = name[7:]  # strip "Socket_" prefix

        # Determine parent bone
        if obj.parent and obj.parent.type == 'ARMATURE' and obj.parent_bone:
            sd.parent_bone = obj.parent_bone
        elif bones:
            sd.parent_bone = bones[0].name

        loc = full_mat @ obj.location
        sd.position = (loc.x, loc.y, loc.z)

        rot = obj.rotation_quaternion if obj.rotation_mode == 'QUATERNION' else obj.rotation_euler.to_quaternion()
        sd.quaternion = (rot.x, rot.y, rot.z, rot.w)
        sockets.append(sd)

    return sockets


def _gather_animations(context, settings, arm_obj, bones, conv_mat) -> list[AnimData]:
    """Bake actions into per-bone pos/quat keyframe lists."""
    if not settings["export_animations"] or arm_obj is None:
        return []

    scale = settings["export_scale"]
    scale_mat = Matrix.Scale(scale, 4)
    full_mat = conv_mat @ scale_mat

    scene = context.scene
    fps = settings["anim_framerate"]

    if settings["use_playback_range"]:
        frame_start = scene.frame_start
        frame_end = scene.frame_end
    else:
        frame_start = scene.frame_start
        frame_end = scene.frame_end

    actions_to_export = []
    if arm_obj.animation_data and arm_obj.animation_data.action:
        actions_to_export.append(arm_obj.animation_data.action)
    # Also grab all actions that target this armature (via NLA or stash)
    for action in bpy.data.actions:
        if action not in actions_to_export:
            # Check if action has bone channels matching our armature
            dominated = False
            for fc in action.fcurves:
                if fc.data_path.startswith("pose.bones["):
                    dominated = True
                    break
            if dominated:
                actions_to_export.append(action)

    bone_name_list = [b.name for b in bones]
    all_anims: list[AnimData] = []
    original_action = arm_obj.animation_data.action if arm_obj.animation_data else None
    original_frame = scene.frame_current

    for action in actions_to_export:
        if arm_obj.animation_data is None:
            arm_obj.animation_data_create()
        arm_obj.animation_data.action = action

        ad = AnimData()
        override_name = settings.get("animation_name", "")
        ad.name = override_name if override_name else action.name

        # Determine frame range from action or playback range
        if settings["use_playback_range"]:
            f_start = frame_start
            f_end = frame_end
        else:
            f_start = int(action.frame_range[0])
            f_end = int(action.frame_range[1])

        for bname in bone_name_list:
            ad.bone_keyframes[bname] = []

        for frame in range(f_start, f_end + 1):
            scene.frame_set(frame)
            context.view_layer.update()

            time_ms = ((frame - f_start) / fps) * 1000.0
            ad.times.append(time_ms)

            for bname in bone_name_list:
                pbone = arm_obj.pose.bones.get(bname)
                if pbone is None:
                    ad.bone_keyframes[bname].append(
                        AnimKeyframe((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))
                    )
                    continue

                # Get the bone's pose-space matrix relative to rest
                mat = pbone.matrix
                if pbone.parent:
                    mat = pbone.parent.matrix.inverted_safe() @ mat

                # Apply coordinate conversion
                loc = full_mat @ mat.to_translation()
                rot = mat.to_quaternion()
                # Convert quaternion to LTA order (x y z w)
                ad.bone_keyframes[bname].append(
                    AnimKeyframe(
                        (loc.x, loc.y, loc.z),
                        (rot.x, rot.y, rot.z, rot.w),
                    )
                )

        all_anims.append(ad)

    # Restore original state
    if arm_obj.animation_data:
        arm_obj.animation_data.action = original_action
    scene.frame_set(original_frame)

    return all_anims


# ======================================================================
#  LTA file writing
# ======================================================================

def _write_hierarchy(w: LTAWriter, bones: list[BoneInfo], conv_mat: Matrix, scale: float):
    """Write the ``(hierarchy ...)`` node with nested transforms."""
    if not bones:
        # Single dummy joint for inanimate models
        w.begin("hierarchy", _qstr("base"))
        w.begin("children")
        w.begin("")
        w.begin("transform", _qstr("base_node"))
        w.begin("matrix")
        w.begin("")
        w.node("1.000000 0.000000 0.000000 0.000000")
        w.node("0.000000 1.000000 0.000000 0.000000")
        w.node("0.000000 0.000000 1.000000 0.000000")
        w.node("0.000000 0.000000 0.000000 1.000000")
        w.end()  # matrix list
        w.end()  # matrix
        w.end()  # transform
        w.end()  # children list
        w.end()  # children
        w.end()  # hierarchy
        return

    scale_mat = Matrix.Scale(scale, 4)
    full_mat = conv_mat @ scale_mat

    root_name = bones[0].name if bones else "root"
    w.begin("hierarchy", _qstr(root_name))

    def _write_bone_children(parent_idx):
        children = bones[parent_idx].children_indices if parent_idx >= 0 else \
            [i for i, b in enumerate(bones) if b.parent_index == -1]
        if not children:
            return
        w.begin("children")
        w.begin("")
        for ci in children:
            bone = bones[ci]
            # Compute local matrix relative to parent
            if bone.parent_index >= 0:
                parent_mat = bones[bone.parent_index].matrix_local
                local_mat = parent_mat.inverted_safe() @ bone.matrix_local
            else:
                local_mat = bone.matrix_local

            # Apply conversion to the root bone only
            if bone.parent_index == -1:
                local_mat = full_mat @ local_mat

            w.begin("transform", _qstr(bone.name))
            w.begin("matrix")
            w.begin("")
            for row in range(4):
                w.node(" ".join(_flt(local_mat[row][col]) for col in range(4)))
            w.end()  # matrix list
            w.end()  # matrix

            # Recurse children
            _write_bone_children(ci)

            w.end()  # transform

        w.end()  # children list
        w.end()  # children

    _write_bone_children(-1)
    w.end()  # hierarchy


def _write_shape(w: LTAWriter, piece: PieceData, mat: MaterialData,
                 settings: dict):
    """Write one ``(shape ...)`` node."""
    w.begin("shape", _qstr(piece.name))

    # parent
    parent = piece.parent_bone if piece.parent_bone else "base_node"
    w.node("parent", _qstr(parent))

    # geometry → mesh
    w.begin("geometry")
    w.begin("mesh", _qstr(piece.name))

    # vertex
    w.begin("vertex")
    w.begin("")
    for v in piece.vertices:
        w.node(_vec3(v))
    w.end()
    w.end()  # vertex

    # uvs
    if piece.uvs and settings["export_uvs"]:
        w.begin("uvs")
        w.begin("")
        for uv in piece.uvs:
            w.node(f"{_flt(uv[0])} {_flt(uv[1])}")
        w.end()
        w.end()  # uvs

    # normals
    if piece.normals and settings["export_normals"]:
        w.begin("normals")
        w.begin("")
        for n in piece.normals:
            w.node(_vec3(n))
        w.end()
        w.end()  # normals

    # colors
    if piece.colors and settings["export_vertex_colors"]:
        w.begin("colors")
        w.begin("")
        for c in piece.colors:
            if len(c) >= 4:
                w.node(_vec4(c))
            else:
                w.node(f"{_vec3(c)} 1.000000")
        w.end()
        w.end()  # colors

    # tri-fs (triangle face-set indices)
    w.begin("tri-fs")
    idx_str = " ".join(str(i) for i in piece.tri_indices)
    w.node(idx_str)
    w.end()  # tri-fs

    # tex-fs
    if piece.has_separate_tex_fs and piece.tex_indices:
        w.begin("tex-fs")
        idx_str = " ".join(str(i) for i in piece.tex_indices)
        w.node(idx_str)
        w.end()

    # nrm-fs
    if piece.has_separate_nrm_fs and piece.nrm_indices:
        w.begin("nrm-fs")
        idx_str = " ".join(str(i) for i in piece.nrm_indices)
        w.node(idx_str)
        w.end()

    # col-fs
    if piece.has_separate_col_fs and piece.col_indices:
        w.begin("col-ifs")
        idx_str = " ".join(str(i) for i in piece.col_indices)
        w.node(idx_str)
        w.end()

    w.end()  # mesh
    w.end()  # geometry

    # appearance → material
    if settings["export_materials"]:
        w.begin("appearance")
        w.begin("material", _qstr(mat.name))

        w.node("texture-index", str(mat.texture_index))

        w.begin("diffuse")
        w.node(_vec4(mat.diffuse))
        w.end()

        w.begin("ambient")
        w.node(_vec3(mat.ambient))
        w.end()

        w.begin("emissive")
        w.node(_vec3(mat.emissive))
        w.end()

        w.begin("specular")
        w.node(_vec3(mat.specular))
        w.end()

        w.node("shininess", _flt(mat.shininess))

        w.end()  # material
        w.end()  # appearance

    # render-priority
    w.node("render-priority", str(settings.get("default_render_priority", 0)))

    w.end()  # shape


def _write_animset(w: LTAWriter, anim: AnimData, bones: list[BoneInfo]):
    """Write one ``(animset ...)`` node with keyframes."""
    w.begin("animset", _qstr(anim.name))

    # keyframe (times / values)
    w.begin("keyframe")
    w.begin("keyframe", _qstr(anim.name))

    # times
    w.begin("times")
    w.node(" ".join(_flt(t) for t in anim.times))
    w.end()

    # values
    w.begin("values")
    w.node(" ".join(_qstr(f"frame{i}") for i in range(len(anim.times))))
    w.end()

    w.end()  # inner keyframe
    w.end()  # outer keyframe

    # anims – one anim per bone
    w.begin("anims")
    w.begin("")
    for bone in bones:
        kfs = anim.bone_keyframes.get(bone.name, [])
        if not kfs:
            continue
        w.begin("anim", _qstr(f"{anim.name}_{bone.name}"))

        w.node("target", _qstr(bone.name))

        # keyframe reference
        w.begin("keyframe", _qstr(anim.name))
        w.begin("times")
        w.node(" ".join(_flt(t) for t in anim.times))
        w.end()
        w.end()  # keyframe

        # frames → posquat
        w.begin("frames")
        w.begin("posquat")
        w.begin("")
        for kf in kfs:
            w.begin("")
            w.begin("pos")
            w.node(_vec3(kf.pos))
            w.end()
            w.begin("quat")
            w.node(_vec4(kf.quat))
            w.end()
            w.end()
        w.end()
        w.end()  # posquat
        w.end()  # frames

        w.end()  # anim

    w.end()  # anims list
    w.end()  # anims

    w.end()  # animset


def _write_on_load_cmds(w: LTAWriter, pieces: list[PieceData],
                        bones: list[BoneInfo], sockets: list[SocketData],
                        anims: list[AnimData], settings: dict):
    """Write ``(on-load-cmds ...)`` with deformers, sockets, weight sets, etc."""
    w.begin("on-load-cmds")
    w.begin("")

    # -- add-deformer (skel-deformer per piece) --------------------------
    if settings["export_skin_weights"] and bones:
        bone_names = [b.name for b in bones]
        for piece in pieces:
            if not piece.weights:
                continue
            w.begin("add-deformer")
            w.begin("")
            w.begin("skel-deformer", _qstr(f"deformer_{piece.name}"))

            w.node("target", _qstr(piece.name))

            # influences
            w.begin("influences")
            w.node(" ".join(_qstr(bn) for bn in bone_names))
            w.end()

            # weightsets
            w.begin("weightsets")
            w.begin("")
            for vert_weights in piece.weights:
                pairs = []
                for bidx, wt in vert_weights:
                    pairs.append(f"{bidx} {_flt(wt)}")
                w.node(" ".join(pairs))
            w.end()
            w.end()  # weightsets

            w.end()  # skel-deformer
            w.end()  # skel-deformer list
            w.end()  # add-deformer

    # -- add-sockets -----------------------------------------------------
    if sockets:
        w.begin("add-sockets")
        w.begin("")
        for s in sockets:
            w.begin("socket", _qstr(s.name))
            w.node("parent", _qstr(s.parent_bone))
            w.begin("pos")
            w.node(_vec3(s.position))
            w.end()
            w.begin("quat")
            w.node(_vec4(s.quaternion))
            w.end()
            w.end()  # socket
        w.end()
        w.end()  # add-sockets

    # -- set-global-radius -----------------------------------------------
    w.node("set-global-radius", _flt(settings["global_radius"]))

    # -- add-anim-weightsets (NOLF2 default: Null, Upper, Lower, blink, twitch)
    if settings["export_weight_sets"] and bones:
        bone_count = len(bones)
        w.begin("add-anim-weightsets")
        w.begin("")

        default_sets = {
            "Null":   [0.0] * bone_count,
            "Upper":  [0.0] * bone_count,
            "Lower":  [0.0] * bone_count,
            "blink":  [0.0] * bone_count,
            "twitch": [2.0] * bone_count,
        }
        for ws_name, ws_weights in default_sets.items():
            w.begin("anim-weightset")
            w.node("name", _qstr(ws_name))
            w.begin("weights")
            w.node(" ".join(_flt(v) for v in ws_weights))
            w.end()
            w.end()  # anim-weightset

        w.end()
        w.end()  # add-anim-weightsets

    # -- anim-bindings ---------------------------------------------------
    if anims:
        w.begin("anim-bindings")
        w.begin("")
        for anim in anims:
            w.begin("anim-binding")
            w.node("name", _qstr(anim.name))

            if settings["export_user_dims"]:
                w.begin("dims")
                w.node(f"{_flt(settings['user_dims_x'])} "
                       f"{_flt(settings['user_dims_y'])} "
                       f"{_flt(settings['user_dims_z'])}")
                w.end()

            w.begin("translation")
            w.node("0.000000 0.000000 0.000000")
            w.end()

            w.node("interp-time", str(settings["interp_time"]))
            w.node("weight-set", _qstr("Null"))

            w.end()  # anim-binding
        w.end()
        w.end()  # anim-bindings

    # -- set-node-flags --------------------------------------------------
    if settings["export_node_flags"] and settings["node_flags_ignore_trans"]:
        flags_list = [s.strip() for s in settings["node_flags_ignore_trans"].split(",") if s.strip()]
        if flags_list:
            w.begin("set-node-flags")
            w.begin("")
            for bname in flags_list:
                w.node(_qstr(bname), "2")
            w.end()
            w.end()

    # -- set-repl-lod-original -------------------------------------------
    if settings["export_lod"]:
        dists = [s.strip() for s in settings["lod_distances"].split(",") if s.strip()]
        pcts = [s.strip() for s in settings["lod_percentages"].split(",") if s.strip()]
        if dists and pcts:
            w.begin("set-repl-lod-original")
            w.begin("dists")
            w.node(" ".join(dists))
            w.end()
            w.begin("tri-%")
            w.node(" ".join(pcts))
            w.end()
            w.end()

    # -- add-childmodels -------------------------------------------------
    if settings["export_child_models"] and settings["child_model_paths"]:
        paths = [s.strip() for s in settings["child_model_paths"].split(";") if s.strip()]
        if paths:
            w.begin("add-childmodels")
            w.begin("")
            for p in paths:
                w.begin("child-model")
                w.node("filename", _qstr(p))
                # node-relations (identity – no offset)
                w.begin("node-relations")
                w.begin("")
                w.end()
                w.end()
                w.end()  # child-model
            w.end()
            w.end()

    # -- add-node-obb-list (optional, from custom props) -----------------
    if settings.get("export_obb") and bones:
        obb_written = False
        for bone in bones:
            # Look for custom properties on the armature's bone
            pass  # placeholder – users can extend this
        # If we add OBB support we'd iterate here

    # -- command string --------------------------------------------------
    cmd = settings.get("command_string", "")
    if cmd:
        w.comment(f"Command String: {cmd}")
        # The command string in LTA is stored in on-load-cmds as a raw comment
        # or via the tools-info. For ModelEdit, it's set separately.
        # We emit it as a comment so ModelEdit can pick it up.

    w.end()  # on-load-cmds list
    w.end()  # on-load-cmds


def _write_tools_info(w: LTAWriter, materials: list[MaterialData],
                      settings: dict):
    """Write ``(tools-info ...)`` with texture-bindings."""
    w.begin("tools-info")
    w.begin("")

    w.begin("texture-bindings")
    w.begin("")
    for idx, mat in enumerate(materials):
        tex_file = mat.texture_file if mat.texture_file else f"texture_{mat.name}.dtx"
        w.node(str(idx), _qstr(tex_file))
    w.end()
    w.end()  # texture-bindings

    w.end()
    w.end()  # tools-info


# ======================================================================
#  Main export entry point
# ======================================================================

def export(context, **settings) -> set:
    filepath = settings["filepath"]

    conv_mat = _make_conversion_matrix(settings["coord_frame"])

    # -- Gather data -----------------------------------------------------
    arm_obj, bones = (None, [])
    if settings["export_skeleton"]:
        arm_obj, bones = _gather_armature(context, settings)

    pieces, materials = ([], [])
    if settings["export_meshes"]:
        pieces, materials = _gather_meshes(context, settings, arm_obj, bones, conv_mat)

    sockets = _gather_sockets(context, settings, arm_obj, bones, conv_mat)
    anims = _gather_animations(context, settings, arm_obj, bones, conv_mat)

    # If no skeleton was found but we have meshes, create a single dummy bone
    if not bones and pieces:
        bi = BoneInfo()
        bi.name = "base_node"
        bi.index = 0
        bones = [bi]
        for p in pieces:
            p.parent_bone = "base_node"

    # -- Write LTA -------------------------------------------------------
    try:
        with open(filepath, "w", encoding="utf-8") as fp:
            w = LTAWriter(fp)

            model_name = os.path.splitext(os.path.basename(filepath))[0]

            w.begin("lt-model-0", _qstr(model_name))

            # coord-frame-type
            hand, up, scope = _coord_frame_tokens(settings["coord_frame"])
            w.node("coord-frame-type", hand, up, scope)

            # hierarchy
            _write_hierarchy(w, bones, conv_mat, settings["export_scale"])

            # shapes
            if settings["export_meshes"]:
                for piece in pieces:
                    mat_idx = piece.material_index
                    if mat_idx < len(materials):
                        mat = materials[mat_idx]
                    else:
                        mat = materials[0]
                    _write_shape(w, piece, mat, settings)

            # animsets
            if settings["export_animations"] and anims:
                for anim in anims:
                    _write_animset(w, anim, bones)

            # on-load-cmds
            _write_on_load_cmds(w, pieces, bones, sockets, anims, settings)

            # tools-info
            if settings["export_materials"]:
                _write_tools_info(w, materials, settings)

            w.end()  # lt-model-0

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {'CANCELLED'}

    return {'FINISHED'}
