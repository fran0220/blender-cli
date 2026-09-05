# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Self-description of the channel and of live RNA, plus corrective error fields.

`describe channel` and `describe schema` are generated from the request table;
they are never hand-written. Errors gain the nearest valid identifier and, when
one correction is unambiguous, the submitted statement rewritten so it runs.
"""

import ast
import difflib
import dis
import inspect
import re
import types

import bpy
import agent

DRAFT = "https://json-schema.org/draft/2020-12/schema"


def registry():
    """The contract as data. `agent_runtime` owns it and validates against it."""
    import agent_runtime
    return agent_runtime.REQUESTS, agent_runtime.EVENTS, agent_runtime.DEFS


def channel():
    """Return the request and event registry as records."""
    requests, events, defs = registry()
    return {
        "kind": "channel",
        # `mutates` is dispatch policy, not part of the request an agent writes.
        "requests": {op: {"doc": entry["doc"],
                          "fields": {name: {**spec, "required": bool(spec.get("required"))}
                                     for name, spec in entry["fields"].items()},
                          **({"exactly_one_of": entry["exactly_one_of"]}
                             if "exactly_one_of" in entry else {}),
                          "events": entry["events"], "example": entry["example"]}
                     for op, entry in requests.items()},
        "events": {name: dict(entry) for name, entry in events.items()},
        "defs": {name: dict(spec) for name, spec in defs.items()},
    }


def json_schema(spec):
    """Project one field spec onto JSON Schema draft 2020-12."""
    if "ref" in spec:
        node = {"$ref": "#/$defs/" + spec["ref"]}
    else:
        node = {}
        if "type" in spec:
            node["type"] = spec["type"]
        if "const" in spec:
            node["const"] = spec["const"]
        if "enum" in spec:
            node["enum"] = spec["enum"]
        if "items" in spec:
            node["items"] = json_schema(spec["items"])
        if "fields" in spec:
            node["properties"] = {name: json_schema(field) for name, field in spec["fields"].items()}
            required = [name for name, field in spec["fields"].items() if field.get("required")]
            if required:
                node["required"] = required
            node["additionalProperties"] = False
        if "exactly_one_of" in spec:
            node["oneOf"] = [{"required": [name]} for name in spec["exactly_one_of"]]
        if "minimum" in spec:
            node["exclusiveMinimum" if spec.get("exclusive_minimum") else "minimum"] = spec["minimum"]
        if "maximum" in spec:
            node["maximum"] = spec["maximum"]
    if "default" in spec:
        node["default"] = spec["default"]
    if "doc" in spec:
        node["description"] = spec["doc"]
    return node


def referenced(spec, defs, found):
    """Collect the shared shapes a spec reaches, so each op's schema stands alone."""
    if "ref" in spec and spec["ref"] not in found:
        found[spec["ref"]] = defs[spec["ref"]]
        referenced(defs[spec["ref"]], defs, found)
    if "items" in spec:
        referenced(spec["items"], defs, found)
    for field in spec.get("fields", {}).values():
        referenced(field, defs, found)


def request_schema(op, entry, defs):
    spec = {"type": "object", "fields": {
        "id": {"type": "integer", "required": True,
               "doc": "Client-chosen integer, unique among outstanding requests on the connection."},
        "op": {"type": "string", "const": op, "required": True, "doc": "Request name."},
        **entry["fields"]}}
    if "exactly_one_of" in entry:
        spec["exactly_one_of"] = entry["exactly_one_of"]
    found = {}
    referenced(spec, defs, found)
    result = {"$schema": DRAFT, "$id": "urn:blender-cli:request:" + op,
              "title": op, "description": entry["doc"], **json_schema(spec)}
    if found:
        result["$defs"] = {name: json_schema(value) for name, value in found.items()}
    return result


def schema():
    """Return one self-contained JSON Schema per request, for a function-calling host."""
    requests, _, defs = registry()
    return {"kind": "schema", "$schema": DRAFT,
            "requests": {op: request_schema(op, entry, defs) for op, entry in requests.items()}}


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
    if path == "channel":
        return channel()
    if path == "schema":
        return schema()
    invalid = ("describe resolves bpy.* and agent.* paths, plus the channel and schema "
               f"registries; got {path!r}")
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


NEAREST_CUTOFF = 0.6
NEAREST_COUNT = 5
FIX_SIMILARITY = 0.85
FIX_MARGIN = 0.05


def ranked(name, identifiers):
    """Candidates above the cutoff with their similarity, ordered as difflib ranks them."""
    matcher = difflib.SequenceMatcher()
    matcher.set_seq2(name)
    scored = []
    for candidate in sorted(identifiers):
        matcher.set_seq1(candidate)
        if (matcher.real_quick_ratio() >= NEAREST_CUTOFF and matcher.quick_ratio() >= NEAREST_CUTOFF
                and matcher.ratio() >= NEAREST_CUTOFF):
            scored.append((matcher.ratio(), candidate))
    return sorted(scored, reverse=True)


def unambiguous(scored):
    """The single certain correction: the only candidate, or a clear winner.

    A lone candidate at the cutoff is unambiguous by construction. In a crowded
    neighbourhood the best must reach FIX_SIMILARITY and beat the runner-up by
    more than FIX_MARGIN. Otherwise there is no fix, never a guess.
    """
    if not scored:
        return None, 0.0
    if len(scored) == 1:
        return scored[0][1], scored[0][0]
    if scored[0][0] >= FIX_SIMILARITY and scored[0][0] - scored[1][0] > FIX_MARGIN:
        return scored[0][1], scored[0][0]
    return None, 0.0


def correction(node, old, new, score, reason):
    return {"node": node, "old": old, "new": new,
            "reason": f"{reason}; nearest {new!r} (similarity {score:.2f})"}


def enum_correction(prop, node, tree, struct):
    """Rewrite an invalid enum item assigned to a known enum property."""
    if prop.type != "ENUM" or node is None or tree is None:
        return None
    literal = next((item.value for item in ast.walk(tree)
                    if isinstance(item, ast.Assign) and any(target is node for target in item.targets)
                    and isinstance(item.value, ast.Constant) and isinstance(item.value.value, str)), None)
    if literal is None or literal.value in [item.identifier for item in prop.enum_items]:
        return None
    new, score = unambiguous(ranked(literal.value, [item.identifier for item in prop.enum_items]))
    if not new:
        return None
    return correction(literal, literal.value, new, score,
                      f"{struct}.{prop.identifier} has no item {literal.value!r}")


def keyword_correction(call, rna_type, path):
    """Rewrite an unknown operator keyword, or an invalid enum item passed to a known one."""
    properties = {prop.identifier: prop for prop in rna_type.properties if prop.identifier != "rna_type"}
    for keyword in call.keywords:
        if keyword.arg is None:
            continue
        if keyword.arg not in properties:
            new, score = unambiguous(ranked(keyword.arg, properties))
            return correction(keyword, keyword.arg, new, score,
                              f"{path} has no argument {keyword.arg!r}") if new else None
        prop = properties[keyword.arg]
        if prop.type != "ENUM" or not isinstance(keyword.value, ast.Constant) \
                or not isinstance(keyword.value.value, str):
            continue
        items = [item.identifier for item in prop.enum_items]
        if keyword.value.value in items:
            continue
        new, score = unambiguous(ranked(keyword.value.value, items))
        return correction(keyword.value, keyword.value.value, new, score,
                          f"{path} argument {keyword.arg} has no item {keyword.value.value!r}") if new else None
    return None


def attribute_context(parent, name, node=None, tree=None):
    """Live RNA context for a failing attribute, and the correction it justifies."""
    rna = getattr(parent, "bl_rna", None)
    if not isinstance(rna, bpy.types.Struct):
        rna = None
    if rna:
        result = {"struct": rna.identifier}
        if name in rna.properties:
            prop = rna.properties[name]
            return {**result, **property_info(prop)}, enum_correction(prop, node, tree, rna.identifier)
        identifiers = list(rna.properties.keys()) + list(rna.functions.keys())
    elif parent is bpy.types or operator_module(parent):
        result = {"struct": "bpy.types" if parent is bpy.types else parent.__name__}
        identifiers = dir(parent)
    else:
        return None, None
    scored = ranked(name, identifiers)
    if rna and not scored:
        # A property the object does not carry often belongs to its data.
        data_rna = getattr(getattr(parent, "data", None), "bl_rna", None)
        if isinstance(data_rna, bpy.types.Struct):
            scored = [(score, "data." + item) for score, item in
                      ranked(name, set(data_rna.properties.keys()) | set(data_rna.functions.keys()))]
    result["nearest"] = [item for _, item in scored[:NEAREST_COUNT]]
    if rna and result["nearest"] and result["nearest"][0] in rna.properties:
        prop = rna.properties[result["nearest"][0]]
        result["type"] = prop.type.lower() + (f"[{prop.array_length}]" if getattr(prop, "is_array", False) else "")
    new, score = unambiguous(scored)
    if not new or node is None:
        return result, None
    return result, correction(node, name, new, score, f"{result['struct']} has no {name!r}")


def failing_expression(error, code, filename):
    """The AST node the failing bytecode names, its tree, and the frame's namespace."""
    frames = []
    tb = error.__traceback__
    while tb:
        if tb.tb_frame.f_code.co_filename == filename:
            frames.append(tb)
        tb = tb.tb_next
    if not frames:
        return None, [], {}
    tb = frames[-1]
    instruction = next(item for item in dis.get_instructions(tb.tb_frame.f_code)
                       if item.offset == tb.tb_lasti)
    pos = instruction.positions
    tree = ast.parse(code)
    nodes = [node for node in ast.walk(tree) if isinstance(node, (ast.Attribute, ast.Call))
             and node.lineno <= tb.tb_lineno <= node.end_lineno
             and (node.lineno, node.col_offset) == (pos.lineno, pos.col_offset)
             and (node.end_lineno, node.end_col_offset) == (pos.end_lineno, pos.end_col_offset)]
    return tree, nodes, {**tb.tb_frame.f_globals, **tb.tb_frame.f_locals}


def diagnose(error, code, filename):
    """The RNA record for a failing statement and the correction its source position allows."""
    tree, nodes, namespace = failing_expression(error, code, filename)
    # Only the failing expression's receiver is read; no user call is re-executed.
    for node in nodes:
        if isinstance(node, ast.Call):
            value = resolve(node.func, namespace)
            if not operator(value):
                continue
            try:
                rna_type = value.get_rna_type()
            except KeyError:
                if isinstance(node.func, ast.Attribute):
                    return attribute_context(resolve(node.func.value, namespace), node.func.attr)
                continue
            return struct_info(rna_type), keyword_correction(node, rna_type, ast.unparse(node.func))
        result, fix = attribute_context(resolve(node.value, namespace), node.attr, node, tree)
        if result:
            return result, fix
    # Python's exception object has obj/name for many reads, but not RNA writes.
    if isinstance(error, AttributeError) and getattr(error, "obj", None) is not None:
        return attribute_context(error.obj, error.name)[0], None
    return None, None


def rewrite(code, node, old, new):
    """The submitted code with one identifier replaced at its source position."""
    data = code.encode("utf-8")
    starts = [0] + [index + 1 for index, byte in enumerate(data) if byte == 0x0A]
    begin = starts[node.lineno - 1] + node.col_offset
    end = starts[node.end_lineno - 1] + node.end_col_offset
    span = data[begin:end]
    matches = list(re.finditer(rb"\b" + re.escape(old.encode("utf-8")) + rb"\b", span))
    if not matches:
        return None
    match = matches[-1]
    fixed = (data[:begin] + span[:match.start()] + new.encode("utf-8")
             + span[match.end():] + data[end:]).decode("utf-8")
    try:
        compile(fixed, "<fix>", "exec")
    except SyntaxError:
        return None
    return fixed if fixed != code else None


def error_fields(error, code, filename):
    """The fields this module contributes to an error object: rna and fix, both optional."""
    if not isinstance(error, (AttributeError, TypeError, ValueError)):
        return None
    try:
        record, fix = diagnose(error, code, filename)
    except Exception:
        # Hints must never replace the original exception or make an unrelated failure RNA-aware.
        return None
    result = {"rna": record} if record else {}
    if fix:
        fixed = rewrite(code, fix["node"], fix["old"], fix["new"])
        if fixed:
            result["fix"] = {"code": fixed, "reason": fix["reason"]}
    return result or None
