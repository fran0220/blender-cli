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


def _fatal(error):
    """Cancellation and interpreter exits end the request; they are not step failures."""
    from agent_runtime import Cancelled
    return isinstance(error, (Cancelled, KeyboardInterrupt, SystemExit))


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
    every ID list, then object transforms and relations, mesh geometry buffers and
    material node graphs. It is what proves a partial re-execution reached the state a
    full run from the base would have reached.
    """
    # Evaluated transforms and edit-mode meshes are only current at a request boundary.
    import agent
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
             [(modifier.type, modifier.name) for modifier in obj.modifiers],
             [slot.material.name if slot.material else None for slot in obj.material_slots])
    for mesh in sorted(bpy.data.meshes, key=lambda item: item.name):
        feed("mesh", mesh.name, len(mesh.vertices), len(mesh.edges),
             len(mesh.loops), len(mesh.polygons),
             [layer.name for layer in mesh.uv_layers],
             [material.name if material else None for material in mesh.materials])
        buffered(mesh.vertices, "co", "f", 3)
        buffered(mesh.edges, "vertices", "i", 2)
        buffered(mesh.loops, "vertex_index", "i", 1)
        buffered(mesh.polygons, "loop_start", "i", 1)
    for material in sorted(bpy.data.materials, key=lambda item: item.name):
        tree = material.node_tree
        feed("material", material.name, [round(value, 6) for value in material.diffuse_color],
             sorted((node.name, node.bl_idname) for node in tree.nodes) if tree else None,
             sorted((link.from_node.name, link.from_socket.identifier,
                     link.to_node.name, link.to_socket.identifier)
                    for link in tree.links) if tree else None)
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

    def run(self, from_step=None):
        """Re-execute from the longest cached prefix (or from `from_step`) and cache each step."""
        start = time.perf_counter()
        keys = [self.key(count) for count in range(len(self.steps) + 1)]
        limit = len(self.steps) if from_step is None else max(0, min(from_step - 1, len(self.steps)))
        begin = None
        for count in range(limit, -1, -1):
            snapshot = self.cache.get(keys[count])
            if snapshot is not None and self._restore(snapshot):
                begin = count
                break
        rebuilt = begin is None
        begin = 0 if rebuilt else begin
        result = {"ok": True, "ran": [], "from_step": begin + 1, "cached": begin}
        try:
            if rebuilt:
                self._run_code(_base_code(self.base), "base")
            # The header is the parameter block: re-run on every run, never touching Main.
            self._run_code(_without_parameters(self.header), "header")
            self.bind()
            if rebuilt:
                self.cache[keys[0]] = self.session.snapshot(None, "program")
        except Exception as error:
            if _fatal(error):
                raise
            result.update(ok=False, error=self._error(error, 0), version=self.current,
                          steps=len(self.steps), digest=digest(),
                          reproducible=self.reproducible,
                          ms=(time.perf_counter() - start) * 1000)
            return result
        for index in range(begin, len(self.steps)):
            try:
                self._run_code(self.steps[index], f"step {index + 1}")
            except Exception as error:
                if _fatal(error):
                    raise
                result.update(ok=False, error=self._error(error, index + 1))
                break
            self.cache[keys[index + 1]] = self.session.snapshot(None, "program")
            result["ran"].append(index + 1)
        content = digest()
        if result["ok"] and rebuilt:
            self._check_divergence(content)
        result.update(version=self.current, steps=len(self.steps), digest=content,
                      reproducible=self.reproducible,
                      ms=(time.perf_counter() - start) * 1000)
        return result

    def _check_divergence(self, content):
        """A full re-run landing on different content than the last one is not reproducible."""
        previous = self.produced.get(self.current)
        if previous is not None and previous != content:
            self.divergent.add(self.current)
        self.produced[self.current] = content

    @staticmethod
    def _error(error, step):
        frames = traceback.extract_tb(error.__traceback__)
        inner = [frame for frame in frames if frame.filename.startswith("<program ")]
        return {"type": type(error).__name__, "message": str(error), "step": step,
                "line": getattr(error, "lineno", None) or (inner[-1].lineno if inner else None)}

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
             "message": message})
        self.index["current"] = self.current = version
        self.write()
        return version

    def set_text(self, text, message="set"):
        ast.parse(text)
        header, steps = _blocks(text)
        _parameters(header)
        self.header, self.steps = header, steps
        self.commit(message)
        return self.run()

    def patch(self, old, new):
        text = self.text
        matches = text.count(old)
        if matches != 1:
            raise ValueError(
                f"program patch requires exactly one match; {matches} found"
                if matches else "program patch found no match for old")
        return self.set_text(text.replace(old, new), "patch")

    def set_params(self, values):
        """Replace named parameters and re-execute only the steps that read them."""
        params = self.params
        unknown = [name for name in values if not isinstance(name, str)]
        if unknown:
            raise ValueError(f"Parameter names must be strings: {unknown!r}")
        params.update(values)
        self.header = _rewrite_parameters(self.header, params)
        self.commit("set params")
        return self.run()

    def rollback(self, reference):
        version = self.resolve(reference)
        with open(self.version_path(version), encoding="utf-8") as stream:
            text = stream.read()
        self.header, self.steps = _blocks(text)
        self.commit("rollback", label=None)
        return self.run()

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
        if not self.index["versions"]:
            raise KeyError("The program has no version to label")
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

    def step(self, code):
        """Execute `code` as the next step: the recording path without an `exec` request."""
        before = self.session.current
        self._run_code(code, f"step {len(self.steps) + 1}")
        after = self.session.snapshot(None, "program")
        return self.record_exec(code, before, after)


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


def on_session_open(session, directory=None, previous_autosave=None):
    """Rebuild the scene from the program when it is newer than the recovered autosave."""
    program = attach(session, directory)
    if not program.steps:
        program.cache[program.key(0)] = session.current
        return {}
    if (previous_autosave and os.path.isfile(previous_autosave)
            and os.path.getmtime(previous_autosave) >= program.modified):
        return {}
    result = program.run()
    recovery = {"recovered_from": "program", "program": program.current,
                "steps": len(program.steps), "ran": result["ran"]}
    if not result["ok"]:
        recovery["error"] = result["error"]
    return recovery


def record_from_exec(session, code, before, after, diff):
    """Recording hook for the `exec` path: record code that changed data."""
    if not code or not any(diff.get(group) for group in ("added", "changed", "removed")):
        return None
    return attach(session).record_exec(code, before, after)


def helper(session=None):
    """`agent.program()`: the program as the agent sees it."""
    if session is None:
        import agent
        session = agent._active()
    program = attach(session)
    return {"text": program.text, "params": program.params, "steps": program.step_records(),
            "version": program.version, "reproducible": program.reproducible}


def request(session, action, **fields):
    """The `program` request: get, set, patch, run, history, rollback, record."""
    program = attach(session)
    accepted = {"get": (), "set": ("text",), "patch": ("old", "new"), "run": ("from_step",),
                "history": (), "rollback": ("version",), "record": ("on",)}
    if action not in accepted:
        raise ValueError("program requires an action: " + "|".join(accepted))
    unexpected = sorted(set(fields) - set(accepted[action]))
    if unexpected:
        raise ValueError(f"program {action} does not accept {', '.join(unexpected)}")
    if action == "get":
        return {"ok": True, "text": program.text, "params": program.params,
                "steps": program.step_records(), "version": program.version,
                "base": program.base, "record": program.recording,
                "digest": digest(), "reproducible": program.reproducible}
    if action == "history":
        return {"ok": True, "versions": program.index["versions"], "current": program.version}
    if action == "record":
        on = fields["on"]
        if not isinstance(on, bool):
            raise ValueError("program record requires on: true|false")
        program.recording = on
        return {"ok": True, "record": on}
    if action == "set":
        result = program.set_text(fields["text"])
    elif action == "patch":
        result = program.patch(fields["old"], fields["new"])
    elif action == "rollback":
        result = program.rollback(fields["version"])
    else:
        result = program.run(fields.get("from_step"))
    return {**result, "version": program.version, "steps": len(program.steps)}
