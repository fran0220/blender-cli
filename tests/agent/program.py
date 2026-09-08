# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Structured programs against the real CLI, memfile store and recovery process."""

import ast
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def main():
    executable = str(Path(sys.argv[1]).resolve())
    with tempfile.TemporaryDirectory(prefix="agent program ") as directory:
        root = Path(directory)

        def call(*args, ok=True, cwd=root):
            process = subprocess.run([executable, *map(str, args), "--json"], cwd=cwd,
                                     capture_output=True, text=True, encoding="utf-8", timeout=300)
            assert process.returncode == (0 if ok else 1), (args, process.stdout, process.stderr)
            result = json.loads(process.stdout)
            assert result.get("ok", True) == ok, result
            return result

        def program(action, *args, ok=True, cwd=root):
            return call("program", action, *args, ok=ok, cwd=cwd)

        def set_model(model, **kwargs):
            return program("set", "--text", json.dumps(model), **kwargs)

        def extension(code, **kwargs):
            return call("exec", "-c", code, "--no-record", **kwargs)

        def model():
            return json.loads(program("get")["text"])

        def objects():
            return call("inspect")["objects"]

        def clear_cache(cwd=root):
            return extension("import agent_program\n"
                             "agent_program.attach(agent._session).cache.clear()", cwd=cwd)

        call("exec", "-c", "1 + 1")
        assert not (root / ".blender-cli" / "program").exists()
        opened = call("session", "open")
        try:
            assert model() == {"base": "factory-default", "params": {}, "steps": []}
            assert call("session", "snapshot", "--label", "start")["version"] is None
            call("scene", "reset")
            call("object", "create", "Body", "--type", "MESH", "--primitive", "cube")
            call("object", "transform", "Body", "--scale", "[1,1,2]")
            call("exec", "-c", 'bpy.data.objects["Body"].location.z = 0.5')
            recorded = model()
            assert [step["op"] for step in recorded["steps"]] == ["scene", "object", "object", "exec"]
            assert all("id" not in step for step in recorded["steps"])
            assert recorded["steps"][3]["code"] == 'bpy.data.objects["Body"].location.z = 0.5'
            path = root / ".blender-cli" / "program" / "model.json"
            assert json.loads(path.read_text()) == recorded
            assert not path.with_suffix(".py").exists()
            assert all(p.suffix == ".json" for p in (path.parent / "versions").iterdir())
            call("exec", "-c", "1 + 1")
            call("exec", "-c", "raise RuntimeError('boom')", ok=False)
            extension("bpy.context.scene.frame_set(3)")
            program("record", "off")
            call("scene", "frame", "--frame", 4)
            program("record", "on")
            assert model() == recorded
            commands = [{"op": "object", "action": "create", "type": "EMPTY",
                         "name": "Recorded", "as": "made"},
                        {"op": "object", "action": "transform", "name": {"$ref": "made.name"},
                         "location": [1, 2, 3]}]
            call("batch", "--steps", json.dumps(commands))
            assert model()["steps"][-1] == {"op": "batch", "steps": commands}

            source = {"base": "factory-empty", "params": {"height": 2.0, "shift": 0.5},
                      "steps": [
                          {"op": "object", "action": "create", "type": "MESH",
                           "primitive": "cube", "name": "Body", "as": "body"},
                          {"op": "object", "action": "transform", "name": {"$ref": "body.name"},
                           "scale": [1, 1, {"$param": "height"}]},
                          {"op": "object", "action": "transform", "name": {"$ref": "body.name"},
                           "location": [0, 0, {"$param": "shift"}]}]}
            baseline = set_model(source)
            assert baseline["ran"] == [1, 2, 3] and baseline["reproducible"], baseline
            idle = program("run")
            assert idle["ran"] == [] and idle["cached"] == 3, idle
            assert idle["digest"] == baseline["digest"]
            forced = program("run", "--from-step", 2)
            assert forced["ran"] == [2, 3] and forced["cached"] == 1, forced
            assert forced["digest"] == baseline["digest"]
            program("run", "--from-step", 4, ok=False)
            edited_source = copy.deepcopy(source)
            edited_source["params"]["height"] = 4.0
            edited = set_model(edited_source)
            assert edited["ran"] == [2, 3] and edited["cached"] == 1, edited
            assert edited["digest"] != baseline["digest"]
            assert objects()[0]["scale"][2] == 4.0
            clear_cache()
            full = program("run")
            assert full["ran"] == [1, 2, 3] and full["digest"] == edited["digest"], full
            fitted = extension("import agent_program\n"
                               "agent_program.attach(agent._session).set_params({'shift': 1.25})['ran']")
            assert fitted["value"] == "[3]", fitted
            unused = extension("import agent_program\n"
                               "agent_program.attach(agent._session).set_params({'unused': 7})['ran']")
            assert unused["value"] == "[]", unused
            named = call("session", "snapshot", "--label", "milestone")
            assert named["version"] == program("get")["version"]
            assert program("rollback", baseline["version"])["digest"] == baseline["digest"]
            program("rollback", "milestone")
            assert model()["params"]["shift"] == 1.25
            missing = program("patch", "--old", "not present", "--new", "x", ok=False)
            assert "no match" in missing["error"]["message"]
            ambiguous = program("patch", "--old", '"op"', "--new", '"op"', ok=False)
            assert "3 found" in ambiguous["error"]["message"]
            patched = program("patch", "--old", '"shift": 1.25', "--new", '"shift": 2.5')
            assert patched["ran"] == [3], patched

            live = objects()
            broken_source = copy.deepcopy(source)
            broken_source["steps"][1] = {"op": "exec", "code": "raise RuntimeError('step two')"}
            broken = set_model(broken_source, ok=False)
            assert broken["error"]["step"] == 2 and broken["error"]["cached_through"] == 1, broken
            assert broken["error"]["version"] == program("get")["version"]
            assert objects() == live, "failed set left partial scene changes"
            assert program("history")["versions"][-1]["failed"] is True
            corrected = copy.deepcopy(source)
            corrected["params"]["height"] = 3.0
            assert set_model(corrected)["ran"] == [2, 3]
            # Invalid source is rejected without replacing the durable current program.
            before = model()
            for invalid in ["P = {}", '{"base":"factory","params":{},"steps":[]}',
                            json.dumps({**source, "steps": [{"op": "program", "action": "run"}]}),
                            json.dumps({**source, "steps": [{"id": 9, "op": "scene", "action": "reset"}]})]:
                program("set", "--text", invalid, ok=False)
                assert model() == before
            for invalid_step in [{"op": "unknown"}, {"op": "scene", "action": "typo"},
                                 {"op": "scene", "action": "reset", "extra": 1},
                                 {"op": "batch", "steps": [], "as": []},
                                 {"op": "object", "action": "create", "location": [0, 1]}]:
                set_model({**source, "steps": [invalid_step]}, ok=False)
                assert model() == before

            # A read-only result must be replayed and its JSON value restored with a cached prefix.
            reading = copy.deepcopy(source)
            reading["steps"] = [source["steps"][0],
                                {"op": "data", "action": "get", "path": 'objects["Body"].location',
                                 "as": "position"},
                                {"op": "object", "action": "create", "type": "EMPTY",
                                 "name": "Copy", "location": {"$ref": "position.value"}},
                                source["steps"][1]]
            set_model(reading)
            reading["params"]["height"] = 6.0
            assert set_model(reading)["ran"] == [4]
            reading["steps"][2]["name"] = "Other"
            assert set_model(reading)["ran"] == [3, 4]
            assert {item["name"] for item in objects()} == {"Body", "Other"}

            # Batch refs see outer producers and local earlier results, never forward results.
            batch = copy.deepcopy(source)
            batch["steps"] = [source["steps"][0], {"op": "batch", "steps": [
                {"op": "object", "action": "transform", "name": {"$ref": "body.name"},
                 "location": [1, 2, {"$param": "shift"}]},
                {"op": "object", "action": "create", "name": "Child", "type": "EMPTY", "as": "child"},
                {"op": "object", "action": "transform", "name": {"$ref": "child.name"},
                 "location": [1, 2, 3]}]}]
            set_model(batch)
            assert len(objects()) == 2
            assert model() == batch
            nested = copy.deepcopy(batch)
            nested["steps"][1]["steps"] = [{"op": "batch", "steps": []}]
            set_model(nested, ok=False)
            forward = copy.deepcopy(source)
            forward["steps"][0]["name"] = {"$ref": "later.name"}
            assert set_model(forward, ok=False)["error"]["step"] == 1

            # Explicit extensions retain the upstream expression result, P and a fresh
            # namespace. RNA globals are rebuilt, not resurrected after memfile restore.
            python = copy.deepcopy(source)
            python["steps"] = [source["steps"][0],
                               {"op": "exec", "code": 'saved = bpy.data.objects["Body"]\nP["height"]',
                                "as": "expression"},
                               {"op": "exec", "code": 'saved.scale.z = P["height"]'}]
            set_model(python)
            result = extension("import agent_program\n"
                               "agent_program.attach(agent._session).results['expression']['value']")
            assert ast.literal_eval(result["value"]) == "2.0", result
            python["params"]["height"] = 8.0
            assert set_model(python)["ran"] == [2, 3]
            assert objects()[0]["scale"][2] == 8.0
            assert program("run")["ran"] == [2, 3]
            python["steps"][2]["code"] = "import time\nbpy.context.scene.frame_set(int(time.time()) % 8)"
            assert set_model(python)["reproducible"] is False
            python["steps"][2]["code"] = 'open("external.txt", "w").write("output")'
            assert set_model(python)["reproducible"] is False

            # Digest still distinguishes content that does not move object bounds:
            # modifier settings, stored mesh attributes, node inputs and curve points.
            for code in [
                    'bpy.ops.mesh.primitive_cube_add()\n'
                    'bpy.context.object.modifiers.new("Subsurf", "SUBSURF").levels = P["amount"]',
                    'bpy.ops.mesh.primitive_cube_add()\n'
                    'attribute = bpy.context.object.data.attributes.new("density", "FLOAT", "POINT")\n'
                    'attribute.data[0].value = P["amount"]',
                    'material = bpy.data.materials.new("Material")\n'
                    'material.use_nodes = True\n'
                    'material.node_tree.nodes["Principled BSDF"].inputs["Roughness"].default_value = P["amount"] / 4',
                    'bpy.ops.curve.primitive_bezier_circle_add(radius=P["amount"])',
                    'bpy.ops.object.grease_pencil_add(type="EMPTY")\n'
                    'pencil = bpy.data.grease_pencils[0]\n'
                    'layer = pencil.layers[0] if pencil.layers else pencil.layers.new("L")\n'
                    'frame = layer.frames[0] if layer.frames else layer.frames.new(1)\n'
                    'frame.drawing.add_strokes([3])\n'
                    'frame.drawing.strokes[0].points[0].position = (0, P["amount"], 0)']:
                content = {"base": "factory-empty", "params": {"amount": 1},
                           "steps": [{"op": "exec", "code": code}]}
                low = set_model(content)
                content["params"]["amount"] = 2
                high = set_model(content)
                assert high["digest"] != low["digest"], code
                clear_cache()
                assert program("run")["digest"] == high["digest"], code

            restored = set_model(source)
            autosave = root / ".blender-cli" / f'autosave-{opened["session"]}.blend'
            extension('bpy.ops.mesh.primitive_torus_add()')
            deadline = time.monotonic() + 30
            reader = root / "reader"
            reader.mkdir()
            while time.monotonic() < deadline:
                if autosave.exists():
                    saved = call("inspect", "--file", autosave, cwd=reader)["objects"]
                    if any(item["name"] == "Torus" for item in saved):
                        break
                time.sleep(0.1)
            else:
                raise AssertionError("autosave never captured unrecorded Torus")
            extension("import os; os._exit(3)", ok=False)
        finally:
            call("session", "close")
        os.utime(autosave, None)
        assert call("session", "open")["recovered_from"] == "autosave"
        try:
            assert {item["name"] for item in objects()} == {"Body", "Torus"}
            assert program("get")["version"] == restored["version"]
        finally:
            call("session", "close")
        for saved in (root / ".blender-cli").glob("autosave-*.blend"):
            os.utime(saved, (time.time() - 3600, time.time() - 3600))
        assert call("session", "open")["recovered_from"] == "program"
        try:
            assert {item["name"] for item in objects()} == {"Body"}
            assert program("run")["digest"] == restored["digest"]
            program("rollback", "milestone")
            assert model()["params"]["shift"] == 1.25
        finally:
            call("session", "close")

        # Explicit file opens do not replay a previously persisted program over the file.
        scene = root / "explicit.blend"
        call("session", "open")
        try:
            extension("bpy.ops.wm.read_factory_settings(use_empty=True)\n"
                      "bpy.ops.mesh.primitive_torus_add()")
            call("session", "save", "--file", scene)
        finally:
            call("session", "close")
        call("session", "open", "--file", scene)
        try:
            assert call("session", "status")["recovered_from"] is None
            assert {item["name"] for item in objects()} == {"Torus"}
            file_model = {"base": str(scene), "params": {}, "steps": []}
            assert model() == file_model
            call("object", "create", "AfterOpen", "--type", "EMPTY")
            assert len(model()["steps"]) == 1 and model()["base"] == str(scene)
            assert program("run")["ran"] == [1]
            assert {item["name"] for item in objects()} == {"Torus", "AfterOpen"}
            assert set_model(file_model)["reproducible"] is False
            assert {item["name"] for item in objects()} == {"Torus"}
        finally:
            call("session", "close")

        # Portable source alone creates its durable version without making an old
        # file look newer than an autosave. No index/version sidecars are copied.
        portable = root / "portable"
        portable_source = portable / ".blender-cli" / "program" / "model.json"
        portable_source.parent.mkdir(parents=True)
        portable_source.write_text(json.dumps(source), encoding="utf-8")
        source_time = time.time_ns() - 3_600_000_000_000
        os.utime(portable_source, ns=(source_time, source_time))
        call("session", "open", cwd=portable)
        try:
            assert call("session", "status", cwd=portable)["recovered_from"] == "program"
            assert program("get", cwd=portable)["version"] == baseline["version"]
            history = program("history", cwd=portable)["versions"]
            assert abs(history[0]["at"] - source_time / 1e9) < 0.001
            assert portable_source.stat().st_mtime_ns == source_time
            assert {item["name"] for item in call("inspect", cwd=portable)["objects"]} == {"Body"}
        finally:
            call("session", "close", cwd=portable)

        # A canonical source that cannot replay must fail open, not report a
        # successful factory scene when there is no autosave to fall back to.
        unrecoverable = root / "unrecoverable"
        failed_source = unrecoverable / ".blender-cli" / "program" / "model.json"
        failed_source.parent.mkdir(parents=True)
        failed_model = {"base": "factory-empty", "params": {}, "steps": [
            {"op": "exec", "code": "raise RuntimeError('canonical replay failed')"}]}
        failed_source.write_text(json.dumps(failed_model), encoding="utf-8")
        assert not list((unrecoverable / ".blender-cli").glob("autosave-*.blend"))
        try:
            failed_open = call("session", "open", cwd=unrecoverable, ok=False)
            assert "error" in failed_open, failed_open
            assert failed_open.get("recovered_from") is None, failed_open
            assert json.loads(failed_source.read_text(encoding="utf-8")) == failed_model
        finally:
            call("session", "close", cwd=unrecoverable)

        # Actual bounded memfile eviction, not a synthetic missing-cache fixture.
        heavy = root / "heavy"
        heavy.mkdir()
        heavy_model = {"base": "factory-empty", "params": {"shift": 0}, "steps": [
            {"op": "object", "action": "create", "name": "Body", "type": "MESH", "primitive": "cube"},
            {"op": "object", "action": "transform", "name": "Body", "location": [0, 0, {"$param": "shift"}]}]}
        heavy_model["steps"] += [{"op": "operator", "action": "call", "name": "MESH_OT_primitive_grid_add",
                                  "properties": {"x_subdivisions": 600, "y_subdivisions": 600}}
                                 for _ in range(12)]
        call("session", "open", cwd=heavy)
        try:
            call("session", "feedback", "perception=false", "objective=false", "image.mode=off", cwd=heavy)
            set_model(heavy_model, cwd=heavy)
            reach = extension("import agent_program\n"
                              "p = agent_program.attach(agent._session)\n"
                              "def restorable(snapshot):\n"
                              "    try: agent._session.native['rollback'](snapshot)\n"
                              "    except KeyError: return False\n"
                              "    return True\n"
                              "(restorable(p.cache[p.key(1)]), restorable(p.cache[p.key(14)]))", cwd=heavy)
            assert reach["value"] == "(False, True)", reach
            heavy_model["params"]["shift"] = 1.5
            moved = set_model(heavy_model, cwd=heavy)
            assert moved["cached"] == 0 and moved["ran"] == list(range(1, 15)), moved
            clear_cache(heavy)
            assert program("run", cwd=heavy)["digest"] == moved["digest"]
        finally:
            call("session", "close", cwd=heavy)
    print("agent structured program: all assertions passed")


if __name__ == "__main__":
    main()
