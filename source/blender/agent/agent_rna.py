# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Live RNA descriptions and best-effort exception context, without replaying calls."""

import ast
import difflib
import dis
import inspect
import types

import bpy
import agent


def resolve(node, namespace):
    if isinstance(node, ast.Name):
        return namespace[node.id]
    if isinstance(node, ast.Attribute) and not node.attr.startswith("_"):
        return getattr(resolve(node.value, namespace), node.attr)
    if isinstance(node, ast.Subscript):
        return resolve(node.value, namespace)[ast.literal_eval(node.slice)]
    raise ValueError("RNA paths allow names, public attributes and literal indices only")


def property_info(prop):
    result = {"identifier": prop.identifier, "description": prop.description,
              "type": prop.type.lower(), "subtype": prop.subtype,
              "animatable": prop.is_animatable, "readonly": prop.is_readonly}
    for name in ("array_length", "hard_min", "hard_max", "soft_min", "soft_max"):
        if hasattr(prop, name):
            result[name] = getattr(prop, name)
    if getattr(prop, "is_array", False):
        result["default"] = list(prop.default_array)
    elif hasattr(prop, "default"):
        result["default"] = prop.default
    if prop.type == "ENUM":
        result["enum_items"] = [{"identifier": item.identifier, "name": item.name,
                                 "description": item.description} for item in prop.enum_items]
    if prop.type in {"POINTER", "COLLECTION"}:
        result["fixed_type"] = prop.fixed_type.identifier
    return result


def struct_info(rna):
    return {"struct": rna.identifier, "description": rna.description,
            "base": rna.base.identifier if rna.base else None,
            "properties": {prop.identifier: property_info(prop) for prop in rna.properties
                           if prop.identifier != "rna_type"}}


def operator(value):
    return isinstance(value, type(bpy.ops.mesh.bevel))


def operator_module(value):
    return isinstance(value, types.ModuleType) and value.__name__.startswith("bpy.ops.")


def describe(path):
    invalid = f"describe resolves bpy.* and agent.* paths; got {path!r}"
    try:
        node = ast.parse(path, mode="eval").body
        namespace = {"bpy": bpy, "agent": agent}
        if isinstance(node, ast.Name) and node.id not in namespace:
            namespace[node.id] = getattr(bpy.types, node.id)
        if isinstance(node, ast.Attribute):
            parent = resolve(node.value, namespace)
            rna = getattr(parent, "bl_rna", None)
            if isinstance(rna, bpy.types.Struct) and node.attr in rna.properties:
                return {"kind": "property", "struct": rna.identifier,
                        **property_info(rna.properties[node.attr])}
        value = resolve(node, namespace)
    except (AttributeError, KeyError, IndexError, SyntaxError, ValueError, TypeError):
        raise ValueError(invalid) from None
    if value is agent:
        return {"kind": "module", "path": path, "doc": inspect.getdoc(value), "functions": {
            name: describe("agent." + name) for name, function in inspect.getmembers(agent, inspect.isfunction)
            if not name.startswith("_")}}
    if inspect.isfunction(value) and value.__module__ == "agent":
        signature = inspect.signature(value)
        return {"kind": "function", "signature": path + str(signature), "doc": inspect.getdoc(value),
                "parameters": [{"name": parameter.name,
                                "default": None if parameter.default is inspect.Parameter.empty
                                else repr(parameter.default)} for parameter in signature.parameters.values()]}
    if operator_module(value):
        return {"kind": "module", "path": path, "operators": {
            name: getattr(value, name).get_rna_type().description for name in sorted(dir(value))}}
    if operator(value):
        agent._native["context"]()
        result = {"kind": "operator", "path": path, **struct_info(value.get_rna_type())}
        agent._native["poll_message"]()
        result["poll"] = value.poll()
        reason = agent._native["poll_message"]()
        if not result["poll"] and reason:
            result["poll_reason"] = reason
        result["context"] = "Adopted window/screen, VIEW_3D area and WINDOW region; no drawn GPU viewport"
        result["signature"] = path + "(*, " + ", ".join(result["properties"]) + ")"
        return result
    if hasattr(value, "bl_rna"):
        return {"kind": "struct", **struct_info(value.bl_rna)}
    raise ValueError(invalid)


def attribute_context(parent, name):
    rna = getattr(parent, "bl_rna", None)
    if not isinstance(rna, bpy.types.Struct):
        rna = None
    if rna:
        result = {"struct": rna.identifier}
        if name in rna.properties:
            return {**result, **property_info(rna.properties[name])}
        identifiers = list(rna.properties.keys()) + list(rna.functions.keys())
    elif parent is bpy.types or operator_module(parent):
        result = {"struct": "bpy.types" if parent is bpy.types else parent.__name__}
        identifiers = dir(parent)
    else:
        return None
    result["nearest"] = difflib.get_close_matches(name, sorted(identifiers), n=5, cutoff=0.6)
    if rna and not result["nearest"]:
        data_rna = getattr(getattr(parent, "data", None), "bl_rna", None)
        if isinstance(data_rna, bpy.types.Struct):
            matches = difflib.get_close_matches(
                name, sorted(set(data_rna.properties.keys()) | set(data_rna.functions.keys())), n=5, cutoff=0.6)
            result["nearest"] = ["data." + match for match in matches]
    if rna and result["nearest"] and result["nearest"][0] in rna.properties:
        prop = rna.properties[result["nearest"][0]]
        result["type"] = prop.type.lower() + (f"[{prop.array_length}]" if getattr(prop, "is_array", False) else "")
    return result


def error_context(error, code, filename):
    if not isinstance(error, (AttributeError, TypeError, ValueError)):
        return None
    try:
        # Python's exception object has obj/name for many reads, but not RNA writes.
        if isinstance(error, AttributeError) and getattr(error, "obj", None) is not None:
            result = attribute_context(error.obj, error.name)
            if result:
                return result
        frames = []
        tb = error.__traceback__
        while tb:
            if tb.tb_frame.f_code.co_filename == filename:
                frames.append(tb)
            tb = tb.tb_next
        if not frames:
            return None
        tb = frames[-1]
        instruction = next(item for item in dis.get_instructions(tb.tb_frame.f_code)
                           if item.offset == tb.tb_lasti)
        pos = instruction.positions
        nodes = [node for node in ast.walk(ast.parse(code)) if isinstance(node, (ast.Attribute, ast.Call))
                 and node.lineno <= tb.tb_lineno <= node.end_lineno
                 and (node.lineno, node.col_offset) == (pos.lineno, pos.col_offset)
                 and (node.end_lineno, node.end_col_offset) == (pos.end_lineno, pos.end_col_offset)]
        namespace = {**tb.tb_frame.f_globals, **tb.tb_frame.f_locals}
        # Only the failing expression's receiver is read; no user call is re-executed.
        for node in nodes:
            if isinstance(node, ast.Call):
                value = resolve(node.func, namespace)
                if operator(value):
                    try:
                        return struct_info(value.get_rna_type())
                    except KeyError:
                        if isinstance(node.func, ast.Attribute):
                            return attribute_context(resolve(node.func.value, namespace), node.func.attr)
            else:
                result = attribute_context(resolve(node.value, namespace), node.attr)
                if result:
                    return result
    except Exception:
        # Hints must never replace the original exception or make an unrelated failure RNA-aware.
        pass
    return None
