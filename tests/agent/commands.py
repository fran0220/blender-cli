# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Native commands through the installed CLI and persistent real-Blender stream."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    executable = str(Path(sys.argv[1]).resolve())
    with tempfile.TemporaryDirectory(prefix="agent native commands ") as directory:
        root = Path(directory)

        def cli(*arguments):
            process = subprocess.run([executable, *arguments, "--json"], cwd=root,
                                     capture_output=True, text=True, encoding="utf-8", timeout=180)
            assert process.returncode == 0, (arguments, process.stdout, process.stderr)
            result = json.loads(process.stdout)
            assert result["ok"], result
            return result

        capabilities = cli("capabilities")
        assert {"object", "data", "operator", "rig", "animation"} <= set(capabilities["native_commands"])
        assert capabilities["features"] and all(type(v) is bool for v in capabilities["features"].values())
        created = cli("object", "create", "CLI Cube", "--location", "[1,2,3]")
        assert created["name"] == "CLI Cube", created
        schema = cli("describe", "schema")
        assert {"object", "data", "operator", "scene", "capabilities"} <= set(schema["requests"]), schema
        assert schema["requests"]["data"]["properties"]["action"]["enum"] == ["get", "set", "call"]
        assert schema["requests"]["object"]["properties"]["location"]["items"]["type"] == "number"

        requests = []
        expected = {}

        def add(op, action=None, *, check=None, error=None, **fields):
            request = {"id": len(requests) + 1, "op": op, **fields}
            if action is not None:
                request["action"] = action
            requests.append(request)
            expected[request["id"]] = (check, error)

        def equals(field, value):
            return lambda result: result[field] == value

        cube = 'objects["Native Cube"]'
        material = 'materials["Native Material"]'
        add("scene", "reset")
        add("object", "create", name="Native Cube", location=[1, 2, 3],
            check=equals("name", "Native Cube"))
        add("data", "get", path=cube + ".location", check=equals("value", [1, 2, 3]))
        add("object", "transform", objects=["Native Cube"], location=[4, 5, 6], scale=[2, 2, 2])
        add("data", "get", path=cube + ".location[1]", check=equals("value", 5))
        add("data", "set", path=cube + ".location[1]", value=7, check=equals("value", 7))
        add("data", "set", path=cube + ".location", value=[1, 2], error="length")
        add("data", "set", path=cube + ".hide_render", value="yes", error="boolean")
        add("data", "set", path=cube + ".type", value="EMPTY", error="read-only")
        add("data", "get", path='objects["Missing"].location', error="resolve")
        add("data", "get", path=cube + ".location[999]", error="resolve")
        add("data", "set", path=cube + ".rotation_mode", value="NOT_A_MODE", error="enum")
        add("data", "call", path="materials.new", arguments={"name": "Native Material"},
            check=lambda result: result["value"]["material"]["path"] == material)
        add("data", "get", path=material,
            check=lambda result: result["value"]["path"] == material)
        add("data", "get", path=material + ".node_tree",
            check=lambda result: result["value"]["path"] == material + ".node_tree")
        add("data", "call", path=cube + ".data.materials.append", arguments={"material": {"path": material}})
        add("data", "get", path=cube + ".data.materials[0]",
            check=lambda result: result["value"]["name"] == "Native Material")
        add("data", "call", path=material + ".node_tree.nodes.new", arguments={"type": "ShaderNodeValue"},
            check=lambda result: result["value"]["node"]["path"] == material + '.node_tree.nodes["Value"]')
        add("data", "set", path=material + '.node_tree.nodes["Value"].outputs[0].default_value', value=0.25,
            check=equals("value", 0.25))
        add("data", "call", path="collections.new", arguments={"name": "Native Collection"})
        add("data", "call", path='scenes[0].collection.children.link',
            arguments={"child": {"path": 'collections["Native Collection"]'}})
        add("data", "call", path='collections["Native Collection"].objects.link',
            arguments={"object": {"path": cube}})
        add("data", "call", path=cube + ".vertex_groups.new", arguments={"name": "Weights"})
        group = cube + '.vertex_groups["Weights"]'
        add("data", "call", path=group + ".add", arguments={"index": [0, 1, 2, 3], "weight": 0.5, "type": "REPLACE"})
        add("data", "call", path=group + ".weight", arguments={"index": 2},
            check=lambda result: result["value"]["weight"] == 0.5)
        add("data", "call", path=group + ".add", arguments={"index": [0], "weight": 2, "type": "REPLACE"}, error="range")
        add("data", "call", path="materials.new", arguments={}, error="required")
        add("data", "call", path="materials.new", arguments={"name": "Bad", "unknown": 1}, error="argument")
        add("data", "call", path=cube + ".data.materials.append", arguments={"material": {"path": cube}}, error="type")
        # Upstream returns zero for an out-of-mesh index; an existing unassigned
        # vertex reports the RNA error that this test exercises.
        add("data", "call", path=group + ".weight", arguments={"index": 9999},
            check=lambda result: result["value"]["weight"] == 0.0)
        add("data", "call", path=group + ".weight", arguments={"index": 7}, error="Vertex not in group")
        add("object", "create", name="Native Camera", type="CAMERA", scale=[2, 3, 4])
        add("data", "get", path='objects["Native Camera"].scale', check=equals("value", [2, 3, 4]))
        add("data", "set", path="scenes[0].camera", value={"path": 'objects["Native Camera"]'})
        add("data", "get", path="scenes[0].camera", check=lambda result: result["value"]["name"] == "Native Camera")
        add("data", "set", path="scenes[0].camera", value=None, check=equals("value", None))
        add("operator", "describe", name="mesh.primitive_cube_add",
            check=lambda result: any(prop["name"] == "size" for prop in result["properties"]))
        add("operator", "list", check=lambda result: "MESH_OT_primitive_cube_add" in result["operators"])
        add("operator", "call", name="object.shade_smooth", objects=["Native Cube"], active="Native Cube", mode="OBJECT")
        add("operator", "call", name="mesh.select_all", objects=["Native Cube"], active="Native Cube", mode="EDIT", properties={"action": "SELECT"})
        add("operator", "call", name="object.mode_set", properties={"mode": "OBJECT"})
        add("operator", "call", name="object.shade_smooth", objects=["Missing"], error="not found")
        add("operator", "call", name="mesh.primitive_cube_add", properties={"not_a_property": 1}, error="property")
        add("operator", "call", name="mesh.select_all", error="poll")
        # Only registration is an explicit extension. Executing this operator through
        # the native channel must reject it, not silently cross into Python.
        add("exec", code="class NativeTestOperator(bpy.types.Operator):\n"
                         "    bl_idname = 'object.native_test_extension'\n"
                         "    bl_label = 'Native test extension'\n"
                         "    def execute(self, context):\n"
                         "        return {'FINISHED'}\n"
                         "bpy.utils.register_class(NativeTestOperator)")
        add("operator", "call", name="object.native_test_extension", error="explicit exec extension")
        add("scene", "frame", frame=17, check=equals("frame", 17))
        blend = root / "native scene.blend"
        add("scene", "save", path=str(blend), check=equals("path", str(blend)))
        add("object", "delete", objects=["Native Cube"])
        add("data", "get", path=cube, error="resolve")
        add("scene", "open", path=str(blend))
        add("data", "get", path=cube + ".location", check=equals("value", [4, 7, 6]))
        add("scene", "save", path="relative.blend", error="absolute")
        add("scene", "reset", empty=False)
        add("data", "get", path='objects["Cube"]', check=lambda result: result["value"]["name"] == "Cube")
        add("scene", "reset")
        add("data", "get", path="objects", check=equals("value", []))

        process = subprocess.run([executable, "repl", "--standalone"], cwd=root,
                                 input="".join(json.dumps(request) + "\n" for request in requests),
                                 capture_output=True, text=True, encoding="utf-8", timeout=600)
        assert process.returncode == 0, (process.stdout, process.stderr)
        events = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
        for request in requests:
            own = [event for event in events if event.get("id") == request["id"]]
            done = [event for event in own if event["event"] == "done"]
            errors = [event for event in own if event["event"] == "error"]
            check, error = expected[request["id"]]
            if error is not None:
                assert len(errors) == 1 and not done, (request, own)
                assert own[-1] == errors[0] and error.lower() in json.dumps(errors).lower(), (request, own)
            else:
                assert len(done) == 1 and not errors and own[-1] == done[0], (request, own)
                assert check is None or check(done[0]), (request, own)
        assert blend.is_file() and blend.stat().st_size > 0
        print(f"native commands: CLI/schema and {len(requests)} persistent requests passed")


if __name__ == "__main__":
    main()
