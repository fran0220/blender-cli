# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""The session's structured command program, durable versions and prefix cache."""

import array
import ast
import hashlib
import json
import os
import sys
import time
import traceback

import bpy

DEFAULT_BASE = "factory-empty"
FACTORY_BASES = {DEFAULT_BASE, "factory-default"}

# Modules whose results a re-run cannot reproduce.
NONDETERMINISTIC = frozenset(
    {"time", "datetime", "uuid", "secrets", "socket", "subprocess", "urllib",
     "requests", "http", "getpass", "tempfile", "webbrowser"})
# Modules that become reproducible once the program seeds them with a literal.
SEEDABLE = frozenset({"random", "numpy"})
# Attribute paths that are nondeterministic wherever they appear.
NONDETERMINISTIC_PATHS = frozenset(
    {"os.urandom", "os.environ", "os.getpid", "bpy.app.timers", "bpy.utils.time"})
# External file reads are not captured by scene snapshots, even for relative paths.
READERS = frozenset({"open", "bpy.ops.wm.open_mainfile", "bpy.ops.wm.append",
                     "bpy.ops.wm.link", "bpy.ops.wm.revert_mainfile"})
# How to read a geometry attribute's values in bulk: property, array code, width.
ATTRIBUTE_BUFFERS = {
    "FLOAT": ("value", "f", 1), "INT": ("value", "i", 1), "INT8": ("value", "i", 1),
    "BOOLEAN": ("value", "b", 1), "FLOAT_VECTOR": ("vector", "f", 3),
    "FLOAT2": ("vector", "f", 2), "FLOAT_COLOR": ("color", "f", 4),
    "BYTE_COLOR": ("color", "f", 4), "QUATERNION": ("value", "f", 4),
    "INT32_2D": ("value", "i", 2), "FLOAT4X4": ("value", "f", 16),
}


def _topology(name):
    """Mesh attributes the vertex, edge, loop and polygon buffers already carry."""
    return name == "position" or name.startswith(".")


def _fatal(error):
    """Cancellation and interpreter exits end the request; they are not step failures."""
    from agent_runtime import Cancelled
    return isinstance(error, (Cancelled, KeyboardInterrupt, SystemExit))


class StepError(Exception):
    """A program step raised.

    `agent_type` and `lineno` are what the kernel's error event reports today.
    `agent_fields` carries what a corrected `set` needs — the step, the version that
    holds the failing text, and the prefix still cached — for the kernel to merge.
    """

    def __init__(self, step, error, line):
        super().__init__(f"step {step}: {error}")
        self.step = step
        self.agent_type = type(error).__name__
        self.lineno = line
        self.agent_fields = {"step": step}


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":"))


def _parse(text):
    model = json.loads(text)
    if not isinstance(model, dict) or set(model) != {"base", "params", "steps"}:
        raise ValueError("program requires exactly base, params and steps")
    base = model["base"]
    if not isinstance(base, str) or (base not in FACTORY_BASES and not os.path.isabs(base)):
        raise ValueError("program base must be factory-empty, factory-default or an absolute blend path")
    if not isinstance(model["params"], dict) or not isinstance(model["steps"], list):
        raise ValueError("program params must be an object and steps an array")
    _canonical(model)  # Reject non-finite JSON numbers before modifying the program.
    names = set()
    for step in model["steps"]:
        validate_step(step)
        name = step.get("as")
        if name is not None:
            if not isinstance(name, str) or not name or "." in name or name in names:
                raise ValueError("step as must be a unique nonempty name without dots")
            names.add(name)
    return model


def validate_step(step):
    from agent_contract import REQUESTS
    if not isinstance(step, dict) or not isinstance(step.get("op"), str):
        raise ValueError("program steps must be request objects with an op")
    if "id" in step or step["op"] in {
            "program", "session", "target", "fit", "cancel", "repl"}:
        raise ValueError("program steps cannot contain ids or control requests")
    if step["op"] not in REQUESTS:
        raise ValueError(f"Unknown program op: {step['op']!r}")
    if "as" in step and (not isinstance(step["as"], str) or not step["as"] or "." in step["as"]):
        raise ValueError("step as must be a nonempty name without dots")
    _validate_fields(step["op"], REQUESTS[step["op"]],
                     {key: value for key, value in step.items() if key not in {"op", "as"}})
    if step["op"] == "exec" and (not isinstance(step.get("code"), str) or "script" in step):
        raise ValueError("explicit exec steps require inline code")
    if step["op"] == "batch":
        if not isinstance(step.get("steps"), list):
            raise ValueError("batch steps must be an array")
        names = set()
        for child in step["steps"]:
            validate_step(child)
            if child["op"] == "batch":
                raise ValueError("nested batches are not allowed")
            name = child.get("as")
            if name is not None:
                if not isinstance(name, str) or not name or "." in name or name in names:
                    raise ValueError("batch step names must be unique and contain no dots")
                names.add(name)


def _validate_fields(where, spec, values):
    fields = spec["fields"]
    for key, value in values.items():
        if key not in fields:
            raise ValueError(f"Unknown field for {where}: {key!r}")
        _validate_value(f"{where}.{key}", fields[key], value)
    for key, field in fields.items():
        if field.get("required") and key not in values:
            raise ValueError(f"{where} requires {key}")
    exclusive = spec.get("exactly_one_of")
    if exclusive and sum(key in values for key in exclusive) != 1:
        raise ValueError(f"{where} requires exactly one of {' or '.join(exclusive)}")


def _validate_value(where, spec, value):
    """Use registry validation, deferring only literal substitution nodes."""
    from agent_contract import DEFS
    from agent_runtime import check_value
    if isinstance(value, dict) and set(value) in ({"$param"}, {"$ref"}):
        if not isinstance(next(iter(value.values())), str) or not next(iter(value.values())):
            raise ValueError(f"{where} substitution must name a parameter or prior result")
        return
    if "ref" in spec:
        spec = {"type": "object", **DEFS[spec["ref"]]}
    check_value(where, {key: item for key, item in spec.items() if key not in {"items", "fields"}}, value)
    if isinstance(value, list):
        if len(value) < spec.get("minItems", 0) or len(value) > spec.get("maxItems", len(value)):
            raise ValueError(f"{where} has an invalid array length")
        for index, item in enumerate(value):
            _validate_value(f"{where}[{index}]", spec.get("items", {}), item)
    elif isinstance(value, dict) and "fields" in spec:
        _validate_fields(where, spec, value)


def resolve_step(step, params, results):
    """Leave batch children to the runtime's ordered, locally scoped execution."""
    validate_step(step)
    return {key: (value if step["op"] == "batch" and key == "steps"
                  else resolve_values(value, params, results))
            for key, value in step.items() if key != "as"}


def resolve_values(value, params, results):
    """Resolve request values once; substituted user data is never interpreted again."""
    if isinstance(value, list):
        return [resolve_values(item, params, results) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"$param"}:
        if not isinstance(value["$param"], str):
            raise ValueError("$param must name a parameter")
        return json.loads(_canonical(params[value["$param"]]))
    if set(value) == {"$ref"}:
        if not isinstance(value["$ref"], str) or not value["$ref"]:
            raise ValueError("$ref must name a previous result")
        path = value["$ref"].split(".")
        result = results[path[0]]
        for part in path[1:]:
            result = result[int(part)] if isinstance(result, list) else result[part]
        return json.loads(_canonical(result))
    return {key: resolve_values(item, params, results) for key, item in value.items()}


def _has_exec(value):
    if isinstance(value, list):
        return any(_has_exec(item) for item in value)
    return isinstance(value, dict) and (value.get("op") == "exec" or
                                      any(_has_exec(item) for item in value.values()))


def _parameter_names(value):
    if isinstance(value, list):
        names = set()
        for item in value:
            used = _parameter_names(item)
            if used is None:
                return None
            names |= used
        return names
    if not isinstance(value, dict):
        return set()
    if set(value) == {"$param"}:
        return {value["$param"]} if isinstance(value["$param"], str) else None
    names = _parameter_names(list(value.values()))
    if value.get("op") == "exec":
        used = dependencies(value["code"])
        return None if used is None or names is None else names | used
    return names


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def dependencies(text):
    """Parameter names this text reads from `P`; None when `P` is used opaquely."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    names, keyed = set(), set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and node.value.id == "P" and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)):
            names.add(node.slice.value)
            keyed.add(id(node.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "P" and id(node) not in keyed:
            return None
    return names


def _reads_outside(call):
    name = _dotted(call.func)
    if name is None:
        return False
    return (name in READERS
            or (name.startswith("bpy.data.") and name.endswith(".load"))
            or name.startswith("bpy.ops.import_")
            or (name.startswith("bpy.ops.") and name.endswith("_import")))


def _seeded(text):
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and (_dotted(node.func) or "").endswith("seed")
                and node.args and isinstance(node.args[0], ast.Constant)):
            return True
    return False


def reproducible(text, seeded=False):
    """Whether a re-run replays this text: no unseeded randomness, clock, network or outside file.

    The verdict is static and conservative: anything the parser cannot resolve is
    reported as not reproducible.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        roots = []
        if isinstance(node, ast.Import):
            roots = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            roots = [(node.module or "").split(".")[0]]
        elif isinstance(node, (ast.Attribute, ast.Name)):
            name = _dotted(node)
            if name in NONDETERMINISTIC_PATHS:
                return False
            roots = [name.split(".")[0]] if name else []
        elif isinstance(node, ast.Call):
            if _dotted(node.func) == "input" or _reads_outside(node):
                return False
        for root in roots:
            if root in NONDETERMINISTIC:
                return False
            if root in SEEDABLE and not seeded:
                return False
    return True


def request_reproducible(step):
    """External files/caches and generic calls cannot be certified by scene undo."""
    op, action = step["op"], step.get("action")
    if isinstance(action, dict):
        return False
    if op == "exec":
        code = step["code"]
        return reproducible(code, _seeded(code)) and not any(
            isinstance(node, ast.Call) and (_dotted(node.func) == "open" or
            (_dotted(node.func) or "").startswith(("bpy.ops.wm.", "bpy.ops.export_",
                                                  "bpy.ops.ptcache.", "bpy.ops.fluid.",
                                                  "bpy.ops.render.", "bpy.ops.cachefile.")))
            for node in ast.walk(ast.parse(code)))
    if op == "batch":
        return all(request_reproducible(child) for child in step["steps"])
    if op in {"operator", "render", "simulation"} or (op == "data" and action == "call"):
        return False
    return not (op == "scene" and action in {"open", "save"})


def digest():
    """A content hash of `Main`: the same scene hashes the same in any process.

    Memfile snapshot IDs are process-local identities that carry allocation state, so
    two runs that build the same scene do not share one. This walks the data instead:
    every ID list, then object transforms and relations, mesh geometry buffers,
    material node graphs, and the RNA settings of modifiers, constraints and the
    non-mesh data types. It is what proves a partial re-execution reached the state a
    full run from the base would have reached.

    The RNA walk is `agent_runtime.settings`, the same one `inspect --full` uses, so
    what an agent can read is what the digest distinguishes. Meshes are excluded from
    it: their content is the geometry buffers below, and walking a million-vertex
    collection as RNA references would cost far more and say less.
    """
    # Evaluated transforms and edit-mode meshes are only current at a request boundary.
    import agent
    from agent_runtime import serialize, settings
    agent._native["flush"]()
    bpy.context.view_layer.update()
    stream = hashlib.sha256()

    def tunable(value):
        """The settings an agent can set.

        Read-only RNA is derived from the rest, and some of it is a measurement rather
        than a setting: a Nodes modifier reports its own `execution_time`, which would
        make two runs of one program disagree about the scene.
        """
        walked = settings(value)
        return sorted((name, walked[name]) for name in walked
                      if not value.bl_rna.properties[name].is_readonly)

    def feed(*parts):
        for part in parts:
            stream.update(repr(part).encode("utf-8"))
            stream.update(b"\0")

    def buffered(collection, attribute, code, width):
        buffer = array.array(code, bytes(len(collection) * width * array.array(code).itemsize))
        if len(buffer):
            collection.foreach_get(attribute, buffer)
        stream.update(buffer.tobytes())

    def attributes(group, *identity, covered=None):
        """Hash a geometry attribute domain: the values, not a list of references.

        `covered` names the attributes another walk already carries.
        """
        for name in sorted(group.keys()):
            if covered is not None and covered(name):
                continue
            attribute = group[name]
            feed("attribute", *identity, name, attribute.domain, attribute.data_type,
                 len(attribute.data))
            spelling = ATTRIBUTE_BUFFERS.get(attribute.data_type)
            if spelling:
                buffered(attribute.data, *spelling)

    def node_graph(tree, *identity):
        """A node tree's content: the nodes, what they are set to, and what they carry.

        An unlinked input socket's `default_value` is the number the agent typed, so a
        tree whose only difference is one socket is a different scene.
        """
        if tree is None:
            return
        for node in sorted(tree.nodes, key=lambda item: item.name):
            feed("node", *identity, node.name, node.bl_idname, tunable(node),
                 [(socket.identifier, socket.is_linked,
                   serialize(getattr(socket, "default_value", None)))
                  for socket in node.inputs])
        feed("links", *identity,
             sorted((link.from_node.name, link.from_socket.identifier,
                     link.to_node.name, link.to_socket.identifier) for link in tree.links))
        interface = getattr(tree, "interface", None)
        if interface is not None:
            feed("interface", *identity,
                 [(item.identifier, item.item_type, getattr(item, "in_out", None),
                   getattr(item, "socket_type", None),
                   serialize(getattr(item, "default_value", None)))
                  for item in interface.items_tree])

    for prop in sorted(bpy.data.bl_rna.properties, key=lambda item: item.identifier):
        if prop.type == "COLLECTION":
            items = getattr(bpy.data, prop.identifier)
            feed("ids", prop.identifier, sorted(getattr(item, "name", "") for item in items))
    for scene in sorted(bpy.data.scenes, key=lambda item: item.name):
        feed("scene", scene.name, scene.frame_current,
             scene.camera.name if scene.camera else None,
             sorted(obj.name for obj in scene.objects))
    for obj in sorted(bpy.data.objects, key=lambda item: item.name):
        feed("object", obj.name, obj.type,
             obj.parent.name if obj.parent else None,
             obj.data.name if obj.data else None,
             [round(value, 6) for row in obj.matrix_world for value in row],
             [slot.material.name if slot.material else None for slot in obj.material_slots])
        # A modifier or constraint that differs only in a numeric setting is a
        # different scene, so the settings themselves are hashed, not just the type.
        for modifier in obj.modifiers:
            feed("modifier", obj.name, modifier.name, modifier.type, tunable(modifier))
            # A Nodes modifier keeps its group's input values in a struct per socket,
            # which the settings walk can only report as a bare reference.
            inputs = getattr(getattr(modifier, "properties", None), "inputs", None)
            if inputs is not None:
                for prop in sorted(inputs.bl_rna.properties, key=lambda item: item.identifier):
                    if prop.type != "POINTER" or prop.identifier == "rna_type":
                        continue
                    # A geometry socket declares the property but carries no value.
                    value = getattr(inputs, prop.identifier, None)
                    if value is not None:
                        feed("gninput", obj.name, modifier.name, prop.identifier, tunable(value))
        for constraint in obj.constraints:
            feed("constraint", obj.name, constraint.name, constraint.type, tunable(constraint))
    for mesh in sorted(bpy.data.meshes, key=lambda item: item.name):
        feed("mesh", mesh.name, len(mesh.vertices), len(mesh.edges),
             len(mesh.loops), len(mesh.polygons),
             [layer.name for layer in mesh.uv_layers],
             [material.name if material else None for material in mesh.materials])
        buffered(mesh.vertices, "co", "f", 3)
        buffered(mesh.edges, "vertices", "i", 2)
        buffered(mesh.loops, "vertex_index", "i", 1)
        buffered(mesh.polygons, "loop_start", "i", 1)
        # Everything else on the mesh: UV and colour layers, sharpness, creases and
        # whatever geometry nodes stored by name. `position` and the dot-prefixed
        # topology attributes are the buffers above, so they are not read twice.
        attributes(mesh.attributes, "mesh", mesh.name, covered=_topology)
    # Every node tree is content: shader, geometry and compositor alike. A group's
    # socket value decides what the scene looks like even when nothing else moves.
    for material in sorted(bpy.data.materials, key=lambda item: item.name):
        feed("material", material.name, [round(value, 6) for value in material.diffuse_color])
        node_graph(material.node_tree, "material", material.name)
    for group in sorted(bpy.data.node_groups, key=lambda item: item.name):
        feed("group", group.name, group.bl_idname)
        node_graph(group, "group", group.name)
    for world in sorted(bpy.data.worlds, key=lambda item: item.name):
        feed("world", world.name, [round(value, 6) for value in world.color])
        node_graph(world.node_tree, "world", world.name)
    for scene in sorted(bpy.data.scenes, key=lambda item: item.name):
        node_graph(getattr(scene, "node_tree", None), "compositor", scene.name)
    # Non-mesh data: the RNA walk, then the point buffers RNA collapses to references.
    for name in ("curves", "metaballs", "lattices", "armatures", "volumes",
                 "pointclouds", "hair_curves", "grease_pencils_v3", "grease_pencils"):
        collection = getattr(bpy.data, name, None)
        if collection is None:
            continue
        for item in sorted(collection, key=lambda entry: entry.name):
            feed("data", name, item.name, tunable(item))
    for curve in sorted(bpy.data.curves, key=lambda item: item.name):
        for index, spline in enumerate(curve.splines):
            feed("spline", curve.name, index, spline.type,
                 len(spline.points), len(spline.bezier_points))
            buffered(spline.points, "co", "f", 4)
            for attribute in ("co", "handle_left", "handle_right"):
                buffered(spline.bezier_points, attribute, "f", 3)
    for ball in sorted(bpy.data.metaballs, key=lambda item: item.name):
        for index, element in enumerate(ball.elements):
            feed("element", ball.name, index, element.type, list(element.co), element.radius,
                 element.size_x, element.size_y, element.size_z, element.stiffness)
    for lattice in sorted(bpy.data.lattices, key=lambda item: item.name):
        buffered(lattice.points, "co_deform", "f", 3)
    # Grease pencil, point clouds and hair curves keep their geometry in attribute
    # domains that RNA reports as bare references, so the values are read in bulk.
    for pencil in sorted(getattr(bpy.data, "grease_pencils", ()), key=lambda item: item.name):
        for layer in sorted(pencil.layers, key=lambda item: item.name):
            feed("gplayer", pencil.name, layer.name, round(layer.opacity, 6),
                 layer.blend_mode, layer.hide,
                 [round(value, 6) for row in layer.matrix_local for value in row])
            for frame in layer.frames:
                drawing = frame.drawing
                feed("gpframe", pencil.name, layer.name, frame.frame_number,
                     frame.keyframe_type, len(drawing.curve_offsets))
                buffered(drawing.curve_offsets, "value", "i", 1)
                attributes(drawing.attributes, "gp", pencil.name, layer.name,
                           frame.frame_number)
    for name in ("pointclouds", "hair_curves"):
        for item in sorted(getattr(bpy.data, name, ()), key=lambda entry: entry.name):
            attributes(item.attributes, name, item.name)
    for armature in sorted(bpy.data.armatures, key=lambda item: item.name):
        for bone in sorted(armature.bones, key=lambda item: item.name):
            feed("bone", armature.name, bone.name,
                 bone.parent.name if bone.parent else None, bone.use_deform,
                 [round(value, 6) for value in bone.head_local],
                 [round(value, 6) for value in bone.tail_local])
    for camera in sorted(bpy.data.cameras, key=lambda item: item.name):
        feed("camera", camera.name, camera.type, round(camera.lens, 6),
             round(camera.ortho_scale, 6))
    for light in sorted(bpy.data.lights, key=lambda item: item.name):
        feed("light", light.name, light.type, round(light.energy, 6),
             [round(value, 6) for value in light.color])
    for collection in sorted(bpy.data.collections, key=lambda item: item.name):
        feed("collection", collection.name, sorted(obj.name for obj in collection.objects),
             sorted(child.name for child in collection.children))
    return "sha256:" + stream.hexdigest()


class Program:
    """One session's model.json, version tree and per-step snapshot/result cache."""

    def __init__(self, session, directory):
        self.session = session
        self.directory = directory
        self.recording = True
        self.replaying = False  # Runtime snapshots are transient while a step replays.
        self.cache = {}          # prefix key -> memfile snapshot id
        self.result_cache = {}   # prefix key -> JSON-only named results
        self.results = {}
        self.produced = {}       # version -> memfile snapshot id of its last full run
        self.divergent = set()   # versions whose re-run produced a different snapshot
        self.load()

    # ---- files -----------------------------------------------------------

    @property
    def path(self):
        return os.path.join(self.directory, "model.json")

    @property
    def index_path(self):
        return os.path.join(self.directory, "index.json")

    def version_path(self, version):
        return os.path.join(self.directory, "versions", version.split(":")[-1] + ".json")

    @property
    def modified(self):
        """Source time of the selected version, not an unrelated history branch."""
        return self.index["versions"][-1]["at"] if self.index["versions"] else 0.0

    def load(self):
        os.makedirs(os.path.join(self.directory, "versions"), exist_ok=True)
        self.index = {"versions": [], "current": None}
        if os.path.isfile(self.index_path):
            with open(self.index_path, encoding="utf-8") as stream:
                self.index = json.load(stream)
        self.current = self.index.get("current")
        if self.session.opened_file:
            # Explicit opens start a new baseline, even if the unrelated old source
            # is broken. Old versions remain available for deliberate rollback.
            self.model = {"base": os.path.abspath(self.session.opened_file), "params": {}, "steps": []}
            self.commit("open file")
        elif os.path.isfile(self.path):
            source_time = os.stat(self.path).st_mtime_ns
            with open(self.path, encoding="utf-8") as stream:
                self.model = _parse(stream.read())
            version = "sha256:" + hashlib.sha256(self.text.encode("utf-8")).hexdigest()
            if self.current != version or not os.path.isfile(self.version_path(version)):
                self.commit("load source", at=source_time / 1e9)
                os.utime(self.path, ns=(source_time, source_time))
        else:
            base = bpy.data.filepath or ("factory-default" if bpy.data.objects else DEFAULT_BASE)
            self.model = {"base": base, "params": {}, "steps": []}
            self.write()
        self.bind()

    def write(self):
        for path, text in ((self.path, self.text), (self.index_path, _canonical(self.index))):
            temporary = path + "@"
            with open(temporary, "w", encoding="utf-8") as stream:
                stream.write(text)
            os.replace(temporary, path)

    # ---- text ------------------------------------------------------------

    @property
    def text(self):
        return json.dumps(self.model, sort_keys=True, indent=2, allow_nan=False) + "\n"

    @property
    def steps(self):
        return self.model["steps"]

    @property
    def params(self):
        return json.loads(_canonical(self.model["params"]))

    @property
    def base(self):
        return self.model["base"]

    @property
    def version(self):
        return self.current

    def bind(self):
        """Make the parameter block visible to code the agent runs outside a program run."""
        self.session.namespace["P"] = dict(self.params)

    def step_records(self):
        return [{"n": number, "request": step, "reproducible": request_reproducible(step)}
                for number, step in enumerate(self.steps, 1)]

    @property
    def static_reproducible(self):
        return self.base in FACTORY_BASES and all(request_reproducible(step) for step in self.steps)

    @property
    def reproducible(self):
        return self.static_reproducible and self.current not in self.divergent

    # ---- prefix cache ----------------------------------------------------

    def key(self, count):
        """Earlier requests include every producer of a reference, transitively."""
        steps = self.steps[:count]
        names = _parameter_names(steps)
        params = self.params
        read = params if names is None else {name: params[name] for name in names if name in params}
        payload = _canonical([self.base, steps, read])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _restore(self, snapshot):
        if snapshot == self.session.current:
            return True
        try:
            self.session.rollback(snapshot)
        except Exception as error:
            if _fatal(error):
                raise
            # An evicted memfile invalidates every prefix that named it.
            self.cache = {key: value for key, value in self.cache.items() if value != snapshot}
            self.result_cache = {key: value for key, value in self.result_cache.items()
                                 if key in self.cache}
            return False
        return True

    def _cache(self, count):
        key = self.key(count)
        self.cache[key] = self.session.snapshot(None, "program")
        self.result_cache[key] = json.loads(_canonical(self.results))

    # ---- operations ------------------------------------------------------

    def run(self, from_step=None):
        """Re-execute from the longest cached prefix, caching the snapshot of each step.

        Raises `StepError` when a step fails. `Main` then returns to the pre-request
        state, while the program text keeps the edit that failed and the prefix cache
        keeps the steps that ran, so a corrected `set` resumes from them for free.
        """
        if "snapshot" not in self.session.native:
            raise ValueError("Re-executing the program requires an open session")
        if from_step is not None and (type(from_step) is not int or
                                      not 1 <= from_step <= max(1, len(self.steps))):
            raise ValueError("from_step must name a program step")
        entry, entry_index = self.session.current, getattr(self.session, "current_index", None)
        keys = [self.key(count) for count in range(len(self.steps) + 1)]
        # Python globals can contain arbitrary live RNA references. Memfiles cannot
        # restore those globals: replay from before the first extension step instead.
        limit = next((i for i, step in enumerate(self.steps)
                      if _has_exec(step) or not request_reproducible(step)), len(self.steps))
        if from_step is not None:
            limit = min(limit, from_step - 1)
        if self.base not in FACTORY_BASES:
            limit = -1  # A file's content may have changed without its path changing.
        begin = None
        for count in range(limit, -1, -1):
            snapshot = self.cache.get(keys[count])
            if (snapshot is not None and keys[count] in self.result_cache
                    and self._restore(snapshot)):
                begin = count
                break
        rebuilt = begin is None
        begin = 0 if rebuilt else begin
        ran = []
        from agent_runtime import fresh_namespace
        self.session.namespace = fresh_namespace()
        self.results = {} if rebuilt else json.loads(_canonical(self.result_cache[keys[begin]]))
        self.bind()
        try:
            if rebuilt:
                self._step({"op": "scene", "action": "reset", "empty": self.base == DEFAULT_BASE}
                           if self.base in FACTORY_BASES else
                           {"op": "scene", "action": "open", "path": self.base}, 0)
                self._cache(0)
            for index in range(begin, len(self.steps)):
                self._step(self.steps[index], index + 1)
                self._cache(index + 1)
                ran.append(index + 1)
        except BaseException as error:
            # The kernel restores the session's current snapshot on a failed request.
            # Point it back at the pre-request state so the failed edit never becomes
            # the live scene; the prefix cache keeps every step that did run, because
            # a cache is not state.
            self.session.current = entry
            if entry_index is not None:
                self.session.current_index = entry_index
            self.session.namespace = fresh_namespace()
            self.bind()
            self.results = {}
            if isinstance(error, StepError):
                error.agent_fields.update(version=self.current, cached_through=begin + len(ran))
            raise
        content = digest()
        if rebuilt:
            self._check_divergence(content)
        return {"version": self.current, "steps": len(self.steps), "digest": content,
                "from_step": begin + 1, "cached": begin, "ran": ran,
                "reproducible": self.reproducible}

    def _step(self, request, number):
        previous_replaying = self.replaying
        self.replaying = True
        try:
            from agent_runtime import execute_step
            result = execute_step(request, self.session, results=self.results)
            if "as" in request:
                self.results[request["as"]] = json.loads(_canonical(result))
        except Exception as error:
            # A step that re-runs the program reports the innermost step that failed.
            if _fatal(error) or isinstance(error, StepError):
                raise
            frames = traceback.extract_tb(error.__traceback__)
            inner = [frame for frame in frames if frame.filename.startswith(("<program ", "<agent>"))]
            line = getattr(error, "lineno", None) or (inner[-1].lineno if inner else None)
            raise StepError(number, error, line) from error
        finally:
            self.replaying = previous_replaying

    def _check_divergence(self, content):
        """A full re-run landing on different content than the last one is not reproducible."""
        previous = self.produced.get(self.current)
        if previous is not None and previous != content:
            self.divergent.add(self.current)
        self.produced[self.current] = content

    def commit(self, message, label=None, at=None):
        """Write the current text as a version and make it current."""
        text = self.text
        version = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        path = self.version_path(version)
        if not os.path.isfile(path):
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(text)
        self.index["versions"].append(
            {"version": version, "parent": self.current, "label": label,
             "at": time.time() if at is None else at,
             "steps": len(self.steps), "reproducible": self.static_reproducible,
             "message": message, "failed": False})
        self.index["current"] = self.current = version
        self.write()
        return version

    def committed_run(self):
        """Run the version just committed, marking its row when a step fails."""
        try:
            return self.run()
        except StepError as error:
            self.index["versions"][-1].update(failed=True, step=error.step, line=error.lineno)
            self.write()
            raise

    def set_text(self, text, message="set", label=None):
        self.model = _parse(text)
        self.commit(message, label)
        return self.committed_run()

    def patch(self, old, new, label=None):
        text = self.text
        matches = text.count(old)
        if matches != 1:
            raise ValueError(
                f"program patch requires exactly one match; {matches} found"
                if matches else "program patch found no match for old")
        return self.set_text(text.replace(old, new), "patch", label)

    def set_params(self, values, label=None):
        """Replace named parameters and re-execute only the steps that read them."""
        params = self.params
        unknown = [name for name in values if not isinstance(name, str)]
        if unknown:
            raise ValueError(f"Parameter names must be strings: {unknown!r}")
        params.update(values)
        _canonical(params)
        self.model["params"] = params
        self.commit("set params", label)
        return self.committed_run()

    def rollback(self, reference, label=None):
        version = self.resolve(reference)
        with open(self.version_path(version), encoding="utf-8") as stream:
            text = stream.read()
        self.model = _parse(text)
        self.commit("rollback", label)
        return self.committed_run()

    def resolve(self, reference):
        if not isinstance(reference, str) or not reference:
            raise KeyError("program rollback requires a version or label")
        rows = self.index["versions"]
        for row in reversed(rows):
            if reference in (row["version"], row["label"]):
                return row["version"]
        digest = reference.split(":")[-1]
        matches = {row["version"] for row in rows if row["version"].split(":")[-1].startswith(digest)}
        if len(matches) == 1:
            return matches.pop()
        raise KeyError(f"Unknown program version: {reference!r}")

    def label(self, name):
        """Name the current version, or answer None when the program has none yet."""
        if not self.index["versions"]:
            return None
        self.index["versions"][-1]["label"] = name
        self.write()
        return self.current

    def record_request(self, request, before, after):
        """Record original requests, never Python wrappers or resolved batch commands."""
        if not self.recording:
            return None
        request = json.loads(_canonical({key: value for key, value in request.items()
                                        if key not in {"id", "record", "feedback"}}))
        validate_step(request)
        parent = self.key(len(self.steps))
        self.steps.append(request)
        version = self.commit(request["op"])
        # The recording hook has no handler result. Named steps must replay rather
        # than fabricating a reference value from the scene or a stale prior result.
        if ("as" not in request and before is not None and self.cache.get(parent) == before
                and parent in self.result_cache):
            key = self.key(len(self.steps))
            self.cache[key] = after
            self.result_cache[key] = json.loads(_canonical(self.result_cache[parent]))
        return version


def session_root(session):
    """The session's `.blender-cli` directory, fixed at open before any `os.chdir`."""
    directory = getattr(session, "directory", None)
    if directory:
        return directory
    snapshots = getattr(session, "snapshot_directory", None)
    return os.path.dirname(snapshots) if snapshots else os.path.abspath(".blender-cli")


def attach(session, directory=None):
    program = getattr(session, "program", None)
    if program is None:
        program = Program(session, os.path.join(directory or session_root(session), "program"))
        session.program = program
    return program


def program_op(request, session, emit):
    """`register_op("program", …)`: get, set, patch, run, history, rollback, record."""
    program = attach(session)
    action = request["action"]
    required = {"set": ("text",), "patch": ("old", "new"),
                "rollback": ("version",), "record": ("on",)}.get(action, ())
    missing = [name for name in required if name not in request]
    if missing:
        raise ValueError(f"program {action} requires {', '.join(missing)}")
    if action == "get":
        return {"text": program.text, "params": program.params,
                "steps": program.step_records(), "version": program.version,
                "base": program.base, "record": program.recording,
                "digest": digest(), "reproducible": program.reproducible}
    if action == "history":
        return {"versions": program.index["versions"], "current": program.version}
    if action == "record":
        program.recording = request["on"]
        return {"record": program.recording}
    label = request.get("label")
    if action == "set":
        result = program.set_text(request["text"], "set", label)
    elif action == "patch":
        result = program.patch(request["old"], request["new"], label)
    elif action == "rollback":
        result = program.rollback(request["version"], label)
    else:
        result = program.run(from_step=request.get("from_step"))
    if result["ran"]:
        # The last step's snapshot is this request's snapshot; the diff needs no second one.
        session.snapshot_taken = True
    return result


def record_hook(session, request, step):
    """`register_record_hook`: record a successful mutating request verbatim."""
    program = attach(session)
    program.record_request(request, session.previous_snapshot, session.current)


def helper(session=None):
    """Backs `agent.program()`; the registry passes the session first."""
    if session is None:
        import agent
        session = agent._active()
    program = attach(session)
    return {"text": program.text, "params": program.params, "steps": program.step_records(),
            "version": program.version, "reproducible": program.reproducible}


def previous_autosave(root):
    """The newest recovery file another process left in this session directory."""
    mine = f"autosave-{os.getpid()}.blend"
    if not os.path.isdir(root):
        return None
    candidates = [os.path.join(root, name) for name in os.listdir(root)
                  if name.startswith("autosave-") and name.endswith(".blend") and name != mine]
    return max(candidates, key=os.path.getmtime, default=None)


def load_autosave(session, path):
    """Load a recovery file exactly as `session open --file` would, sidecar included."""
    bpy.ops.wm.open_mainfile(filepath=path, load_ui=False, use_scripts=False)
    metadata_path = os.path.splitext(path)[0] + ".json"
    if os.path.isfile(metadata_path):
        with open(metadata_path, encoding="utf-8") as stream:
            metadata = json.load(stream)
        session.native["restore_metadata"](metadata["filepath"], metadata["dirty"])
    bpy.context.view_layer.update()
    session.snapshot(None, "recover")


def at_base(program):
    """Called during attach, before any request changes the initial scene."""
    if bpy.data.filepath:
        return program.base == bpy.data.filepath
    return program.base == ("factory-default" if bpy.data.objects else DEFAULT_BASE)


def recover(program, session):
    """Recover the newest source, and never come back empty-handed.

    A reopen that finds work left behind restores it: the program when its newest
    version is newer than the recovery file, the recovery file otherwise, and the
    recovery file again when replaying the program fails. `recovered_from` is null
    only when there was nothing to recover.

    A session opened on a file is not a recovery. The agent named what it wanted, and
    replaying over it would destroy what it loaded.
    """
    autosave = None if session.opened_file else previous_autosave(
        os.path.dirname(program.directory))
    if program.current and not session.opened_file and (
            autosave is None or os.path.getmtime(autosave) < program.modified):
        try:
            program.run()
        except Exception as error:
            if _fatal(error) or autosave is None:
                # A failed canonical source is not an empty session. Without a
                # recovery file, propagate the replay error and fail session open.
                raise
            # A program that no longer runs falls back to the file, never to nothing.
            print(f"Agent program: replay failed, recovering the autosave instead: "
                  f"{type(error).__name__}: {error}", file=sys.stderr, flush=True)
        else:
            session.recovered_from = "program"
            return
    if autosave is not None:
        load_autosave(session, autosave)
        # The program's text is still the record; its prefix cache starts empty,
        # because the scene now on screen is the file's, not any prefix of the program.
        program.cache.clear()
        program.result_cache.clear()
        program.results.clear()
        session.recovered_from = "autosave"
        return
    if not program.steps and at_base(program):
        # An empty program starts in sync with the session, so the base prefix is cached.
        program.cache[program.key(0)] = session.current
        program.result_cache[program.key(0)] = {}


def register(session):
    """`PROVIDER_MODULES` entry point: install the `program` op, the recorder and the helper.

    A program belongs to a session. One-shot mode has no snapshot store, so it gets no
    program: a bare `blender-cli exec` must not leave a `model.json` in the working
    directory for the next session to replay.
    """
    if "snapshot" not in session.native:
        return
    import agent_runtime
    agent_runtime.register_op("program", program_op)
    agent_runtime.register_helper("program", helper)
    agent_runtime.register_record_hook(record_hook)
    recover(attach(session), session)
