# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Drive the installed binary's self-description and its corrective errors.

The channel record and the JSON Schema projection are generated from the
request table, so this test checks them against the table's own examples and
against the request block in `doc/agent/design.md`. Corrective errors are
checked by running the correction the process proposes.
"""

import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

DESIGN = Path(__file__).resolve().parents[2] / "doc" / "agent" / "design.md"
DRAFT = "https://json-schema.org/draft/2020-12/schema"

# The keyword subset `describe schema` emits; adding a keyword there adds one here.
TYPES = {"object": dict, "array": list, "string": str, "boolean": bool}


def validate(schema, value, root, path="$"):
    """Return a list of validation errors; empty means the value satisfies the schema."""
    if "$ref" in schema:
        target = root
        for part in schema["$ref"].split("/")[1:]:
            target = target[part]
        return validate(target, value, root, path)
    kind = schema.get("type")
    if kind == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        return [f"{path}: expected integer, got {value!r}"]
    if kind == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
        return [f"{path}: expected number, got {value!r}"]
    if kind in TYPES and not isinstance(value, TYPES[kind]):
        return [f"{path}: expected {kind}, got {value!r}"]
    if kind == "string" and isinstance(value, bool):
        return [f"{path}: expected string, got {value!r}"]
    errors = []
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}: missing required {name!r}")
        for name, item in value.items():
            if name in properties:
                errors.extend(validate(properties[name], item, root, f"{path}.{name}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unknown field {name!r}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors.extend(validate(schema["items"], item, root, f"{path}[{index}]"))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} below minimum {schema['minimum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: {value} not above {schema['exclusiveMinimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} above maximum {schema['maximum']}")
    if "oneOf" in schema:
        matched = sum(not validate(option, value, root, path) for option in schema["oneOf"])
        if matched != 1:
            errors.append(f"{path}: matched {matched} oneOf branches, expected exactly 1")
    return errors


def documented_requests():
    """Field names per op from the request block of design.md, which K owns."""
    text = DESIGN.read_text(encoding="utf-8")
    block = text.split("### Requests", 1)[1].split("```", 2)[1]
    documented = {}
    for entry in re.split(r"\n(?=\{\"id\")", block.strip()):
        op = re.search(r'"op":\s*"(\w+)"', entry).group(1)
        documented[op] = {name for name in re.findall(r'"(\w+)":', entry)} - {"id", "op"}
    return documented


def main():
    executable = str(Path(sys.argv[1]).resolve())
    with tempfile.TemporaryDirectory(prefix="agent describe ") as directory:
        root = Path(directory)

        def call(*args, ok=True):
            process = subprocess.run([executable, *map(str, args), "--json"], cwd=root,
                                     capture_output=True, text=True, timeout=120)
            assert process.returncode == (0 if ok else 1), (args, process.stdout, process.stderr)
            try:
                result = json.loads(process.stdout)
            except ValueError:
                raise AssertionError((args, process.stdout, process.stderr)) from None
            assert result["ok"] is ok, result
            return result

        def run(code, ok=True):
            return call("exec", "-c", code, ok=ok)

        channels = iter(range(100))

        def conversation(*requests):
            """One `repl --standalone` session; returns each request's events, in order.

            Some RNA facts only exist across requests: an operator's poll depends on
            the mode the previous request left behind.
            """
            channel = root / f"channel-{next(channels)}"
            channel.mkdir()
            process = subprocess.run([executable, "repl", "--standalone"], cwd=channel, timeout=300,
                                     input="".join(json.dumps(request) + "\n" for request in requests),
                                     capture_output=True, text=True)
            assert process.returncode == 0, (process.stdout, process.stderr)
            answers = {}
            for line in process.stdout.splitlines():
                if line.strip():
                    event = json.loads(line)
                    answers.setdefault(event["id"], []).append(event)
            return answers

        # --- describe channel: the request and event set, generated from the table ---
        channel = call("describe", "channel")
        assert channel["kind"] == "channel", channel
        requests, events = channel["requests"], channel["events"]
        documented = documented_requests()
        assert set(requests) == set(documented), (sorted(requests), sorted(documented))
        for op, names in documented.items():
            missing = names - set(requests[op]["fields"])
            assert not missing, (op, missing)
        for op, record in requests.items():
            assert record["doc"].endswith("."), record
            assert record["events"] and set(record["events"]) <= set(events), record
            for name, field in record["fields"].items():
                assert isinstance(field["required"], bool), (op, name, field)
                assert "type" in field or "ref" in field, (op, name, field)
                assert field["doc"].endswith("."), (op, name, field)
                if "ref" in field:
                    assert field["ref"] in channel["defs"], (op, name, field)
        assert requests["exec"]["fields"]["code"]["type"] == "string", requests["exec"]
        assert requests["exec"]["fields"]["script"]["type"] == "string", requests["exec"]
        assert requests["observe"]["fields"]["size"]["enum"] == [256, 512, 768, 1024], requests["observe"]
        assert requests["cancel"]["fields"]["target"]["type"] == "integer", requests["cancel"]
        assert requests["describe"]["fields"]["path"]["required"] is True, requests["describe"]
        assert "fix" in events["error"]["fields"] and "rna" in events["error"]["fields"], events["error"]
        for name, event in events.items():
            assert event["doc"].endswith("."), (name, event)
            assert event["fields"], (name, event)

        # --- describe schema: one self-contained draft 2020-12 document per op ---
        schema = call("describe", "schema")
        assert schema["kind"] == "schema" and schema["$schema"] == DRAFT, schema
        assert set(schema["requests"]) == set(requests), sorted(schema["requests"])
        for op, document in schema["requests"].items():
            assert document["$schema"] == DRAFT, document
            assert document["$id"] == "urn:blender-cli:request:" + op, document
            assert document["type"] == "object" and document["additionalProperties"] is False, document
            assert document["properties"]["op"]["const"] == op, document
            assert "id" in document["required"] and "op" in document["required"], document
            # A referenced shape resolves inside the same document, so a host can use it alone.
            for text in json.dumps(document).split('"$ref": "#/$defs/')[1:]:
                assert text.split('"')[0] in document["$defs"], (op, text[:40])
            example = requests[op]["example"]
            assert not validate(document, example, document), (op, validate(document, example, document))

        # Negative cases: the schema is a real constraint, not decoration.
        for broken in ({"id": 1, "op": "exec", "code": "pass", "typo": 1},
                       {"id": 1, "op": "exec", "code": 3},
                       {"id": "1", "op": "exec", "code": "pass"},
                       {"id": 1, "op": "observe", "code": "pass"},
                       {"id": 1, "op": "exec", "code": "pass", "timeout": 0},
                       {"id": 1, "op": "observe", "size": 640},
                       {"id": 1, "op": "target", "action": "add"}):
            document = schema["requests"][broken["op"]]
            assert validate(document, broken, document), broken
        # An exclusive choice the table declares is projected, so a host cannot build a
        # request the validator then rejects: exec needs code or script, never both.
        exec_schema = schema["requests"]["exec"]
        for broken in ({"id": 1, "op": "exec"},
                       {"id": 1, "op": "exec", "code": "pass", "script": "/tmp/a.py"}):
            assert validate(exec_schema, broken, exec_schema), broken
        fit_schema = schema["requests"]["fit"]
        for params in ([{"min": 0, "max": 1}],
                       [{"name": "a", "path": "b", "min": 0, "max": 1}],
                       [{"name": "a", "min": 0}]):
            broken = {"id": 1, "op": "fit", "params": params}
            assert validate(fit_schema, broken, fit_schema), params
        assert not validate(fit_schema, {"id": 1, "op": "fit",
                                         "params": [{"path": "objects[\"C\"].scale[0]", "min": 0.5, "max": 2}]},
                            fit_schema)
        # The projection is exactly as strict as the table, never stricter.
        for op, document in schema["requests"].items():
            assert ("oneOf" in document) == ("exactly_one_of" in requests[op]), op

        # --- describe of live RNA and of the agent helpers ---
        described = call("describe", "bpy.types.Object.location")
        assert described["type"] == "float" and described["array_length"] == 3, described
        helper = call("describe", "agent.compare")
        assert helper["kind"] == "function" and "fit='bbox'" in helper["signature"], helper
        assert helper["doc"] and {p["name"]: p["default"] for p in helper["parameters"]}["metrics"] == "('iou',)"
        module = call("describe", "agent")
        assert {"observe", "compare", "describe", "snapshot", "rollback", "diff",
                "history"} <= set(module["functions"]), sorted(module["functions"])
        for name, function in module["functions"].items():
            assert function["kind"] == "function" and function["doc"], (name, function)
            assert function["signature"].startswith("agent." + name + "("), (name, function)
            assert isinstance(function["parameters"], list), (name, function)
        # Helpers the feedback, target and kernel workstreams add describe themselves.
        print("agent helpers described:", " ".join(sorted(module["functions"])))
        for path in ("foo", "agent.no_such_helper", "bpy.types.NoSuchStruct", "__import__('os')"):
            error = call("describe", path, ok=False)["error"]
            assert error["type"] == "ValueError", error
            assert "describe resolves bpy.* and agent.*" in error["message"], error
            assert "channel and schema" in error["message"], error
            # An argument error names no user-code line, and never an agent module's own.
            assert error["line"] is None, error

        # --- nearest identifiers, including the one-hop data. search ---
        for receiver in ("bpy.context.object", "bpy.data.objects['Handle']"):
            error = run("bpy.ops.curve.primitive_bezier_circle_add(); "
                        "bpy.context.object.name = 'Handle'; " + receiver + ".bevel_dept",
                        ok=False)["error"]
            assert "data.bevel_depth" in error["rna"]["nearest"], error

        # --- RNA records, real operator polls, and errors that carry no RNA at all ---
        answers = conversation(
            {"id": 1, "op": "exec", "code": "bpy.data.objects['Cube'].locaton = (0, 0, 0)"},
            {"id": 2, "op": "exec", "code": "bpy.data.objects['Cube'].rotation_mode = 'NOT_AN_ENUM'"},
            {"id": 3, "op": "exec", "code": "bpy.context.scene.render.resolution_x = 2**40"},
            {"id": 4, "op": "exec", "code": "bpy.data.objects['Cube'].location = 'wrong'"},
            {"id": 5, "op": "exec", "code": "bpy.ops.mesh.primitive_cube_add(unknown_keyword=1)"},
            {"id": 6, "op": "exec", "code": "int('not a number')"},
            {"id": 7, "op": "exec", "code": "int(bpy.data.objects['Cube'].location)"},
            {"id": 8, "op": "exec", "code": "bpy.types.Object.locaton"},
            {"id": 9, "op": "exec", "code": "bpy.ops.mesh.primitive_cub_add()"},
            {"id": 10, "op": "describe", "path": "bpy.ops.mesh.bevel"},
            {"id": 11, "op": "exec", "code": "bpy.ops.object.mode_set(mode='EDIT')"},
            {"id": 12, "op": "describe", "path": "bpy.ops.mesh.bevel"},
            {"id": 13, "op": "exec", "code": "bpy.ops.object.mode_set(mode='OBJECT')"},
            {"id": 14, "op": "describe", "path": "bpy.ops.view3d.select"},
            {"id": 15, "op": "describe", "path": "Modifier"},
            {"id": 16, "op": "describe", "path": "bpy.types.BevelModifier"},
            {"id": 17, "op": "exec", "code": "bpy.data.objects['Cube'].modifiers.new('Bevel', 'BEVEL')"},
            {"id": 18, "op": "describe", "path": 'bpy.data.objects["Cube"].modifiers[0]'},
            {"id": 19, "op": "describe", "path": "bpy.ops.mesh"},
            {"id": 20, "op": "exec", "code": "agent.describe('bpy.types.Object.location')['array_length']"},
            {"id": 21, "op": "describe", "path": "__import__('os').getcwd()"},
        )

        def answer(request_id, event="done"):
            terminal = answers[request_id][-1]
            assert terminal["event"] == event, terminal
            return terminal

        typo = answer(1, "error")
        assert "location" in typo["rna"]["nearest"] and typo["line"] == 1, typo
        assert "XYZ" in {item["identifier"] for item in answer(2, "error")["rna"]["enum_items"]}, answers[2]
        overflow = answer(3, "error")
        assert overflow["rna"]["hard_min"] <= 512 <= overflow["rna"]["hard_max"], overflow
        assert answer(4, "error")["rna"]["array_length"] == 3, answers[4]
        assert "size" in answer(5, "error")["rna"]["properties"], answers[5]
        # A failure with no RNA receiver stays an ordinary Python error, never a decorated one.
        assert "rna" not in answer(6, "error"), answers[6]
        assert "rna" not in answer(7, "error"), answers[7]
        assert "location" in answer(8, "error")["rna"]["nearest"], answers[8]
        assert "primitive_cube_add" in answer(9, "error")["rna"]["nearest"], answers[9]
        # `poll` is the operator's real poll in the context the previous request left.
        bevel = answer(10)
        assert bevel["kind"] == "operator" and bevel["poll"] is False, bevel
        assert "offset" in bevel["properties"] and bevel["signature"].startswith("bpy.ops.mesh.bevel(*, "), bevel
        assert answer(12)["poll"] is True, answers[12]
        blocked = answer(14)
        assert blocked["poll"] is False and "GPU viewport selection" in blocked["poll_reason"], blocked
        assert answer(15)["struct"] == "Modifier", answers[15]
        assert answer(16)["base"] == "Modifier", answers[16]
        instance = answer(18)
        assert instance["struct"] == "BevelModifier" and "width" in instance["properties"], instance
        assert answer(19)["kind"] == "module" and answer(19)["operators"]["bevel"], answers[19]
        assert [event["value"] for event in answers[20] if event["event"] == "value"] == ["3"], answers[20]
        assert answer(21, "error")["type"] == "ValueError", answers[21]

        # --- corrective fixes, proven by running the correction the process proposes ---
        def fields(code):
            """The error object the failing statement really produces on the wire."""
            return run(code, ok=False)["error"]

        unambiguous = {
            "attribute": "bpy.context.object.locaton = (1, 0, 0)",
            "enum": "bpy.ops.object.select_by_type(type='MESHES')",
            "data hop": "bpy.ops.curve.primitive_bezier_circle_add(); bpy.context.object.bevel_dept",
            "operator keyword": "bpy.ops.mesh.primitive_cube_add(sise=2)",
            # Source positions are UTF-8 byte offsets, and the correction is a byte edit.
            "multibyte source": "名前 = 'キューブ'\nbpy.context.object.locaton = (1, 0, 0)\n",
        }
        for kind, code in unambiguous.items():
            result = fields(code)
            assert result["rna"], (kind, result)
            fix = result["fix"]
            assert fix["code"] != code and fix["reason"], (kind, result)
            print(f"fix ({kind}): {json.dumps(result)}")
            # The correction has to run as submitted, not merely look plausible.
            run(fix["code"])
        assert fields(unambiguous["attribute"])["fix"]["code"] == \
            "bpy.context.object.location = (1, 0, 0)", fields(unambiguous["attribute"])
        assert fields(unambiguous["enum"])["fix"]["code"] == \
            "bpy.ops.object.select_by_type(type='MESH')", fields(unambiguous["enum"])
        assert fields(unambiguous["data hop"])["fix"]["code"].endswith(
            "bpy.context.object.data.bevel_depth"), fields(unambiguous["data hop"])
        assert fields(unambiguous["operator keyword"])["fix"]["code"] == \
            "bpy.ops.mesh.primitive_cube_add(size=2)", fields(unambiguous["operator keyword"])
        assert fields(unambiguous["multibyte source"])["fix"]["code"] == \
            "名前 = 'キューブ'\nbpy.context.object.location = (1, 0, 0)\n", \
            fields(unambiguous["multibyte source"])
        # An identifier with nothing close to it proposes nothing.
        assert "fix" not in fields("bpy.context.object.zzzzzzqqq"), fields("bpy.context.object.zzzzzzqqq")

        # 'XYZY' sits equally close to the 'XYZ' and 'XZY' rotation modes: no fix, never a guess.
        ambiguous = fields("bpy.context.object.rotation_mode = 'XYZY'")
        print("no fix (ambiguous):", json.dumps({**ambiguous, "rna": {
            **ambiguous["rna"], "enum_items": len(ambiguous["rna"]["enum_items"])}}))
        assert "fix" not in ambiguous, ambiguous
        assert {"XYZ", "XZY"} <= {item["identifier"] for item in ambiguous["rna"]["enum_items"]}, ambiguous
    print("agent describe: all assertions passed")


if __name__ == "__main__":
    main()
