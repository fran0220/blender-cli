# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Main-thread RNA implementation of the agent one-shot verbs."""

import argparse
import ast
import contextlib
import io
import json
import math
import os
import sys
import time
import traceback
import tokenize

import bpy
import bmesh
import mathutils


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


class NotImplemented(Exception):
    pass


def parse(arguments):
    parser = ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("verb", choices=("session", "exec", "inspect", "observe", "compare", "describe"))
    parser.add_argument("--file")
    parser.add_argument("--save", nargs="?", const="")
    parser.add_argument("--json", action="store_true")
    # Later-phase verbs must report NotImplemented even with their future arguments.
    if arguments[0] in {"session", "observe", "compare", "describe"}:
        raise NotImplemented(f"{arguments[0]} is not implemented in Phase 1")
    if arguments[0] not in {"exec", "inspect"}:
        raise ValueError(f"Unknown verb: {arguments[0]}")
    if arguments[0] == "exec":
        parser.add_argument("-c", dest="code")
        parser.add_argument("script", nargs="?")
        parser.add_argument("--timeout", type=float)
    else:
        parser.add_argument("--object")
        parser.add_argument("--full", action="store_true")
        parser.add_argument("--select", nargs="+", action="extend", default=[])
    args = parser.parse_args(arguments)
    if args.verb == "exec":
        if (args.code is None) == (args.script is None):
            raise ValueError("exec requires exactly one of -c CODE or FILE.py")
        if args.timeout is not None and (not math.isfinite(args.timeout) or args.timeout <= 0):
            raise ValueError("--timeout must be a finite positive number")
    return args


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


def inspect(args):
    if args.select:
        return {"ok": True, "selected": {
            path: serialize(bpy.data.path_resolve(path, False)) for path in args.select}}
    objects = [bpy.data.objects[args.object]] if args.object else bpy.data.objects
    scene = bpy.context.scene
    return {
        "ok": True,
        "scene": {"name": scene.name, "frame": scene.frame_current,
                  "camera": scene.camera.name if scene.camera else None,
                  "collection": scene.collection.name,
                  "objects": [obj.name for obj in scene.objects]},
        "objects": [object_state(obj, args.full) for obj in objects],
        "materials": [{"name": mat.name, "diffuse_color": list(mat.diffuse_color),
                       "node_tree": node_tree(mat.node_tree, args.full)} for mat in bpy.data.materials],
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

    def order(item): return (item["type"], item["name"])
    return {"added": sorted(added, key=order), "changed": sorted(changed, key=order),
            "removed": sorted(removed, key=order)}


def execute(args, snapshot, fields):
    filename = os.path.abspath(args.script) if args.script else "<agent>"
    if args.script:
        with tokenize.open(filename) as stream:
            code = stream.read()
    else:
        code = args.code
    namespace = {"__name__": "__main__", "__file__": filename,
                 "bpy": bpy, "bmesh": bmesh, "mathutils": mathutils, "math": math}
    bpy.context.view_layer.update()
    before = snapshot(True)
    start = time.perf_counter()
    deadline = start + args.timeout if args.timeout else None

    def check_timeout(frame, event, arg):
        if time.perf_counter() >= deadline:
            raise TimeoutError(f"Execution exceeded {args.timeout:g} seconds")
        return check_timeout

    previous_trace = sys.gettrace()
    try:
        if deadline:
            sys.settrace(check_timeout)
        tree = ast.parse(code, filename, "exec")
        expression = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
        # Compile both pieces before executing either, preserving future-import compiler flags.
        statements = compile(tree, filename, "exec")
        tail = compile(ast.Expression(expression.value), filename, "eval", flags=statements.co_flags) \
            if expression else None
        exec(statements, namespace)
        value = repr(eval(tail, namespace)) if tail else None
        if deadline and time.perf_counter() >= deadline:
            raise TimeoutError(f"Execution exceeded {args.timeout:g} seconds")
    finally:
        sys.settrace(previous_trace)
    elapsed = (time.perf_counter() - start) * 1000
    return {"ok": True, "value": value, "diff": id_diff(before, snapshot(False), fields), "ms": elapsed}


def run(arguments, snapshot, fields):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            args = parse(arguments)
            save = (args.save or args.file) if args.save is not None else None
            if args.save is not None and not save:
                raise ValueError("--save requires a path or --file")
            if args.file:
                path = os.path.abspath(args.file)
                if not os.path.isfile(path):
                    raise FileNotFoundError(path)
                bpy.ops.wm.open_mainfile(filepath=path, load_ui=False, use_scripts=False)
            result = execute(args, snapshot, fields) if args.verb == "exec" else inspect(args)
            if save:
                bpy.context.preferences.filepaths.file_preview_type = "NONE"
                bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(save), check_existing=False)
        except BaseException as error:
            frames = traceback.extract_tb(error.__traceback__)
            user_frames = [frame for frame in frames if frame.filename != __file__]
            line = getattr(error, "lineno", None) or (user_frames[-1].lineno if user_frames else None)
            result = {"ok": False, "error": {"type": type(error).__name__, "message": str(error), "line": line}}
    if arguments[0] == "exec" or not result["ok"]:
        result.update(stdout=stdout.getvalue(), stderr=stderr.getvalue())
    text = json.dumps(serialize(result), ensure_ascii=True, allow_nan=False,
                      indent=None if "--json" in arguments else 2)
    return text, 0 if result["ok"] else 1
