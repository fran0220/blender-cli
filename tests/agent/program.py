# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""The program model against the real CLI: recording, prefix cache, versions, recovery."""

import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def main():
    executable = str(Path(sys.argv[1]).resolve())
    with (
        tempfile.TemporaryDirectory(prefix="agent program ") as directory,
        contextlib.chdir(directory),
    ):
        root = Path(directory).resolve()
        counters = root / "steps.log"

        def call(*args, ok=True, cwd=root):
            process = subprocess.run([executable, *map(str, args), "--json"], cwd=cwd,
                                     capture_output=True, text=True, timeout=180)
            assert process.returncode == (0 if ok else 1), (args, process.stdout, process.stderr)
            result = json.loads(process.stdout)
            assert result.get("ok", True) == ok, (args, result)
            return result

        def execute(code, *args, ok=True):
            return call("exec", "-c", code, *args, ok=ok)

        def program(action, *args, ok=True):
            return call("program", action, *args, ok=ok)

        def value(code):
            """The repr of one expression, evaluated in the session."""
            return execute(code)["value"]

        def wait_autosave(path):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if path.is_file() and path.stat().st_size > 0:
                    return
                time.sleep(0.05)
            raise AssertionError(f"Autosave did not appear: {path}")

        def steps_ran():
            ran = counters.read_text().split() if counters.exists() else []
            counters.unlink(missing_ok=True)
            return [int(number) for number in ran]

        # One-shot mode has no session and so no program: a bare exec must not leave a
        # model.py behind for the next session to replay.
        call("exec", "-c", "bpy.ops.mesh.primitive_cube_add()")
        assert not (root / ".blender-cli" / "program").exists(), "one-shot wrote a program"
        assert "not implemented" in call("program", "get", ok=False)["error"]["message"]

        opened = call("session", "open")
        try:
            assert program("get")["text"] == "# blender-cli program\n# base: factory\nP = {}\n"
            # An empty program has no version to label, and that is not an error.
            assert call("session", "snapshot", "--label", "start")["snapshot"]

            # Three actions become three steps, recorded by the exec path itself.
            execute("bpy.ops.wm.read_factory_settings(use_empty=True)")
            execute("bpy.ops.mesh.primitive_cylinder_add(radius=0.4, depth=1.0)")
            execute('bpy.data.objects["Cylinder"].location.z = 0.5')
            model = (root / ".blender-cli" / "program" / "model.py").read_text()
            expected = (
                "# blender-cli program\n"
                "# base: factory\n"
                "P = {}\n"
                "\n# step 1\n"
                "bpy.ops.wm.read_factory_settings(use_empty=True)\n"
                "\n# step 2\n"
                "bpy.ops.mesh.primitive_cylinder_add(radius=0.4, depth=1.0)\n"
                "\n# step 3\n"
                'bpy.data.objects["Cylinder"].location.z = 0.5\n')
            assert model == expected, model
            print("=== model.py after three execs ===\n" + model, flush=True)
            current = program("get")
            assert current["text"] == model
            assert [record["n"] for record in current["steps"]] == [1, 2, 3]
            # `agent.program()` answers the same program from inside the session.
            assert value("agent.program()['version']") == repr(current["version"])
            assert value("sorted(agent.program())") == repr(
                ["params", "reproducible", "steps", "text", "version"])

            # An exec that changes no data is not a step.
            execute("1 + 1")
            assert program("get")["steps"][-1]["n"] == 3
            # A failed exec is not a step.
            execute("raise RuntimeError('boom')", ok=False)
            assert program("get")["steps"][-1]["n"] == 3
            # `--no-record` is not a step either.
            execute("bpy.ops.mesh.primitive_cube_add()", "--no-record")
            assert program("get")["steps"][-1]["n"] == 3, "--no-record must not append a step"
            # Neither is anything at all while recording is off.
            assert program("record", "off")["record"] is False
            execute("bpy.ops.mesh.primitive_cone_add()")
            assert program("get")["steps"][-1]["n"] == 3, "recording off must not append a step"
            assert program("record", "on")["record"] is True

            # A parameterised program. Each step logs its number, so the test sees what ran.
            text = ("# blender-cli program\n"
                    "# base: factory-empty\n"
                    'P = {"radius": 0.4, "height": 2.0, "shift": 0.5}\n'
                    "\n# step 1\n"
                    'open("steps.log", "a").write("1\\n")\n'
                    'bpy.ops.mesh.primitive_cylinder_add(radius=P["radius"], depth=1.0)\n'
                    "\n# step 2\n"
                    'open("steps.log", "a").write("2\\n")\n'
                    'bpy.context.object.scale[2] = P["height"]\n'
                    "\n# step 3\n"
                    'open("steps.log", "a").write("3\\n")\n'
                    'bpy.data.objects["Cylinder"].location.z = P["shift"]\n')
            counters.unlink(missing_ok=True)
            baseline = program("set", "--text", text)
            assert baseline["ran"] == [1, 2, 3] and steps_ran() == [1, 2, 3], baseline
            assert baseline["reproducible"] is True, baseline
            first_set = baseline["version"]

            # Nothing changed: the whole program is a cached prefix.
            idle = program("run")
            assert idle["ran"] == [] and idle["cached"] == 3 and steps_ran() == [], idle
            assert idle["digest"] == baseline["digest"], idle

            # One parameter changes: only the steps from the first reader of it re-run.
            edited = program("set", "--text", text.replace('"height": 2.0', '"height": 4.0'))
            assert edited["ran"] == [2, 3] and steps_ran() == [2, 3], edited
            assert edited["from_step"] == 2 and edited["cached"] == 1, edited
            assert edited["digest"] != baseline["digest"], edited

            # The same state reached by a full run from the base is the same content.
            # These execs drive the program, so recording them would nest it in itself.
            full = execute("import agent, agent_program\n"
                           "_program = agent_program.attach(agent._session)\n"
                           "_program.cache.clear()\n"
                           "_program.run()['digest']", "--no-record")
            assert steps_ran() == [1, 2, 3], full
            assert full["value"] == repr(edited["digest"]), (full["value"], edited["digest"])
            print(f're-execution: 2 of 3 steps {edited["ms"]:.1f} ms, '
                  f'3 of 3 steps from the base {full["ms"]:.1f} ms', flush=True)
            print(f'program set transcript: ran={edited["ran"]} from_step={edited["from_step"]} '
                  f'cached={edited["cached"]} digest={edited["digest"]}', flush=True)

            # One `fit` evaluation: the Program API workstream T drives.
            fitted = execute("import agent, agent_program\n"
                             "_program = agent_program.attach(agent._session)\n"
                             "(_program.set_params({'shift': 1.25})['ran'], _program.params)",
                             "--no-record")
            assert fitted["value"] == repr(([3], {"radius": 0.4, "height": 4.0, "shift": 1.25}))
            assert steps_ran() == [3], fitted
            assert 'P = {"radius": 0.4, "height": 4.0, "shift": 1.25}' in program("get")["text"]
            # A parameter no step reads re-executes nothing.
            unused = execute("import agent, agent_program\n"
                             "agent_program.attach(agent._session).set_params({'unused': 7})['ran']",
                             "--no-record")
            assert unused["value"] == "[]" and steps_ran() == [], unused

            # History is a tree of parents; rollback moves between versions.
            history = program("history")
            versions = history["versions"]
            assert versions[0]["parent"] is None, versions[0]
            assert all(row["parent"] == versions[index]["version"]
                       for index, row in enumerate(versions[1:])), versions
            assert history["current"] == versions[-1]["version"], history
            assert {"version", "parent", "label", "at", "steps", "reproducible", "message",
                    "failed"} == set(versions[-1]), versions[-1]
            program("rollback", "--version", first_set, "--label", "shape")
            steps_ran()
            assert program("get")["params"] == {"radius": 0.4, "height": 2.0, "shift": 0.5}
            scaled, = call("inspect")["objects"]
            assert scaled["name"] == "Cylinder" and scaled["scale"][2] == 2.0, scaled
            assert scaled["location"][2] == 0.5, scaled
            # A digest prefix and a label name the same version.
            program("rollback", "--version", first_set.removeprefix("sha256:")[:12])
            assert program("rollback", "--version", "shape")["version"] == first_set
            steps_ran()

            # `patch` needs exactly one match.
            ambiguous = program("patch", "--old", 'open("steps.log", "a")',
                                "--new", 'open("steps.log", "a")', ok=False)
            assert ambiguous["error"]["type"] == "ValueError", ambiguous
            assert "3 found" in ambiguous["error"]["message"], ambiguous
            absent = program("patch", "--old", "not in the program", "--new", "x", ok=False)
            assert "no match" in absent["error"]["message"], absent
            assert "program set requires text" in program("set", ok=False)["error"]["message"]
            patched = program("patch", "--old", "depth=1.0", "--new", "depth=3.0")
            assert patched["ran"] == [1, 2, 3] and steps_ran() == [1, 2, 3], patched
            assert "depth=3.0" in program("get")["text"]

            # A failing step names itself, and the failed edit never becomes the scene.
            live = call("inspect")["objects"]
            broken = program("set", "--text", text.replace(
                'bpy.context.object.scale[2] = P["height"]',
                'raise RuntimeError("step two")'), ok=False)
            assert broken["error"]["type"] == "RuntimeError", broken
            assert broken["error"]["line"] == 2, broken
            assert broken["error"]["message"] == "step 2: step two", broken
            assert call("inspect")["objects"] == live, "a failed request leaves no partial edit"
            # The text keeps the edit that failed, and its version says why: a file is
            # not Main, and the agent patches the text it can see.
            assert 'raise RuntimeError("step two")' in program("get")["text"]
            failed = program("history")["versions"][-1]
            assert failed["failed"] is True and failed["step"] == 2, failed
            assert failed["line"] == 2 and failed["version"] == program("get")["version"], failed
            assert all(row["failed"] is False for row in program("history")["versions"][:-1])
            steps_ran()

            # The prefix the failed run reached is still cached, so the correction is
            # cheap: step 1 does not run again even though steps 2 and 3 are new text.
            corrected = program("set", "--text", text.replace(
                'bpy.context.object.scale[2] = P["height"]',
                'bpy.context.object.scale[2] = P["height"] * 1.5'))
            assert corrected["ran"] == [2, 3] and steps_ran() == [2, 3], corrected
            assert corrected["from_step"] == 2 and corrected["cached"] == 1, corrected

            # Reproducibility is a static verdict per step.
            mixed = program("set", "--text", (
                "# blender-cli program\n# base: factory-empty\nP = {}\n"
                "\n# step 1\nbpy.ops.mesh.primitive_cube_add()\n"
                "\n# step 2\nimport time\nbpy.context.scene.frame_current = int(time.time()) % 8\n"))
            assert mixed["reproducible"] is False, mixed
            assert [record["reproducible"] for record in program("get")["steps"]] == [True, False]

            # Scenes that differ only in a modifier setting are different scenes. The
            # viewport level never reaches the mesh datablock, so only the RNA walk
            # separates these two.
            subsurf = ("# blender-cli program\n# base: factory-empty\n"
                       'P = {"levels": 2}\n'
                       "\n# step 1\nbpy.ops.mesh.primitive_cube_add()\n"
                       'bpy.context.object.modifiers.new("Subsurf", "SUBSURF").levels = P["levels"]\n')
            coarse = program("set", "--text", subsurf)
            fine = program("set", "--text", subsurf.replace('"levels": 2', '"levels": 3'))
            assert fine["ran"] == [1], fine
            assert fine["digest"] != coarse["digest"], (coarse["digest"], fine["digest"])

            # Curve control points are content too; RNA collapses them to references.
            circle = ("# blender-cli program\n# base: factory-empty\n"
                      'P = {"radius": 1.0}\n'
                      "\n# step 1\n"
                      'bpy.ops.curve.primitive_bezier_circle_add(radius=P["radius"])\n')
            small = program("set", "--text", circle)
            large = program("set", "--text", circle.replace('"radius": 1.0', '"radius": 2.0'))
            assert large["digest"] != small["digest"], (small["digest"], large["digest"])

            # A crash loses nothing the program can rebuild.
            restored = program("set", "--text", text)
            steps_ran()
            assert restored["reproducible"] is True, restored
            wait_autosave(root / ".blender-cli" / f'autosave-{opened["session"]}.blend')
            execute("import os; os._exit(3)", ok=False)
        finally:
            # The crashed daemon leaves a stale endpoint; close reports it and cleans up.
            call("session", "close")

        autosave = root / ".blender-cli" / f'autosave-{opened["session"]}.blend'
        assert autosave.is_file(), autosave
        # An autosave newer than the program stays the recovery path.
        os.utime(autosave, (time.time(), time.time()))
        call("session", "open")
        try:
            assert call("session", "status")["recovered_from"] is None, "autosave wins"
            assert steps_ran() == []
        finally:
            call("session", "close")

        # An autosave older than the program's newest version does not.
        stale = time.time() - 3600
        os.utime(autosave, (stale, stale))
        reopened = call("session", "open")
        try:
            status = call("session", "status")
            assert status["recovered_from"] == "program", status
            print(f"crash recovery: {json.dumps(status)}", flush=True)
            assert steps_ran() == [1, 2, 3]
            rebuilt, = call("inspect")["objects"]
            assert rebuilt["name"] == "Cylinder", rebuilt
            assert rebuilt["scale"][2] == 2.0 and rebuilt["location"][2] == 0.5, rebuilt
            assert program("get")["version"] == restored["version"]
            # The rebuilt session records the next action as step 4.
            execute("bpy.ops.mesh.primitive_cube_add(size=0.2)")
            assert program("get")["steps"][-1]["n"] == 4
        finally:
            call("session", "close")
        assert reopened["session"] != opened["session"]

        # A session opened on a file gets that file. The program is the truth only
        # when recovering, never over a scene the agent asked for by name.
        scene = root / "explicit.blend"
        call("session", "open")
        try:
            execute("bpy.ops.wm.read_factory_settings(use_empty=True); "
                    "bpy.ops.mesh.primitive_torus_add()", "--no-record")
            call("session", "save", "--file", scene)
        finally:
            call("session", "close")
        # That plain open recovered from the program, as it should; drain its counters
        # so the next open's silence is the thing being measured.
        assert steps_ran() == [1, 2, 3]
        call("session", "open", "--file", scene)
        try:
            assert call("session", "status")["recovered_from"] is None, "replayed over --file"
            loaded, = call("inspect")["objects"]
            assert loaded["name"] == "Torus", loaded
            assert steps_ran() == [], "opening a file must not run the program"
        finally:
            call("session", "close")
    print("agent program: all assertions passed")


if __name__ == "__main__":
    main()
