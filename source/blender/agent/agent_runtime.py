# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Request dispatch, the session, and the feedback provider registry.

One request object arrives as a JSON line; its consequences leave as JSON-line
events, written by C++ the moment this module produces them. The same code
serves the session socket, `repl` stdio and one-shot verbs; the folded envelope
that one-shot verbs print is derived from those events, in C++, by one function.

Extension points. Other workstreams plug in through these names and never edit
the dispatch or the event assembly:

  REQUESTS, EVENTS, DEFS   The contract as data. The validator here and
                           `describe channel|schema` (workstream D) read the
                           same tables; nothing else defines the contract.
  register_provider(p)     Feedback providers. `p.name`, `p.order`
                           (diff=100, perception=200, objective=300,
                           image=400), `p.before(request, session)`,
                           `p.after(request, session, emit)`. A provider that
                           raises becomes a `log` event on stderr and never
                           fails the request. Workstream F registers
                           perception and image; workstream T registers
                           objective.
  PROVIDER_MODULES         Module names imported once per session, each
                           exposing `register(session)`. This is where F, T
                           and P attach without touching this file.
  register_op(op, handler) Replaces a request handler. `handler(request,
                           session, emit) -> dict` returns the op-specific
                           fields of its `done` event and may emit events of
                           its own. Workstream P owns `program`; workstream T
                           owns `target` and `fit`.
  register_helper(name, f) Backs an `agent.<name>` helper.
  register_record_hook(f)  `f(session, code, step)` runs after an `exec` whose
                           diff is non-empty and whose `record` is not false.
                           Workstream P records the program step there.
  Session.last_perception  Written by F's perception provider, read by F's
                           image provider.
  Session.last_diff        The diff dict of the request being answered.
  Session.request_feedback The feedback policy in force for this request.
  Session.targets          Target storage, owned by workstream T.
  Session.recovered_from   "autosave", "program" or None; reported by
                           `session status` and by `session open`.
"""

import ast
import contextlib
import io
import json
import math
import os
import sys
import time
import tokenize
import traceback

import bpy
import bmesh
import mathutils
import agent


# ---------------------------------------------------------------------------
# The contract as data: requests, events and the shapes they share.

DEFS = {
    "image_policy": {
        "doc": "How pictures of a change are returned.",
        "fields": {
            "mode": {"type": "string", "enum": ["delta", "full", "off"], "default": "delta",
                     "doc": "delta crops the changed region; off sends no image event."},
            "threshold": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.002,
                          "doc": "Changed-pixel fraction below which no image is sent."},
            "views": {"type": "array", "items": {"type": "string"}, "default": ["front"],
                      "doc": "Budget views rendered for feedback."},
            "pass": {"type": "string", "default": "color", "doc": "Render pass of the image."},
            "size": {"type": "integer", "minimum": 1, "default": 256,
                     "doc": "Budget tile size in pixels."},
            "overlay": {"type": "boolean", "default": True,
                        "doc": "Emit a before/after overlay of the changed region."},
        },
    },
    "feedback_policy": {
        "doc": "The per-session feedback budget.",
        "fields": {
            "perception": {"type": "boolean", "default": True,
                           "doc": "Emit a perception event after every action."},
            "objective": {"type": "boolean", "default": True,
                          "doc": "Score registered targets after every action."},
            "image": {"ref": "image_policy", "doc": "Image budget."},
        },
    },
    "fit_param": {
        "doc": "One searched parameter: a program parameter or an RNA path.",
        "fields": {
            "name": {"type": "string", "doc": "Program parameter name in the P block."},
            "path": {"type": "string", "doc": "RNA path assigned directly instead."},
            "min": {"type": "number", "required": True, "doc": "Lower bound."},
            "max": {"type": "number", "required": True, "doc": "Upper bound."},
        },
    },
    "fit_objective": {
        "doc": "What the search minimises or maximises.",
        "fields": {
            "target": {"type": "string", "doc": "Single target name."},
            "targets": {"type": "array", "items": {"type": "string"},
                        "doc": "Several target names, combined by weights."},
            "weights": {"type": "array", "items": {"type": "number"}, "doc": "Per-target weights."},
            "metric": {"type": "string", "enum": ["iou", "chamfer", "ssim", "hist"],
                       "default": "iou", "doc": "Metric to optimise."},
            "code": {"type": "string", "doc": "Python expression returning a score instead."},
        },
    },
    "fit_budget": {
        "doc": "The bound on a search.",
        "fields": {
            "evals": {"type": "integer", "minimum": 1, "default": 200,
                      "doc": "Maximum evaluations."},
            "seconds": {"type": "number", "minimum": 0, "exclusive_minimum": True,
                        "doc": "Wall-clock bound."},
            "size": {"type": "integer", "minimum": 1, "default": 128,
                     "doc": "Tile size used while searching."},
        },
    },
}

REQUESTS = {
    "exec": {
        "doc": "Run Python in the session namespace and push its consequences.",
        "mutates": True,
        "fields": {
            "code": {"type": "string",
                     "doc": "Python source; exactly one of code or script is required."},
            "script": {"type": "string",
                       "doc": "Absolute path of a Python file to run instead of code."},
            "record": {"type": "boolean", "default": True,
                       "doc": "Record this statement as the program's next step."},
            "timeout": {"type": "number", "minimum": 0, "exclusive_minimum": True,
                        "doc": "Cooperative wall-clock deadline in seconds."},
            "feedback": {"ref": "image_policy",
                         "doc": "Image policy override for this request only."},
        },
        "events": ["log", "value", "diff", "perception", "objective", "image", "done", "error"],
        "example": {"id": 7, "op": "exec", "code": "bpy.ops.mesh.primitive_cube_add()",
                    "record": True},
    },
    "program": {
        "doc": "Read or edit the program that reproduces the scene.",
        "mutates": {"set", "patch", "run", "rollback"},
        "fields": {
            "action": {"type": "string", "required": True,
                       "enum": ["get", "set", "patch", "run", "history", "rollback", "record"],
                       "doc": "Program operation."},
            "text": {"type": "string", "doc": "Replacement program text for set."},
            "old": {"type": "string", "doc": "Text to replace for patch; must match exactly once."},
            "new": {"type": "string", "doc": "Replacement text for patch."},
            "label": {"type": "string", "doc": "Label for the version this creates."},
            "version": {"type": "string", "doc": "Version hash or label for rollback."},
            "on": {"type": "boolean", "doc": "Recording state for record."},
            "feedback": {"ref": "image_policy",
                         "doc": "Image policy override for this request only."},
        },
        "events": ["log", "diff", "perception", "objective", "image", "done", "error"],
        "example": {"id": 8, "op": "program", "action": "get"},
    },
    "target": {
        "doc": "Bind a reference image to a view; targets are scored after every action.",
        "mutates": False,
        "fields": {
            "action": {"type": "string", "required": True, "enum": ["set", "list", "clear"],
                       "doc": "Target operation."},
            "name": {"type": "string", "doc": "Target name."},
            "ref": {"type": "string", "doc": "Reference image path."},
            "view": {"type": "string", "default": "front",
                     "doc": "Preset view the reference is bound to."},
            "mask": {"type": "string", "enum": ["auto", "none"], "default": "auto",
                     "doc": "Reference background removal."},
            "fit": {"type": "string", "enum": ["bbox", "none"], "default": "bbox",
                    "doc": "Reference normalisation."},
            "metrics": {"type": "array", "items": {"type": "string",
                                                   "enum": ["iou", "chamfer", "ssim", "hist"]},
                        "default": ["iou"], "doc": "Metrics scored for this target."},
        },
        "events": ["log", "done", "error"],
        "example": {"id": 9, "op": "target", "action": "set", "name": "front",
                    "ref": "reference.png", "view": "front"},
    },
    "fit": {
        "doc": "Search parameters against the registered targets inside the process.",
        "mutates": True,
        "fields": {
            "params": {"type": "array", "required": True, "items": {"ref": "fit_param"},
                       "doc": "Parameters to search."},
            "objective": {"ref": "fit_objective", "doc": "What to optimise."},
            "budget": {"ref": "fit_budget", "doc": "Search bound."},
            "method": {"type": "string", "enum": ["coordinate", "nelder-mead", "random"],
                       "default": "coordinate", "doc": "Search method."},
        },
        "events": ["log", "progress", "diff", "perception", "objective", "image", "done", "error"],
        "example": {"id": 10, "op": "fit",
                    "params": [{"name": "handle_x", "min": 0.2, "max": 0.6}],
                    "objective": {"target": "front", "metric": "iou"},
                    "budget": {"evals": 40}, "method": "coordinate"},
    },
    "inspect": {
        "doc": "Read scene state from RNA.",
        "mutates": False,
        "fields": {
            "object": {"type": "string", "doc": "Restrict the objects array to this name."},
            "full": {"type": "boolean", "default": False,
                     "doc": "Expand node trees and modifier settings."},
            "select": {"type": "array", "items": {"type": "string"},
                       "doc": "RNA paths resolved relative to bpy.data."},
        },
        "events": ["log", "done", "error"],
        "example": {"id": 11, "op": "inspect", "object": "Cube", "full": False},
    },
    "observe": {
        "doc": "Render deterministic offscreen views.",
        "mutates": False,
        "fields": {
            "views": {"type": "array", "items": {"type": "string"}, "default": ["front", "persp"],
                      "doc": "Preset views."},
            "passes": {"type": "array", "items": {"type": "string"}, "default": ["color"],
                       "doc": "Render passes."},
            "size": {"type": "integer", "enum": [512, 768, 1024], "default": 512,
                     "doc": "Tile size."},
            "ref": {"type": "string", "doc": "Reference image placed beside the first view."},
            "layout": {"type": "string", "enum": ["sheet", "separate"], "default": "sheet",
                       "doc": "One contact sheet or one file per view and pass."},
            "overlay": {"type": "boolean", "default": False,
                        "doc": "Blend the reference over the first tile."},
            "frame": {"type": "string", "doc": "Object whose bounds frame the views."},
            "out": {"type": "string", "doc": "Output file, or directory for separate layout."},
            "inline": {"type": "boolean", "default": False,
                       "doc": "Return base64 instead of writing files."},
        },
        "events": ["log", "done", "error"],
        "example": {"id": 12, "op": "observe", "views": ["front"], "passes": ["color"],
                    "size": 512},
    },
    "describe": {
        "doc": "Answer from live RNA, the agent helpers or the channel registry.",
        "mutates": False,
        "fields": {
            "path": {"type": "string", "required": True,
                     "doc": "RNA path, agent path, `channel` or `schema`."},
        },
        "events": ["log", "done", "error"],
        "example": {"id": 13, "op": "describe", "path": "bpy.ops.mesh.bevel"},
    },
    "session": {
        "doc": "Session state: snapshots, history, feedback policy and lifetime.",
        "mutates": {"rollback"},
        "fields": {
            "action": {"type": "string", "required": True,
                       "enum": ["status", "feedback", "save", "close", "snapshot", "rollback",
                                "history"],
                       "doc": "Session operation."},
            "label": {"type": "string",
                      "doc": "Snapshot label; a labelled snapshot is also written to disk."},
            "snapshot": {"type": "string", "doc": "Snapshot hash, label or ~N for rollback."},
            "file": {"type": "string", "doc": "Path for save."},
            "feedback": {"ref": "feedback_policy", "doc": "Feedback policy to merge."},
        },
        "events": ["log", "diff", "perception", "objective", "image", "done", "error"],
        "example": {"id": 14, "op": "session", "action": "status"},
    },
    "cancel": {
        "doc": "Stop the running request. Answered on the transport thread, out of order.",
        "mutates": False,
        "fields": {
            "target": {"type": "integer", "required": True,
                       "doc": "id of the request to stop."},
        },
        "events": ["done"],
        "example": {"id": 15, "op": "cancel", "target": 10},
    },
}

EVENTS = {
    "log": {"doc": "Captured Python stdout or stderr, emitted line by line as it is produced.",
            "fields": {"stream": {"type": "string", "enum": ["stdout", "stderr"]},
                       "text": {"type": "string"}}},
    "value": {"doc": "repr of the statement's final expression, or null when there is none.",
              "fields": {"value": {"type": "string"}}},
    "diff": {"doc": "Datablocks the request added, changed or removed, and the new state.",
             "fields": {"added": {"type": "array"}, "changed": {"type": "array"},
                        "removed": {"type": "array"}, "snapshot": {"type": "string"},
                        "step": {"type": "integer"}}},
    "perception": {"doc": "Counts, bounds, framing and what changed in the budget view.",
                   "fields": {"objects": {"type": "integer"}, "verts": {"type": "integer"},
                              "faces": {"type": "integer"}, "bounds": {"type": "object"},
                              "dims": {"type": "array"}, "framing": {"type": "object"},
                              "changed": {"type": "object"}, "symmetry": {"type": "object"}}},
    "objective": {"doc": "Per-target metrics, their deltas and the best state so far.",
                  "fields": {"targets": {"type": "object"}, "best": {"type": "object"}}},
    "image": {"doc": "A picture of the change, or a requested frame.",
              "fields": {"kind": {"type": "string", "enum": ["delta", "full", "overlay", "error"]},
                         "view": {"type": "string"}, "pass": {"type": "string"},
                         "path": {"type": "string"}, "inline": {"type": "string"},
                         "size": {"type": "array"}, "region": {"type": "array"}}},
    "progress": {"doc": "Search progress, at most every 0.5 s.",
                 "fields": {"eval": {"type": "integer"}, "of": {"type": "integer"},
                            "best": {"type": "number"}, "params": {"type": "object"}}},
    "done": {"doc": "Terminal success event, carrying the op-specific result fields.",
             "fields": {"ok": {"type": "boolean"}, "ms": {"type": "number"}}},
    "error": {"doc": "Terminal failure event.",
              "fields": {"ok": {"type": "boolean"}, "type": {"type": "string"},
                         "message": {"type": "string"}, "line": {"type": "integer"},
                         "rna": {"type": "object"}, "fix": {"type": "object"},
                         "autosave": {"type": "string"}}},
}

TYPES = {"string": str, "number": (int, float), "integer": int, "boolean": bool,
         "object": dict, "array": list}


class ProtocolError(Exception):
    pass


class Cancelled(Exception):
    pass


class NotImplementedRequest(Exception):
    agent_type = "NotImplemented"


def check_value(where, spec, value):
    if "ref" in spec:
        spec = {"type": "object", **DEFS[spec["ref"]]}
    kind = spec.get("type")
    if kind is None:
        return
    if (isinstance(value, bool) != (kind == "boolean")) or not isinstance(value, TYPES[kind]):
        raise ProtocolError(f"{where} must be a {kind}, not {type(value).__name__}")
    if "enum" in spec and value not in spec["enum"]:
        raise ProtocolError(f"{where} must be one of: "
                            f"{', '.join(str(item) for item in spec['enum'])}")
    if "minimum" in spec and (value <= spec["minimum"] if spec.get("exclusive_minimum")
                              else value < spec["minimum"]):
        raise ProtocolError(f"{where} must be greater than {spec['minimum']}")
    if "maximum" in spec and value > spec["maximum"]:
        raise ProtocolError(f"{where} must be at most {spec['maximum']}")
    if kind == "number" and not math.isfinite(value):
        raise ProtocolError(f"{where} must be a finite number")
    if kind == "array" and "items" in spec:
        for index, item in enumerate(value):
            check_value(f"{where}[{index}]", spec["items"], item)


def validate(request):
    """Reject anything the contract does not name, before any of it runs."""
    if not isinstance(request, dict):
        raise ProtocolError("A request is a JSON object")
    if not isinstance(request.get("id"), int) or isinstance(request.get("id"), bool):
        raise ProtocolError("A request needs an integer id")
    op = request.get("op")
    if op not in REQUESTS:
        raise ProtocolError(f"Unknown op: {op!r}; expected one of {', '.join(sorted(REQUESTS))}")
    fields = REQUESTS[op]["fields"]
    for name, value in request.items():
        if name in ("id", "op"):
            continue
        if name not in fields:
            raise ProtocolError(f"Unknown field for {op}: {name!r}; "
                                f"{op} accepts {', '.join(sorted(fields))}")
        check_value(f"{op}.{name}", fields[name], value)
    for name, spec in fields.items():
        if spec.get("required") and name not in request:
            raise ProtocolError(f"{op} requires {name}")
    if op == "exec" and ("code" in request) == ("script" in request):
        raise ProtocolError("exec requires exactly one of code or script")


def mutating(request):
    rule = REQUESTS[request["op"]]["mutates"]
    if isinstance(rule, (set, frozenset)):
        return request.get("action") in rule
    return bool(rule)


# ---------------------------------------------------------------------------
# Registries.

PROVIDERS = []
HANDLERS = {}
HELPERS = {}
PROVIDER_MODULES = ["agent_feedback", "agent_target", "agent_program"]
RECORD_HOOK = None


def register_provider(provider):
    """Add or replace a feedback provider; providers run in ascending order."""
    global PROVIDERS
    PROVIDERS = sorted([item for item in PROVIDERS if item.name != provider.name] + [provider],
                       key=lambda item: item.order)


def register_op(op, handler):
    """Install the handler for a request the contract already declares."""
    if op not in REQUESTS:
        raise KeyError(f"{op} is not a declared request")
    HANDLERS[op] = handler


def register_helper(name, function):
    """Back an `agent.<name>` helper from a workstream module."""
    HELPERS[name] = function


def register_record_hook(hook):
    """Install `hook(session, code, step)`, run after every recorded exec."""
    global RECORD_HOOK
    RECORD_HOOK = hook


def helper(name):
    if name not in HELPERS:
        raise NotImplementedRequest(f"agent.{name} is not implemented in this build")
    return HELPERS[name]


class DiffProvider:
    """The structural channel: which datablocks changed, and the state they left."""

    name = "diff"
    order = 100

    def before(self, request, session):
        bpy.context.view_layer.update()
        session.before = session.native["id_state"](True)

    def after(self, request, session, emit):
        diff = id_diff(session.before, session.native["id_state"](False),
                       session.native["fields"])
        session.last_diff = diff
        if any(diff.values()):
            session.step += 1
            if not session.snapshot_taken and "snapshot" in session.native:
                session.snapshot(None, request["op"])
            if request["op"] == "exec" and request.get("record", True):
                session.on_recorded(session.last_code, session.step)
        emit({"event": "diff", **diff, "snapshot": session.current, "step": session.step})


# ---------------------------------------------------------------------------
# Values and the ID diff.


def reference(value):
    return {"type": value.bl_rna.identifier, "name": getattr(value, "name", "")}


def serialize(value):
    """RNA pointers are references, not recursively expanded graphs (which contain cycles)."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, dict):
        return {key: serialize(item) for key, item in value.items()}
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, bpy.types.bpy_struct):
        return reference(value)
    if isinstance(value, set):
        return sorted(value)
    return [serialize(item) for item in value]


def settings(value):
    return {prop.identifier: serialize(getattr(value, prop.identifier))
            for prop in value.bl_rna.properties if prop.identifier != "rna_type"}


def node_tree(tree, full):
    if tree is None:
        return None
    nodes = []
    for node in tree.nodes:
        data = {"name": node.name, "type": node.bl_idname}
        if full:
            data["settings"] = settings(node)
            for direction in ("inputs", "outputs"):
                data[direction] = [
                    {"name": socket.name, "identifier": socket.identifier,
                     "type": socket.bl_idname, "linked": socket.is_linked,
                     **({"value": serialize(socket.default_value)}
                        if hasattr(socket, "default_value") else {})}
                    for socket in getattr(node, direction)]
        nodes.append(data)
    links = [{"from_node": link.from_node.name, "from_socket": link.from_socket.identifier,
              "to_node": link.to_node.name, "to_socket": link.to_socket.identifier}
             for link in tree.links]
    return {"name": tree.name, "nodes": nodes, "links": links if full else len(links)}


def object_state(obj, full):
    result = {
        "name": obj.name, "type": obj.type,
        "location": list(obj.location), "rotation_mode": obj.rotation_mode,
        "rotation_euler": list(obj.rotation_euler),
        "rotation_quaternion": list(obj.rotation_quaternion),
        "rotation_axis_angle": list(obj.rotation_axis_angle), "scale": list(obj.scale),
        "matrix_world": serialize(obj.matrix_world), "dimensions": list(obj.dimensions),
        "bounds": [list(corner) for corner in obj.bound_box],
        "parent": obj.parent.name if obj.parent else None,
        "data": obj.data.name if obj.data else None,
        "modifiers": [{"name": modifier.name, "type": modifier.type,
                       **({"settings": settings(modifier)} if full else {})}
                      for modifier in obj.modifiers],
        "materials": [slot.material.name if slot.material else None for slot in obj.material_slots],
    }
    if obj.type == "MESH":
        mesh = obj.data
        result["mesh"] = {"vertices": len(mesh.vertices), "edges": len(mesh.edges),
                          "faces": len(mesh.polygons),
                          "uv_layers": [layer.name for layer in mesh.uv_layers]}
    return result


def id_diff(before, after, fields):
    def identity(item):
        return {"type": item[0], "name": item[1]}

    added = [identity(after[uid]) for uid in after.keys() - before.keys()]
    removed = [identity(before[uid]) for uid in before.keys() - after.keys()]
    changed = []
    for uid in before.keys() & after.keys():
        item = after[uid]
        groups = [name for name, mask in fields.items() if item[2] & mask]
        if before[uid][1] != item[1]:
            groups.append("name")
        if groups:
            changed.append({**identity(item), "fields": groups})

    def order(item):
        return (item["type"], item["name"])

    return {"added": sorted(added, key=order), "changed": sorted(changed, key=order),
            "removed": sorted(removed, key=order)}


# ---------------------------------------------------------------------------
# Request handlers.


def exec_op(request, session, emit):
    filename = request.get("script", "<agent>")
    if "script" in request:
        with tokenize.open(filename) as stream:
            code = stream.read()
        if "request_source" in session.native:
            # A native crash inside this statement names the file and its first line.
            session.native["request_source"](json.dumps(
                {"id": request["id"], "file": filename,
                 "first_line": code.split("\n", 1)[0][:512]}))
    else:
        code = request["code"]
    session.last_code = code
    namespace = session.namespace
    namespace["__file__"] = filename
    agent._native["context"]()
    start = time.perf_counter()
    timeout = request.get("timeout")
    deadline = start + timeout if timeout else None
    cancelled = session.native.get("cancelled")

    def check(frame, event, arg):
        if cancelled and cancelled():
            raise Cancelled("Execution cancelled")
        if deadline and time.perf_counter() >= deadline:
            raise TimeoutError(f"Execution exceeded {timeout:g} seconds")
        return check

    previous_trace = sys.gettrace()
    try:
        sys.settrace(check)
        tree = ast.parse(code, filename, "exec")
        expression = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
        # Compile both pieces before executing either, preserving future-import compiler flags.
        statements = compile(tree, filename, "exec")
        tail = compile(ast.Expression(expression.value), filename, "eval",
                       flags=statements.co_flags) if expression else None
        exec(statements, namespace)
        value = repr(eval(tail, namespace)) if tail else None
        if cancelled and cancelled():
            raise Cancelled("Execution cancelled")
        if deadline and time.perf_counter() >= deadline:
            raise TimeoutError(f"Execution exceeded {timeout:g} seconds")
    except BaseException as error:
        error._agent_source = (code, filename)
        raise
    finally:
        sys.settrace(previous_trace)
        # Keep edit mode active, but make Mesh RNA, inspection and saving current.
        agent._native["flush"]()
        bpy.context.view_layer.update()
    emit({"event": "value", "value": value})
    return {}


def inspect_op(request, session, emit):
    if request.get("select"):
        selected = {}
        for path in request["select"]:
            try:
                value = bpy.data.path_resolve(path)
            except ValueError:
                raise ValueError(
                    f'select {json.dumps(path)} could not be resolved relative to bpy.data; '
                    'use a path such as objects["Cube"].location') from None
            selected[path] = serialize(value)
        return {"selected": selected}
    objects = [bpy.data.objects[request["object"]]] if request.get("object") else bpy.data.objects
    full = request.get("full", False)
    scene = bpy.context.scene
    return {
        "scene": {"name": scene.name, "frame": scene.frame_current,
                  "camera": scene.camera.name if scene.camera else None,
                  "collection": scene.collection.name,
                  "objects": [obj.name for obj in scene.objects]},
        "objects": [object_state(obj, full) for obj in objects],
        "materials": [{"name": mat.name, "diffuse_color": list(mat.diffuse_color),
                       "node_tree": node_tree(mat.node_tree, full)}
                      for mat in bpy.data.materials],
        "armatures": [{"name": arm.name, "bones": [
            {"name": bone.name, "parent": bone.parent.name if bone.parent else None,
             "head": list(bone.head_local), "tail": list(bone.tail_local),
             "matrix": serialize(bone.matrix_local), "use_deform": bone.use_deform}
            for bone in arm.bones]} for arm in bpy.data.armatures],
        "cameras": [{"name": cam.name, "type": cam.type, "lens": cam.lens,
                     "ortho_scale": cam.ortho_scale, "clip_start": cam.clip_start,
                     "clip_end": cam.clip_end} for cam in bpy.data.cameras],
        "lights": [{"name": light.name, "type": light.type, "energy": light.energy,
                    "color": list(light.color)} for light in bpy.data.lights],
        "collections": [{"name": col.name, "objects": [obj.name for obj in col.objects],
                         "children": [child.name for child in col.children]}
                        for col in bpy.data.collections],
    }


def observe_op(request, session, emit):
    from agent_observe import observe
    keys = ("views", "passes", "size", "ref", "layout", "frame", "overlay", "out", "inline")
    result = observe(**{key: request[key] for key in keys if key in request})
    result.pop("ok", None)
    return result


def describe_op(request, session, emit):
    path = request["path"]
    if path in ("channel", "schema"):
        return helper("describe_channel")(path)
    return agent.describe(path)


def merge(policy, changes):
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(policy.get(key), dict):
            merge(policy[key], value)
        else:
            policy[key] = value
    return policy


def session_op(request, session, emit):
    action = request["action"]
    if action == "snapshot":
        label = request.get("label")
        return {"snapshot": session.snapshot(label, "snapshot"), "label": label}
    if action == "rollback":
        target = request.get("snapshot")
        if not target:
            raise ValueError("session rollback requires a snapshot hash, label or ~N")
        session.rollback(target)
        return {"snapshot": session.current}
    if action == "history":
        return {"history": [dict(event) for event in session.history]}
    if action == "save":
        path = request.get("file") or bpy.data.filepath
        if not path:
            raise ValueError("session save requires a file for an unsaved session")
        bpy.context.preferences.filepaths.file_preview_type = "NONE"
        bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(path), check_existing=False)
        return {"file": os.path.abspath(path)}
    if action == "close":
        session.closing = True
        return {}
    if action == "feedback":
        if "feedback" in request:
            merge(session.feedback, request["feedback"])
        return {"feedback": session.feedback}
    return {"session": str(os.getpid()), "file": bpy.data.filepath,
            "dirty": bool(bpy.data.is_dirty), "step": session.step,
            "snapshot": session.current, "feedback": session.feedback,
            "targets": sorted(session.targets), "recovered_from": session.recovered_from}


def unimplemented(op):
    def handler(request, session, emit):
        raise NotImplementedRequest(
            f"The {op} request is declared but not implemented in this build")

    return handler


HANDLERS.update({"exec": exec_op, "inspect": inspect_op, "observe": observe_op,
                 "describe": describe_op, "session": session_op,
                 "program": unimplemented("program"), "target": unimplemented("target"),
                 "fit": unimplemented("fit"), "cancel": unimplemented("cancel")})


# ---------------------------------------------------------------------------
# The session.


def fresh_namespace():
    return {"__name__": "__main__", "bpy": bpy, "bmesh": bmesh,
            "mathutils": mathutils, "math": math, "agent": agent}


def default_feedback():
    return {"perception": True, "objective": True,
            "image": {"mode": "delta", "threshold": 0.002, "views": ["front"],
                      "pass": "color", "size": 256, "overlay": True}}


class LogStream(io.TextIOBase):
    """Python output becomes `log` events line by line, while the request runs."""

    def __init__(self, emit, stream):
        self.emit, self.stream, self.pending = emit, stream, ""

    def writable(self):
        return True

    def write(self, text):
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            self.emit({"event": "log", "stream": self.stream, "text": line + "\n"})
        return len(text)

    def flush(self):
        if self.pending:
            self.emit({"event": "log", "stream": self.stream, "text": self.pending})
            self.pending = ""


class Session:
    """One Main, one namespace, one program, one event stream."""

    def __init__(self, native, config_text):
        config = json.loads(config_text)
        self.native = native
        self.namespace = fresh_namespace()
        self.targets = {}
        self.feedback = default_feedback()
        self.request_feedback = self.feedback
        self.current = None
        self.step = 0
        self.before = {}
        self.last_diff = None
        self.last_code = ""
        self.last_perception = None
        self.recovered_from = None
        self.snapshot_taken = False
        self.closing = False
        self.save_after = config.get("save")
        self.snapshot_directory = os.path.abspath(".blender-cli/snapshots")
        self.snapshot_index = os.path.join(self.snapshot_directory, "index.json")
        self.durable = []
        if os.path.isfile(self.snapshot_index):
            with open(self.snapshot_index, encoding="utf-8") as stream:
                self.durable = json.load(stream)
        self.history = [dict(item, snapshot=item["id"], at=item["created"], op="snapshot",
                             step=0, durable=True) for item in self.durable]
        path = config.get("file")
        if path:
            path = os.path.abspath(path)
            if not os.path.isfile(path):
                raise FileNotFoundError(path)
            bpy.ops.wm.open_mainfile(filepath=path, load_ui=False, use_scripts=False)
            metadata_path = os.path.splitext(path)[0] + ".json"
            if os.path.basename(path).startswith("autosave-") and os.path.isfile(metadata_path):
                with open(metadata_path, encoding="utf-8") as stream:
                    metadata = json.load(stream)
                native["restore_metadata"](metadata["filepath"], metadata["dirty"])
                self.recovered_from = "autosave"
        agent._native["context"]()
        bpy.context.view_layer.update()
        self.before = native["id_state"](True)
        register_provider(DiffProvider())
        if "snapshot" in native:
            self.snapshot(None, "open")
        agent._session = self
        for name in PROVIDER_MODULES:
            try:
                module = __import__(name)
            except ModuleNotFoundError:
                continue
            module.register(self)

    # -- snapshots ----------------------------------------------------------

    def snapshot(self, label, op):
        """A labelled snapshot is also a disk checkpoint that survives a crash."""
        if label is not None and not isinstance(label, str):
            raise TypeError("Snapshot label must be a string or None")
        if "snapshot" not in self.native:
            raise RuntimeError("This operation requires an open blender-cli session")
        parent = self.current
        filepath, dirty = bpy.data.filepath, bpy.data.is_dirty
        self.current = self.native["snapshot"]()
        self.snapshot_taken = True
        event = {"snapshot": self.current, "label": label, "parent": parent,
                 "op": op, "step": self.step, "at": time.time()}
        if label is not None:
            os.makedirs(self.snapshot_directory, exist_ok=True)
            path = os.path.join(self.snapshot_directory,
                                self.current.removeprefix("sha256:") + ".blend")
            if not os.path.isfile(path):
                self.native["persist"](path)
            item = {"id": self.current, "label": label, "parent": parent,
                    "created": event["at"], "bytes": os.path.getsize(path),
                    "filepath": filepath, "dirty": dirty}
            entries = [*self.durable, item]
            temporary = self.snapshot_index + "@"
            with open(temporary, "w", encoding="utf-8") as stream:
                json.dump(entries, stream, ensure_ascii=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.snapshot_index)
            self.durable = entries
            event.update(durable=True, bytes=item["bytes"], filepath=filepath, dirty=dirty)
        self.history.append(event)
        self.current_index = len(self.history) - 1
        return self.current

    def rollback(self, target):
        if "rollback" not in self.native:
            raise RuntimeError("This operation requires an open blender-cli session")
        if target.startswith("~"):
            count = int(target[1:])
            if count < 0:
                raise ValueError("Rollback offset must be nonnegative")
            if count > self.current_index:
                raise ValueError("Rollback offset precedes session history")
            index = self.current_index - count
            target = self.history[index]["snapshot"]
        else:
            index = next((i for i in range(len(self.history) - 1, -1, -1)
                          if self.history[i]["snapshot"] == target or
                          self.history[i]["label"] == target), -1)
            if index >= 0:
                target = self.history[index]["snapshot"]
        try:
            self.native["rollback"](target)
        except KeyError:
            item = next((item for item in reversed(self.durable) if item["id"] == target), None)
            if item is None:
                raise
            path = os.path.join(self.snapshot_directory,
                                target.removeprefix("sha256:") + ".blend")
            bpy.ops.wm.open_mainfile(filepath=path, load_ui=False, use_scripts=False)
            self.native["restore_metadata"](item["filepath"], item["dirty"])
            bpy.context.view_layer.update()
            # A recovered disk checkpoint seeds a fresh process-local memfile chain.
            self.current = target
            self.snapshot(None, "rollback")
            return
        self.current = target
        self.current_index = index
        self.snapshot_taken = True
        bpy.context.view_layer.update()

    def diff(self):
        return id_diff(self.before, self.native["id_state"](False), self.native["fields"])

    def on_recorded(self, code, step):
        if RECORD_HOOK is not None:
            RECORD_HOOK(self, code, step)

    # -- dispatch -----------------------------------------------------------

    def emit(self, event):
        self.native["emit"](json.dumps(serialize(event), ensure_ascii=True, allow_nan=False))

    def serve(self, line):
        """One protocol line in, one ordered event sequence out."""
        try:
            request = json.loads(line)
        except ValueError as error:
            self.emit({"id": None, "event": "error", "ok": False,
                       "type": "ProtocolError", "message": str(error)})
            return
        self.dispatch(request)

    def dispatch(self, request):
        identifier = request.get("id") if isinstance(request, dict) else None
        started = time.perf_counter()

        def emit(event):
            self.emit({"id": identifier, **event})

        stdout, stderr = LogStream(emit, "stdout"), LogStream(emit, "stderr")
        changes = False
        try:
            validate(request)
            changes = mutating(request)
            self.snapshot_taken = False
            self.last_diff = None
            self.request_feedback = self.feedback
            if "feedback" in request and request["op"] in ("exec", "program"):
                self.request_feedback = merge(json.loads(json.dumps(self.feedback)),
                                              {"image": request["feedback"]})
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                if changes:
                    for provider in PROVIDERS:
                        provider.before(request, self)
                result = HANDLERS[request["op"]](request, self, emit)
                if changes:
                    for provider in PROVIDERS:
                        try:
                            provider.after(request, self, emit)
                        except BaseException as error:
                            # A feedback channel that fails is a report, never a failed request.
                            stderr.write(f"provider {provider.name}: "
                                         f"{type(error).__name__}: {error}\n")
                if self.save_after:
                    bpy.context.preferences.filepaths.file_preview_type = "NONE"
                    bpy.ops.wm.save_as_mainfile(filepath=self.save_after, check_existing=False)
            stdout.flush()
            stderr.flush()
            emit({"event": "done", "ok": True,
                  "ms": (time.perf_counter() - started) * 1000, **result})
        except BaseException as error:
            stdout.flush()
            stderr.flush()
            frames = traceback.extract_tb(error.__traceback__)
            user_frames = [frame for frame in frames if frame.filename != __file__]
            event = {"event": "error", "ok": False,
                     "type": getattr(error, "agent_type", type(error).__name__),
                     "message": str(error),
                     "line": getattr(error, "lineno", None) or (
                         user_frames[-1].lineno if user_frames else None)}
            source = getattr(error, "_agent_source", None)
            if source:
                from agent_rna import error_context
                rna = error_context(error, *source)
                if rna:
                    event["rna"] = rna
            # A failed request leaves no partial edit: the pre-request state returns.
            if changes and self.current and "rollback" in self.native:
                try:
                    self.native["rollback"](self.current)
                    bpy.context.view_layer.update()
                except BaseException as failure:
                    event["message"] += f" (rollback failed: {failure})"
            emit(event)
        self.request_feedback = self.feedback


def one_shot(native, config_text):
    """A one-shot verb is a session of exactly one request, plus load and save."""
    request = json.loads(config_text).get("request", {})
    try:
        session = Session(native, config_text)
    except BaseException as error:
        native["emit"](json.dumps(
            {"id": request.get("id"), "event": "error", "ok": False,
             "type": type(error).__name__, "message": str(error), "line": None},
            ensure_ascii=True, allow_nan=False))
        return
    session.dispatch(request)
