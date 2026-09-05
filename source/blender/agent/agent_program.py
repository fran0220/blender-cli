# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""The session's program: `model.py`, its parameter block, steps, versions and prefix cache.

The program is the record of the scene. Every `exec` that changes data becomes a
`# step N` block; the agent edits the text and the process re-executes it from the
longest prefix whose memfile snapshot is still cached.
"""

import array
import ast
import hashlib
import json
import math
import os
import sys
import time
import traceback

import bpy

MARKER = "# blender-cli program"
BASE_PREFIX = "# base:"
STEP_PREFIX = "# step "
DEFAULT_BASE = "factory-empty"

# Modules whose results a re-run cannot reproduce.
NONDETERMINISTIC = frozenset(
    {"time", "datetime", "uuid", "secrets", "socket", "subprocess", "urllib",
     "requests", "http", "getpass", "tempfile", "webbrowser"})
# Modules that become reproducible once the program seeds them with a literal.
SEEDABLE = frozenset({"random", "numpy"})
# Attribute paths that are nondeterministic wherever they appear.
NONDETERMINISTIC_PATHS = frozenset(
    {"os.urandom", "os.environ", "os.getpid", "bpy.app.timers", "bpy.utils.time"})
# Calls that read a file the program can only replay from its own directory.
READERS = frozenset({"open", "bpy.ops.wm.open_mainfile", "bpy.ops.wm.append",
                     "bpy.ops.wm.link", "bpy.ops.wm.revert_mainfile"})
PATH_KEYWORDS = ("filepath", "filename", "file", "directory", "path")
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


def _literal(value):
    """Render a parameter value as Python source that `ast.literal_eval` reads back."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if value is None or isinstance(value, (bool, int)):
        return repr(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"Parameter values must be finite: {value!r}")
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_literal(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_literal(str(key))}: {_literal(item)}"
                               for key, item in value.items()) + "}"
    raise ValueError(f"Parameter values must be literals: {value!r}")


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":"), default=list)


def _blocks(text):
    """Split program text into its header and its `# step N` blocks, in order."""
    header, steps, current = [], [], None
    for line in text.splitlines():
        if line.startswith(STEP_PREFIX) and line[len(STEP_PREFIX):].strip().isdigit():
            current = []
            steps.append(current)
        elif current is None:
            header.append(line)
        else:
            current.append(line)
    def trim(lines):
        return "\n".join(lines).strip("\n").rstrip()
    return trim(header), [trim(block) for block in steps]


def _assignment(header, name="P"):
    """Return the top-level literal assignment node for `name`, or None."""
    found = None
    for node in ast.parse(header).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name for target in node.targets):
            found = node
        elif (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
              and node.target.id == name and node.value is not None):
            found = node
    return found


def _parameters(header):
    node = _assignment(header)
    if node is None:
        return {}
    try:
        value = ast.literal_eval(node.value)
    except ValueError:
        raise ValueError("The program's P assignment must be a literal dict") from None
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("The program's P assignment must be a dict with string keys")
    return value


def _without_parameters(header):
    """The header with its `P = {...}` statement removed, so values enter keys only once."""
    node = _assignment(header)
    if node is None:
        return header
    lines = header.splitlines()
    del lines[node.lineno - 1:node.end_lineno]
    return "\n".join(lines).strip("\n")


def _rewrite_parameters(header, params):
    line = "P = " + _literal(params)
    node = _assignment(header)
    lines = header.splitlines()
    if node is None:
        return "\n".join([*lines, line]).strip("\n")
    lines[node.lineno - 1:node.end_lineno] = [line]
    return "\n".join(lines).strip("\n")


def _base_of(header):
    for line in header.splitlines():
        if line.startswith(BASE_PREFIX):
            return line[len(BASE_PREFIX):].strip()
    return DEFAULT_BASE


def _base_code(base):
    if base == "factory-empty":
        return "bpy.ops.wm.read_factory_settings(use_empty=True)"
    if base == "factory":
        return "bpy.ops.wm.read_factory_settings()"
    if base.startswith("file "):
        path = base[len("file "):].strip()
        return ("bpy.ops.wm.open_mainfile(filepath=%s, load_ui=False, use_scripts=False)"
                % json.dumps(path, ensure_ascii=True))
    raise ValueError(f"Unknown program base: {base!r}; use factory-empty, factory or file PATH")


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


def _replayable_path(node):
    """A file argument is replayable when it is a literal path inside the program directory."""
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return False
    path = node.value
    if not path or path.startswith("//") or os.path.isabs(path):
        return False
    return not os.path.normpath(path).startswith(os.pardir)


def _reads_outside(call):
    name = _dotted(call.func)
    if name is None:
        return False
    if not (name in READERS
            or (name.startswith("bpy.data.") and name.endswith(".load"))
            or name.startswith("bpy.ops.import_")
            or (name.startswith("bpy.ops.") and name.endswith("_import"))):
        return False
    argument = call.args[0] if call.args else None
    for keyword in call.keywords:
        if keyword.arg in PATH_KEYWORDS:
            argument = keyword.value
            break
    return argument is None or not _replayable_path(argument)


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
    from agent_runtime import settings
    agent._native["flush"]()
    bpy.context.view_layer.update()
    stream = hashlib.sha256()

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
            feed("modifier", obj.name, modifier.name, sorted(settings(modifier).items()))
        for constraint in obj.constraints:
            feed("constraint", obj.name, constraint.name, sorted(settings(constraint).items()))
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
    for material in sorted(bpy.data.materials, key=lambda item: item.name):
        tree = material.node_tree
        feed("material", material.name, [round(value, 6) for value in material.diffuse_color],
             sorted((node.name, node.bl_idname) for node in tree.nodes) if tree else None,
             sorted((link.from_node.name, link.from_socket.identifier,
                     link.to_node.name, link.to_socket.identifier)
                    for link in tree.links) if tree else None)
    # Non-mesh data: the RNA walk, then the point buffers RNA collapses to references.
    for name in ("curves", "metaballs", "lattices", "armatures", "volumes",
                 "pointclouds", "hair_curves", "grease_pencils_v3", "grease_pencils"):
        collection = getattr(bpy.data, name, None)
        if collection is None:
            continue
        for item in sorted(collection, key=lambda entry: entry.name):
            feed("data", name, item.name, sorted(settings(item).items()))
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
    """One session's `model.py`, its version tree and its per-step snapshot cache."""

    def __init__(self, session, directory):
        self.session = session
        self.directory = directory
        self.recording = True
        self.cache = {}          # prefix key -> memfile snapshot id
        self.produced = {}       # version -> memfile snapshot id of its last full run
        self.divergent = set()   # versions whose re-run produced a different snapshot
        self.load()

    # ---- files -----------------------------------------------------------

    @property
    def path(self):
        return os.path.join(self.directory, "model.py")

    @property
    def index_path(self):
        return os.path.join(self.directory, "index.json")

    def version_path(self, version):
        return os.path.join(self.directory, "versions", version.split(":")[-1] + ".py")

    @property
    def modified(self):
        """Unix time of the program's newest version, or 0 when it has none."""
        return max((row["at"] for row in self.index["versions"]), default=0.0)

    def load(self):
        os.makedirs(os.path.join(self.directory, "versions"), exist_ok=True)
        self.index = {"versions": [], "current": None}
        if os.path.isfile(self.index_path):
            with open(self.index_path, encoding="utf-8") as stream:
                self.index = json.load(stream)
        self.current = self.index.get("current")
        if os.path.isfile(self.path):
            with open(self.path, encoding="utf-8") as stream:
                self.header, self.steps = _blocks(stream.read())
        else:
            base = "file " + bpy.data.filepath if bpy.data.filepath else "factory"
            self.header = f"{MARKER}\n{BASE_PREFIX} {base}\nP = {{}}"
            self.steps = []
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
        blocks = [self.header.rstrip()]
        blocks += [f"{STEP_PREFIX}{number}\n{code}" for number, code in enumerate(self.steps, 1)]
        return "\n\n".join(block for block in blocks if block) + "\n"

    @property
    def params(self):
        return _parameters(self.header)

    @property
    def base(self):
        return _base_of(self.header)

    @property
    def version(self):
        return self.current

    def bind(self):
        """Make the parameter block visible to code the agent runs outside a program run."""
        self.session.namespace["P"] = dict(self.params)

    def step_records(self):
        seeded = _seeded(self.header) or any(_seeded(code) for code in self.steps)
        return [{"n": number, "code": code, "reproducible": reproducible(code, seeded)}
                for number, code in enumerate(self.steps, 1)]

    @property
    def static_reproducible(self):
        seeded = _seeded(self.header) or any(_seeded(code) for code in self.steps)
        return reproducible(_without_parameters(self.header), seeded) and all(
            reproducible(code, seeded) for code in self.steps)

    @property
    def reproducible(self):
        return self.static_reproducible and self.current not in self.divergent

    # ---- prefix cache ----------------------------------------------------

    def key(self, count):
        """Content key of the state after `count` steps: the texts plus the parameters they read."""
        texts = [_without_parameters(self.header), *self.steps[:count]]
        names, everything = set(), False
        for text in texts:
            used = dependencies(text)
            if used is None:
                everything = True
            else:
                names |= used
        params = self.params
        read = params if everything else {name: params[name] for name in names if name in params}
        payload = "\0".join([MARKER, self.base, *texts, _canonical(read)])
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
            return False
        return True

    def _run_code(self, code, label):
        if code.strip():
            exec(compile(code, f"<program {label}>", "exec"), self.session.namespace)

    # ---- operations ------------------------------------------------------

    def run(self):
        """Re-execute from the longest cached prefix, caching the snapshot of each step.

        Raises `StepError` when a step fails. `Main` then returns to the pre-request
        state, while the program text keeps the edit that failed and the prefix cache
        keeps the steps that ran, so a corrected `set` resumes from them for free.
        """
        if "snapshot" not in self.session.native:
            raise ValueError("Re-executing the program requires an open session")
        entry, entry_index = self.session.current, getattr(self.session, "current_index", None)
        keys = [self.key(count) for count in range(len(self.steps) + 1)]
        begin = None
        for count in range(len(self.steps), -1, -1):
            snapshot = self.cache.get(keys[count])
            if snapshot is not None and self._restore(snapshot):
                begin = count
                break
        rebuilt = begin is None
        begin = 0 if rebuilt else begin
        ran = []
        try:
            if rebuilt:
                self._step(_base_code(self.base), "base", 0)
            # The header is the parameter block: re-run on every run, never touching Main.
            self._step(_without_parameters(self.header), "header", 0)
            self.bind()
            if rebuilt:
                self.cache[keys[0]] = self.session.snapshot(None, "program")
            for index in range(begin, len(self.steps)):
                self._step(self.steps[index], f"step {index + 1}", index + 1)
                self.cache[keys[index + 1]] = self.session.snapshot(None, "program")
                ran.append(index + 1)
        except StepError as error:
            # The kernel restores the session's current snapshot on a failed request.
            # Point it back at the pre-request state so the failed edit never becomes
            # the live scene; the prefix cache keeps every step that did run, because
            # a cache is not state.
            self.session.current = entry
            if entry_index is not None:
                self.session.current_index = entry_index
            error.agent_fields.update(version=self.current, cached_through=begin + len(ran))
            raise
        content = digest()
        if rebuilt:
            self._check_divergence(content)
        return {"version": self.current, "steps": len(self.steps), "digest": content,
                "from_step": begin + 1, "cached": begin, "ran": ran,
                "reproducible": self.reproducible}

    def _step(self, code, label, number):
        try:
            self._run_code(code, label)
        except Exception as error:
            # A step that re-runs the program reports the innermost step that failed.
            if _fatal(error) or isinstance(error, StepError):
                raise
            frames = traceback.extract_tb(error.__traceback__)
            inner = [frame for frame in frames if frame.filename.startswith("<program ")]
            line = getattr(error, "lineno", None) or (inner[-1].lineno if inner else None)
            raise StepError(number, error, line) from error

    def _check_divergence(self, content):
        """A full re-run landing on different content than the last one is not reproducible."""
        previous = self.produced.get(self.current)
        if previous is not None and previous != content:
            self.divergent.add(self.current)
        self.produced[self.current] = content

    def commit(self, message, label=None):
        """Write the current text as a version and make it current."""
        text = self.text
        version = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        path = self.version_path(version)
        if not os.path.isfile(path):
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(text)
        self.index["versions"].append(
            {"version": version, "parent": self.current, "label": label, "at": time.time(),
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
        ast.parse(text)
        header, steps = _blocks(text)
        _parameters(header)
        self.header, self.steps = header, steps
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
        self.header = _rewrite_parameters(self.header, params)
        self.commit("set params", label)
        return self.committed_run()

    def rollback(self, reference, label=None):
        version = self.resolve(reference)
        with open(self.version_path(version), encoding="utf-8") as stream:
            text = stream.read()
        self.header, self.steps = _blocks(text)
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

    def record_exec(self, code, before, after):
        """Append a successful `exec` as the next step, keeping its snapshot as that prefix."""
        code = code.strip()
        if not self.recording or not code:
            return None
        parent = self.key(len(self.steps))
        self.steps.append(code)
        version = self.commit("exec")
        if before is not None and self.cache.get(parent) == before:
            self.cache[self.key(len(self.steps))] = after
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
        result = program.run()
    if result["ran"]:
        # The last step's snapshot is this request's snapshot; the diff needs no second one.
        session.snapshot_taken = True
    return result


def record_hook(session, code, step):
    """`register_record_hook`: an `exec` that changed data becomes the program's next step."""
    program = attach(session)
    event = session.history[session.current_index] if session.history else {}
    program.record_exec(code, event.get("parent"), session.current)


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


def recover(program, session):
    """Rebuild the scene from the program when it is newer than the recovered autosave.

    The program is the truth only when recovering. A session opened on a file asked
    for that file, and replaying over it would destroy what the agent loaded.
    """
    if not program.steps:
        # An empty program starts in sync with the session, so the base prefix is cached.
        program.cache[program.key(0)] = session.current
        return
    if session.opened_file or session.recovered_from == "autosave":
        return
    autosave = previous_autosave(os.path.dirname(program.directory))
    if autosave and os.path.getmtime(autosave) >= program.modified:
        return
    try:
        program.run()
    except Exception as error:
        # A program that no longer runs must not make its directory unopenable.
        print(f"Agent program: recovery failed: {type(error).__name__}: {error}",
              file=sys.stderr, flush=True)
        return
    session.recovered_from = "program"


def register(session):
    """`PROVIDER_MODULES` entry point: install the `program` op, the recorder and the helper.

    A program belongs to a session. One-shot mode has no snapshot store, so it gets no
    program: a bare `blender-cli exec` must not leave a `model.py` in the working
    directory for the next session to replay.
    """
    if "snapshot" not in session.native:
        return
    import agent_runtime
    agent_runtime.register_op("program", program_op)
    agent_runtime.register_helper("program", helper)
    agent_runtime.register_record_hook(record_hook)
    recover(attach(session), session)
