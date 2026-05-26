# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import os
import math
from typing import Iterator, List, Tuple, Union, Optional, Dict

import bpy
from mathutils import Matrix, Vector, Quaternion

# ======================================================================
#  LTA token + parse tree
# ======================================================================

class Atom:
    __slots__ = ("value", "is_string")
    def __init__(self, value: str, is_string: bool = False):
        self.value = value
        self.is_string = is_string
    def __repr__(self):
        return f'"{self.value}"' if self.is_string else self.value

class LtaNode:
    __slots__ = ("children",)
    def __init__(self):
        self.children: List[Union["LtaNode", Atom]] = []

    def name(self) -> Optional[str]:
        if self.children and isinstance(self.children[0], Atom) and not self.children[0].is_string:
            return self.children[0].value
        return None

    def string_id(self) -> Optional[str]:
        if len(self.children) >= 2 and isinstance(self.children[1], Atom) and self.children[1].is_string:
            return self.children[1].value
        return None

    def find_child(self, name: str) -> Optional["LtaNode"]:
        for c in self.children:
            if isinstance(c, LtaNode) and c.name() == name:
                return c
        return None

    def find_all_children(self, name: str) -> List["LtaNode"]:
        return [c for c in self.children if isinstance(c, LtaNode) and c.name() == name]

def _tokenize(text: str) -> Iterator[Union[str, tuple]]:
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c == ';':
            while i < n and text[i] != '\n':
                i += 1
            continue
        if c in '()':
            yield c
            i += 1
            continue
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 1
            yield ('STR', text[i + 1:j])
            i = j + 1
            continue
        j = i
        while j < n and not text[j].isspace() and text[j] not in '()"':
            j += 1
        yield ('ATOM', text[i:j])
        i = j

def _parse(text: str) -> Optional[LtaNode]:
    it = _tokenize(text)
    def parse_list() -> LtaNode:
        node = LtaNode()
        for tok in it:
            if tok == ')': return node
            if tok == '(': node.children.append(parse_list())
            elif isinstance(tok, tuple):
                node.children.append(Atom(tok[1], is_string=(tok[0] == 'STR')))
        return node
    for tok in it:
        if tok == '(': return parse_list()
    return None

def _skip_name(node: LtaNode) -> list:
    ch = node.children
    if ch and isinstance(ch[0], Atom) and not ch[0].is_string:
        return ch[1:]
    return list(ch)

def _atoms_inline(node: LtaNode) -> List[str]:
    sub = _skip_name(node)
    if len(sub) == 1 and isinstance(sub[0], LtaNode):
        sub = sub[0].children
    return [c.value for c in sub if isinstance(c, Atom)]

def _vec_list(node: LtaNode) -> List[List[float]]:
    sub = _skip_name(node)
    if len(sub) == 1 and isinstance(sub[0], LtaNode):
        inner = sub[0]
        if inner.children and all(isinstance(c, Atom) for c in inner.children):
            return [[float(c.value) for c in inner.children]]
        sub = inner.children
    out: List[List[float]] = []
    for c in sub:
        if isinstance(c, LtaNode):
            atoms = [v for v in c.children if isinstance(v, Atom)]
            if atoms: out.append([float(a.value) for a in atoms])
            elif len(c.children) == 1 and isinstance(c.children[0], LtaNode):
                inner_atoms = [v for v in c.children[0].children if isinstance(v, Atom)]
                if inner_atoms: out.append([float(a.value) for a in inner_atoms])
    return out

def _single_vec(node: LtaNode) -> List[float]:
    return [float(a) for a in _atoms_inline(node)]

def _flat_ints(node: LtaNode) -> List[int]:
    return [int(a) for a in _atoms_inline(node)]

def _flat_floats(node: LtaNode) -> List[float]:
    return [float(a) for a in _atoms_inline(node)]

def _descend_anon(node: LtaNode) -> List[LtaNode]:
    sub = _skip_name(node)
    if len(sub) == 1 and isinstance(sub[0], LtaNode):
        inner = sub[0]
        if inner.name() is None:
            sub = inner.children
    return [c for c in sub if isinstance(c, LtaNode)]

def _conversion_matrix(coord_frame: str) -> Matrix:
    if coord_frame in ('LH_YUP', 'RH_YUP'):
        return Matrix(((1, 0, 0, 0), (0, 0, 1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))
    return Matrix.Identity(4)

def _coord_frame_from_atoms(atoms: List[str]) -> str:
    hand = "LH" if atoms and atoms[0] == "left-hand" else "RH"
    up = "YUP" if len(atoms) >= 2 and atoms[1] == "y-up" else "ZUP"
    return f"{hand}_{up}"

class BoneSpec:
    __slots__ = ("name", "local_matrix", "children")
    def __init__(self):
        self.name: str = ""
        self.local_matrix: Matrix = Matrix.Identity(4)
        self.children: List["BoneSpec"] = []

class ShapeSpec:
    __slots__ = ("name", "parent_bone", "verts", "uvs", "normals", "colors",
                 "tris", "tex_idx", "nrm_idx", "col_idx", "material_index",
                 "texture_index", "render_priority")
    def __init__(self):
        self.name: str = ""
        self.parent_bone: str = ""
        self.verts, self.uvs, self.normals, self.colors = [], [], [], []
        self.tris, self.tex_idx, self.nrm_idx, self.col_idx = [], [], [], []
        self.material_index, self.texture_index, self.render_priority = -1, 0, 0

class MaterialSpec:
    __slots__ = ("name", "diffuse", "ambient", "emissive", "specular",
                 "shininess", "texture_index", "texture_file")
    def __init__(self):
        self.name: str = "default"
        self.diffuse, self.ambient, self.emissive, self.specular = (0.8,)*3+(1.0,), (0.2,)*3, (0.0,)*3, (0.0,)*3
        self.shininess, self.texture_index, self.texture_file = 0.0, 0, ""

class SocketSpec:
    __slots__ = ("name", "parent_bone", "pos", "quat")
    def __init__(self):
        self.name, self.parent_bone = "", ""
        self.pos, self.quat = (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0)

class DeformerSpec:
    __slots__ = ("target", "influences", "weightsets")
    def __init__(self):
        self.target: str = ""
        self.influences: List[str] = []
        self.weightsets: List[List[Tuple[int, float]]] = []

class AnimSpec:
    __slots__ = ("name", "times", "bone_anims")
    def __init__(self):
        self.name: str = ""
        self.times: List[float] = []
        self.bone_anims: Dict[str, List[Tuple[Tuple[float, float, float], Tuple[float, float, float, float]]]] = {}

class AnimBindingSpec:
    __slots__ = ("name", "dims", "translation", "interp_time", "weight_set")
    def __init__(self):
        self.name, self.weight_set = "", ""
        self.dims, self.translation = (16.0, 16.0, 128.0), (0.0, 0.0, 0.0)
        self.interp_time = 200

class AnimWeightSetSpec:
    __slots__ = ("name", "weights")
    def __init__(self):
        self.name: str = ""
        self.weights: List[float] = []

def _read_hierarchy(hier_node: LtaNode) -> List[BoneSpec]:
    if hier_node is None: return []
    children_node = hier_node.find_child('children')
    if children_node is None: return []

    def parse_transforms(parent_children_node: LtaNode) -> List[BoneSpec]:
        out: List[BoneSpec] = []
        for sub in _descend_anon(parent_children_node):
            if sub.name() != 'transform': continue
            bone = BoneSpec()
            bone.name = sub.string_id() or "unnamed"
            mat_node = sub.find_child('matrix')
            if mat_node is not None:
                rows = _vec_list(mat_node)
                if len(rows) == 4 and all(len(r) == 4 for r in rows):
                    bone.local_matrix = Matrix(rows).transposed()
            child_children = sub.find_child('children')
            if child_children is not None:
                bone.children = parse_transforms(child_children)
            out.append(bone)
        return out
    return parse_transforms(children_node)

def _read_materials_from_shapes(shape_nodes: List[LtaNode], shapes: List[ShapeSpec]) -> List[MaterialSpec]:
    materials, name_to_idx = [], {}
    for shape_node, shape in zip(shape_nodes, shapes):
        app = shape_node.find_child('appearance')
        mat_node = app.find_child('material') if app else None
        if mat_node is None:
            shape.material_index = -1
            continue

        mname = mat_node.string_id() or f"material_{len(materials)}"
        if mname in name_to_idx:
            shape.material_index = name_to_idx[mname]
            continue

        m = MaterialSpec()
        m.name = mname
        tex_idx_node = mat_node.find_child('texture-index')
        if tex_idx_node and (vals := _atoms_inline(tex_idx_node)):
            try: m.texture_index = int(vals[0])
            except ValueError: pass

        for key in ('diffuse', 'ambient', 'emissive', 'specular'):
            if n := mat_node.find_child(key):
                v = _single_vec(n)
                if key == 'diffuse' and len(v) >= 3: m.diffuse = tuple(v[:3] + [1.0])[:4]
                elif len(v) >= 3: setattr(m, key, tuple(v[:3]))

        shin_node = mat_node.find_child('shininess')
        if shin_node and (vals := _atoms_inline(shin_node)):
            try: m.shininess = float(vals[0])
            except ValueError: pass

        shape.material_index = len(materials)
        name_to_idx[mname] = len(materials)
        materials.append(m)
    return materials

def _read_shape(shape_node: LtaNode) -> ShapeSpec:
    s = ShapeSpec()
    s.name = shape_node.string_id() or "shape"

    if p_node := shape_node.find_child('parent'):
        if ps := _atoms_inline(p_node): s.parent_bone = ps[0]

    geom = shape_node.find_child('geometry')
    mesh = geom.find_child('mesh') if geom else None
    if not mesh: return s

    if v_node := mesh.find_child('vertex'): s.verts = [tuple(v) for v in _vec_list(v_node)]
    if uv_node := mesh.find_child('uvs'): s.uvs = [tuple(v[:2]) for v in _vec_list(uv_node)]
    if n_node := mesh.find_child('normals'): s.normals = [tuple(v[:3]) for v in _vec_list(n_node)]
    if c_node := mesh.find_child('colors'): s.colors = [tuple(v[:4]) if len(v) >= 4 else (v[0], v[1], v[2], 1.0) for v in _vec_list(c_node)]
    if tri_node := mesh.find_child('tri-fs'): s.tris = _flat_ints(tri_node)
    if tex_node := mesh.find_child('tex-fs'): s.tex_idx = _flat_ints(tex_node)
    if nrm_node := mesh.find_child('nrm-fs'): s.nrm_idx = _flat_ints(nrm_node)
    if col_node := (mesh.find_child('col-ifs') or mesh.find_child('col-fs')): s.col_idx = _flat_ints(col_node)
    
    return s

def _read_socket(socket_node: LtaNode) -> SocketSpec:
    sk = SocketSpec()
    sk.name = socket_node.string_id() or "socket"
    if p_node := socket_node.find_child('parent'):
        if ps := _atoms_inline(p_node): sk.parent_bone = ps[0]
    if (pos_node := socket_node.find_child('pos')) and (v := _single_vec(pos_node)): sk.pos = tuple(v[:3])
    if (quat_node := socket_node.find_child('quat')) and (v := _single_vec(quat_node)): sk.quat = tuple(v[:4])
    return sk

def _read_deformer(deformer_node: LtaNode) -> DeformerSpec:
    d = DeformerSpec()
    if (t := deformer_node.find_child('target')) and (ats := _atoms_inline(t)): d.target = ats[0]
    if infl := deformer_node.find_child('influences'): d.influences = _atoms_inline(infl)
    if ws := deformer_node.find_child('weightsets'):
        for wnode in _descend_anon(ws):
            atoms = [a.value for a in wnode.children if isinstance(a, Atom)]
            pairs = []
            for i in range(0, len(atoms) - 1, 2):
                try: pairs.append((int(atoms[i]), float(atoms[i + 1])))
                except ValueError: pass
            d.weightsets.append(pairs)
    return d

def _read_animset(animset_node: LtaNode) -> AnimSpec:
    a = AnimSpec()
    a.name = animset_node.string_id() or "anim"

    kf_outer = animset_node.find_child('keyframe')
    if kf_outer:
        inner = kf_outer.find_child('keyframe')
        if inner:
            times = inner.find_child('times')
            if times:
                a.times = _flat_floats(times)

    anims = animset_node.find_child('anims')
    if not anims:
        return a

    for wrapper in _descend_anon(anims):

        nodes = [wrapper]
        if wrapper.name() is None:
            nodes = [x for x in wrapper.children if isinstance(x,LtaNode)]

        for anim_node in nodes:

            if anim_node.name() != "anim":
                continue

            bone_name = None

            parent_node = anim_node.find_child("parent")
            if parent_node:
                vals = _atoms_inline(parent_node)
                if vals:
                    bone_name = vals[0]

            if bone_name is None:
                target_node = anim_node.find_child("target")
                if target_node:
                    vals = _atoms_inline(target_node)
                    if vals:
                        bone_name = vals[0]

            if not bone_name:
                continue

            frames_node = anim_node.find_child("frames")
            if not frames_node:
                continue

            posquat = frames_node.find_child("posquat")
            if not posquat:
                continue

            frames=[]

            frame_nodes=_descend_anon(posquat)

            for fn in frame_nodes:

                if len(fn.children)<2:
                    continue

                pnode=fn.children[0]
                qnode=fn.children[1]

                if not isinstance(pnode,LtaNode):
                    continue

                if not isinstance(qnode,LtaNode):
                    continue

                pos=[float(x.value) for x in pnode.children if isinstance(x,Atom)]
                quat=[float(x.value) for x in qnode.children if isinstance(x,Atom)]

                if len(pos)<3:
                    continue

                if len(quat)<4:
                    continue

                frames.append(
                    (
                        tuple(pos[:3]),
                        tuple(quat[:4])
                    )
                )

            a.bone_anims[bone_name]=frames

    return a

# ======================================================================
#  Blender scene-building
# ======================================================================

def _create_armature(
        model_name,
        root_bones,
        inv_full_mat,
        bone_default_length=0.1
):

    arm_data=bpy.data.armatures.new(model_name)
    arm_obj=bpy.data.objects.new(model_name,arm_data)

    bpy.context.collection.objects.link(arm_obj)

    bpy.context.view_layer.objects.active=arm_obj
    bpy.ops.object.mode_set(mode='EDIT')

    armature_space={}
    lta_local_rest={}

    def add_recursive(
            bone_spec,
            parent_lta_world,
            parent_edit
    ):

        local=bone_spec.local_matrix.copy()

        if parent_lta_world:
            lta_world=parent_lta_world @ local
        else:
            lta_world=local.copy()

        world_rest=inv_full_mat @ lta_world

        eb=arm_data.edit_bones.new(
            bone_spec.name
        )

        eb.matrix=world_rest

        if parent_edit:
            eb.parent=parent_edit
            eb.use_connect=False

        head=world_rest.translation

        eb.head=head

        if bone_spec.children:

            child_local=bone_spec.children[0].local_matrix

            child_world=lta_world @ child_local

            child_head=(inv_full_mat @ child_world).translation

            d=(child_head-head)

            if d.length<0.0001:
                d=Vector((0,0.1,0))
        else:
            d=world_rest.to_quaternion() @ Vector(
                (0,bone_default_length,0)
            )

        eb.tail=head+d

        armature_space[
            bone_spec.name
        ]=world_rest.copy()

        lta_local_rest[
            bone_spec.name
        ]=local.copy()

        for child in bone_spec.children:
            add_recursive(
                child,
                lta_world,
                eb
            )

    for root in root_bones:
        add_recursive(
            root,
            None,
            None
        )

    bpy.ops.object.mode_set(mode='OBJECT')

    return (
        arm_obj,
        armature_space,
        lta_local_rest
    )

def _create_mesh_object(shape: ShapeSpec, materials: List[MaterialSpec], material_bl: List[Optional[bpy.types.Material]], inv_full_mat: Matrix, import_uvs: bool, import_normals: bool, import_vcolors: bool) -> bpy.types.Object:
    mesh = bpy.data.meshes.new(shape.name)
    faces = [(shape.tris[i], shape.tris[i + 1], shape.tris[i + 2]) for i in range(0, len(shape.tris), 3) if i + 2 < len(shape.tris)]
    blender_verts = [(inv_full_mat @ Vector(v)).xyz for v in shape.verts]

    mesh.from_pydata(blender_verts, [], faces)
    
    if 0 <= shape.material_index < len(material_bl) and material_bl[shape.material_index]:
        mesh.materials.append(material_bl[shape.material_index])

    if import_uvs and shape.uvs:
        uv_layer = mesh.uv_layers.new(name="UVMap")
        tex_indices = shape.tex_idx or shape.tris
        for li in range(len(mesh.loops)):
            if li < len(tex_indices) and 0 <= tex_indices[li] < len(shape.uvs):
                uv_layer.data[li].uv = (shape.uvs[tex_indices[li]][0], 1.0 - shape.uvs[tex_indices[li]][1])

    if import_normals and shape.normals:
        inv3 = inv_full_mat.to_3x3()
        nrm_indices = shape.nrm_idx or shape.tris
        loop_normals = [(0.0, 0.0, 1.0)] * len(mesh.loops)
        for li in range(len(mesh.loops)):
            if li < len(nrm_indices) and 0 <= nrm_indices[li] < len(shape.normals):
                n = inv3 @ Vector(shape.normals[nrm_indices[li]])
                if n.length > 1e-8: loop_normals[li] = n.normalized().xyz
        for poly in mesh.polygons: poly.use_smooth = True
        try: mesh.normals_split_custom_set(loop_normals)
        except Exception: pass

    mesh.update()
    obj = bpy.data.objects.new(shape.name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj

def _make_blender_material(mspec: MaterialSpec, texture_bindings: Dict[int, str], texture_search_dirs: List[str]) -> bpy.types.Material:
    mat = bpy.data.materials.new(name=mspec.name)
    mat.use_nodes = True
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    bsdf = nodes.get("Principled BSDF") or nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.inputs['Base Color'].default_value = mspec.diffuse
    if mspec.shininess > 0: bsdf.inputs['Roughness'].default_value = max(0.0, 1.0 - (mspec.shininess / 128.0))

    tex_file = texture_bindings.get(mspec.texture_index)
    if tex_file:
        img = None
        rel = tex_file.replace('\\', '/').strip()
        if rel:
            base, ext = os.path.splitext(rel)
            for d in texture_search_dirs:
                if not d: continue
                for alt in ('', '.tga', '.png', '.jpg', '.jpeg', '.bmp'):
                    full = os.path.normpath(os.path.join(d, base + alt if alt else rel))
                    if os.path.isfile(full):
                        try:
                            img = bpy.data.images.load(full, check_existing=True)
                            break
                        except Exception: pass
                if img: break
        if img:
            tex_node = nodes.new("ShaderNodeTexImage")
            tex_node.image = img
            links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
    return mat

def _apply_skinning(mesh_obj: bpy.types.Object, arm_obj: bpy.types.Object, shape: ShapeSpec, deformer: Optional[DeformerSpec]):
    mesh_obj.parent = arm_obj
    mod = mesh_obj.modifiers.new(name="Armature", type='ARMATURE')
    mod.object, mod.use_vertex_groups = arm_obj, True

    if deformer and deformer.weightsets:
        vg_map = {bn: mesh_obj.vertex_groups.new(name=bn) for bn in deformer.influences}
        for vi, pairs in enumerate(deformer.weightsets):
            if vi >= len(mesh_obj.data.vertices): break
            for bone_idx, w in pairs:
                if 0 <= bone_idx < len(deformer.influences) and w != 0.0:
                    vg_map[deformer.influences[bone_idx]].add([vi], w, 'REPLACE')
    elif shape.parent_bone:
        vg = mesh_obj.vertex_groups.new(name=shape.parent_bone)
        if verts := list(range(len(mesh_obj.data.vertices))): vg.add(verts, 1.0, 'REPLACE')

def _import_animset(
        anim,
        arm_obj,
        lta_local_rest,
        fps,
        frame_offset,
        True,
    )

    if not anim.times:
        return None

    if not anim.bone_anims:
        return None

    frames=[
        frame_offset+
        int(round(
            t*fps/1000.0
        ))
        for t in anim.times
    ]

    action=bpy.data.actions.new(
        anim.name
    )

    action.use_fake_user=True

    if arm_obj.animation_data is None:
        arm_obj.animation_data_create()

    bpy.context.view_layer.objects.active=arm_obj

    bpy.ops.object.mode_set(
        mode='POSE'
    )

    try:

        for pb in arm_obj.pose.bones:
            pb.rotation_mode='QUATERNION'

        for bone_name,kfs in anim.bone_anims.items():

            pb=arm_obj.pose.bones.get(
                bone_name
            )

            if not pb:
                continue

            rest=lta_local_rest.get(
                bone_name
            )

            if not rest:
                continue

            for i,(pos,quat) in enumerate(kfs):

                if i>=len(frames):
                    break

                frame=frames[i]

                px,py,pz=pos
                qx,qy,qz,qw=quat

                anim_local=(
                    Matrix.Translation(
                        Vector(
                            (px,py,pz)
                        )
                    )
                    @
                    Quaternion(
                        (qw,qx,qy,qz)
                    ).to_matrix().to_4x4()
                )

                delta=(
                    rest.inverted_safe()
                    @
                    anim_local
                )

                loc,rot,sca=delta.decompose()

                pb.location=loc
                pb.rotation_quaternion=rot

                pb.keyframe_insert(
                    "location",
                    frame=frame,
                    group=bone_name
                )

                pb.keyframe_insert(
                    "rotation_quaternion",
                    frame=frame,
                    group=bone_name
                )

    finally:

        bpy.ops.object.mode_set(
            mode='OBJECT'
        )

    if arm_obj.animation_data is None:
        arm_obj.animation_data_create()

    track=arm_obj.animation_data.nla_tracks.new()

    track.name=anim.name

    strip=track.strips.new(
        anim.name,
        frames[0],
        action
    )

    strip.name=anim.name

    arm_obj.animation_data.action=None

    return action

# ======================================================================
#  Top-level entry point
# ======================================================================

def import_lta(context, **settings) -> set:
    try:
        with open(settings["filepath"], 'r', encoding='utf-8', errors='replace') as f:
            root = _parse(f.read())
    except Exception: return {'CANCELLED'}

    if not root or root.name() != 'lt-model-0': return {'CANCELLED'}
    
    model_name = root.string_id() or os.path.splitext(os.path.basename(settings["filepath"]))[0]
    coord_frame = settings.get("coord_frame", "AUTO")
    if coord_frame == "AUTO":
        cft_node = root.find_child('coord-frame-type')
        coord_frame = _coord_frame_from_atoms(_atoms_inline(cft_node)) if cft_node else 'LH_YUP'

    inv_full_mat = (_conversion_matrix(coord_frame) @ Matrix.Scale(settings.get("import_scale", 1.0), 4)).inverted_safe()

    hierarchy = root.find_child('hierarchy')
    root_bones = _read_hierarchy(hierarchy) if hierarchy else []

    arm_obj, armature_space, lta_local_rest = None, {}, {}
    if settings.get("import_armature", True) and root_bones:
        arm_obj, armature_space, lta_local_rest = _create_armature(model_name, root_bones, inv_full_mat, settings.get("bone_length", 0.1))

    shapes = [_read_shape(sn) for sn in root.find_all_children('shape')]
    materials_spec = _read_materials_from_shapes(root.find_all_children('shape'), shapes)
    texture_bindings = _read_texture_bindings(root.find_child('tools-info'))

    lta_dir = os.path.dirname(os.path.abspath(settings["filepath"]))
    search_dirs = [lta_dir] + [p.strip() for p in settings.get("texture_search_path", "").split(';') if p.strip()]
    parent = lta_dir
    for _ in range(4):
        parent = os.path.dirname(parent)
        if parent and os.path.isdir(parent): search_dirs.append(parent)

    material_bl = [_make_blender_material(m, texture_bindings, search_dirs) for m in materials_spec] if settings.get("import_materials", True) else [None] * len(materials_spec)

    mesh_objects = []
    if settings.get("import_meshes", True):
        for shape in shapes:
            mesh_objects.append(_create_mesh_object(shape, materials_spec, material_bl, inv_full_mat, settings.get("import_uvs", True), settings.get("import_normals", True), settings.get("import_vertex_colors", True)))

    cmds = _read_on_load_cmds(root.find_child('on-load-cmds'))
    if arm_obj and mesh_objects:
        deformer_by_target = {d.target: d for d in cmds['deformers']}
        for shape, obj in zip(shapes, mesh_objects): _apply_skinning(obj, arm_obj, shape, deformer_by_target.get(shape.name))

    if arm_obj and settings.get("import_animations", True):
        animsets_node = root
        first = True
        for a in [_read_animset(an) for an in animsets_node.find_all_children('animset')]:
            if a.times:
                _import_animset(anim, arm_obj, lta_local_rest, fps, settings.get("anim_frame_offset", 1), first)
                first = False

    if arm_obj:
        bpy.context.view_layer.objects.active = arm_obj
        for obj in bpy.context.selected_objects: obj.select_set(False)
        arm_obj.select_set(True)

    return {'FINISHED'}