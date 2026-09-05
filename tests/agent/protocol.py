# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Exercise the installed process and real Blender data; no scene fixtures.

One-shot verbs are checked through the folded envelope; `repl --standalone`
is checked through the event stream itself, because the envelope is derived
from that stream and nothing else.
"""

import json
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    executable = str(Path(sys.argv[1]).resolve())
    with tempfile.TemporaryDirectory(prefix="agent protocol ") as directory:
        root = Path(directory)

        def raw(*args):
            return subprocess.run([executable, *map(str, args)], cwd=root,
                                  capture_output=True, text=True, timeout=60)

        def call(*args, ok=True):
            process = raw(*args, "--json")
            assert process.returncode == (0 if ok else 1), (args, process.returncode, process.stdout, process.stderr)
            try:
                result = json.loads(process.stdout)
            except ValueError:
                raise AssertionError((args, process.stdout, process.stderr)) from None
            assert result["ok"] is ok, result
            return result

        # A repl is a session: each conversation gets its own directory, as a
        # real one has, so none of them inherits another's `.blender-cli`.
        channels = iter(range(100))

        def repl(*lines, args=("--standalone",)):
            """Return the event stream of one `repl` conversation, in order."""
            channel = root / f"channel-{next(channels)}"
            channel.mkdir()
            process = subprocess.run([executable, "repl", *args], cwd=channel, timeout=120,
                                     input="".join(json.dumps(line) + "\n" for line in lines),
                                     capture_output=True, text=True)
            assert process.returncode == 0, (process.stdout, process.stderr)
            return [json.loads(line) for line in process.stdout.splitlines() if line.strip()]

        help_result = raw("--help")
        assert help_result.returncode == 0 and "repl" in help_result.stdout, help_result
        version = raw("--version")
        assert version.returncode == 0 and "blender-cli 5.3.0-alpha-agent.1" in version.stdout, version

        # Factory startup really contains Blender's default scene, not an invented empty one.
        startup = call("inspect")
        assert {"Cube", "Camera", "Light"} <= {obj["name"] for obj in startup["objects"]}
        blend = root / "scene with spaces.blend"
        call("exec", "-c", "bpy.ops.wm.read_factory_settings(use_empty=True)", "--save", blend)
        result = call("exec", "-c", "import bpy; bpy.ops.mesh.primitive_cube_add()",
                      "--file", blend, "--save")
        assert {"type": "OBJECT", "name": "Cube"} in result["diff"]["added"], result
        assert {"type": "MESH", "name": "Cube"} in result["diff"]["added"], result
        assert result["value"] == "{'FINISHED'}" and result["ms"] >= 0, result
        # A one-shot verb has no snapshot store, but the step counter still counts changes.
        assert result["diff"]["snapshot"] is None and result["diff"]["step"] == 1, result
        inspected = call("inspect", "--file", blend)
        cube, = inspected["objects"]
        assert cube["name"] == "Cube" and cube["type"] == "MESH", cube
        assert cube["mesh"] == {"vertices": 8, "edges": 12, "faces": 6, "uv_layers": ["UVMap"]}, cube

        no_op = call("exec", "-c", "pass", "--file", blend)
        assert no_op["diff"] == {"added": [], "changed": [], "removed": [],
                                 "snapshot": None, "step": 0}, no_op
        assert no_op["value"] is None, no_op
        changed = call("exec", "-c", "bpy.data.objects['Cube'].location.x = 2; bpy.context.view_layer.update()",
                       "--file", blend)
        assert any(item["type"] == "OBJECT" and item["name"] == "Cube" and "transform" in item["fields"]
                   for item in changed["diff"]["changed"]), changed
        geometry = call("exec", "-c",
                        "bpy.data.meshes['Cube'].vertices[0].co.x = 4; bpy.data.meshes['Cube'].update(); "
                        "bpy.context.view_layer.update()", "--file", blend)
        assert any(item["type"] == "MESH" and "geometry" in item["fields"]
                   for item in geometry["diff"]["changed"]), geometry
        deleted = call("exec", "-c", "bpy.data.objects.remove(bpy.data.objects['Cube'], do_unlink=True); "
                       "bpy.data.meshes.remove(bpy.data.meshes['Cube'])", "--file", blend)
        assert {"type": "OBJECT", "name": "Cube"} in deleted["diff"]["removed"], deleted
        assert {"type": "MESH", "name": "Cube"} in deleted["diff"]["removed"], deleted

        expression = call("exec", "-c", "import sys, os; print('hello'); print('error', file=sys.stderr); "
                          "os.write(1, b'native output\\n'); math.sqrt(81)")
        assert expression["stdout"] == "hello\n" and expression["stderr"] == "error\n", expression
        assert expression["value"] == "9.0", expression
        future = call("exec", "-c", "from __future__ import annotations\nx: Missing = 1\n__annotations__")
        assert future["value"] == "{'x': 'Missing'}", future

        script = root / "raising script.py"
        script.write_text("print('before')\nraise RuntimeError('intentional')\n", encoding="utf-8")
        failure = call("exec", script, "--save", root / "must-not-exist.blend", ok=False)
        assert failure["error"] == {"type": "RuntimeError", "message": "intentional", "line": 2}, failure
        assert failure["stdout"] == "before\n" and not (root / "must-not-exist.blend").exists(), failure
        syntax = call("exec", "-c", "x =\n", ok=False)
        assert syntax["error"]["type"] == "SyntaxError" and syntax["error"]["line"] == 1, syntax
        exited = call("exec", "-c", "raise SystemExit(7)", ok=False)
        assert exited["error"]["type"] == "SystemExit", exited
        timed = call("exec", "-c", "while True:\n    pass", "--timeout", "0.01", ok=False)
        assert timed["error"]["type"] == "TimeoutError", timed

        # Build every inspection category through bpy, including expanded RNA settings.
        call("exec", "-c", """
obj = bpy.data.objects['Cube']
obj.modifiers.new('Subdivision', 'SUBSURF').levels = 2
mat = bpy.data.materials.new('Agent Material')
obj.data.materials.append(mat)
arm = bpy.data.armatures.new('Agent Armature')
rig = bpy.data.objects.new('Rig', arm)
bpy.context.collection.objects.link(rig)
bpy.context.view_layer.objects.active = rig
rig.select_set(True)
bpy.ops.object.mode_set(mode='EDIT')
bone = arm.edit_bones.new('Bone')
bone.tail.z = 1
bpy.ops.object.mode_set(mode='OBJECT')
bpy.ops.object.camera_add()
bpy.ops.object.light_add(type='AREA')
collection = bpy.data.collections.new('Agent Collection')
bpy.context.scene.collection.children.link(collection)
collection.objects.link(obj)
""", "--file", blend, "--save")
        full = call("inspect", "--file", blend, "--object", "Cube", "--full")
        cube, = full["objects"]
        assert cube["modifiers"][0]["settings"]["levels"] == 2, cube
        material = next(mat for mat in full["materials"] if mat["name"] == "Agent Material")
        assert material["node_tree"]["nodes"] and isinstance(material["node_tree"]["links"], list), material
        assert full["armatures"][0]["bones"][0]["name"] == "Bone", full
        assert full["cameras"] and full["lights"] and full["collections"], full
        assert full["collections"] == [{"name": "Agent Collection", "objects": ["Cube"], "children": []}], full
        selected = call("inspect", "--file", blend, "--select", 'objects["Cube"].location',
                        'objects["Cube"].modifiers["Subdivision"].levels')
        assert selected["selected"] == {'objects["Cube"].location': [0, 0, 0],
                                        'objects["Cube"].modifiers["Subdivision"].levels': 2}, selected
        missing = call("inspect", "--object", "No Such Object", ok=False)
        assert missing["error"]["type"] == "KeyError", missing
        bad_path = call("inspect", "--select", '__import__("os")', ok=False)
        assert not bad_path["ok"], bad_path
        missing_file = call("inspect", "--file", root / "absent.blend", ok=False)
        assert missing_file["error"]["type"] == "FileNotFoundError", missing_file
        selection = call("inspect", "--select", "location", ok=False)["error"]
        assert selection["type"] == "ValueError" and "relative to bpy.data" in selection["message"], selection
        # Only the request's own source carries a line; runtime frames are not the agent's.
        assert selection["line"] is None, selection
        separate_inline = call("observe", "--layout", "separate", "--inline", ok=False)
        assert "one image" in separate_inline["error"]["message"], separate_inline
        human = raw("exec", "-c", "42")
        assert human.returncode == 0 and json.loads(human.stdout)["value"] == "42", human
        assert "\n  " in human.stdout, "human output is the indented envelope"

        # Every request field is checked against one table; a verb is one request.
        usage = call("session", ok=False)["error"]
        assert usage == {"type": "ProtocolError", "line": None, "message":
            "session requires action: status|feedback|save|close|snapshot|rollback|history"}, usage
        assert "Unknown verb: not-a-verb" in call("not-a-verb", ok=False)["error"]["message"]
        assert "Unknown option for exec" in call("exec", "-c", "1", "--nope", ok=False)["error"]["message"]
        bad_action = call("session", "resurrect", ok=False)["error"]
        assert bad_action["type"] == "ProtocolError" and "must be one of" in bad_action["message"], bad_action
        both = call("exec", "-c", "1", str(script), ok=False)["error"]
        assert both["message"] == "exec requires exactly one of code or script", both
        assert call("exec", ok=False)["error"]["message"] == "exec requires exactly one of code or script"

        # Ops the contract declares but this build does not implement say so by name.
        for op, extra in (("target", ("list",)), ("fit", ("--params", "[]"))):
            stub = call(op, *extra, ok=False)["error"]
            assert stub["type"] == "NotImplemented" and op in stub["message"], stub

        # The event stream is the source; the envelope above is derived from it.
        events = repl({"id": 4, "op": "exec",
                       "code": "import sys\nprint('out')\nprint('err', file=sys.stderr)\n"
                               "bpy.ops.mesh.primitive_cube_add()\n'done'"},
                      {"id": 5, "op": "session", "action": "status"})
        # The order is the contract; which feedback channels are registered is not.
        order = ["value", "diff", "perception", "objective", "image", "done"]
        kinds = [event["event"] for event in events if event["id"] == 4]
        assert kinds[:2] == ["log", "log"] and kinds[-1] == "done", kinds
        ranked = [order.index(kind) for kind in kinds if kind != "log"]
        assert ranked == sorted(ranked) and {"value", "diff"} <= set(kinds), kinds
        pick = lambda kind: next(e for e in events if e["id"] == 4 and e["event"] == kind)
        first, second = [e for e in events if e["id"] == 4 and e["event"] == "log"]
        value, diff, done = pick("value"), pick("diff"), pick("done")
        assert first == {"id": 4, "event": "log", "stream": "stdout", "text": "out\n"}, first
        assert second == {"id": 4, "event": "log", "stream": "stderr", "text": "err\n"}, second
        assert value == {"id": 4, "event": "value", "value": "'done'"}, value
        assert diff["snapshot"].startswith("sha256:") and diff["step"] == 1, diff
        # A repl starts from factory startup, whose default cube already owns the name.
        assert [item["name"] for item in diff["added"] if item["type"] == "OBJECT"] == [
            "Cube.001"], diff
        assert done["ok"] is True and done["ms"] >= 0, done
        status, = [event for event in events if event["id"] == 5]
        assert status["step"] == 1 and status["snapshot"] == diff["snapshot"], status
        assert status["feedback"]["image"]["mode"] == "delta", status

        # Log events arrive while the request runs, not buffered until it ends.
        streamed = repl({"id": 6, "op": "session", "action": "feedback",
                         "feedback": {"perception": False, "image": {"mode": "off"}}},
                        {"id": 7, "op": "exec",
                         "code": "for i in range(3):\n    print('line', i)\n"})
        assert [event.get("text") for event in streamed if event["event"] == "log"] == [
            "line 0\n", "line 1\n", "line 2\n"], streamed

        # Unknown fields and malformed lines are rejected without running anything.
        rejected = repl({"id": 8, "op": "exec", "code": "1", "observe": "front"},
                        {"id": 9, "op": "nonsense"})
        assert rejected[0]["event"] == "error" and rejected[0]["type"] == "ProtocolError", rejected
        assert "Unknown field for exec: 'observe'" in rejected[0]["message"], rejected
        assert "Unknown op" in rejected[1]["message"], rejected

        # A failed request leaves no partial edit behind.
        rolled = repl({"id": 10, "op": "session", "action": "feedback",
                       "feedback": {"perception": False, "image": {"mode": "off"}}},
                      {"id": 11, "op": "exec", "code": "bpy.ops.mesh.primitive_cube_add()"},
                      {"id": 12, "op": "exec",
                       "code": "bpy.ops.mesh.primitive_uv_sphere_add()\nraise RuntimeError('half')"},
                      {"id": 13, "op": "inspect"})
        failed, = [event for event in rolled if event["id"] == 12 and event["event"] == "error"]
        assert failed["type"] == "RuntimeError" and failed["line"] == 2, failed
        surviving, = [event for event in rolled if event["id"] == 13]
        names = {obj["name"] for obj in surviving["objects"]}
        assert "Cube.001" in names and not any(name.startswith("Sphere") for name in names), names
    print("agent protocol: all assertions passed")


if __name__ == "__main__":
    main()
