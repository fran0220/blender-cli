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

MARK = "#program#"


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
            if isinstance(result, dict):
                assert result.get("ok", True) == ok, result
            return result

        def execute(code, ok=True):
            return call("exec", "-c", code, ok=ok)

        def marked(body, ok=True):
            """Run `body` in the session and read back the JSON it prints as `_result`."""
            code = ("import json, agent, agent_program\n" + body +
                    "\n_result['snapshot'] = agent._session.current\n"
                    f"print({MARK!r} + json.dumps(_result))\n")
            envelope = execute(code)
            line = next(text for text in envelope["stdout"].splitlines() if text.startswith(MARK))
            result = json.loads(line[len(MARK):])
            assert result.get("ok", True) == ok, result
            return result

        def program(action, ok=True, **fields):
            # Workstream K's dispatch calls agent_program.request with these same fields.
            payload = json.dumps({"action": action, **fields})
            return marked(
                f"_request = json.loads({payload!r})\n"
                "try:\n"
                "    _result = agent_program.request(agent._session, **_request)\n"
                "except BaseException as _error:\n"
                "    _result = {'ok': False, 'error': {'type': type(_error).__name__,\n"
                "                                      'message': str(_error)}}\n", ok=ok)

        def open_program(previous_autosave=None):
            # Workstream K's session open calls on_session_open and merges its dict.
            return marked(
                "_program = agent_program.attach(agent._session)\n"
                "_result = dict(agent_program.on_session_open("
                f"agent._session, previous_autosave={previous_autosave!r}))\n"
                "_result['base'] = _program.cache.get(_program.key(0))\n")

        def helper():
            return marked("_result = agent_program.helper(agent._session)\n")

        def wait_autosave(path):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if path.is_file() and path.stat().st_size > 0:
                    return
                time.sleep(0.05)
            raise AssertionError(f"Autosave did not appear: {path}")

        def steps_ran():
            ran = counters.read_text().split() if counters.exists() else []
            counters.unlink(missing_ok=True)
            return [int(number) for number in ran]

        recorded = {"snapshot": None}

        def act(code, ok=True, record=True):
            """One agent action: a real exec, then the recording hook the exec path will call."""
            envelope = execute(code, ok=ok)
            if ok and record:
                # A failed exec never reaches this call, so it is never recorded.
                execute("import agent, agent_program\n"
                        f"agent_program.record_from_exec(agent._session, {code!r}, "
                        f"{recorded['snapshot']!r}, {envelope['snapshot']!r}, "
                        f"{envelope['diff']!r})\n")
                recorded["snapshot"] = envelope["snapshot"]
            return envelope

        opened = call("session", "open")
        try:
            recorded["snapshot"] = open_program()["base"]
            assert recorded["snapshot"], "session open must seed the program's base prefix"
            assert program("get")["text"] == "# blender-cli program\n# base: factory\nP = {}\n"

            # Three actions become three steps.
            act("bpy.ops.wm.read_factory_settings(use_empty=True)")
            act("bpy.ops.mesh.primitive_cylinder_add(radius=0.4, depth=1.0)")
            act('bpy.data.objects["Cylinder"].location.z = 0.5')
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
            assert program("get")["text"] == model
            assert [record["n"] for record in program("get")["steps"]] == [1, 2, 3]
            assert helper().keys() >= {"text", "params", "steps", "version", "reproducible"}

            # An exec that changes no data is not a step.
            act("1 + 1")
            assert program("get")["steps"][-1]["n"] == 3
            # A failed exec is not a step.
            act("raise RuntimeError('boom')", ok=False)
            assert program("get")["steps"][-1]["n"] == 3
            # Recording off is the request-level form of `exec --no-record`.
            assert program("record", on=False)["record"] is False
            act("bpy.ops.mesh.primitive_cube_add()")
            assert program("get")["steps"][-1]["n"] == 3, "recording off must not append a step"
            assert program("record", on=True)["record"] is True

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
            baseline = program("set", text=text)
            assert baseline["ran"] == [1, 2, 3] and steps_ran() == [1, 2, 3], baseline
            assert baseline["reproducible"] is True, baseline
            first_set = baseline["version"]

            # Nothing changed: the whole program is a cached prefix.
            idle = program("run")
            assert idle["ran"] == [] and idle["cached"] == 3 and steps_ran() == [], idle
            assert idle["digest"] == baseline["digest"], idle

            # One parameter changes: only the steps from the first reader of it re-run.
            edited = program("set", text=text.replace('"height": 2.0', '"height": 4.0'))
            assert edited["ran"] == [2, 3] and steps_ran() == [2, 3], edited
            assert edited["from_step"] == 2 and edited["cached"] == 1, edited
            assert edited["digest"] != baseline["digest"], edited
            partial_ms, partial = edited["ms"], edited["digest"]

            # The same state reached by a full run from the base is the same content.
            full = marked("_program = agent_program.attach(agent._session)\n"
                          "_program.cache.clear()\n"
                          "_result = _program.run()\n")
            assert full["ran"] == [1, 2, 3] and steps_ran() == [1, 2, 3], full
            assert full["digest"] == partial, (full["digest"], partial)
            # Memfile IDs are process-local identities, so they do not compare.
            assert full["snapshot"] != edited["snapshot"], full
            print(f"re-execution: 2 of 3 steps {partial_ms:.1f} ms, "
                  f"3 of 3 steps from the base {full['ms']:.1f} ms", flush=True)
            print(f'program set transcript: ran={edited["ran"]} from_step={edited["from_step"]} '
                  f'cached={edited["cached"]} digest={partial} full_run_digest={full["digest"]}',
                  flush=True)

            # `Program.set_params` and `Program.run` are the API `fit` drives.
            fitted = marked("_program = agent_program.attach(agent._session)\n"
                            "_result = _program.set_params({'shift': 1.25})\n"
                            "_result['params'] = _program.params\n"
                            "_result['api_version'] = _program.version\n")
            assert fitted["ran"] == [3] and steps_ran() == [3], fitted
            assert fitted["params"] == {"radius": 0.4, "height": 4.0, "shift": 1.25}, fitted
            assert fitted["api_version"] == fitted["version"], fitted
            assert 'P = {"radius": 0.4, "height": 4.0, "shift": 1.25}' in program("get")["text"]

            # History is a tree of parents; rollback moves between versions.
            history = program("history")
            versions = history["versions"]
            assert versions[0]["parent"] is None, versions[0]
            assert all(row["parent"] == versions[index]["version"]
                       for index, row in enumerate(versions[1:])), versions
            assert history["current"] == versions[-1]["version"], history
            assert {"version", "parent", "label", "at", "steps", "reproducible", "message"} == set(
                versions[-1]), versions[-1]
            back = program("rollback", version=first_set)
            assert program("get")["params"] == {"radius": 0.4, "height": 2.0, "shift": 0.5}, back
            steps_ran()
            scaled, = call("inspect")["objects"]
            assert scaled["name"] == "Cylinder" and scaled["scale"][2] == 2.0, scaled
            assert scaled["location"][2] == 0.5, scaled
            # A digest prefix names the same version.
            program("rollback", version=first_set.removeprefix("sha256:")[:12])
            steps_ran()

            # `patch` needs exactly one match.
            ambiguous = program("patch", ok=False, old='open("steps.log", "a")',
                                new='open("steps.log", "a")')
            assert ambiguous["error"]["type"] == "ValueError", ambiguous
            assert "3 found" in ambiguous["error"]["message"], ambiguous
            absent = program("patch", ok=False, old="not in the program", new="x")
            assert "no match" in absent["error"]["message"], absent
            patched = program("patch", old="depth=1.0", new="depth=3.0")
            assert patched["ran"] == [1, 2, 3] and steps_ran() == [1, 2, 3], patched
            assert "depth=3.0" in program("get")["text"]

            # A failing step stops the run and keeps the failing text.
            broken = program("set", ok=False, text=text.replace(
                'bpy.context.object.scale[2] = P["height"]', 'raise RuntimeError("step two")'))
            # Step 1 is unchanged, so it stays a cached prefix and only step 2 is attempted.
            assert broken["ran"] == [] and broken["from_step"] == 2, broken
            assert broken["error"]["step"] == 2, broken
            assert broken["error"]["type"] == "RuntimeError" and broken["error"]["line"] == 2, broken
            assert 'raise RuntimeError("step two")' in program("get")["text"]
            steps_ran()

            # Reproducibility is a static verdict per step.
            mixed = program("set", text=(
                "# blender-cli program\n# base: factory-empty\nP = {}\n"
                "\n# step 1\nbpy.ops.mesh.primitive_cube_add()\n"
                "\n# step 2\nimport time\nbpy.context.scene.frame_current = int(time.time()) % 8\n"))
            assert mixed["reproducible"] is False, mixed
            assert [record["reproducible"] for record in program("get")["steps"]] == [True, False]

            # A crash loses nothing the program can rebuild.
            program("set", text=text)
            version = program("get")["version"]
            steps_ran()
            wait_autosave(root / ".blender-cli" / f'autosave-{opened["session"]}.blend')
            execute("import os; os._exit(3)", ok=False)
        finally:
            # The crashed daemon leaves a stale endpoint; close reports it and cleans up.
            call("session", "close")
        autosave = root / ".blender-cli" / f'autosave-{opened["session"]}.blend'
        assert autosave.is_file(), autosave
        call("session", "open")
        try:
            # An autosave newer than the program stays the recovery path.
            os.utime(autosave, (time.time(), time.time()))
            yielded = open_program(previous_autosave=str(autosave))
            assert "recovered_from" not in yielded and yielded["base"] is None, yielded
            # An autosave older than the program's newest version does not.
            stale = time.time() - 3600
            os.utime(autosave, (stale, stale))
            recovery = open_program(previous_autosave=str(autosave))
            assert recovery["recovered_from"] == "program", recovery
            assert recovery["program"] == version and recovery["ran"] == [1, 2, 3], recovery
            print(f"crash recovery: {json.dumps(recovery)}", flush=True)
            assert steps_ran() == [1, 2, 3]
            rebuilt, = call("inspect")["objects"]
            assert rebuilt["name"] == "Cylinder", rebuilt
            assert rebuilt["scale"][2] == 2.0 and rebuilt["location"][2] == 0.5, rebuilt
            assert program("get")["version"] == version
        finally:
            call("session", "close")
    print("agent program: all assertions passed")


if __name__ == "__main__":
    main()
