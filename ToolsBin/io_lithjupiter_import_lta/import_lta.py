# SPDX-License-Identifier: GPL-3.0-or-later
# import_lta.py – LTA reader + scene builder for LithTech Jupiter models.
#
# Companion to the LTA exporter.  Targets Blender 4.5+.

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
    """A leaf value – either a quoted string or a raw atom."""
    __slots__ = ("value", "is_string")

    def __init__(self, value: str, is_string: bool = False):
        self.value = value
        self.is_string = is_string

    def __repr__(self):
        return f'"{self.value}"' if self.is_string else self.value


class LtaNode:
    """A parenthetical list of children (atoms and/or sub-nodes)."""
    __slots__ = ("children",)

    def __init__(self):
        self.children: List[Union["LtaNode", Atom]] = []

    # ---- navigation helpers ----------------------------------------
    def name(self) -> Optional[str]:
        """The first non-string atom (the node 'type')."""
        if self.children and isinstance(self.children[0], Atom) and not self.children[0].is_string:
            return self.children[0].value
        return None

    def string_id(self) -> Optional[str]:
        """Second child if it's a quoted string – e.g. ``(shape "Body" …)``."""
        if len(self.children) >= 2 and isinstance(self.children[1], Atom) and self.children[1].is_string:
            return self.children[1].value
        return None

    def find_child(self, name: str) -> Optional["LtaNode"]:
        for c in self.children:
            if isinstance(c, LtaNode) and c.name() == name:
                return c
        return None

    def find_all_children(self, name: str) -> List["LtaNode"]:
        return [c for c in self.children
                if isinstance(c, LtaNode) and c.name() == name]


# ======================================================================
#  Tokenizer / parser
# ======================================================================

def _tokenize(text: str) -> Iterator[Union[str, tuple]]:
    """Stream of ``'('``, ``')'``, ``('STR', value)``, ``('ATOM', value)``."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c == ';':                                # line comment
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
    """Parse LTA text into a tree; return the top-level node."""
    it = _tokenize(text)

    def parse_list() -> LtaNode:
        node = LtaNode()
        for tok in it:
            if tok == ')':
                return node
            if tok == '(':
                node.children.append(parse_list())
            elif isinstance(tok, tuple):
                kind, value = tok
                node.children.append(Atom(value, is_string=(kind == 'STR')))
        return node

    for tok in it:
        if tok == '(':
            return parse_list()
    return None


# ======================================================================
#  Convenience getters – the LTA format sometimes wraps lists in an
#  anonymous parens layer, sometimes not; these helpers normalise it.
# ======================================================================

def _skip_name(node: LtaNode) -> list:
    """Children of a node, dropping the leading name atom."""
    ch = node.children
    if ch and isinstance(ch[0], Atom) and not ch[0].is_string:
        return ch[1:]
    return list(ch)


def _atoms_inline(node: LtaNode) -> List[str]:
    """Atom values inside a node, descending one anonymous wrapper if present."""
    sub = _skip_name(node)
    if len(sub) == 1 and isinstance(sub[0], LtaNode):
        sub = sub[0].children
    return [c.value for c in sub if isinstance(c, Atom)]


def _all_atoms(node: LtaNode) -> List[str]:
    """All atom values inside a node, INCLUDING what _skip_name would treat
    as the leading name atom.  Useful for bare numeric tuples like the LTA
    posquat frame's ``(0.5 0.6 0.7)`` — there's no name, just three floats,
    but the parser treats the first one as the node's ``name``.
    """
    out: List[str] = []
    for c in node.children:
        if isinstance(c, Atom):
            out.append(c.value)
        elif isinstance(c, LtaNode) and c.name() is None:
            # one-level descent through anonymous wrapper
            for cc in c.children:
                if isinstance(cc, Atom):
                    out.append(cc.value)
    return out


def _vec_list(node: LtaNode) -> List[List[float]]:
    """Vectors inside a node (e.g. ``(vertex ((x y z) (x y z) …))``)."""
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
            if atoms:
                out.append([float(a.value) for a in atoms])
            elif len(c.children) == 1 and isinstance(c.children[0], LtaNode):
                inner_atoms = [v for v in c.children[0].children if isinstance(v, Atom)]
                if inner_atoms:
                    out.append([float(a.value) for a in inner_atoms])
    return out


def _single_vec(node: LtaNode) -> List[float]:
    return [float(a) for a in _atoms_inline(node)]


def _flat_ints(node: LtaNode) -> List[int]:
    return [int(a) for a in _atoms_inline(node)]


def _flat_floats(node: LtaNode) -> List[float]:
    return [float(a) for a in _atoms_inline(node)]


def _flat_strings(node: LtaNode) -> List[str]:
    return _atoms_inline(node)


def _descend_anon(node: LtaNode) -> List[LtaNode]:
    """Return list of child LtaNodes, descending one anonymous wrapper."""
    sub = _skip_name(node)
    if len(sub) == 1 and isinstance(sub[0], LtaNode):
        inner = sub[0]
        if inner.name() is None:                    # truly anonymous
            sub = inner.children
    return [c for c in sub if isinstance(c, LtaNode)]


# ======================================================================
#  Coordinate-frame conversion
#
#  The exporter writes everything through a chosen conversion matrix
#  (Blender RH-Z-up → LTA frame).  On import we apply its inverse to get
#  back to Blender space.
# ======================================================================

def _conversion_matrix(coord_frame: str) -> Matrix:
    """Same matrix the exporter uses for Blender → LTA."""
    if coord_frame == 'LH_YUP':
        # Swap Y ↔ Z
        return Matrix(((1, 0, 0, 0),
                       (0, 0, 1, 0),
                       (0, 1, 0, 0),
                       (0, 0, 0, 1)))
    if coord_frame == 'LH_ZUP':
        return Matrix.Identity(4)
    if coord_frame == 'RH_YUP':
        return Matrix(((1, 0, 0, 0),
                       (0, 0, 1, 0),
                       (0, 1, 0, 0),
                       (0, 0, 0, 1)))
    if coord_frame == 'RH_ZUP':
        return Matrix.Identity(4)
    return Matrix.Identity(4)


def _coord_frame_from_atoms(atoms: List[str]) -> str:
    """``(coord-frame-type left-hand y-up global)`` → ``'LH_YUP'``."""
    hand = "LH" if atoms and atoms[0] == "left-hand" else "RH"
    up = "YUP" if len(atoms) >= 2 and atoms[1] == "y-up" else "ZUP"
    return f"{hand}_{up}"


def _matrix_mode_from_atoms(atoms: List[str]) -> str:
    """Return 'GLOBAL' or 'LOCAL' based on flag3 of coord-frame-type.

    Per the LTA schema the default flag set is ``left-hand y-up global``,
    so an absent or unrecognised flag3 means absolute (model-space)
    matrices.  ``local`` means each transform's matrix is relative to its
    parent (the convention this addon's exporter uses).
    """
    if len(atoms) >= 3 and atoms[2] == 'local':
        return 'LOCAL'
    return 'GLOBAL'


def _convert_matrix(M: Matrix, conv_mat: Matrix) -> Matrix:
    """Apply a basis change to a coordinate-frame matrix: ``conv @ M @ conv⁻¹``.

    The translation goes through ``conv``; the rotation 3×3 is conjugated so
    the basis vectors land in the new frame.  ``conv`` for our axis-swap is
    its own inverse, so this is symmetric.
    """
    return conv_mat @ M @ conv_mat.inverted_safe()


# ======================================================================
#  Data containers (mirror those in the exporter, simplified)
# ======================================================================

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
        self.verts: List[Tuple[float, float, float]] = []
        self.uvs: List[Tuple[float, float]] = []
        self.normals: List[Tuple[float, float, float]] = []
        self.colors: List[Tuple[float, float, float, float]] = []
        self.tris: List[int] = []
        self.tex_idx: List[int] = []
        self.nrm_idx: List[int] = []
        self.col_idx: List[int] = []
        self.material_index: int = -1   # index into the model-level materials list
        self.texture_index: int = 0
        self.render_priority: int = 0


class MaterialSpec:
    __slots__ = ("name", "diffuse", "ambient", "emissive", "specular",
                 "shininess", "texture_index", "texture_file")

    def __init__(self):
        self.name: str = "default"
        self.diffuse = (0.8, 0.8, 0.8, 1.0)
        self.ambient = (0.2, 0.2, 0.2)
        self.emissive = (0.0, 0.0, 0.0)
        self.specular = (0.0, 0.0, 0.0)
        self.shininess: float = 0.0
        self.texture_index: int = 0
        self.texture_file: str = ""


class SocketSpec:
    __slots__ = ("name", "parent_bone", "pos", "quat")

    def __init__(self):
        self.name = ""
        self.parent_bone = ""
        self.pos = (0.0, 0.0, 0.0)
        self.quat = (0.0, 0.0, 0.0, 1.0)


class DeformerSpec:
    __slots__ = ("target", "influences", "weightsets")

    def __init__(self):
        self.target: str = ""
        self.influences: List[str] = []
        # one list per vertex of [(bone_idx, weight), …]
        self.weightsets: List[List[Tuple[int, float]]] = []


class AnimSpec:
    __slots__ = ("name", "times", "bone_anims")

    def __init__(self):
        self.name = ""
        self.times: List[float] = []                # ms
        # bone_name → list of (pos_xyz, quat_xyzw) per time
        self.bone_anims: Dict[str, List[Tuple[Tuple[float, float, float],
                                              Tuple[float, float, float, float]]]] = {}


class AnimBindingSpec:
    __slots__ = ("name", "dims", "translation", "interp_time", "weight_set")

    def __init__(self):
        self.name = ""
        self.dims = (16.0, 16.0, 128.0)
        self.translation = (0.0, 0.0, 0.0)
        self.interp_time = 200
        self.weight_set = ""


class AnimWeightSetSpec:
    __slots__ = ("name", "weights")

    def __init__(self):
        self.name = ""
        self.weights: List[float] = []


# ======================================================================
#  Extracting data from a parsed lt-model-0 tree
# ======================================================================

def _read_hierarchy(hier_node: LtaNode) -> List[BoneSpec]:
    """Return top-level BoneSpec list (each may have a tree of children)."""
    if hier_node is None:
        return []

    children_node = hier_node.find_child('children')
    if children_node is None:
        return []

    def parse_transforms(parent_children_node: LtaNode) -> List[BoneSpec]:
        out: List[BoneSpec] = []
        for sub in _descend_anon(parent_children_node):
            if sub.name() != 'transform':
                continue
            bone = BoneSpec()
            bone.name = sub.string_id() or "unnamed"

            mat_node = sub.find_child('matrix')
            if mat_node is not None:
                rows = _vec_list(mat_node)
                if len(rows) == 4 and all(len(r) == 4 for r in rows):
                    bone.local_matrix = Matrix(rows)

            child_children = sub.find_child('children')
            if child_children is not None:
                bone.children = parse_transforms(child_children)
            out.append(bone)
        return out

    return parse_transforms(children_node)


def _read_materials_from_shapes(shape_nodes: List[LtaNode],
                                shapes: List[ShapeSpec]) -> List[MaterialSpec]:
    """Each shape can have one material in (appearance (material …)).  We
    collect unique materials by name so duplicates aren't created."""
    materials: List[MaterialSpec] = []
    name_to_idx: Dict[str, int] = {}

    for shape_node, shape in zip(shape_nodes, shapes):
        app = shape_node.find_child('appearance')
        if app is None:
            shape.material_index = -1
            continue
        mat_node = app.find_child('material')
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
        if tex_idx_node is not None:
            vals = _atoms_inline(tex_idx_node)
            if vals:
                try:
                    m.texture_index = int(vals[0])
                except ValueError:
                    pass

        for key in ('diffuse', 'ambient', 'emissive', 'specular'):
            n = mat_node.find_child(key)
            if n is None:
                continue
            v = _single_vec(n)
            if key == 'diffuse':
                if len(v) == 3:
                    v = v + [1.0]
                if len(v) >= 4:
                    m.diffuse = tuple(v[:4])
            elif key == 'ambient' and len(v) >= 3:
                m.ambient = tuple(v[:3])
            elif key == 'emissive' and len(v) >= 3:
                m.emissive = tuple(v[:3])
            elif key == 'specular' and len(v) >= 3:
                m.specular = tuple(v[:3])

        shin_node = mat_node.find_child('shininess')
        if shin_node is not None:
            vals = _atoms_inline(shin_node)
            if vals:
                try:
                    m.shininess = float(vals[0])
                except ValueError:
                    pass

        idx = len(materials)
        materials.append(m)
        name_to_idx[mname] = idx
        shape.material_index = idx

    return materials


def _read_shape(shape_node: LtaNode) -> ShapeSpec:
    s = ShapeSpec()
    s.name = shape_node.string_id() or "shape"

    parent_node = shape_node.find_child('parent')
    if parent_node is not None:
        parents = _atoms_inline(parent_node)
        if parents:
            s.parent_bone = parents[0]

    rp_node = shape_node.find_child('render-priority')
    if rp_node is not None:
        vals = _atoms_inline(rp_node)
        if vals:
            try:
                s.render_priority = int(vals[0])
            except ValueError:
                pass

    geom = shape_node.find_child('geometry')
    if geom is None:
        return s
    mesh = geom.find_child('mesh')
    if mesh is None:
        return s

    v_node = mesh.find_child('vertex')
    if v_node is not None:
        s.verts = [tuple(v) for v in _vec_list(v_node)]

    uv_node = mesh.find_child('uvs')
    if uv_node is not None:
        s.uvs = [tuple(v[:2]) for v in _vec_list(uv_node)]

    n_node = mesh.find_child('normals')
    if n_node is not None:
        s.normals = [tuple(v[:3]) for v in _vec_list(n_node)]

    c_node = mesh.find_child('colors')
    if c_node is not None:
        cols = []
        for v in _vec_list(c_node):
            if len(v) >= 4:
                cols.append(tuple(v[:4]))
            elif len(v) == 3:
                cols.append((v[0], v[1], v[2], 1.0))
        s.colors = cols

    tri_node = mesh.find_child('tri-fs')
    if tri_node is not None:
        s.tris = _flat_ints(tri_node)

    tex_node = mesh.find_child('tex-fs')
    if tex_node is not None:
        s.tex_idx = _flat_ints(tex_node)

    nrm_node = mesh.find_child('nrm-fs')
    if nrm_node is not None:
        s.nrm_idx = _flat_ints(nrm_node)

    col_node = mesh.find_child('col-ifs') or mesh.find_child('col-fs')
    if col_node is not None:
        s.col_idx = _flat_ints(col_node)

    return s


def _read_socket(socket_node: LtaNode) -> SocketSpec:
    sk = SocketSpec()
    sk.name = socket_node.string_id() or "socket"

    p_node = socket_node.find_child('parent')
    if p_node is not None:
        ps = _atoms_inline(p_node)
        if ps:
            sk.parent_bone = ps[0]

    pos_node = socket_node.find_child('pos')
    if pos_node is not None:
        v = _single_vec(pos_node)
        if len(v) >= 3:
            sk.pos = tuple(v[:3])

    quat_node = socket_node.find_child('quat')
    if quat_node is not None:
        v = _single_vec(quat_node)
        if len(v) >= 4:
            sk.quat = tuple(v[:4])

    return sk


def _read_deformer(deformer_node: LtaNode) -> DeformerSpec:
    d = DeformerSpec()

    t = deformer_node.find_child('target')
    if t is not None:
        ats = _atoms_inline(t)
        if ats:
            d.target = ats[0]

    infl = deformer_node.find_child('influences')
    if infl is not None:
        d.influences = _atoms_inline(infl)

    ws = deformer_node.find_child('weightsets')
    if ws is not None:
        for wnode in _descend_anon(ws):
            atoms = [a.value for a in wnode.children if isinstance(a, Atom)]
            pairs: List[Tuple[int, float]] = []
            for i in range(0, len(atoms) - 1, 2):
                try:
                    pairs.append((int(atoms[i]), float(atoms[i + 1])))
                except ValueError:
                    continue
            d.weightsets.append(pairs)

    return d


def _read_animset(animset_node: LtaNode) -> AnimSpec:
    """Parse one ``(animset "name" ...)`` block into an AnimSpec.

    Tolerates BOTH formats seen in the wild:

    Real-world LTA (ModelEdit output)::

      (animset "fire"
        (keyframe (keyframe (times (...)) (values (...))))
        (anims (
          (anim (parent "BoneName")
                (frames (posquat
                  ( ((px py pz) (qx qy qz qw))
                    ((px py pz) (qx qy qz qw)) ... )))) ...)))

    Exporter output (used by this addon's exporter)::

      (animset "fire"
        (keyframe (keyframe "fire" (times (...)) (values (...))))
        (anims (
          (anim "fire_BoneName"
                (target "BoneName")
                (keyframe "fire" (times (...)))
                (frames (posquat ( ( (pos (x y z)) (quat (w x y z)) ) ... ))))
          ...)))

    Quaternion order in BOTH formats stored in the file is ``(x y z w)``
    in the flat case, ``(w x y z)`` inside ``(quat ...)`` wrappers.  We
    normalise to ``(x, y, z, w)`` in :class:`AnimSpec.bone_anims`.
    """
    a = AnimSpec()
    a.name = animset_node.string_id() or "anim"

    # ---- times --------------------------------------------------------
    kf_outer = animset_node.find_child('keyframe')
    if kf_outer is not None:
        # times can live either at (keyframe (times ...)) or
        # (keyframe (keyframe (times ...))) depending on writer.
        kf_inner = kf_outer.find_child('keyframe') or kf_outer
        times_node = kf_inner.find_child('times')
        if times_node is not None:
            a.times = _flat_floats(times_node)

    # ---- per-bone anim tracks ----------------------------------------
    anims = animset_node.find_child('anims')
    if anims is None:
        return a

    for anim_node in _descend_anon(anims):
        if anim_node.name() != 'anim':
            continue

        # bone name: real LTA uses (parent "X"), exporter uses (target "X")
        bone_name = None
        for key in ('parent', 'target'):
            n = anim_node.find_child(key)
            if n is not None:
                v = _atoms_inline(n)
                if v:
                    bone_name = v[0]
                    break
        if not bone_name:
            continue

        frames_node = anim_node.find_child('frames')
        if frames_node is None:
            continue
        posquat_node = frames_node.find_child('posquat')
        if posquat_node is None:
            continue

        frames: List[Tuple[Tuple[float, float, float],
                           Tuple[float, float, float, float]]] = []
        for entry in _descend_anon(posquat_node):
            # ----- exporter-style: entry has (pos ...) and (quat ...) children
            pos_sub = entry.find_child('pos')
            quat_sub = entry.find_child('quat')
            if pos_sub is not None and quat_sub is not None:
                pv = _single_vec(pos_sub)
                qv = _single_vec(quat_sub)        # stored as (w x y z)
                pos = (pv[0], pv[1], pv[2]) if len(pv) >= 3 else (0.0, 0.0, 0.0)
                if len(qv) >= 4:
                    # convert (w x y z) → (x y z w) for our spec
                    quat = (qv[1], qv[2], qv[3], qv[0])
                else:
                    quat = (0.0, 0.0, 0.0, 1.0)
                frames.append((pos, quat))
                continue

            # ----- real-LTA style: entry directly contains two child lists
            # of bare floats: pos triple then quat quad (x y z w).  The parser
            # treats the FIRST atom of each bare list as the list's "name",
            # so we use _all_atoms which restores the dropped leading value.
            children = [c for c in entry.children if isinstance(c, LtaNode)]
            if len(children) >= 2:
                pa = _all_atoms(children[0])
                qa = _all_atoms(children[1])
                try:
                    pv = [float(x) for x in pa]
                    qv = [float(x) for x in qa]
                except ValueError:
                    continue
                if len(pv) >= 3 and len(qv) >= 4:
                    frames.append(((pv[0], pv[1], pv[2]),
                                   (qv[0], qv[1], qv[2], qv[3])))

        if frames:
            a.bone_anims[bone_name] = frames

    return a


def _read_texture_bindings(tools_info: Optional[LtaNode]) -> Dict[int, str]:
    """Return {texture_index: filename, …} from ``tools-info``."""
    if tools_info is None:
        return {}
    out: Dict[int, str] = {}
    for c in _descend_anon(tools_info):
        if c.name() != 'texture-bindings':
            continue
        for b in _descend_anon(c):
            atoms = [a for a in b.children if isinstance(a, Atom)]
            if len(atoms) >= 2:
                try:
                    out[int(atoms[0].value)] = atoms[1].value
                except ValueError:
                    pass
    return out


def _read_on_load_cmds(olc: Optional[LtaNode]) -> dict:
    """Extract structured commands from on-load-cmds."""
    result = {
        'deformers':     [],   # DeformerSpec list
        'sockets':       [],   # SocketSpec list
        'anim_weight_sets': [],
        'anim_bindings': [],
        'global_radius': None,
        'child_models':  [],
        'node_flags':    {},
        'lod':           None,
    }
    if olc is None:
        return result

    for cmd in _descend_anon(olc):
        n = cmd.name()
        if n == 'add-deformer':
            for d in _descend_anon(cmd):
                if d.name() == 'skel-deformer':
                    result['deformers'].append(_read_deformer(d))
        elif n == 'add-sockets':
            for s in _descend_anon(cmd):
                if s.name() == 'socket':
                    result['sockets'].append(_read_socket(s))
        elif n == 'set-global-radius':
            vals = _atoms_inline(cmd)
            if vals:
                try:
                    result['global_radius'] = float(vals[0])
                except ValueError:
                    pass
        elif n == 'add-anim-weightsets':
            for w in _descend_anon(cmd):
                if w.name() == 'anim-weightset':
                    ws = AnimWeightSetSpec()
                    name_node = w.find_child('name')
                    if name_node is not None:
                        nm = _atoms_inline(name_node)
                        if nm:
                            ws.name = nm[0]
                    weights_node = w.find_child('weights')
                    if weights_node is not None:
                        ws.weights = _flat_floats(weights_node)
                    result['anim_weight_sets'].append(ws)
        elif n == 'anim-bindings':
            for b in _descend_anon(cmd):
                if b.name() == 'anim-binding':
                    ab = AnimBindingSpec()
                    name_node = b.find_child('name')
                    if name_node is not None:
                        nm = _atoms_inline(name_node)
                        if nm:
                            ab.name = nm[0]
                    dims_node = b.find_child('dims')
                    if dims_node is not None:
                        v = _single_vec(dims_node)
                        if len(v) >= 3:
                            ab.dims = tuple(v[:3])
                    trans_node = b.find_child('translation')
                    if trans_node is not None:
                        v = _single_vec(trans_node)
                        if len(v) >= 3:
                            ab.translation = tuple(v[:3])
                    it_node = b.find_child('interp-time')
                    if it_node is not None:
                        vals = _atoms_inline(it_node)
                        if vals:
                            try:
                                ab.interp_time = int(vals[0])
                            except ValueError:
                                pass
                    ws_node = b.find_child('weight-set')
                    if ws_node is not None:
                        ats = _atoms_inline(ws_node)
                        if ats:
                            ab.weight_set = ats[0]
                    result['anim_bindings'].append(ab)
        elif n == 'add-childmodels':
            for cm in _descend_anon(cmd):
                if cm.name() == 'child-model':
                    fn = cm.find_child('filename')
                    if fn is not None:
                        ats = _atoms_inline(fn)
                        if ats:
                            result['child_models'].append(ats[0])
        elif n == 'set-node-flags':
            for entry in _descend_anon(cmd):
                vals = entry.children
                if len(vals) >= 2 and isinstance(vals[0], Atom) and isinstance(vals[1], Atom):
                    try:
                        result['node_flags'][vals[0].value] = int(vals[1].value)
                    except ValueError:
                        pass
        elif n == 'set-repl-lod-original':
            lod = {}
            dists = cmd.find_child('dists')
            if dists is not None:
                lod['dists'] = _flat_floats(dists)
            pcts = cmd.find_child('tri-%')
            if pcts is not None:
                lod['percentages'] = _flat_floats(pcts)
            result['lod'] = lod

    return result


# ======================================================================
#  Blender scene-building
# ======================================================================

def _create_armature(model_name: str,
                     root_bones: List[BoneSpec],
                     conv_mat: Matrix,
                     scale: float,
                     matrix_mode: str,                # 'GLOBAL' or 'LOCAL'
                     bone_default_length: float = 0.1
                     ) -> Tuple[bpy.types.Object, Dict[str, Matrix]]:
    """Build an Armature from the parsed BoneSpec tree.

    ``matrix_mode``:
      * ``'GLOBAL'`` – each bone's stored matrix is its world (model-root)
        matrix, the LTA-schema default ``coord-frame-type flag3 = global``.
        We apply ``conv @ M @ conv⁻¹`` to each one to land in Blender space.
      * ``'LOCAL'`` – each matrix is relative to its parent (the convention
        this addon's exporter writes).  We coord-convert each one then
        chain ``parent_world @ child_local`` to derive world matrices.

    ``conv_mat`` is the coord-frame conversion (e.g. swap Y↔Z for LH_YUP),
    and ``scale`` is applied to bone positions only (rotations are unchanged).

    Returns ``(armature_object, {bone_name: world_matrix_blender})``.
    """
    arm_data = bpy.data.armatures.new(model_name)
    arm_obj = bpy.data.objects.new(model_name, arm_data)
    bpy.context.collection.objects.link(arm_obj)

    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode='EDIT')

    # ---- pass 1: compute every bone's world matrix in Blender space -------
    world_mats: Dict[str, Matrix] = {}
    parent_of: Dict[str, Optional[str]] = {}
    order: List[BoneSpec] = []          # depth-first order, parents before children

    inv_conv = conv_mat.inverted_safe()

    def _compute(bone: BoneSpec, parent: Optional[BoneSpec]):
        order.append(bone)
        parent_of[bone.name] = parent.name if parent else None
        M_lta = bone.local_matrix
        if matrix_mode == 'GLOBAL':
            # absolute model-space matrix; convert basis and we're done
            wm = conv_mat @ M_lta @ inv_conv
        else:                                       # LOCAL
            local_bl = conv_mat @ M_lta @ inv_conv
            wm = (world_mats[parent.name] @ local_bl) if parent else local_bl
        # apply scale to translation only
        if scale != 1.0:
            t = wm.to_translation() * scale
            r3 = wm.to_3x3()
            wm = Matrix.Translation(t) @ r3.to_4x4()
        world_mats[bone.name] = wm
        for c in bone.children:
            _compute(c, bone)

    for rb in root_bones:
        _compute(rb, None)

    # ---- pass 2: create edit_bones with sensible head/tail ----------------
    # For visual clarity we point each bone's head at its world position and,
    # if it has a child, aim its tail at that child's head.  Leaf bones
    # default to ``bone_default_length`` along the bone-local +Y axis.
    edit_bones: Dict[str, bpy.types.EditBone] = {}

    # Group children by parent name for the "aim at first child" pass.
    children_of: Dict[str, List[str]] = {}
    for b in order:
        p = parent_of.get(b.name)
        if p:
            children_of.setdefault(p, []).append(b.name)

    for b in order:
        eb = arm_data.edit_bones.new(b.name)
        wm = world_mats[b.name]
        head = wm.to_translation()
        # tail: aim at first child's head if available, else along world Y
        kids = children_of.get(b.name, [])
        tail = None
        if kids:
            child_head = world_mats[kids[0]].to_translation()
            if (child_head - head).length > 1e-5:
                tail = child_head
        if tail is None:
            # use the bone's own world +Y direction for a default-length stub
            y_axis = Vector((wm[0][1], wm[1][1], wm[2][1]))
            if y_axis.length < 1e-6:
                y_axis = Vector((0.0, 1.0, 0.0))
            else:
                y_axis.normalize()
            tail = head + y_axis * bone_default_length
        eb.head = head
        eb.tail = tail
        edit_bones[b.name] = eb

    # ---- pass 3: parent edit_bones (after head/tail are stable) -----------
    for name, eb in edit_bones.items():
        p_name = parent_of.get(name)
        if p_name and p_name in edit_bones:
            eb.parent = edit_bones[p_name]
            # Only connect if the bone is exactly at the parent's tail.
            try:
                if (eb.head - eb.parent.tail).length < 1e-4:
                    eb.use_connect = True
            except Exception:
                pass

    bpy.ops.object.mode_set(mode='OBJECT')
    return arm_obj, world_mats


def _build_loop_arrays(shape: ShapeSpec) -> Tuple[List[Tuple[float, float, float]],
                                                  List[Tuple[int, int, int]],
                                                  List[List[int]]]:
    """Convert the LTA per-loop arrays into per-vertex + face arrays.

    Returns (verts, faces, loop_index_map) where ``loop_index_map[face_i] =
    [loop_i_a, loop_i_b, loop_i_c]`` indexes into the original tri-fs stream.
    """
    # The exporter emits each loop as its own vertex (per-vertex data is
    # already unwrapped).  We keep that mapping intact so UV / normal /
    # colour arrays can be indexed directly via the loop number.
    verts = shape.verts
    faces: List[Tuple[int, int, int]] = []
    loop_index_map: List[List[int]] = []
    tris = shape.tris
    for i in range(0, len(tris), 3):
        if i + 2 >= len(tris):
            break
        a, b, c = tris[i], tris[i + 1], tris[i + 2]
        faces.append((a, b, c))
        loop_index_map.append([i, i + 1, i + 2])
    return verts, faces, loop_index_map


def _create_mesh_object(shape: ShapeSpec,
                        materials: List[MaterialSpec],
                        material_bl: List[Optional[bpy.types.Material]],
                        inv_full_mat: Matrix,
                        import_uvs: bool,
                        import_normals: bool,
                        import_vcolors: bool) -> bpy.types.Object:
    mesh = bpy.data.meshes.new(shape.name)

    verts, faces, _loop_map = _build_loop_arrays(shape)

    # Convert LTA-space vertices back to Blender space.
    blender_verts = []
    for v in verts:
        co = inv_full_mat @ Vector((v[0], v[1], v[2]))
        blender_verts.append((co.x, co.y, co.z))

    mesh.from_pydata(blender_verts, [], faces)
    mesh.update()

    # Assign material slot
    if shape.material_index >= 0 and shape.material_index < len(material_bl):
        bl_mat = material_bl[shape.material_index]
        if bl_mat is not None:
            mesh.materials.append(bl_mat)
            for poly in mesh.polygons:
                poly.material_index = 0

    # UVs ------------------------------------------------------------
    if import_uvs and shape.uvs:
        uv_layer = mesh.uv_layers.new(name="UVMap")
        # tri-fs[k] indexes a vertex; tex-fs[k] (if present) indexes a uv.
        # If there's no separate tex-fs the uv index equals the vertex index.
        n_loops = len(mesh.loops)
        tex_indices = shape.tex_idx if shape.tex_idx else shape.tris
        for li in range(n_loops):
            if li >= len(tex_indices):
                continue
            ui = tex_indices[li]
            if 0 <= ui < len(shape.uvs):
                u, v = shape.uvs[ui]
                # Exporter flips V (writes 1-V); reverse here.
                uv_layer.data[li].uv = (u, 1.0 - v)

    # Custom split normals ------------------------------------------
    if import_normals and shape.normals:
        inv3 = inv_full_mat.to_3x3()
        n_loops = len(mesh.loops)
        nrm_indices = shape.nrm_idx if shape.nrm_idx else shape.tris
        loop_normals: List[Tuple[float, float, float]] = [(0.0, 0.0, 1.0)] * n_loops
        for li in range(n_loops):
            if li >= len(nrm_indices):
                continue
            ni = nrm_indices[li]
            if 0 <= ni < len(shape.normals):
                nx, ny, nz = shape.normals[ni]
                n = (inv3 @ Vector((nx, ny, nz)))
                if n.length > 1e-8:
                    n.normalize()
                    loop_normals[li] = (n.x, n.y, n.z)
        # Tag faces as smooth, then set split normals.
        for poly in mesh.polygons:
            poly.use_smooth = True
        try:
            mesh.normals_split_custom_set(loop_normals)
        except Exception as e:
            print(f"[LTA importer] normals_split_custom_set failed for "
                  f"{shape.name}: {e}")

    # Vertex colours -------------------------------------------------
    if import_vcolors and shape.colors:
        attr = mesh.color_attributes.new(name="Color",
                                         type='BYTE_COLOR',
                                         domain='CORNER')
        n_loops = len(mesh.loops)
        col_indices = shape.col_idx if shape.col_idx else shape.tris
        for li in range(n_loops):
            if li >= len(col_indices):
                continue
            ci = col_indices[li]
            if 0 <= ci < len(shape.colors):
                attr.data[li].color = shape.colors[ci]

    mesh.update()

    obj = bpy.data.objects.new(shape.name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj


def _make_blender_material(mspec: MaterialSpec,
                           texture_bindings: Dict[int, str],
                           texture_search_dirs: List[str]) -> bpy.types.Material:
    mat = bpy.data.materials.new(name=mspec.name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    # Find / re-create principled BSDF
    bsdf = nodes.get("Principled BSDF")
    if bsdf is None:
        bsdf = nodes.new("ShaderNodeBsdfPrincipled")

    output_node = nodes.get("Material Output")
    if output_node is None:
        output_node = nodes.new("ShaderNodeOutputMaterial")
    if not bsdf.outputs[0].is_linked:
        links.new(bsdf.outputs[0], output_node.inputs['Surface'])

    # Diffuse → Base Color
    bsdf.inputs['Base Color'].default_value = mspec.diffuse

    # Shininess heuristic – LTA range 0-128, where 0 means off.
    if mspec.shininess > 0:
        # higher shininess = lower roughness
        roughness = max(0.0, 1.0 - (mspec.shininess / 128.0))
        bsdf.inputs['Roughness'].default_value = roughness

    # Specular – best-effort.  Blender 4.5's principled BSDF uses
    # "Specular IOR Level" (was "Specular" in older versions).
    spec_input = bsdf.inputs.get('Specular IOR Level') or bsdf.inputs.get('Specular')
    if spec_input is not None:
        spec_strength = (mspec.specular[0] + mspec.specular[1] +
                         mspec.specular[2]) / 3.0
        spec_input.default_value = max(0.0, min(spec_strength, 1.0))

    # Emission (Blender 4.x renamed to "Emission Color")
    em_input = (bsdf.inputs.get('Emission Color')
                or bsdf.inputs.get('Emission'))
    if em_input is not None:
        em_input.default_value = (mspec.emissive[0], mspec.emissive[1],
                                  mspec.emissive[2], 1.0)

    # Texture – look up via texture-bindings
    tex_file = texture_bindings.get(mspec.texture_index)
    if tex_file:
        # Try several path resolutions
        img = _resolve_texture(tex_file, texture_search_dirs)
        if img is not None:
            tex_node = nodes.new("ShaderNodeTexImage")
            tex_node.image = img
            tex_node.location = (bsdf.location.x - 320, bsdf.location.y)
            links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])

    return mat


def _resolve_texture(rel_path: str,
                     search_dirs: List[str]) -> Optional[bpy.types.Image]:
    """Try to load a texture from a list of search directories.

    LTA texture paths are usually relative DTX paths.  We try variations
    with different separators and with common image extensions.
    """
    rel = rel_path.replace('\\', '/').strip()
    if not rel:
        return None

    base, ext = os.path.splitext(rel)
    candidates = [rel]
    # DTX is the LithTech runtime format – not loadable by Blender.  Try
    # the common source-asset extensions as substitutes.
    for alt in ('.tga', '.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff'):
        if ext.lower() != alt:
            candidates.append(base + alt)

    for d in search_dirs:
        if not d:
            continue
        for c in candidates:
            full = os.path.normpath(os.path.join(d, c))
            if os.path.isfile(full):
                try:
                    return bpy.data.images.load(full, check_existing=True)
                except Exception as e:
                    print(f"[LTA importer] failed to load image {full}: {e}")
    return None


def _apply_skinning(mesh_obj: bpy.types.Object,
                    arm_obj: bpy.types.Object,
                    shape: ShapeSpec,
                    deformer: Optional[DeformerSpec]):
    """Create vertex groups + an Armature modifier for the mesh.

    If ``deformer`` is None, falls back to rigid parenting (single group
    with weight 1.0 on ``shape.parent_bone``)."""
    # Make sure the armature is the parent
    mesh_obj.parent = arm_obj

    mod = mesh_obj.modifiers.get("Armature")
    if mod is None:
        mod = mesh_obj.modifiers.new(name="Armature", type='ARMATURE')
    mod.object = arm_obj
    mod.use_vertex_groups = True

    if deformer is not None and deformer.weightsets:
        # Pre-create one VG per influence (preserve order)
        vg_map: Dict[str, bpy.types.VertexGroup] = {}
        for bn in deformer.influences:
            vg = mesh_obj.vertex_groups.get(bn) or mesh_obj.vertex_groups.new(name=bn)
            vg_map[bn] = vg

        # weightsets are indexed by vertex order in the LTA file
        for vi, pairs in enumerate(deformer.weightsets):
            if vi >= len(mesh_obj.data.vertices):
                break
            for bone_idx, w in pairs:
                if 0 <= bone_idx < len(deformer.influences):
                    bn = deformer.influences[bone_idx]
                    vg = vg_map.get(bn)
                    if vg is not None and w != 0.0:
                        vg.add([vi], w, 'REPLACE')
    elif shape.parent_bone:
        # Rigid: weight all verts to parent bone
        vg = mesh_obj.vertex_groups.get(shape.parent_bone) \
            or mesh_obj.vertex_groups.new(name=shape.parent_bone)
        verts = list(range(len(mesh_obj.data.vertices)))
        if verts:
            vg.add(verts, 1.0, 'REPLACE')


def _create_socket_empties(sockets: List[SocketSpec],
                           arm_obj: bpy.types.Object,
                           armature_space: Dict[str, Matrix],
                           inv_full_mat: Matrix):
    for s in sockets:
        empty = bpy.data.objects.new(f"Socket_{s.name}", None)
        empty.empty_display_type = 'ARROWS'
        empty.empty_display_size = 0.1
        bpy.context.collection.objects.link(empty)

        # Parent to bone
        if s.parent_bone and s.parent_bone in armature_space:
            empty.parent = arm_obj
            empty.parent_type = 'BONE'
            empty.parent_bone = s.parent_bone

        # Reverse coord-frame conversion for the position
        local_pos = inv_full_mat @ Vector(s.pos)
        # Quaternion is stored as (x, y, z, w); Blender Quaternion expects (w, x, y, z)
        q = Quaternion((s.quat[3], s.quat[0], s.quat[1], s.quat[2]))

        # Sockets are bone-relative.  We position the empty in armature space
        # equal to bone_rest_world * (pos / quat).  Blender bone parenting
        # uses the bone's tail as the origin by default, which is
        # inconvenient; we instead set the world matrix directly and let
        # Blender compute the parent-inverse internally.
        if s.parent_bone and s.parent_bone in armature_space:
            bone_world = armature_space[s.parent_bone]
            local_mat = Matrix.Translation(local_pos) @ q.to_matrix().to_4x4()
            # Blender bone parenting places the child at bone's TAIL by
            # default; cancel that offset using the bone's length.
            tail_offset = Matrix.Translation(Vector((0.0, _bone_length(arm_obj, s.parent_bone), 0.0)))
            empty.matrix_world = bone_world @ tail_offset.inverted() @ local_mat
        else:
            empty.location = local_pos
            empty.rotation_mode = 'QUATERNION'
            empty.rotation_quaternion = q


def _bone_length(arm_obj: bpy.types.Object, bone_name: str) -> float:
    bone = arm_obj.data.bones.get(bone_name)
    if bone is None:
        return 0.0
    return bone.length


# ======================================================================
#  Animation creation
# ======================================================================

def _import_animset(anim: AnimSpec,
                    arm_obj: bpy.types.Object,
                    armature_space_rest: Dict[str, Matrix],
                    conv_mat: Matrix,
                    fps: float,
                    frame_offset: int,
                    push_to_nla: bool,
                    set_as_current: bool):
    """Create a Blender Action for one LTA ``animset`` block.

    Animation samples in real LTA files are stored as the bone's
    parent-RELATIVE pose: ``(pos, quat)`` is the pose-bone's transform
    relative to its parent's pose at that frame.  We chain those through
    the hierarchy to get an absolute (model-space) world pose per bone,
    then derive each pose bone's ``matrix_basis`` against the Blender
    rest pose:

      * root bone: ``matrix_basis = rest_local⁻¹ @ target_world``
      * child bone: ``matrix_basis = rest_offset⁻¹ @ parent_target_world⁻¹
                                     @ target_world``
        where ``rest_offset = parent.bone.matrix_local⁻¹ @ bone.matrix_local``.

    ``armature_space_rest`` maps bone-name → Blender world rest matrix and is
    used to substitute the rest pose for bones missing from this animset.

    When ``push_to_nla`` is True the finished Action is pushed onto a new
    NLA track with strip name == ``anim.name`` (the animset's identifier
    in the .lta).  When False the Action is stashed only.
    """
    if not anim.times or not anim.bone_anims:
        return None

    inv_conv = conv_mat.inverted_safe()
    arm_data = arm_obj.data

    # Per-bone Blender rest data
    rest_world: Dict[str, Matrix] = {}
    rest_local: Dict[str, Matrix] = {}        # parent-relative rest offset
    parent_name: Dict[str, Optional[str]] = {}
    for bone in arm_data.bones:
        rest_world[bone.name] = bone.matrix_local.copy()
        if bone.parent is None:
            rest_local[bone.name] = bone.matrix_local.copy()
            parent_name[bone.name] = None
        else:
            rest_local[bone.name] = (bone.parent.matrix_local.inverted_safe()
                                     @ bone.matrix_local)
            parent_name[bone.name] = bone.parent.name

    # Pre-compute Blender world target matrices for every (bone, frame).
    # Missing entries fall back to rest world.
    n_frames = len(anim.times)
    bones_with_data = set(anim.bone_anims.keys())

    def _target_world_lta(bone_name: str, fi: int) -> Optional[Matrix]:
        """World matrix for ``bone_name`` at frame index ``fi``, in LTA frame.

        Per-bone animation tracks in the LTA format are ALWAYS stored as
        parent-relative pose transforms (this matches what the exporter
        writes and what real ModelEdit files contain).  We chain through
        the parent's target world to recover an absolute world matrix.

        Bones not animated by this animset fall back to their rest world
        (converted back to LTA frame for chaining).
        """
        kfs = anim.bone_anims.get(bone_name)
        if kfs is not None and fi < len(kfs):
            (px, py, pz), (qx, qy, qz, qw) = kfs[fi]
            T = Matrix.Translation(Vector((px, py, pz)))
            # Blender's Quaternion takes (w, x, y, z)
            R = Quaternion((qw, qx, qy, qz)).to_matrix().to_4x4()
            local_lta = T @ R
            pname = parent_name.get(bone_name)
            if pname is None:
                return local_lta
            parent_lta = _target_world_lta(pname, fi)
            if parent_lta is None:
                return local_lta
            return parent_lta @ local_lta
        # bone not animated this frame – use its rest world in LTA space
        rw = rest_world.get(bone_name)
        if rw is None:
            return None
        return inv_conv @ rw @ conv_mat   # Blender→LTA inverse of S M S⁻¹

    # Build Action + push to NLA --------------------------------------
    action = bpy.data.actions.new(name=anim.name)
    action.use_fake_user = True

    if arm_obj.animation_data is None:
        arm_obj.animation_data_create()
    saved_action = arm_obj.animation_data.action
    arm_obj.animation_data.action = action

    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode='POSE')

    try:
        for fi in range(n_frames):
            frame = frame_offset + int(round(anim.times[fi] * fps / 1000.0))

            # Resolve all world targets in LTA space at this frame, then
            # convert each to Blender space.  We cache so per-bone lookups
            # used by parent chaining hit the cache too.
            world_lta_cache: Dict[str, Optional[Matrix]] = {}

            def _get_lta(name: str) -> Optional[Matrix]:
                if name in world_lta_cache:
                    return world_lta_cache[name]
                v = _target_world_lta(name, fi)
                world_lta_cache[name] = v
                return v

            for bone_name in bones_with_data:
                pbone = arm_obj.pose.bones.get(bone_name)
                if pbone is None:
                    continue

                tgt_lta = _get_lta(bone_name)
                if tgt_lta is None:
                    continue
                tgt_bl = conv_mat @ tgt_lta @ inv_conv

                pname = parent_name.get(bone_name)
                if pname is None:
                    matrix_basis = rest_world[bone_name].inverted_safe() @ tgt_bl
                else:
                    par_lta = _get_lta(pname)
                    if par_lta is None:
                        # parent missing – treat as identity world
                        par_bl = Matrix.Identity(4)
                    else:
                        par_bl = conv_mat @ par_lta @ inv_conv
                    rest_off = rest_local.get(bone_name, Matrix.Identity(4))
                    matrix_basis = (rest_off.inverted_safe()
                                    @ par_bl.inverted_safe()
                                    @ tgt_bl)

                loc, rot_q, _scl = matrix_basis.decompose()
                pbone.location = loc
                pbone.rotation_mode = 'QUATERNION'
                pbone.rotation_quaternion = rot_q

                pbone.keyframe_insert('location', frame=frame, group=bone_name)
                pbone.keyframe_insert('rotation_quaternion',
                                      frame=frame, group=bone_name)
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')

    # Linear interpolation – LTA stores explicit samples per keyframe.
    for fc in action.fcurves:
        for kp in fc.keyframe_points:
            kp.interpolation = 'LINEAR'

    # NLA stash --------------------------------------------------------
    if push_to_nla:
        # Detach from active so each animset is independent
        arm_obj.animation_data.action = None
        track = arm_obj.animation_data.nla_tracks.new()
        track.name = anim.name
        try:
            strip = track.strips.new(name=anim.name, start=frame_offset,
                                     action=action)
            strip.name = anim.name
        except Exception as e:
            print(f"[LTA importer] could not push '{anim.name}' to NLA: {e}")

    # Restore previous active action unless caller asked us to keep this one
    if set_as_current:
        arm_obj.animation_data.action = action
    elif not push_to_nla:
        arm_obj.animation_data.action = saved_action

    return action


# ======================================================================
#  Top-level entry point
# ======================================================================

def import_lta(context, **settings) -> set:
    filepath = settings["filepath"]

    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
    except Exception as e:
        print(f"[LTA importer] failed to open file: {e}")
        return {'CANCELLED'}

    root = _parse(text)
    if root is None or root.name() != 'lt-model-0':
        print("[LTA importer] file is not a valid lt-model-0 LTA")
        return {'CANCELLED'}

    model_name = root.string_id() or os.path.splitext(os.path.basename(filepath))[0]

    # ------------- coord-frame ----------------------------------------
    coord_frame = settings.get("coord_frame", "AUTO")
    matrix_mode = settings.get("matrix_mode", "AUTO")
    if coord_frame == "AUTO" or matrix_mode == "AUTO":
        cft_node = root.find_child('coord-frame-type')
        cft_atoms = _atoms_inline(cft_node) if cft_node is not None else []
        if coord_frame == "AUTO":
            coord_frame = (_coord_frame_from_atoms(cft_atoms)
                           if cft_atoms else 'LH_YUP')
        if matrix_mode == "AUTO":
            # Schema default is "global" (absolute), which is what real
            # LTA files (e.g. ModelEdit output) ship with.
            matrix_mode = (_matrix_mode_from_atoms(cft_atoms)
                           if cft_atoms else 'GLOBAL')

    conv_mat = _conversion_matrix(coord_frame)
    scale = settings.get("import_scale", 1.0)
    scale_mat = Matrix.Scale(scale, 4)
    full_mat = conv_mat @ scale_mat
    inv_full_mat = full_mat.inverted_safe()

    # ------------- hierarchy ------------------------------------------
    hierarchy = root.find_child('hierarchy')
    root_bones = _read_hierarchy(hierarchy) if hierarchy is not None else []

    arm_obj = None
    armature_space: Dict[str, Matrix] = {}
    if settings.get("import_armature", True) and root_bones:
        arm_obj, armature_space = _create_armature(
            model_name, root_bones,
            conv_mat=conv_mat,
            scale=scale,
            matrix_mode=matrix_mode,
            bone_default_length=settings.get("bone_length", 0.1))

    # ------------- shapes & materials ---------------------------------
    shape_nodes = root.find_all_children('shape')
    shapes: List[ShapeSpec] = []
    for sn in shape_nodes:
        shapes.append(_read_shape(sn))

    materials_spec = _read_materials_from_shapes(shape_nodes, shapes)

    # tools-info → texture-bindings
    texture_bindings = _read_texture_bindings(root.find_child('tools-info'))

    # Resolve textures: search relative to the LTA file
    search_dirs: List[str] = []
    lta_dir = os.path.dirname(os.path.abspath(filepath))
    search_dirs.append(lta_dir)
    extra = settings.get("texture_search_path", "")
    if extra:
        for p in extra.split(';'):
            p = p.strip()
            if p:
                search_dirs.append(p)
    # Common LithTech texture root: walk up looking for a "skins" or "textures" folder
    parent = lta_dir
    for _ in range(4):
        parent = os.path.dirname(parent)
        if parent and os.path.isdir(parent):
            search_dirs.append(parent)

    material_bl: List[Optional[bpy.types.Material]] = []
    if settings.get("import_materials", True):
        for mspec in materials_spec:
            material_bl.append(_make_blender_material(mspec, texture_bindings, search_dirs))
    else:
        material_bl = [None] * len(materials_spec)

    # Create mesh objects
    mesh_objects: List[bpy.types.Object] = []
    if settings.get("import_meshes", True):
        for shape in shapes:
            obj = _create_mesh_object(shape, materials_spec, material_bl,
                                      inv_full_mat,
                                      import_uvs=settings.get("import_uvs", True),
                                      import_normals=settings.get("import_normals", True),
                                      import_vcolors=settings.get("import_vertex_colors", True))
            mesh_objects.append(obj)

    # ------------- on-load-cmds ---------------------------------------
    olc = root.find_child('on-load-cmds')
    cmds = _read_on_load_cmds(olc)

    # Skinning per shape
    if arm_obj is not None and mesh_objects:
        deformer_by_target: Dict[str, DeformerSpec] = {d.target: d for d in cmds['deformers']}
        for shape, obj in zip(shapes, mesh_objects):
            d = deformer_by_target.get(shape.name)
            _apply_skinning(obj, arm_obj, shape, d)

    # Sockets
    if arm_obj is not None and settings.get("import_sockets", True):
        _create_socket_empties(cmds['sockets'], arm_obj, armature_space, inv_full_mat)

    # ------------- animations -----------------------------------------
    if arm_obj is not None and settings.get("import_animations", True):
        animsets = root.find_all_children('animset')
        anims_to_import = [_read_animset(a) for a in animsets]

        fps = settings.get("anim_framerate", 30.0)
        frame_offset = settings.get("anim_frame_offset", 1)
        push_to_nla = settings.get("import_to_nla", True)
        first = True
        for anim in anims_to_import:
            if not anim.times:
                continue
            _import_animset(anim, arm_obj, armature_space,
                            conv_mat=conv_mat,
                            fps=fps, frame_offset=frame_offset,
                            push_to_nla=push_to_nla,
                            set_as_current=(first and not push_to_nla))
            first = False

    # ------------- store metadata as custom props on the armature -----
    if arm_obj is not None:
        if cmds['global_radius'] is not None:
            arm_obj["lta_global_radius"] = cmds['global_radius']
        if cmds['anim_bindings']:
            arm_obj["lta_anim_bindings"] = [
                {"name": b.name,
                 "dims": list(b.dims),
                 "translation": list(b.translation),
                 "interp_time": b.interp_time,
                 "weight_set": b.weight_set}
                for b in cmds['anim_bindings']
            ]
        if cmds['child_models']:
            arm_obj["lta_child_models"] = list(cmds['child_models'])
        if cmds['node_flags']:
            arm_obj["lta_node_flags"] = dict(cmds['node_flags'])
        if cmds['lod']:
            arm_obj["lta_lod"] = dict(cmds['lod'])
        if cmds['anim_weight_sets']:
            arm_obj["lta_anim_weight_sets"] = [
                {"name": w.name, "weights": list(w.weights)}
                for w in cmds['anim_weight_sets']
            ]

    # Make the armature the active object for the user
    if arm_obj is not None:
        bpy.context.view_layer.objects.active = arm_obj
        for obj in bpy.context.selected_objects:
            obj.select_set(False)
        arm_obj.select_set(True)

    return {'FINISHED'}
