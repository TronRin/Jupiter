# Jupiter / LithTech .LTA Model Importer/Exporter for Blender 4.5+
#
# Supports:
#   - Import/export of armatures, meshes, UVs, normals, vertex colors, skin weights, sockets.
#   - LOD groups (imported as collections with custom properties).
#   - Animation keyframes with frame strings (e.g. FIRE_KEY, SOUND_KEY, SHOW_PIECE_KEY).
#   - Round-trip preservation of anim-binding data (dims, translation, interp-time, weight-set).
#   - Compatible with ModelEdit and the LithTech Jupiter engine (NOLF2, Tron 2.0, etc.).

bl_info = {
    "name": "LithTech LTA (Model) Importer/Exporter",
    "author": "",
    "version": (6, 6, 6),
    "blender": (4, 5, 0),
    "location": "File > Import/Export > LithTech Model (.lta)",
    "description": "Import/export LithTech Jupiter .LTA model files",
    "category": "Import-Export",
}

import os
import re
import time
from collections import defaultdict

import bpy
from bpy.props import (
    StringProperty, BoolProperty, FloatProperty, IntProperty, EnumProperty,
    CollectionProperty,
)
from bpy.types import Operator, OperatorFileListElement
from bpy_extras.io_utils import ExportHelper, ImportHelper
from mathutils import Matrix, Vector, Quaternion


# ---------------------------------------------------------------------------
# LTA parse tree
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r'\(|\)|"[^"]*"|[^\s()"]+')

class LTAParseError(Exception):
    pass

def parse_lta(text):
    root = []
    stack = [root]
    for tok in _TOKEN_RE.findall(text):
        if tok == '(':
            new = []
            stack[-1].append(new)
            stack.append(new)
        elif tok == ')':
            if len(stack) == 1:
                continue
            stack.pop()
        else:
            if tok.startswith('"'):
                tok = tok[1:-1]
            stack[-1].append(tok)
    if len(stack) != 1:
        raise LTAParseError("Unbalanced parentheses")
    return root

def node_name(node):
    if isinstance(node, list) and node and isinstance(node[0], str):
        return node[0]
    return None

def shallow_find(node, name):
    for child in node:
        if isinstance(child, list) and node_name(child) == name:
            return child
    return None

def shallow_find_all(node, name):
    return [c for c in node if isinstance(c, list) and node_name(c) == name]

def find_all(node, name, out=None):
    if out is None:
        out = []
    if isinstance(node, list):
        if node_name(node) == name:
            out.append(node)
        for child in node:
            if isinstance(child, list):
                find_all(child, name, out)
    return out

def lists_of(node):
    return [c for c in node if isinstance(c, list)]

def atoms_of(node):
    return [c for c in node if isinstance(c, str)]

def first_string(node, skip=1):
    for c in node[skip:]:
        if isinstance(c, str):
            return c
    return None

def floats(seq):
    return [float(x) for x in seq if isinstance(x, str)]

def vector_list(node):
    if node is None:
        return []
    subs = lists_of(node)
    if len(subs) == 1 and lists_of(subs[0]) and not node_name(subs[0]):
        subs = lists_of(subs[0])
    return [floats(s) for s in subs if any(isinstance(c, str) for c in s)]

def flat_ints(node):
    if node is None:
        return []
    out = [int(float(a)) for a in atoms_of(node)[1:]]
    for sub in lists_of(node):
        out.extend(int(float(a)) for a in atoms_of(sub))
        for sub2 in lists_of(sub):
            out.extend(int(float(a)) for a in atoms_of(sub2))
    return out

def serialize_node(node, depth=0):
    pad = '\t' * depth
    if isinstance(node, str):
        return node
    simple = all(isinstance(c, str) for c in node)
    if simple:
        return pad + '( ' + ' '.join(_emit_atom(a) for a in node) + ' )'
    parts = [pad + '(']
    head = []
    i = 0
    while i < len(node) and isinstance(node[i], str):
        head.append(_emit_atom(node[i]))
        i += 1
    if head:
        parts[0] += ' ' + ' '.join(head)
    for child in node[i:]:
        parts.append(serialize_node(child, depth + 1))
    parts.append(pad + ')')
    return '\n'.join(parts)

_NUM_RE = re.compile(r'^-?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$')
def _emit_atom(a):
    if _NUM_RE.match(a):
        return a
    return '"%s"' % a


# ---------------------------------------------------------------------------
# Coordinate conversion
# ---------------------------------------------------------------------------
_C = Matrix(((1, 0, 0, 0),
             (0, 0, 1, 0),
             (0, 1, 0, 0),
             (0, 0, 0, 1)))

def vec_to_blender(v, scale=1.0):
    return Vector((v[0] * scale, v[2] * scale, v[1] * scale))

def dir_to_blender(v):
    return Vector((v[0], v[2], v[1]))

def quat_to_blender(q_xyzw):
    x, y, z, w = q_xyzw
    return Quaternion((w, -x, -z, -y))

def mat_to_blender(m, scale=1.0):
    out = _C @ m @ _C
    out.translation = out.translation * scale
    return out

def vec_to_lt(v, scale=1.0):
    return (v[0] * scale, v[2] * scale, v[1] * scale)

def dir_to_lt(v):
    return (v[0], v[2], v[1])

def quat_to_lt(q):
    # Blender Quaternion (w, x, y, z) -> LT (x, y, z, w)
    return (-q.x, -q.z, -q.y, q.w)

def mat_to_lt(m, scale=1.0):
    out = _C @ m @ _C
    out.translation = out.translation * scale
    return out


# ---------------------------------------------------------------------------
# LTA writer (export)
# ---------------------------------------------------------------------------
class LTAWriter:
    def __init__(self, float_digits=6):
        self.lines = []
        self.depth = 0
        self.ffmt = "%%.%df" % float_digits

    def open(self, *head):
        base = '\t' * self.depth
        self.lines.append(base + '( ' + ' '.join(head) if head else base + '(')
        self.depth += 1

    def close(self):
        self.depth -= 1
        self.lines.append('\t' * self.depth + ')')

    def line(self, text):
        self.lines.append('\t' * self.depth + text)

    def raw_block(self, text):
        base = '\t' * self.depth
        for ln in text.splitlines():
            self.lines.append(base + ln)

    def f(self, v):
        return self.ffmt % v

    def s(self, v):
        return '"%s"' % v

    def vec(self, v):
        return '( ' + ' '.join(self.f(c) for c in v) + ' )'

    def leaf(self, name, *vals):
        self.line('( %s %s )' % (name, ' '.join(vals)))

    def text(self):
        return '\n'.join(self.lines) + '\n'


# ---------------------------------------------------------------------------
# Common data structures for import/export
# ---------------------------------------------------------------------------
class BoneInfo:
    __slots__ = ("name", "parent", "rest_arm", "index", "children", "flags")
    def __init__(self, name, parent, rest_arm, index):
        self.name = name
        self.parent = parent
        self.rest_arm = rest_arm
        self.index = index
        self.children = []
        self.flags = 0

class LTAShape:
    def __init__(self, name):
        self.name = name
        self.mesh_name = name
        self.parent_node = None
        self.verts = []
        self.tris = []
        self.uvs = []
        self.tex_fs = []
        self.normals = []
        self.nrm_fs = []
        self.colors = []
        self.col_fs = []
        self.material = {}
        self.render_priority = None
        self.deformer = None  # (influences, weights)
        self.lod_distance = 0.0
        self.lod_group = None

class LTASocket:
    def __init__(self, name, parent, pos, quat):
        self.name = name
        self.parent = parent
        self.pos = pos
        self.quat = quat

class LTAAnim:
    def __init__(self, name):
        self.name = name
        self.times = []
        self.node_tracks = {}
        self.binding = {}
        self.frame_strings = {}  # keyframe index -> string

# ---------------------------------------------------------------------------
# Importer core
# ---------------------------------------------------------------------------
class LTAImporter:
    def __init__(self, operator, context, filepath, opts):
        self.op = operator
        self.context = context
        self.filepath = filepath
        self.o = opts
        self.nodes = {}
        self.node_order = []
        self.roots = []
        self.shapes = []
        self.sockets = []
        self.anims = []
        self.global_radius = None
        self.texture_bindings = {}
        self.preserved = {}
        self.warnings = []
        self.lod_groups = {}  # group name -> list of shape names

    def warn(self, msg):
        self.warnings.append(msg)
        self.op.report({'WARNING'}, msg)

    def load(self):
        with open(self.filepath, 'rb') as f:
            raw = f.read()
        # Check if compressed
        head = raw[:64]
        if b'\x00' in head or (head and head.lstrip()[:1] not in (b'(', b';')):
            raise LTAParseError(
                "File appears compressed (.LTC). Convert to .LTA first.")
        text = raw.decode('ascii', errors='replace')
        self.tree = parse_lta(text)
        models = find_all(self.tree, 'lt-model-0')
        if not models:
            if find_all(self.tree, 'world'):
                raise LTAParseError("This is a World schema .LTA, not a model.")
            raise LTAParseError("No lt-model-0 node found.")
        self.model = models[0]
        self.model_name = first_string(self.model) or os.path.splitext(os.path.basename(self.filepath))[0]

    def read_hierarchy(self):
        hierarchy = shallow_find(self.model, 'hierarchy')
        if hierarchy is None:
            raise LTAParseError("No (hierarchy) node.")
        children = shallow_find(hierarchy, 'children')
        if children:
            for sub in lists_of(children):
                if node_name(sub) == 'transform':
                    self._read_transform(sub, None, Matrix.Identity(4))
                else:
                    for t in lists_of(sub):
                        if node_name(t) == 'transform':
                            self._read_transform(t, None, Matrix.Identity(4))
        if not self.nodes:
            raise LTAParseError("No transform nodes found.")

    def _read_transform(self, tnode, parent, parent_world_b):
        name = first_string(tnode) or "node%d" % len(self.nodes)
        base = name
        n = 1
        while name in self.nodes:
            name = "%s.%03d" % (base, n)
            n += 1

        m = Matrix.Identity(4)
        mnode = shallow_find(tnode, 'matrix')
        if mnode:
            rows = lists_of(mnode)
            if rows and all(isinstance(c, str) for c in rows[0]):
                vals = [floats(r) for r in rows[:4]]
            else:
                vals = [floats(r) for r in lists_of(rows[0])[:4]]
            if len(vals) == 4 and all(len(r) == 4 for r in vals):
                m = Matrix(vals)

        # coord-frame-type: default is global, but we can handle local flags.
        cft = shallow_find(tnode, 'coord-frame-type')
        is_local = False
        if cft:
            flags = atoms_of(cft)[1:]
            if 'local' in flags:
                is_local = True

        node = BoneInfo(name, parent, None, len(self.node_order))
        m_b = mat_to_blender(m, self.o.scale)
        if is_local:
            node.rest_arm = parent_world_b @ m_b if parent else m_b
        else:
            node.rest_arm = m_b
        self.nodes[name] = node
        self.node_order.append(node)
        if parent:
            parent.children.append(node)
        else:
            self.roots.append(node)

        kids = shallow_find(tnode, 'children')
        if kids:
            for sub in lists_of(kids):
                if node_name(sub) == 'transform':
                    self._read_transform(sub, node, node.rest_arm)
                elif node_name(sub) == 'shape':
                    self._read_shape(sub, parent_hint=name)
                else:
                    for t in lists_of(sub):
                        if node_name(t) == 'transform':
                            self._read_transform(t, node, node.rest_arm)
                        elif node_name(t) == 'shape':
                            self._read_shape(t, parent_hint=name)

    def read_shapes(self):
        for s in shallow_find_all(self.model, 'shape'):
            self._read_shape(s)
        # Also collect LOD groups from on-load-cmds
        olc = shallow_find(self.model, 'on-load-cmds')
        if olc:
            for lod_node in find_all(olc, 'lod-groups'):
                for group in lists_of(lod_node):
                    if node_name(group) == 'create-lod-group':
                        self._parse_lod_group(group)

    def _parse_lod_group(self, group):
        # (create-lod-group "shapeName" (lod-dists (0.0 ...)) (shapes ("name1" ...)))
        shape_name = first_string(group)
        if not shape_name:
            return
        dists_node = shallow_find(group, 'lod-dists')
        shapes_node = shallow_find(group, 'shapes')
        if not dists_node or not shapes_node:
            return
        dists = flat_ints(dists_node)
        shape_names = [first_string(s) for s in lists_of(shapes_node) if first_string(s)]
        if not dists or not shape_names:
            return
        # The first dist is usually 0.0 for base LOD.
        # We'll store per shape the distance from the group.
        for idx, sname in enumerate(shape_names):
            if idx < len(dists):
                self.lod_groups.setdefault(shape_name, {})[sname] = dists[idx]

    def _read_shape(self, snode, parent_hint=None):
        name = first_string(snode) or "shape%d" % len(self.shapes)
        shape = LTAShape(name)

        pnode = shallow_find(snode, 'parent')
        if pnode:
            shape.parent_node = first_string(pnode)
        elif parent_hint:
            shape.parent_node = parent_hint

        geometry = shallow_find(snode, 'geometry')
        mesh = shallow_find(geometry, 'mesh') if geometry else None
        if mesh is None:
            self.warn("Shape '%s' has no geometry/mesh; skipped." % name)
            return
        shape.mesh_name = first_string(mesh) or name

        for fv in vector_list(shallow_find(mesh, 'vertex')):
            if len(fv) >= 3:
                shape.verts.append(vec_to_blender(fv, self.o.scale))

        shape.tris = flat_ints(shallow_find(mesh, 'tri-fs'))

        if self.o.import_uvs:
            for fv in vector_list(shallow_find(mesh, 'uvs')):
                if len(fv) >= 2:
                    shape.uvs.append((fv[0], 1.0 - fv[1]))
        shape.tex_fs = flat_ints(shallow_find(mesh, 'tex-fs'))

        if self.o.import_normals:
            for fv in vector_list(shallow_find(mesh, 'normals')):
                if len(fv) >= 3:
                    v = Vector(fv[:3])
                    shape.normals.append(dir_to_blender(fv).normalized() if v.length > 1e-9 else Vector((0,0,1)))
        shape.nrm_fs = flat_ints(shallow_find(mesh, 'nrm-fs'))

        if self.o.import_colors:
            for fv in vector_list(shallow_find(mesh, 'colors')):
                if len(fv) >= 3:
                    if len(fv) == 3:
                        fv.append(1.0)
                    shape.colors.append(tuple(fv[:4]))
        shape.col_fs = flat_ints(shallow_find(mesh, 'col-fs')) or flat_ints(shallow_find(mesh, 'col-ifs'))

        appearance = shallow_find(snode, 'appearance')
        material = shallow_find(appearance, 'material') if appearance else None
        if material:
            shape.material['name'] = first_string(material) or name
            for key in ('diffuse', 'ambient', 'emissive', 'specular'):
                kn = shallow_find(material, key)
                if kn:
                    vals = floats(atoms_of(kn)[1:])
                    if not vals and lists_of(kn):
                        vals = floats(lists_of(kn)[0])
                    if vals:
                        shape.material[key] = vals
            for key in ('shininess', 'texture-index'):
                kn = shallow_find(material, key)
                if kn:
                    vals = atoms_of(kn)[1:]
                    if vals:
                        shape.material[key] = float(vals[0])

        rp = shallow_find(snode, 'render-priority')
        if rp:
            vals = atoms_of(rp)[1:]
            if vals:
                shape.render_priority = int(float(vals[0]))

        # LOD info: check if this shape belongs to a LOD group
        for group_name, lod_data in self.lod_groups.items():
            if shape.name in lod_data:
                shape.lod_distance = lod_data[shape.name]
                shape.lod_group = group_name
                break

        self.shapes.append(shape)

    def read_on_load_cmds(self):
        olc = shallow_find(self.model, 'on-load-cmds')
        if not olc:
            return
        shape_by_name = {s.name: s for s in self.shapes}
        for cmd in lists_of(olc):
            kind = node_name(cmd)
            # Support both wrapped and direct deformer node structures
            if kind == 'add-deformer':
                for deformer in find_all(cmd, 'skel-deformer'):
                    self._read_deformer(deformer, shape_by_name)
            elif kind == 'skel-deformer' or kind == 'deformer':
                self._read_deformer(cmd, shape_by_name)
            elif kind == 'add-sockets':
                for sock in find_all(cmd, 'socket'):
                    self._read_socket(sock)
            elif kind == 'socket':
                self._read_socket(cmd)
            elif kind == 'set-global-radius':
                vals = atoms_of(cmd)[1:]
                if vals:
                    self.global_radius = float(vals[0])
            elif kind == 'set-node-flags':
                for sub in lists_of(cmd):
                    entries = lists_of(sub) or [sub]
                    for e in entries:
                        a = atoms_of(e)
                        if len(a) >= 2 and a[0] in self.nodes:
                            try:
                                self.nodes[a[0]].flags = int(float(a[1]))
                            except ValueError:
                                pass
            elif kind == 'anim-bindings':
                for b in find_all(cmd, 'anim-binding'):
                    nb = {}
                    nm = shallow_find(b, 'name')
                    if not nm:
                        continue
                    bname = first_string(nm)
                    for key in ('dims', 'translation'):
                        kn = shallow_find(b, key)
                        if kn:
                            vals = floats(atoms_of(kn)[1:])
                            if not vals and lists_of(kn):
                                vals = floats(lists_of(kn)[0])
                            if vals:
                                nb[key] = vals
                    it = shallow_find(b, 'interp-time')
                    if it and atoms_of(it)[1:]:
                        nb['interp-time'] = float(atoms_of(it)[1])
                    ws = shallow_find(b, 'weight-set')
                    if ws:
                        wsv = first_string(ws)
                        if wsv is not None:
                            nb['weight-set'] = wsv
                    self.anim_bindings[bname] = nb
            elif kind in ('add-anim-weightsets', 'anim-weightsets'):
                self.preserved['lta_weightsets'] = self.preserved.get('lta_weightsets', '') + serialize_node(cmd) + '\n'
            elif kind in ('add-childmodels', 'child-model'):
                self.preserved['lta_childmodels'] = self.preserved.get('lta_childmodels', '') + serialize_node(cmd) + '\n'
            elif kind == 'set-repl-lod-original':
                self.preserved['lta_lod'] = self.preserved.get('lta_lod', '') + serialize_node(cmd) + '\n'
            elif kind in ('add-node-obb-list', 'add-node-obb'):
                self.preserved['lta_obb'] = self.preserved.get('lta_obb', '') + serialize_node(cmd) + '\n'

    def _read_deformer(self, deformer, shape_by_name):
        tnode = shallow_find(deformer, 'target')
        target = first_string(tnode) if tnode else None
        
        # Case insensitive and fallback search to account for LTA looseness
        shape = shape_by_name.get(target)
        if shape is None and target:
            for sname, s in shape_by_name.items():
                if sname.lower() == target.lower():
                    shape = s
                    break
        
        # If there's only one shape, assume the deformer belongs to it regardless of target string mismatch
        if shape is None and len(shape_by_name) == 1:
            shape = list(shape_by_name.values())[0]
            
        if shape is None:
            self.warn("skel-deformer target '%s' not found. Mesh skinning skipped." % target)
            return
            
        inode = shallow_find(deformer, 'influences')
        influences = []
        if inode:
            influences = atoms_of(inode)[1:]
            if not influences and lists_of(inode):
                influences = atoms_of(lists_of(inode)[0])
                
        wnode = shallow_find(deformer, 'weightsets')
        weights = []
        if wnode:
            entries = lists_of(wnode)
            if len(entries) == 1 and lists_of(entries[0]):
                entries = lists_of(entries[0])
            for e in entries:
                a = floats(atoms_of(e))
                pairs = []
                for i in range(0, len(a)-1, 2):
                    pairs.append((int(a[i]), a[i+1]))
                weights.append(pairs)
        shape.deformer = (influences, weights)

    def _read_socket(self, sock):
        name = first_string(sock) or "socket"
        pnode = shallow_find(sock, 'parent')
        parent = first_string(pnode) if pnode else None
        pos = Vector((0,0,0))
        quat = Quaternion()
        posn = shallow_find(sock, 'pos')
        if posn:
            vals = floats(atoms_of(posn)[1:]) or (floats(lists_of(posn)[0]) if lists_of(posn) else [])
            if len(vals) >= 3:
                pos = vec_to_blender(vals, self.o.scale)
        qn = shallow_find(sock, 'quat')
        if qn:
            vals = floats(atoms_of(qn)[1:]) or (floats(lists_of(qn)[0]) if lists_of(qn) else [])
            if len(vals) >= 4:
                quat = quat_to_blender(vals)
        self.sockets.append(LTASocket(name, parent, pos, quat))

    def read_animsets(self):
        scope = self.tree
        all_anim_nodes = {}
        for a in find_all(scope, 'anim'):
            ident = first_string(a)
            if ident and shallow_find(a, 'frames'):
                all_anim_nodes.setdefault(ident, a)

        for aset in find_all(scope, 'animset'):
            name = first_string(aset) or "anim%d" % len(self.anims)
            anim = LTAAnim(name)

            # keyframe times
            kf_outer = shallow_find(aset, 'keyframe')
            times = []
            if kf_outer:
                kf_inner = shallow_find(kf_outer, 'keyframe') or kf_outer
                tnode = shallow_find(kf_inner, 'times')
                if tnode:
                    times = floats(atoms_of(tnode)[1:]) or (floats(lists_of(tnode)[0]) if lists_of(tnode) else [])
            anim.times = times

            # values (frame strings) - store per keyframe index
            vals_node = None
            if kf_outer:
                kf_inner = shallow_find(kf_outer, 'keyframe') or kf_outer
                vals_node = shallow_find(kf_inner, 'values')
            if vals_node:
                strings = atoms_of(vals_node)[1:]
                if not strings and lists_of(vals_node):
                    strings = atoms_of(lists_of(vals_node)[0])
                for idx, s in enumerate(strings):
                    if s:
                        anim.frame_strings[idx] = s

            anims = shallow_find(aset, 'anims')
            members = []
            if anims:
                for sub in lists_of(anims):
                    if node_name(sub) == 'anim':
                        members.append(sub)
                    else:
                        for a2 in lists_of(sub):
                            if node_name(a2) == 'anim':
                                members.append(a2)
                for ident in atoms_of(anims)[1:]:
                    if ident in all_anim_nodes:
                        members.append(all_anim_nodes[ident])

            for a in members:
                self._read_anim(a, anim)
            if anim.node_tracks or anim.times:
                anim.binding = getattr(self, 'anim_bindings', {}).get(name, {})
                self.anims.append(anim)

    def _read_anim(self, anode, anim):
        tnode = shallow_find(anode, 'parent') or shallow_find(anode, 'target')
        target = first_string(tnode) if tnode else first_string(anode)
        frames = shallow_find(anode, 'frames')
        if frames is None or target not in self.nodes:
            return

        if not anim.times:
            kf = shallow_find(anode, 'keyframe')
            if kf:
                tn = shallow_find(kf, 'times')
                if tn:
                    anim.times = floats(atoms_of(tn)[1:]) or (floats(lists_of(tn)[0]) if lists_of(tn) else [])

        pq = shallow_find(frames, 'posquat')
        if pq is None:
            return  # vertex animation not supported

        entries = lists_of(pq)
        if len(entries) == 1 and lists_of(entries[0]) and not node_name(entries[0]):
            entries = lists_of(entries[0])

        track = []
        for e in entries:
            sub = lists_of(e)
            pos_v, quat_v = None, None
            named_pos = shallow_find(e, 'pos')
            named_quat = shallow_find(e, 'quat')
            if named_pos or named_quat:
                if named_pos:
                    vals = floats(atoms_of(named_pos)[1:]) or (floats(lists_of(named_pos)[0]) if lists_of(named_pos) else [])
                    if len(vals) >= 3:
                        pos_v = vals
                if named_quat:
                    vals = floats(atoms_of(named_quat)[1:]) or (floats(lists_of(named_quat)[0]) if lists_of(named_quat) else [])
                    if len(vals) >= 4:
                        quat_v = vals
            elif len(sub) >= 2:
                a, b = floats(sub[0]), floats(sub[1])
                if len(a) >= 3:
                    pos_v = a
                if len(b) >= 4:
                    quat_v = b
            if pos_v is None and quat_v is None:
                continue
            p = vec_to_blender(pos_v, self.o.scale) if pos_v else Vector()
            q = quat_to_blender(quat_v) if quat_v else Quaternion()
            track.append((p, q))
        if track:
            anim.node_tracks[target] = track

    def build(self):
        context = self.context
        collection = context.scene.collection
        created = []

        # Armature
        arm_data = bpy.data.armatures.new(self.model_name)
        arm_obj = bpy.data.objects.new(self.model_name, arm_data)
        collection.objects.link(arm_obj)
        created.append(arm_obj)
        context.view_layer.objects.active = arm_obj
        arm_obj.select_set(True)

        bpy.ops.object.mode_set(mode='EDIT')
        for node in self.node_order:
            eb = arm_data.edit_bones.new(node.name)
            # Ensure unique name is updated back to the node, so vertex groups match 1:1
            node.name = eb.name
            # Calculate bone length
            length = self._bone_length(node)
            eb.head = (0.0, 0.0, 0.0)
            eb.tail = (0.0, length, 0.0)
            m = node.rest_arm.copy()
            loc, rot, _scale = m.decompose()
            eb.matrix = Matrix.LocRotScale(loc, rot, None)
        for node in self.node_order:
            if node.parent:
                arm_data.edit_bones[node.name].parent = arm_data.edit_bones[node.parent.name]
        bpy.ops.object.mode_set(mode='OBJECT')

        if self.o.bone_display == 'STICK':
            arm_data.display_type = 'STICK'
        else:
            arm_data.display_type = 'OCTAHEDRAL'
        arm_obj.show_in_front = True

        arm_obj['lta_source'] = os.path.basename(self.filepath)
        if self.global_radius is not None:
            arm_obj['lta_global_radius'] = self.global_radius
        for key, text in self.preserved.items():
            arm_obj[key] = text
        for node in self.node_order:
            if node.flags:
                pb = arm_obj.pose.bones.get(node.name)
                if pb:
                    pb['lta_node_flags'] = node.flags

        # Build LOD collections
        lod_collections = {}
        if self.o.import_lods:
            for group_name, lod_data in self.lod_groups.items():
                col = bpy.data.collections.new("LOD_%s" % group_name)
                collection.children.link(col)
                lod_collections[group_name] = col

        # Meshes
        for shape in self.shapes:
            # Skip LOD levels if not desired
            if not self.o.import_lods and shape.lod_distance != 0.0:
                continue
            obj = self._build_mesh(shape, arm_obj, collection)
            if obj:
                created.append(obj)
                # Place in LOD collection if applicable
                if shape.lod_group and shape.lod_group in lod_collections:
                    lod_collections[shape.lod_group].objects.link(obj)
                    # Also keep in main collection? We'll unlink from main if we link to LOD col.
                    if obj.name in collection.objects:
                        collection.objects.unlink(obj)

        # Sockets
        if self.o.import_sockets:
            for sock in self.sockets:
                obj = self._build_socket(sock, arm_obj, collection)
                if obj:
                    created.append(obj)

        # Animations
        if self.o.import_anims and self.anims:
            self._build_actions(arm_obj)

        for obj in created:
            obj.select_set(True)
        context.view_layer.objects.active = arm_obj
        return arm_obj

    def _bone_length(self, node):
        head = node.rest_arm.translation
        best = None
        for c in node.children:
            d = (c.rest_arm.translation - head).length
            if d > 1e-5 and (best is None or d < best):
                best = d
        if best is None and node.parent:
            best = (node.parent.rest_arm.translation - head).length * 0.5
        if best is None or best < 1e-4:
            best = max(0.1, 2.0 * self.o.scale)
        return best

    def _build_mesh(self, shape, arm_obj, collection):
        if not shape.verts:
            return None
        nverts = len(shape.verts)
        idx = shape.tris
        faces = []
        uv_loops, nrm_loops, col_loops = [], [], []
        has_texfs = bool(shape.tex_fs) and len(shape.tex_fs) >= len(idx)
        has_nrmfs = bool(shape.nrm_fs) and len(shape.nrm_fs) >= len(idx)
        has_colfs = bool(shape.col_fs) and len(shape.col_fs) >= len(idx)

        for t in range(len(idx)//3):
            a, b, c = idx[t*3], idx[t*3+1], idx[t*3+2]
            if a >= nverts or b >= nverts or c >= nverts:
                continue
            if a == b or b == c or a == c:
                continue
            faces.append((a, c, b))  # winding flip for Blender
            order = (t*3, t*3+2, t*3+1)
            if shape.uvs and has_texfs:
                uv_loops.extend(shape.tex_fs[i] for i in order)
            if shape.normals and has_nrmfs:
                nrm_loops.extend(shape.nrm_fs[i] for i in order)
            if shape.colors and has_colfs:
                col_loops.extend(shape.col_fs[i] for i in order)

        mesh = bpy.data.meshes.new(shape.mesh_name)
        mesh.from_pydata([v[:] for v in shape.verts], [], faces)
        mesh.validate(verbose=False)

        if shape.uvs and faces:
            uv_layer = mesh.uv_layers.new(name="UVMap")
            nuv = len(shape.uvs)
            for li, loop in enumerate(mesh.loops):
                uvi = uv_loops[li] if has_texfs else loop.vertex_index
                if 0 <= uvi < nuv:
                    uv_layer.data[li].uv = shape.uvs[uvi]

        if shape.normals and self.o.import_normals and faces:
            try:
                if has_nrmfs:
                    loops_n = []
                    nn = len(shape.normals)
                    for li, loop in enumerate(mesh.loops):
                        i = nrm_loops[li] if li < len(nrm_loops) else 0
                        loops_n.append(shape.normals[i][:] if 0 <= i < nn else (0,0,1))
                    mesh.normals_split_custom_set(loops_n)
                elif len(shape.normals) >= nverts:
                    mesh.normals_split_custom_set_from_vertices([n[:] for n in shape.normals[:nverts]])
            except Exception as ex:
                self.warn("Custom normals failed for %s: %s" % (shape.name, ex))
        if self.o.shade_smooth:
            mesh.shade_smooth()

        if shape.colors and self.o.import_colors and faces:
            attr = mesh.color_attributes.new("Color", 'FLOAT_COLOR', 'CORNER')
            nc = len(shape.colors)
            for li, loop in enumerate(mesh.loops):
                ci = col_loops[li] if has_colfs else loop.vertex_index
                if 0 <= ci < nc:
                    attr.data[li].color = shape.colors[ci]

        mesh.update()

        obj = bpy.data.objects.new(shape.name, mesh)
        collection.objects.link(obj)

        if self.o.import_materials:
            obj.data.materials.append(self._build_material(shape))

        if shape.render_priority is not None:
            obj['lta_render_priority'] = shape.render_priority
        if shape.lod_distance is not None:
            obj['lta_lod_distance'] = shape.lod_distance
        if shape.lod_group:
            obj['lta_lod_group'] = shape.lod_group

        # Skinning
        if shape.deformer:
            influences, weights = shape.deformer
            groups = {}
            for iname in influences:
                # IMPORTANT: Map LTA's influence string to the final, deduplicated bone name enforced by Blender
                vgroup_name = self.nodes[iname].name if iname in self.nodes else iname
                groups[iname] = obj.vertex_groups.new(name=vgroup_name)
            
            for vi, pairs in enumerate(weights):
                if vi >= nverts:
                    break
                for ii, w in pairs:
                    if 0 <= ii < len(influences) and w != 0.0:
                        groups[influences[ii]].add([vi], w, 'REPLACE')
            
            mod = obj.modifiers.new("Armature", 'ARMATURE')
            mod.object = arm_obj
            mod.use_vertex_groups = True
            obj.parent = arm_obj
        elif shape.parent_node and shape.parent_node in self.nodes:
            node = self.nodes[shape.parent_node]
            bone = arm_obj.data.bones.get(node.name)
            obj.parent = arm_obj
            if bone and self.o.rigid_parent_to_bones:
                obj.parent_type = 'BONE'
                obj.parent_bone = node.name
                tail = Matrix.Translation((0, bone.length, 0))
                obj.matrix_basis = (bone.matrix_local @ tail).inverted_safe()
        else:
            obj.parent = arm_obj
        return obj

    def _build_material(self, shape):
        mname = shape.material.get('name', shape.name)
        mat = bpy.data.materials.new(mname)
        mat.use_nodes = True
        bsdf = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
        diffuse = shape.material.get('diffuse')
        if bsdf and diffuse:
            col = list(diffuse[:3]) + [diffuse[3] if len(diffuse) > 3 else 1.0]
            if max(col[:3]) > 1.001:
                col = [c/255.0 for c in col[:3]] + [col[3]]
            bsdf.inputs['Base Color'].default_value = col
        shin = shape.material.get('shininess')
        if bsdf and shin is not None:
            bsdf.inputs['Roughness'].default_value = max(0.0, 1.0 - min(shin, 128.0)/128.0)
        tex_index = int(shape.material.get('texture-index', 0))
        mat['lta_texture_index'] = tex_index
        tex_name = self.texture_bindings.get(tex_index)
        if tex_name:
            mat['lta_texture'] = tex_name
            if self.o.texture_dir and bsdf:
                img = self._find_texture(tex_name)
                if img:
                    nt = mat.node_tree
                    tex_node = nt.nodes.new('ShaderNodeTexImage')
                    tex_node.image = img
                    tex_node.location = (-340, 220)
                    nt.links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
        return mat

    def _find_texture(self, tex_name):
        base = os.path.splitext(os.path.basename(tex_name.replace('\\','/')))[0].lower()
        exts = ('.png', '.tga', '.dds', '.jpg', '.bmp', '.tif')
        for root, _, files in os.walk(bpy.path.abspath(self.o.texture_dir)):
            for f in files:
                fb, fe = os.path.splitext(f)
                if fb.lower() == base and fe.lower() in exts:
                    try:
                        return bpy.data.images.load(os.path.join(root, f), check_existing=True)
                    except RuntimeError:
                        return None
        return None

    def _build_socket(self, sock, arm_obj, collection):
        node = self.nodes.get(sock.parent)
        empty = bpy.data.objects.new("s_" + sock.name, None)
        empty.empty_display_type = 'ARROWS'
        empty.empty_display_size = max(0.05, 3.0 * self.o.scale)
        empty['lta_socket'] = sock.name
        collection.objects.link(empty)
        rel = Matrix.LocRotScale(sock.pos, sock.quat, None)
        if node:
            bone = arm_obj.data.bones.get(node.name)
            empty.parent = arm_obj
            if bone:
                empty.parent_type = 'BONE'
                empty.parent_bone = node.name
                empty['lta_socket_parent'] = node.name
                tail = Matrix.Translation((0, bone.length, 0))
                empty.matrix_basis = tail.inverted_safe() @ rel
            else:
                empty.matrix_basis = rel
        else:
            self.warn("Socket '%s' parent node '%s' not found." % (sock.name, sock.parent))
            empty.matrix_basis = rel
        return empty

    def _build_actions(self, arm_obj):
        fps = self.o.anim_fps
        ad = arm_obj.animation_data_create()
        first_action = None

        for anim in self.anims:
            action = bpy.data.actions.new(anim.name)
            action.use_fake_user = True
            ad.action = action
            if hasattr(action, "slots"):
                if not len(action.slots):
                    try:
                        slot = action.slots.new(id_type='OBJECT', name=arm_obj.name)
                    except TypeError:
                        slot = action.slots.new('OBJECT', arm_obj.name)
                else:
                    slot = action.slots[0]
                ad.action_slot = slot

            if first_action is None:
                first_action = action

            b = anim.binding
            action['lta_dims'] = b.get('dims', [16.0, 16.0, 16.0])
            action['lta_translation'] = b.get('translation', [0.0, 0.0, 0.0])
            action['lta_interp_time'] = b.get('interp-time', 200)
            if 'weight-set' in b:
                action['lta_weight_set'] = b['weight-set']

            # Store frame strings in action custom property
            if anim.frame_strings:
                action['lta_frame_strings'] = {str(k): v for k, v in anim.frame_strings.items()}

            times = anim.times
            nframes = max((len(t) for t in anim.node_tracks.values()), default=len(times))
            if not times:
                times = [i * (1000.0 / fps) for i in range(nframes)]
            t0 = times[0] if times else 0.0
            frames = [1.0 + (t - t0) * fps / 1000.0 for t in times]
            while len(frames) < nframes:
                frames.append((frames[-1] + 1.0) if frames else 1.0)

            basis_per_node = {nd.name: [] for nd in self.node_order}
            rest = {nd.name: arm_obj.data.bones[nd.name].matrix_local for nd in self.node_order}
            for fi in range(nframes):
                pose_ws = {}
                for nd in self.node_order:
                    track = anim.node_tracks.get(nd.name)
                    if track:
                        p, q = track[min(fi, len(track)-1)]
                        local = Matrix.LocRotScale(p, q, None)
                    else:
                        local = nd.rest_arm
                    parent_ws = pose_ws[nd.parent.name] if nd.parent else Matrix.Identity(4)
                    P = parent_ws @ local
                    pose_ws[nd.name] = P
                    if nd.parent:
                        basis = rest[nd.name].inverted_safe() @ rest[nd.parent.name] @ pose_ws[nd.parent.name].inverted_safe() @ P
                    else:
                        basis = rest[nd.name].inverted_safe() @ P
                    basis_per_node[nd.name].append(basis)

            for nd in self.node_order:
                pb = arm_obj.pose.bones.get(nd.name)
                if pb is None:
                    continue
                pb.rotation_mode = 'QUATERNION'
                bases = basis_per_node[nd.name]
                locs, quats = [], []
                prev_q = None
                for bm in bases:
                    loc, q, _s = bm.decompose()
                    if prev_q is not None and prev_q.dot(q) < 0.0:
                        q.negate()
                    prev_q = q
                    locs.append(loc)
                    quats.append(q)

                if self.o.skip_static_tracks:
                    if all((l - locs[0]).length < 1e-6 for l in locs) and \
                       all(abs(q.dot(quats[0])) > 1.0 - 1e-7 for q in quats) and \
                       nd.name not in anim.node_tracks:
                        continue

                path_l = 'pose.bones["%s"].location' % nd.name
                path_q = 'pose.bones["%s"].rotation_quaternion' % nd.name
                for axis in range(3):
                    fc = action.fcurves.new(path_l, index=axis, action_group=nd.name)
                    self._fill_fcurve(fc, frames, [l[axis] for l in locs])
                for axis in range(4):
                    fc = action.fcurves.new(path_q, index=axis, action_group=nd.name)
                    self._fill_fcurve(fc, frames, [q[axis] for q in quats])

        if first_action:
            ad.action = first_action
            if hasattr(first_action, "slots") and len(first_action.slots):
                ad.action_slot = first_action.slots[0]
            scn = self.context.scene
            scn.frame_start = 1
            a0 = self.anims[0]
            if a0.times and len(a0.times) > 1:
                span = (a0.times[-1] - a0.times[0]) * fps / 1000.0
                scn.frame_end = max(scn.frame_end, int(round(1 + span)))

    @staticmethod
    def _fill_fcurve(fc, frames, values):
        n = len(values)
        fc.keyframe_points.add(n)
        flat = [0.0] * (n * 2)
        for i in range(n):
            flat[i*2] = frames[i] if i < len(frames) else frames[-1] + i
            flat[i*2+1] = values[i]
        fc.keyframe_points.foreach_set('co', flat)
        for kp in fc.keyframe_points:
            kp.interpolation = 'LINEAR'
        fc.update()

    def run(self):
        t0 = time.time()
        self.load()
        self.read_hierarchy()
        self.read_shapes()
        self.read_on_load_cmds()
        if self.o.import_anims:
            self.read_animsets()
        arm = self.build()
        self.op.report({'INFO'}, "Imported '%s': %d nodes, %d shapes, %d sockets, %d animations in %.2fs" %
                        (self.model_name, len(self.node_order), len(self.shapes), len(self.sockets), len(self.anims),
                         time.time()-t0))
        return arm


# ---------------------------------------------------------------------------
# Exporter core
# ---------------------------------------------------------------------------
class ExportError(Exception):
    pass

class LTAExporter:
    def __init__(self, operator, context, filepath, opts):
        self.op = operator
        self.context = context
        self.filepath = filepath
        self.o = opts
        self.bones = []
        self.bone_by_name = {}
        self.warnings = []

    def warn(self, msg):
        self.warnings.append(msg)
        self.op.report({'WARNING'}, msg)

    def find_objects(self):
        ctx = self.context
        if self.o.use_selection:
            pool = list(ctx.selected_objects)
        else:
            pool = [o for o in ctx.scene.objects if o.visible_get()]

        armatures = [o for o in pool if o.type == 'ARMATURE']
        if not armatures:
            for o in pool:
                if o.type == 'MESH':
                    for mod in o.modifiers:
                        if mod.type == 'ARMATURE' and mod.object:
                            armatures.append(mod.object)
                    if o.parent and o.parent.type == 'ARMATURE':
                        armatures.append(o.parent)
        armatures = list(dict.fromkeys(armatures))
        if not armatures:
            raise ExportError("No armature found. Add an armature with at least one bone.")
        if len(armatures) > 1:
            self.warn("Multiple armatures; using '%s'." % armatures[0].name)
        self.arm_obj = armatures[0]

        self.mesh_objs = []
        scene_meshes = ctx.selected_objects if self.o.use_selection else ctx.scene.objects
        for o in scene_meshes:
            if o.type != 'MESH' or not o.visible_get():
                continue
            uses_arm = (o.parent == self.arm_obj)
            for mod in o.modifiers:
                if mod.type == 'ARMATURE' and mod.object == self.arm_obj:
                    uses_arm = True
            if uses_arm or self.o.use_selection:
                self.mesh_objs.append(o)
        if not self.mesh_objs:
            self.warn("No mesh objects found; exporting skeleton/animations only.")

        self.socket_objs = []
        if self.o.export_sockets:
            for o in ctx.scene.objects:
                if o.type == 'EMPTY' and o.parent == self.arm_obj and \
                        (o.parent_type == 'BONE' or 'lta_socket' in o):
                    self.socket_objs.append(o)

    def gather_bones(self):
        arm = self.arm_obj.data
        index = 0
        ordered = []

        def visit(bone, parent_info):
            nonlocal index
            info = BoneInfo(bone.name, parent_info, bone.matrix_local.copy(), index)
            index += 1
            self.bones.append(info)
            self.bone_by_name[bone.name] = info
            for c in bone.children:
                visit(c, info)

        roots = [b for b in arm.bones if b.parent is None]
        if not roots:
            raise ExportError("Armature has no bones.")
        for r in roots:
            visit(r, None)
        if len(roots) > 1:
            self.warn("Multiple root bones; ModelEdit expects a single root.")

    def extract_meshes(self):
        depsgraph = None
        disabled = []
        if self.o.apply_modifiers:
            for o in self.mesh_objs:
                for mod in o.modifiers:
                    if mod.type == 'ARMATURE' and mod.show_viewport:
                        mod.show_viewport = False
                        disabled.append(mod)
            depsgraph = self.context.evaluated_depsgraph_get()

        arm_inv = self.arm_obj.matrix_world.inverted_safe()
        shapes = []
        try:
            for obj in self.mesh_objs:
                if self.o.apply_modifiers:
                    ev = obj.evaluated_get(depsgraph)
                    mesh = ev.to_mesh()
                else:
                    ev = obj
                    mesh = obj.to_mesh()
                try:
                    shapes.append(self._extract_one(obj, mesh, arm_inv))
                finally:
                    ev.to_mesh_clear()
        finally:
            for mod in disabled:
                mod.show_viewport = True
        return [s for s in shapes if s]

    def _extract_one(self, obj, mesh, arm_inv):
        scale = self.o.scale
        to_arm = arm_inv @ obj.matrix_world
        nrm_mat = to_arm.to_3x3().inverted_safe().transposed()

        mesh.calc_loop_triangles()
        if not mesh.loop_triangles:
            self.warn("Mesh '%s' has no faces; skipped." % obj.name)
            return None

        verts_lt = [vec_to_lt(to_arm @ v.co, scale) for v in mesh.vertices]

        deformer = None
        if self.o.export_weights:
            valid_groups = {g.index: g.name for g in obj.vertex_groups if g.name in self.bone_by_name}
            if valid_groups:
                influences = []
                inf_index = {}
                weights = []
                limit = self.o.max_weights
                for v in mesh.vertices:
                    pairs = []
                    for ge in v.groups:
                        nm = valid_groups.get(ge.group)
                        if nm and ge.weight > 1e-5:
                            pairs.append((nm, ge.weight))
                    pairs.sort(key=lambda p: -p[1])
                    if limit > 0:
                        pairs = pairs[:limit]
                    total = sum(w for _n, w in pairs)
                    if total <= 0.0:
                        root = self.bones[0].name
                        pairs, total = [(root, 1.0)], 1.0
                    out = []
                    for nm, w in pairs:
                        if nm not in inf_index:
                            inf_index[nm] = len(influences)
                            influences.append(nm)
                        out.append((inf_index[nm], w / total))
                    weights.append(out)
                deformer = (influences, weights)

        uv_layer = mesh.uv_layers.active if self.o.export_uvs else None
        uvs, uv_index = [], {}
        loop_uv = None
        if uv_layer:
            loop_uv = [None] * len(mesh.loops)
            data = uv_layer.uv if hasattr(uv_layer, "uv") else uv_layer.data
            for li in range(len(mesh.loops)):
                uv = data[li].vector if hasattr(data[li], "vector") else data[li].uv
                key = (round(uv[0], 6), round(1.0 - uv[1], 6))
                i = uv_index.get(key)
                if i is None:
                    i = len(uvs)
                    uv_index[key] = i
                    uvs.append(key)
                loop_uv[li] = i

        normals, nrm_index, loop_nrm = [], {}, None
        if self.o.export_normals:
            loop_nrm = [0] * len(mesh.loops)
            corner_normals = mesh.corner_normals
            for li in range(len(mesh.loops)):
                n = nrm_mat @ Vector(corner_normals[li].vector)
                n.normalize()
                key = dir_to_lt((round(n[0],4), round(n[1],4), round(n[2],4)))
                i = nrm_index.get(key)
                if i is None:
                    i = len(normals)
                    nrm_index[key] = i
                    normals.append(key)
                loop_nrm[li] = i

        colors, col_index, loop_col = [], {}, None
        if self.o.export_colors and mesh.color_attributes:
            attr = mesh.color_attributes.active_color or mesh.color_attributes[0]
            if attr.domain in {'CORNER', 'POINT'}:
                loop_col = [0] * len(mesh.loops)
                for li, loop in enumerate(mesh.loops):
                    di = li if attr.domain == 'CORNER' else loop.vertex_index
                    c = attr.data[di].color
                    key = (round(c[0],4), round(c[1],4), round(c[2],4), round(c[3],4))
                    i = col_index.get(key)
                    if i is None:
                        i = len(colors)
                        col_index[key] = i
                        colors.append(key)
                    loop_col[li] = i

        tri_fs, tex_fs, nrm_fs, col_fs = [], [], [], []
        for tri in mesh.loop_triangles:
            lv = tri.vertices
            ll = tri.loops
            order = (0, 2, 1)  # winding flip for LTA
            tri_fs.extend(lv[i] for i in order)
            if loop_uv is not None:
                tex_fs.extend(loop_uv[ll[i]] for i in order)
            if loop_nrm is not None:
                nrm_fs.extend(loop_nrm[ll[i]] for i in order)
            if loop_col is not None:
                col_fs.extend(loop_col[ll[i]] for i in order)

        material = {'name': obj.active_material.name if obj.active_material else obj.name}
        texture = None
        mat = obj.active_material
        if mat:
            material['texture-index'] = int(mat.get('lta_texture_index', 0))
            texture = mat.get('lta_texture')
            if mat.use_nodes:
                bsdf = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
                if bsdf:
                    bc = bsdf.inputs['Base Color']
                    material['diffuse'] = tuple(bc.default_value)[:4]
                    if not texture:
                        for link in bc.links:
                            n = link.from_node
                            if n.type == 'TEX_IMAGE' and n.image:
                                texture = os.path.splitext(os.path.basename(n.image.filepath or n.image.name))[0]
                                texture += ".dtx"
        else:
            material['texture-index'] = 0

        parent = self.bones[0].name
        if obj.parent_type == 'BONE' and obj.parent_bone in self.bone_by_name:
            parent = obj.parent_bone

        return {
            'name': obj.name,
            'parent': parent,
            'verts': verts_lt,
            'tri_fs': tri_fs,
            'uvs': uvs, 'tex_fs': tex_fs,
            'normals': normals, 'nrm_fs': nrm_fs,
            'colors': colors, 'col_fs': col_fs,
            'deformer': deformer,
            'material': material,
            'texture': texture,
            'render_priority': obj.get('lta_render_priority'),
            'lod_distance': obj.get('lta_lod_distance', 0.0),
            'lod_group': obj.get('lta_lod_group', None),
        }

    def extract_sockets(self):
        out = []
        arm_inv = self.arm_obj.matrix_world.inverted_safe()
        for e in self.socket_objs:
            bone_name = e.parent_bone or e.get('lta_socket_parent')
            info = self.bone_by_name.get(bone_name)
            if info is None:
                self.warn("Socket '%s' parent bone not found; skipped." % e.name)
                continue
            rel = info.rest_arm.inverted_safe() @ arm_inv @ e.matrix_world
            loc, rot, _s = rel.decompose()
            name = e.get('lta_socket') or (e.name[2:] if e.name.lower().startswith('s_') else e.name)
            name = name.split('.')[0] if name.count('.') and name.split('.')[-1].isdigit() else name
            out.append((name, bone_name, vec_to_lt(loc, self.o.scale), quat_to_lt(rot)))
        return out

    def collect_actions(self):
        mode = self.o.anim_mode
        if mode == 'NONE':
            actions = []
        elif mode == 'ACTIVE':
            ad = self.arm_obj.animation_data
            actions = [ad.action] if ad and ad.action else []
        else:
            actions = [a for a in bpy.data.actions if self._action_targets_bones(a)]
        if not actions and self.o.add_base_anim:
            actions = [None]
        elif not actions:
            self.warn("No animations exported. Enable 'Add Base Animation' or export an action.")
        return actions

    def _action_targets_bones(self, action):
        for fc in action.fcurves:
            if fc.data_path.startswith('pose.bones['):
                return True
        return False

    def sample_action(self, action):
        scn = self.context.scene
        fps = scn.render.fps / scn.render.fps_base
        arm_obj = self.arm_obj

        if action is None:
            name = "base"
            frames = [scn.frame_current]
            binding = {'dims': (16.0, 16.0, 16.0),
                       'translation': (0.0, 0.0, 0.0),
                       'interp-time': 200}
            frame_strings = {}
        else:
            name = action.name
            f0, f1 = action.frame_range
            step = max(1, self.o.frame_step)
            frames = list(range(int(round(f0)), int(round(f1)) + 1, step))
            if not frames:
                frames = [int(round(f0))]
            if frames[-1] != int(round(f1)):
                frames.append(int(round(f1)))
            binding = {
                'dims': tuple(action.get('lta_dims', (16.0, 16.0, 16.0))),
                'translation': tuple(action.get('lta_translation', (0.0, 0.0, 0.0))),
                'interp-time': int(action.get('lta_interp_time', 200)),
            }
            ws = action.get('lta_weight_set')
            if ws:
                binding['weight-set'] = ws
            frame_strings = action.get('lta_frame_strings', {})
            # Convert keys to int
            if isinstance(frame_strings, dict):
                frame_strings = {int(k): v for k, v in frame_strings.items()}

        ad = arm_obj.animation_data_create()
        prev_action = ad.action
        prev_slot = getattr(ad, "action_slot", None)
        prev_frame = scn.frame_current

        if action is not None:
            ad.action = action
            if hasattr(action, "slots") and len(action.slots):
                slot = next((s for s in action.slots if getattr(s, "target_id_type", 'OBJECT') == 'OBJECT'), action.slots[0])
                try:
                    ad.action_slot = slot
                except Exception:
                    self.warn("Could not assign action slot for '%s'." % action.name)
        else:
            ad.action = None

        tracks = {b.name: [] for b in self.bones}
        f_start = frames[0]
        times = []
        try:
            for f in frames:
                scn.frame_set(f)
                depsgraph = self.context.evaluated_depsgraph_get()
                ev = arm_obj.evaluated_get(depsgraph)
                pose_arm = {pb.name: pb.matrix.copy() for pb in ev.pose.bones}
                for b in self.bones:
                    P = pose_arm[b.name]
                    if b.parent:
                        local_b = pose_arm[b.parent.name].inverted_safe() @ P
                    else:
                        local_b = P
                    local_lt = mat_to_lt(local_b, self.o.scale)
                    loc, rot, _s = local_lt.decompose()
                    tracks[b.name].append(((loc.x, loc.y, loc.z), (rot.x, rot.y, rot.z, rot.w)))
                times.append(int(round((f - f_start) * 1000.0 / fps)))
        finally:
            ad.action = prev_action
            if prev_slot is not None and hasattr(ad, "action_slot"):
                try:
                    ad.action_slot = prev_slot
                except Exception:
                    pass
            scn.frame_set(prev_frame)

        # Quaternion continuity
        for tk in tracks.values():
            for i in range(1, len(tk)):
                p, q = tk[i]
                qp = tk[i-1][1]
                if sum(a*b for a,b in zip(q, qp)) < 0.0:
                    tk[i] = (p, tuple(-c for c in q))

        # Ensure at least 2 keyframes
        if len(times) == 1:
            times.append(times[0] + 200)
            for tk in tracks.values():
                tk.append(tk[0])

        return name, times, tracks, binding, frame_strings

    def write(self, shapes, sockets, anims):
        w = LTAWriter(self.o.float_digits)
        model_name = self.o.model_name.strip() or os.path.splitext(os.path.basename(self.filepath))[0]

        w.open('lt-model-0', w.s(model_name))

        # on-load-cmds
        w.open('on-load-cmds')
        w.open()

        # anim-bindings
        if anims:
            w.open('anim-bindings')
            w.open()
            for (aname, _times, _tracks, binding, _fs) in anims:
                w.open('anim-binding')
                w.leaf('name', w.s(aname))
                w.leaf('dims', w.vec(binding['dims']))
                w.leaf('translation', w.vec(binding['translation']))
                w.leaf('interp-time', str(int(binding['interp-time'])))
                if binding.get('weight-set'):
                    w.leaf('weight-set', w.s(binding['weight-set']))
                w.close()
            w.close()
            w.close()

        # set-node-flags
        if self.o.write_node_flags:
            w.open('set-node-flags')
            w.open()
            for b in self.bones:
                pb = self.arm_obj.pose.bones.get(b.name)
                flags = int(pb.get('lta_node_flags', 0)) if pb else 0
                w.line('( %s %d )' % (w.s(b.name), flags))
            w.close()
            w.close()

        # deformers
        for shape in shapes:
            if shape['deformer']:
                influences, weights = shape['deformer']
                w.open('add-deformer')
                w.open('skel-deformer', w.s(shape['name'] + "_deformer"))
                w.leaf('target', w.s(shape['name']))
                w.open('influences')
                w.line('( ' + ' '.join(w.s(n) for n in influences) + ' )')
                w.close()
                w.open('weightsets')
                w.open()
                for pairs in weights:
                    w.line('( ' + ' '.join('%d %s' % (i, w.f(wt)) for i, wt in pairs) + ' )')
                w.close()
                w.close()
                w.close()
                w.close()

        # sockets
        if sockets:
            w.open('add-sockets')
            w.open()
            for (name, parent, pos, quat) in sockets:
                w.open('socket', w.s(name))
                w.leaf('parent', w.s(parent))
                w.leaf('pos', w.vec(pos))
                w.leaf('quat', w.vec(quat))
                w.close()
            w.close()
            w.close()

        radius = self.arm_obj.get('lta_global_radius', self.o.global_radius)
        w.leaf('set-global-radius', w.f(float(radius)))

        # preserved blocks
        if self.o.write_preserved:
            for key in ('lta_weightsets', 'lta_childmodels', 'lta_lod', 'lta_obb'):
                blob = self.arm_obj.get(key)
                if blob:
                    w.raw_block(str(blob))

        # LOD groups - export if any mesh has lod_distance set
        lod_groups = defaultdict(list)
        for shape in shapes:
            if shape.get('lod_group') and shape.get('lod_distance') is not None:
                lod_groups[shape['lod_group']].append((shape['name'], shape['lod_distance']))
        if lod_groups:
            w.open('lod-groups')
            w.open()
            for group_name, items in lod_groups.items():
                w.open('create-lod-group', w.s(group_name))
                w.open('lod-dists')
                dists = [str(d) for _, d in sorted(items, key=lambda x: x[1])]
                w.line('( ' + ' '.join(dists) + ' )')
                w.close()
                w.open('shapes')
                names = [w.s(name) for name, _ in sorted(items, key=lambda x: x[1])]
                w.line('( ' + ' '.join(names) + ' )')
                w.close()
                w.close()
            w.close()
            w.close()

        w.close()  # anonymous list
        w.close()  # on-load-cmds

        # hierarchy
        w.open('hierarchy', w.s(model_name))
        w.open('children')
        w.open()
        roots = [b for b in self.bones if b.parent is None]
        for r in roots:
            self._write_transform(w, r)
        w.close()
        w.close()
        w.close()

        # shapes
        for shape in shapes:
            self._write_shape(w, shape)

        # animsets
        for (aname, times, tracks, binding, frame_strings) in anims:
            self._write_animset(w, aname, times, tracks, frame_strings)

        # tools-info / texture-bindings
        seen = {}
        for shape in shapes:
            tex = shape['texture']
            idx = shape['material'].get('texture-index', 0)
            if tex and idx not in seen:
                seen[idx] = tex
        if seen:
            w.open('tools-info')
            w.open()
            w.open('texture-bindings')
            w.open()
            for idx in sorted(seen):
                w.line('( %d %s )' % (idx, w.s(seen[idx])))
            w.close()
            w.close()
            w.close()
            w.close()

        w.close()  # lt-model-0
        return w.text()

    def _write_transform(self, w, bone):
        m = mat_to_lt(bone.rest_arm, self.o.scale)
        w.open('transform', w.s(bone.name))
        w.open('matrix')
        w.open()
        for r in range(4):
            w.line(w.vec(m[r]))
        w.close()
        w.close()
        children = [b for b in self.bones if b.parent is bone]
        if children:
            w.open('children')
            w.open()
            for c in children:
                self._write_transform(w, c)
            w.close()
            w.close()
        w.close()

    def _write_shape(self, w, shape):
        w.open('shape', w.s(shape['name']))
        w.leaf('parent', w.s(shape['parent']))
        if shape['render_priority'] is not None:
            w.leaf('render-priority', str(int(shape['render_priority'])))

        w.open('geometry')
        w.open('mesh', w.s(shape['name']))

        w.open('vertex')
        w.open()
        for v in shape['verts']:
            w.line(w.vec(v))
        w.close()
        w.close()

        if shape['normals']:
            w.open('normals')
            w.open()
            for n in shape['normals']:
                w.line(w.vec(n))
            w.close()
            w.close()

        if shape['uvs']:
            w.open('uvs')
            w.open()
            for uv in shape['uvs']:
                w.line(w.vec(uv))
            w.close()
            w.close()

        if shape['colors']:
            w.open('colors')
            w.open()
            for c in shape['colors']:
                w.line(w.vec(c))
            w.close()
            w.close()

        self._write_faceset(w, 'tri-fs', shape['tri_fs'])
        if shape['tex_fs']:
            self._write_faceset(w, 'tex-fs', shape['tex_fs'])
        if shape['nrm_fs']:
            self._write_faceset(w, 'nrm-fs', shape['nrm_fs'])
        if shape['col_fs']:
            self._write_faceset(w, 'col-fs', shape['col_fs'])

        w.close()  # mesh
        w.close()  # geometry

        w.open('appearance')
        w.open('material', w.s(shape['material']['name']))
        w.leaf('texture-index', str(int(shape['material'].get('texture-index', 0))))
        diffuse = shape['material'].get('diffuse')
        if diffuse:
            w.leaf('diffuse', w.vec(diffuse[:4]))
        w.close()
        w.close()

        w.close()  # shape

    @staticmethod
    def _write_faceset(w, name, indices, per_line=30):
        w.open(name)
        for i in range(0, len(indices), per_line):
            chunk = indices[i:i+per_line]
            prefix = '( ' if i == 0 else '  '
            suffix = ' )' if i+per_line >= len(indices) else ''
            w.line(prefix + ' '.join(str(x) for x in chunk) + suffix)
        if not indices:
            w.line('( )')
        w.close()

    def _write_animset(self, w, name, times, tracks, frame_strings):
        w.open('animset', w.s(name))

        w.open('keyframe')
        w.open('keyframe')
        w.open('times')
        self._write_numbers(w, [str(t) for t in times])
        w.close()
        w.open('values')
        # Write frame strings for each keyframe index
        values = []
        for i in range(len(times)):
            values.append(frame_strings.get(i, ""))
        self._write_numbers(w, [w.s(v) for v in values])
        w.close()
        w.close()
        w.close()

        w.open('anims')
        w.open()
        for b in self.bones:
            track = tracks.get(b.name)
            if not track:
                continue
            w.open('anim')
            w.leaf('parent', w.s(b.name))
            w.open('frames')
            w.open('posquat')
            w.open()
            for (pos, quat) in track:
                w.line('( %s %s )' % (w.vec(pos), w.vec(quat)))
            w.close()
            w.close()
            w.close()
            w.close()
            w.close()
        w.close()
        w.close()
        w.close()

    @staticmethod
    def _write_numbers(w, items, per_line=20):
        for i in range(0, len(items), per_line):
            chunk = items[i:i+per_line]
            prefix = '( ' if i == 0 else '  '
            suffix = ' )' if i+per_line >= len(items) else ''
            w.line(prefix + ' '.join(chunk) + suffix)
        if not items:
            w.line('( )')

    def run(self):
        t0 = time.time()
        self.find_objects()
        self.gather_bones()
        shapes = self.extract_meshes()
        sockets = self.extract_sockets() if self.o.export_sockets else []
        anims = []
        for action in self.collect_actions():
            anims.append(self.sample_action(action))
        text = self.write(shapes, sockets, anims)
        with open(self.filepath, 'w', encoding='ascii', errors='replace', newline='\n') as f:
            f.write(text)
        self.op.report({'INFO'}, "Exported '%s': %d nodes, %d shapes, %d sockets, %d animations in %.2fs" %
                        (os.path.basename(self.filepath), len(self.bones), len(shapes), len(sockets), len(anims),
                         time.time()-t0))


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------
class IMPORT_OT_lta_jupiter(Operator, ImportHelper):
    bl_idname = "import_scene.lta_jupiter"
    bl_label = "Import LithTech LTA"
    bl_options = {'REGISTER', 'UNDO', 'PRESET'}

    filename_ext = ".lta"
    filter_glob: StringProperty(default="*.lta;*.ltc", options={'HIDDEN'})
    files: CollectionProperty(type=OperatorFileListElement, options={'HIDDEN', 'SKIP_SAVE'})
    directory: StringProperty(subtype='DIR_PATH', options={'HIDDEN', 'SKIP_SAVE'})

    scale: FloatProperty(name="Scale", default=1.0, min=0.0001, max=1000.0)
    import_anims: BoolProperty(name="Import Animations", default=True)
    anim_fps: FloatProperty(name="Animation FPS", default=30.0, min=1.0, max=240.0)
    skip_static_tracks: BoolProperty(name="Skip Unanimated Bones", default=False)
    import_sockets: BoolProperty(name="Import Sockets", default=True)
    import_uvs: BoolProperty(name="Import UVs", default=True)
    import_normals: BoolProperty(name="Import Custom Normals", default=True)
    import_colors: BoolProperty(name="Import Vertex Colors", default=True)
    import_materials: BoolProperty(name="Create Materials", default=True)
    texture_dir: StringProperty(name="Texture Folder", subtype='DIR_PATH', default="")
    rigid_parent_to_bones: BoolProperty(name="Parent Rigid Pieces to Bones", default=True)
    shade_smooth: BoolProperty(name="Shade Smooth", default=True)
    bone_display: EnumProperty(name="Bone Display", items=(('OCTAHEDRAL','Octahedral',''),('STICK','Stick','')), default='STICK')
    import_lods: BoolProperty(name="Import LODs", default=True, description="Import all LOD levels; otherwise only base LOD (distance 0)")

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        box = layout.box()
        box.label(text="Transform", icon='ORIENTATION_GLOBAL')
        box.prop(self, "scale")
        box = layout.box()
        box.label(text="Geometry", icon='MESH_DATA')
        box.prop(self, "import_uvs")
        box.prop(self, "import_normals")
        box.prop(self, "import_colors")
        box.prop(self, "shade_smooth")
        box.prop(self, "rigid_parent_to_bones")
        box.prop(self, "import_lods")
        box = layout.box()
        box.label(text="Materials", icon='MATERIAL')
        box.prop(self, "import_materials")
        sub = box.column()
        sub.enabled = self.import_materials
        sub.prop(self, "texture_dir")
        box = layout.box()
        box.label(text="Animation", icon='ARMATURE_DATA')
        box.prop(self, "import_anims")
        sub = box.column()
        sub.enabled = self.import_anims
        sub.prop(self, "anim_fps")
        sub.prop(self, "skip_static_tracks")
        box = layout.box()
        box.label(text="Extras", icon='EMPTY_ARROWS')
        box.prop(self, "import_sockets")
        box.prop(self, "bone_display")

    def execute(self, context):
        if context.object and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        paths = []
        if self.files and self.directory:
            paths = [os.path.join(self.directory, f.name) for f in self.files if f.name]
        if not paths and self.filepath:
            paths = [self.filepath]
        if not paths:
            self.report({'ERROR'}, "No file selected.")
            return {'CANCELLED'}
        ok = 0
        for path in paths:
            try:
                LTAImporter(self, context, path, self).run()
                ok += 1
            except LTAParseError as ex:
                self.report({'ERROR'}, "%s: %s" % (os.path.basename(path), ex))
            except Exception as ex:
                import traceback
                traceback.print_exc()
                self.report({'ERROR'}, "Unexpected error importing %s: %s" % (os.path.basename(path), ex))
        return {'FINISHED'} if ok else {'CANCELLED'}

class EXPORT_OT_lta_jupiter(Operator, ExportHelper):
    bl_idname = "export_scene.lta_jupiter"
    bl_label = "Export LithTech LTA"
    bl_options = {'REGISTER', 'PRESET'}

    filename_ext = ".lta"
    filter_glob: StringProperty(default="*.lta", options={'HIDDEN'})

    model_name: StringProperty(name="Model Name", default="")
    use_selection: BoolProperty(name="Selection Only", default=False)
    scale: FloatProperty(name="Scale", default=1.0, min=0.0001, max=10000.0)
    apply_modifiers: BoolProperty(name="Apply Modifiers", default=True)
    export_uvs: BoolProperty(name="Export UVs", default=True)
    export_normals: BoolProperty(name="Export Normals", default=True)
    export_colors: BoolProperty(name="Export Vertex Colors", default=False)
    export_weights: BoolProperty(name="Export Skin Weights", default=True)
    max_weights: IntProperty(name="Max Weights per Vertex", default=4, min=0, max=16)
    export_sockets: BoolProperty(name="Export Sockets", default=True)
    anim_mode: EnumProperty(name="Animations", items=(('ALL','All Actions',''),('ACTIVE','Active Action',''),('NONE','None','')), default='ALL')
    frame_step: IntProperty(name="Frame Step", default=1, min=1, max=10)
    add_base_anim: BoolProperty(name="Add 'base' Animation if None", default=True)
    write_node_flags: BoolProperty(name="Write Node Flags", default=True)
    write_preserved: BoolProperty(name="Write Preserved LTA Blocks", default=True)
    global_radius: FloatProperty(name="Global Radius", default=96.0, min=0.0)
    float_digits: IntProperty(name="Float Precision", default=6, min=3, max=9)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        box = layout.box()
        box.label(text="General", icon='EXPORT')
        box.prop(self, "model_name")
        box.prop(self, "use_selection")
        box.prop(self, "scale")
        box.prop(self, "float_digits")
        box = layout.box()
        box.label(text="Geometry", icon='MESH_DATA')
        box.prop(self, "apply_modifiers")
        box.prop(self, "export_uvs")
        box.prop(self, "export_normals")
        box.prop(self, "export_colors")
        box.prop(self, "export_weights")
        sub = box.column()
        sub.enabled = self.export_weights
        sub.prop(self, "max_weights")
        box = layout.box()
        box.label(text="Animation", icon='ARMATURE_DATA')
        box.prop(self, "anim_mode")
        sub = box.column()
        sub.enabled = self.anim_mode != 'NONE'
        sub.prop(self, "frame_step")
        box.prop(self, "add_base_anim")
        box = layout.box()
        box.label(text="LithTech Extras", icon='TOOL_SETTINGS')
        box.prop(self, "export_sockets")
        box.prop(self, "write_node_flags")
        box.prop(self, "write_preserved")
        box.prop(self, "global_radius")

    def execute(self, context):
        if context.object and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        try:
            LTAExporter(self, context, self.filepath, self).run()
        except ExportError as ex:
            self.report({'ERROR'}, str(ex))
            return {'CANCELLED'}
        except Exception as ex:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, "Unexpected error: %s" % ex)
            return {'CANCELLED'}
        return {'FINISHED'}

class IO_FH_lta_jupiter(bpy.types.FileHandler):
    bl_idname = "IO_FH_lta_jupiter"
    bl_label = "LithTech LTA"
    bl_import_operator = "import_scene.lta_jupiter"
    bl_file_extensions = ".lta"

    @classmethod
    def poll_drop(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D'


def menu_func_import(self, context):
    self.layout.operator(IMPORT_OT_lta_jupiter.bl_idname, text="LithTech Model (.lta)")
def menu_func_export(self, context):
    self.layout.operator(EXPORT_OT_lta_jupiter.bl_idname, text="LithTech Model (.lta)")

classes = (
    IMPORT_OT_lta_jupiter,
    EXPORT_OT_lta_jupiter,
    IO_FH_lta_jupiter,
)

def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)

def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)

if __name__ == "__main__":
    register()