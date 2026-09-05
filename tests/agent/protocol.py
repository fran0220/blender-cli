# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Exercise the installed process and real Blender data; no scene fixtures."""

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
                                  capture_output=True, text=True, timeout=30)

        def call(*args, ok=True):
            process = raw(*args, "--json")
            assert process.returncode == (0 if ok else 1), (args, process.returncode, process.stdout, process.stderr)
            try:
                result = json.loads(process.stdout)
            except ValueError:
                raise AssertionError((args, process.stdout, process.stderr)) from None
            assert result["ok"] is ok, result
            return result

        help_result = raw("--help")
        assert help_result.returncode == 0 and "exec" in help_result.stdout, help_result
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
        inspected = call("inspect", "--file", blend)
        cube, = inspected["objects"]
        assert cube["name"] == "Cube" and cube["type"] == "MESH", cube
        assert cube["mesh"] == {"vertices": 8, "edges": 12, "faces": 6, "uv_layers": ["UVMap"]}, cube

        no_op = call("exec", "-c", "pass", "--file", blend)
        assert no_op["diff"] == {"added": [], "changed": [], "removed": []}, no_op
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
        for verb in ("session", "compare", "describe"):
            assert call(verb, ok=False)["error"]["type"] == "NotImplemented"
        assert call("not-a-verb", ok=False)["error"]["type"] == "ValueError"
        human = raw("exec", "-c", "42")
        assert human.returncode == 0 and json.loads(human.stdout)["value"] == "42", human
    print("agent protocol: all assertions passed")


if __name__ == "__main__":
    main()
