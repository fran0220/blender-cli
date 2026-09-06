# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Real installed CLI/AF_UNIX session, memfile restoration and latency checks."""

import contextlib
import json
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import tempfile
import time

from gpu import require_device


@contextlib.contextmanager
def connection(endpoint):
    # CPython 3.13 does not expose Windows AF_UNIX address conversion
    # (python/cpython#77589). Connect the real Winsock socket directly, then
    # use Python's normal timeout, stream and send/receive operations.
    family = 1 if sys.platform == "win32" else socket.AF_UNIX
    with socket.socket(family, socket.SOCK_STREAM) as client:
        if sys.platform == "win32":
            import ctypes

            class Address(ctypes.Structure):
                _fields_ = [("family", ctypes.c_ushort), ("path", ctypes.c_char * 108)]

            address = Address(1, endpoint.encode("utf-8"))
            winsock = ctypes.WinDLL("ws2_32.dll")
            winsock.connect.argtypes = [ctypes.c_size_t, ctypes.c_void_p, ctypes.c_int]
            if winsock.connect(client.fileno(), ctypes.byref(address), ctypes.sizeof(address)):
                raise OSError(winsock.WSAGetLastError(), "Winsock AF_UNIX connect")
        else:
            client.connect(endpoint)
        client.settimeout(10)
        yield client


def greeting(stream):
    """Every peer is told what it joined before it asks anything."""
    event = json.loads(stream.readline())
    assert event["event"] == "session" and event["id"] is None, event
    return event


def events_until_end(stream, request_id):
    """Collect one request's events, in order, up to its terminal event."""
    events = []
    while True:
        event = json.loads(stream.readline())
        assert event["id"] == request_id, event
        events.append(event)
        if event["event"] in ("done", "error"):
            return events


def main():
    executable = str(Path(sys.argv[1]).resolve())
    # Deliberately exceed sockaddr_un.sun_path even on Linux. Raw clients use
    # the same endpoint relative to cwd; shortening TMPDIR would hide the bug.
    with (
        tempfile.TemporaryDirectory(prefix="agent session " + "x" * 96) as directory,
        contextlib.chdir(directory),
    ):
        root = Path(directory).resolve()

        def call(*args, ok=True, cwd=root, timeout=30):
            process = subprocess.run([executable, *map(str, args), "--json"], cwd=cwd,
                                     capture_output=True, text=True, timeout=timeout)
            assert process.returncode == (0 if ok else 1), (args, process.stdout, process.stderr)
            result = json.loads(process.stdout)
            assert result.get("ok", True) == ok, result
            return result

        def execute(code, **kwargs):
            return call("exec", "-c", code, **kwargs)

        def quiet():
            """Feedback has its own test; this one measures the channel itself."""
            call("session", "feedback", "perception=false", "objective=false", "image.mode=off")

        def history():
            return call("session", "history")["history"]

        def vertices():
            cube = next(obj for obj in call("inspect")["objects"] if obj["name"] == "Cube")
            return cube["mesh"]["vertices"]

        def wait_autosave(path, previous=None):
            deadline = time.monotonic() + 2.5
            while time.monotonic() < deadline:
                if path.is_file() and path.stat().st_size > 0 and path.stat().st_mtime_ns != previous:
                    return path.stat().st_mtime_ns
                time.sleep(0.02)
            raise AssertionError(f"Idle autosave did not update: {path}")

        opened = call("session", "open")
        quiet()
        endpoint = opened["socket"]
        assert endpoint == str(root / ".blender-cli" / "session.sock"), opened
        local_endpoint = str(Path(endpoint).relative_to(root))
        try:
            call("session", "open", ok=False)
            usage = call("session", ok=False)["error"]
            assert usage == {"type": "ProtocolError", "line": None, "message":
                             "session requires action: "
                             "status|feedback|save|close|snapshot|rollback|history"}, usage
            for index in range(10):
                code = "x = 0; x" if index == 0 else "x += 1; x"
                result = execute(code)
                assert result["value"] == str(index), result
                assert result["diff"]["snapshot"].startswith("sha256:"), result
                if index in (0, 5, 9):
                    print(f"exec {index + 1}: {json.dumps(result)}", flush=True)
            # A statement that changes no datablock advances neither step nor snapshot.
            assert execute("x")["diff"]["step"] == 0, "namespace-only work is not a scene change"
            assert execute("agent is __import__('agent')")["value"] == "True"
            assert execute(repr("模型" * 300))["value"] == repr("模型" * 300)
            assert "error" in execute("agent.compare('missing.png', 'front')", ok=False)
            baseline = call("session", "snapshot", "--label", "before")
            assert vertices() == 8
            modified = execute("""
mesh = bpy.data.objects['Cube'].data
bm = bmesh.new()
bm.from_mesh(mesh)
bmesh.ops.subdivide_edges(bm, edges=list(bm.edges), cuts=1, use_grid_fill=True)
bm.to_mesh(mesh)
bm.free()
mesh.update()
len(mesh.vertices)
""")
            assert vertices() > 8, modified
            assert modified["diff"]["step"] == 1, modified
            call("session", "rollback", baseline["snapshot"])
            assert vertices() == 8
            execute("branch = x + 100; bpy.data.objects['Cube'].location.x = 3")
            # The former future remains reachable after a new branch.
            call("session", "rollback", modified["diff"]["snapshot"])
            assert vertices() > 8
            call("session", "rollback", baseline["snapshot"])
            assert history()[0]["op"] == "open", history()
            assert any(item["label"] == "before" for item in history()), history()
            assert [item["at"] for item in history()] == sorted(item["at"] for item in history())
            assert execute("len(agent.history())")["value"] == str(len(history()))
            execute("saved = agent.snapshot('helper'); bpy.data.objects['Cube'].location.x = 12")
            execute("agent.rollback(saved); bpy.data.objects['Cube'].location.x")
            assert execute("bpy.data.objects['Cube'].location.x")["value"] == "0.0"
            execute("bpy.data.objects['Cube'].location.x = 7; agent.diff()")
            empty = execute("agent.diff()")["diff"]
            assert (empty["added"], empty["changed"], empty["removed"]) == ([], [], []), empty
            call("session", "rollback", "~1")
            execute("bpy.data.objects['Cube'].location.x = 4")
            execute("bpy.data.objects['Cube'].location.x = 8")
            assert execute("bpy.ops.ed.undo(); bpy.data.objects['Cube'].location.x")["value"] == "4.0"
            call("session", "rollback", baseline["snapshot"])
            assert len(execute("'x' * 200000")["value"]) == 200002
            execute("bpy.ops.wm.read_factory_settings(use_empty=True)")
            call("session", "rollback", baseline["snapshot"])
            assert vertices() == 8
            execute("bpy.app.timers.register(lambda: globals().update(timer_fired=True), first_interval=0.02)")
            time.sleep(0.1)
            assert execute("timer_fired")["value"] == "True"

            # The feedback policy is per session and is what `session status` reports.
            policy = call("session", "feedback", "image.mode=full", "perception=true")["feedback"]
            assert policy["image"]["mode"] == "full" and policy["perception"] is True, policy
            status = call("session", "status")
            assert status["feedback"] == policy and status["targets"] == [], status
            assert status["session"] == opened["session"] and status["recovered_from"] is None
            assert call("session", "feedback", "image.mode=delta",
                        "perception=true")["feedback"] == call("session", "status")["feedback"]
            quiet()

            # Cancellation is on a second connection while the original is executing, and
            # is answered at once instead of queueing behind the running request.
            with connection(local_endpoint) as running, connection(local_endpoint) as control:
                run_stream, control_stream = running.makefile("rb"), control.makefile("rb")
                assert greeting(run_stream)["step"] == greeting(control_stream)["step"]
                running.sendall((json.dumps({"id": 9001, "op": "exec",
                                             "code": "while True:\n    pass"}) + "\n").encode())
                time.sleep(0.1)
                control.sendall((json.dumps({"id": 9002, "op": "cancel", "target": 9001}) + "\n").encode())
                answer = json.loads(control_stream.readline())
                assert answer == {"id": 9002, "event": "done", "ok": True,
                                  "target": 9001, "cancelled": True}, answer
                final, = events_until_end(run_stream, 9001)
                assert final["event"] == "error" and final["type"] == "Cancelled", final
                # An id that is not running is answered too, and changes nothing.
                control.sendall((json.dumps({"id": 9003, "op": "cancel", "target": 4242}) + "\n").encode())
                idle = json.loads(control_stream.readline())
                assert idle["cancelled"] is False, idle
            assert execute("x")["value"] == "9"

            # Multiple complete lines on one connection retain order and matching IDs.
            with connection(local_endpoint) as pipeline:
                for index, code in enumerate(("queued = 40", "queued += 2; queued")):
                    message = {"id": 9100 + index, "op": "exec", "code": code}
                    pipeline.sendall((json.dumps(message) + "\n").encode())
                stream = pipeline.makefile("rb")
                greeting(stream)
                first = events_until_end(stream, 9100)
                second = events_until_end(stream, 9101)
                assert first[-1]["event"] == "done" and second[-1]["event"] == "done"
                assert second[0] == {"id": 9101, "event": "value", "value": "42"}, second

            # `repl` is the same protocol on stdio: it bridges to this session.
            bridged = subprocess.run(
                [executable, "repl"], cwd=root, capture_output=True, text=True, timeout=60,
                input=json.dumps({"id": 9200, "op": "exec",
                                  "code": "bridged = 'yes'\nbridged"}) + "\n")
            assert bridged.returncode == 0, bridged
            streamed = [json.loads(line) for line in bridged.stdout.splitlines() if line.strip()]
            opening = streamed.pop(0)
            assert opening["event"] == "session" and opening["id"] is None, opening
            assert opening["snapshot"] == call("session", "status")["snapshot"], opening
            order = ["value", "diff", "perception", "objective", "image", "done"]
            ranked = [order.index(event["event"]) for event in streamed]
            assert ranked == sorted(ranked) and streamed[-1]["event"] == "done", streamed
            assert streamed[0] == {"id": 9200, "event": "value", "value": "'yes'"}, streamed
            assert execute("bridged")["value"] == "'yes'", "repl shares the session namespace"

            # The pipe outlives the process behind it. In its own directory, so a
            # crashed session's recovery file cannot outlive this conversation.
            channel = root / "channel"
            channel.mkdir()
            pipe = subprocess.Popen([executable, "repl"], cwd=channel, text=True, bufsize=1,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            try:
                def send(request):
                    pipe.stdin.write(json.dumps(request) + "\n")
                    pipe.stdin.flush()

                def until(kind):
                    while True:
                        event = json.loads(pipe.stdout.readline())
                        if event["event"] == kind:
                            return event

                # `repl` opens the session it needs, and says what it opened.
                opening = until("session")
                assert opening["id"] is None and opening["recovered_from"] is None, opening
                send({"id": 9400, "op": "exec", "code": "bpy.ops.mesh.primitive_cube_add()",
                      "feedback": {"mode": "off"}})
                assert until("done")["ok"] is True
                crashed_autosave = channel / ".blender-cli" / f'autosave-{opening["session"]}.blend'
                wait_autosave(crashed_autosave)
                send({"id": 9401, "op": "exec", "code": "import os; os._exit(3)"})
                lost = json.loads(pipe.stdout.readline())
                # What the reopened session rebuilt from is its own verdict; the
                # bridge states it rather than deciding it.
                assert lost == {"id": 9401, "event": "error", "ok": False, "type": "Crashed",
                                "message": f'Session {opening["session"]} exited during this '
                                           "request; see .blender-cli/session.log. The session "
                                           "was reopened and this pipe still serves it",
                                "recovered_from": lost["recovered_from"], "step": lost["step"],
                                "snapshot": lost["snapshot"],
                                "autosave": str(crashed_autosave)}, lost
                assert lost["recovered_from"] in ("program", "autosave", None), lost
                # The same pipe now serves the session it just reopened, and says so.
                reopened = until("session")
                assert reopened["session"] != opening["session"], reopened
                assert reopened["recovered_from"] == lost["recovered_from"], (reopened, lost)
                assert reopened["snapshot"] == lost["snapshot"], (reopened, lost)
                send({"id": 9402, "op": "inspect"})
                assert until("done")["ok"] is True, "the reopened session answers on this pipe"
                pipe.stdin.close()
                assert pipe.wait(timeout=60) == 0, "recovery succeeded, so the bridge did not fail"
            finally:
                pipe.kill()
            subprocess.run([executable, "session", "close", "--json"], cwd=channel, timeout=60)

            failed = execute("raise RuntimeError('intentional')", ok=False)
            assert "diff" not in failed, failed
            before_count = len(history())
            call("inspect")
            assert len(history()) == before_count
            blend = root / "out.blend"
            call("session", "save", "--file", blend)
            assert blend.is_file()
            # Timing includes process creation, transport, execution, snapshot and JSON
            # printing, with the feedback budget off: this is the channel's own cost.
            samples = []
            for _ in range(20):
                start = time.perf_counter()
                execute("1 + 1")
                samples.append((time.perf_counter() - start) * 1000)
            median = statistics.median(samples)
            print(f"20 CLI round trips, feedback off (ms): median={median:.3f} min={min(samples):.3f} max={max(samples):.3f}", flush=True)
            if sys.platform.startswith("linux"):
                assert median < 10, samples
        finally:
            call("session", "close")
        assert not Path(endpoint).exists()
        assert not (root / ".blender-cli" / "session.pid").exists()
        assert not (root / ".blender-cli" / f'autosave-{opened["session"]}.blend').exists()
        assert call("inspect", "--file", blend)["ok"]
        fallback = execute("'x' in globals()")
        assert fallback["value"] == "False" and fallback["diff"]["snapshot"] is None, fallback
        call("session", "open", "--file", blend)
        quiet()
        try:
            assert vertices() == 8
        finally:
            call("session", "close")
        call("session", "open", "--file", root / "absent.blend", ok=False)
        # A native call cannot be preempted; close still has a bounded forced-exit path.
        call("session", "open")
        quiet()
        with connection(local_endpoint) as hung:
            request = {"id": 9300, "op": "exec", "code": "import time; time.sleep(30)"}
            hung.sendall((json.dumps(request) + "\n").encode())
            time.sleep(0.1)
            assert call("session", "close")["forced"] is True
        assert not Path(endpoint).exists()
        crashed = call("session", "open")
        quiet()
        autosave = root / ".blender-cli" / f'autosave-{crashed["session"]}.blend'
        first_write = wait_autosave(autosave)
        execute("bpy.ops.wm.read_factory_settings(use_empty=True); bpy.ops.mesh.primitive_cube_add(); "
                "bpy.context.object.name = 'RecoveredCube'; "
                "saved_state = (bpy.data.filepath, bpy.data.is_dirty)")
        # A failed edit leaves neither the live scene nor the autosave contaminated.
        execute("bpy.data.objects['RecoveredCube'].location.x = 9; raise RuntimeError('not a snapshot')",
                ok=False)
        assert execute("bpy.data.objects['RecoveredCube'].location.x")["value"] == "0.0"
        wait_autosave(autosave, first_write)
        reader = root / "one-shot-reader"
        reader.mkdir()
        saved_cube, = call("inspect", "--file", autosave, cwd=reader)["objects"]
        assert saved_cube["name"] == "RecoveredCube" and saved_cube["location"][0] == 0, saved_cube
        assert execute("(bpy.data.filepath, bpy.data.is_dirty) == saved_state")["value"] == "True"
        # Rollback itself dirties the autosave, even without another successful exec.
        # `~0` is the current snapshot: only state changes advance the history.
        stamp = autosave.stat().st_mtime_ns
        call("session", "rollback", "~0")
        wait_autosave(autosave, stamp)
        stamp = autosave.stat().st_mtime_ns
        call("session", "save", "--file", root / "explicit.blend")
        time.sleep(1.1)
        assert autosave.stat().st_mtime_ns == stamp, "Explicit save must not dirty the autosave"
        if sys.platform == "win32":
            import ctypes

            kernel = ctypes.WinDLL("kernel32.dll")
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            process = kernel.OpenProcess(0x00100000, False, int(crashed["session"]))  # SYNCHRONIZE
            assert process, "Could not open daemon process for exit synchronization"
            try:
                killed = execute("import os; os._exit(0)", ok=False)
                # Socket EOF can precede process teardown completion on Windows.
                assert kernel.WaitForSingleObject(process, 10000) == 0, "Daemon did not exit"
            finally:
                kernel.CloseHandle(process)
        else:
            killed = execute("import os; os._exit(3)", ok=False)
        assert killed["error"]["type"] == "SessionError", killed
        assert killed["autosave"] == str(autosave), killed
        assert "exited unexpectedly" in killed["error"]["message"], killed
        # A plain reopen recovers from the newest source; naming the autosave
        # would make it a chosen file and recover nothing.
        assert "`session open` recovers it" in killed["error"]["message"], killed
        assert "--file" not in killed["error"]["message"], killed
        assert "session.log" in killed["error"]["message"], killed
        logged = [json.loads(line.removeprefix("Agent request: "))
                  for line in (root / ".blender-cli/session.log").read_text().splitlines()
                  if line.startswith("Agent request: ")]
        assert logged[-1]["id"] and logged[-1]["op"] == "exec", logged[-1]
        assert "os._exit" in logged[-1]["code"], logged[-1]
        # Abrupt exit leaves a real stale endpoint. Open must clean it and restart.
        for args in (("exec", "-c", "42"), ("session", "history")):
            dead = call(*args, ok=False)
            assert dead["error"]["type"] == "SessionError" and "exited unexpectedly" in dead["error"]["message"], dead
            assert dead["autosave"] == str(autosave), dead
        # A named file is not a recovery: nothing replays over it, so the result
        # names the recovery file that is still there rather than a source.
        recovered = call("session", "open", "--file", autosave)
        quiet()
        assert recovered["previous_autosave"] == str(autosave), recovered
        assert "recovered_from" not in recovered, recovered
        with autosave.with_suffix(".json").open() as stream:
            metadata = json.load(stream)
        assert metadata["filepath"] == "", metadata
        assert execute(f"(bpy.data.filepath, bpy.data.is_dirty) == {metadata['filepath'], metadata['dirty']!r}")["value"] == "True"
        call("session", "save", ok=False)  # Must not overwrite the recovery file.
        cube, = call("inspect")["objects"]
        assert cube["name"] == "RecoveredCube" and cube["location"][0] == 0, cube
        new_autosave = root / ".blender-cli" / f'autosave-{recovered["session"]}.blend'
        wait_autosave(new_autosave)
        call("session", "save", "--file", root / "with-texture.blend")
        stamp = new_autosave.stat().st_mtime_ns
        execute("image = bpy.data.images.new('Texture', width=2, height=2); "
                "image.filepath_raw = '//texture.png'; image.file_format = 'PNG'; image.save(); "
                "bpy.data.images.remove(image); image = bpy.data.images.load(bpy.path.abspath('//texture.png')); "
                "image.name = 'Texture'; image.filepath = '//texture.png'; image.use_fake_user = True; "
                "saved_state = (bpy.data.filepath, bpy.data.is_dirty)")
        wait_autosave(new_autosave, stamp)
        selected = call("inspect", "--file", new_autosave, "--select", 'images["Texture"].filepath', cwd=reader)
        assert selected["selected"]['images["Texture"].filepath'] == str(root / "texture.png"), selected
        assert execute("image.filepath == '//texture.png' and (bpy.data.filepath, bpy.data.is_dirty) == saved_state")["value"] == "True"
        execute("import os; os.chdir('..')")
        call("session", "close")
        assert not new_autosave.exists() and autosave.exists()
        cube, = call("inspect", "--file", autosave)["objects"]
        assert cube["name"] == "RecoveredCube", cube
        stale = call("session", "open")
        quiet()
        stale_autosave = root / ".blender-cli" / f'autosave-{stale["session"]}.blend'
        wait_autosave(stale_autosave)
        execute("import os; os._exit(3)", ok=False)
        time.sleep(0.1)
        closed = call("session", "close")
        assert closed["stale"] and closed["autosave"] == str(stale_autosave), closed
        assert stale_autosave.exists()
        assert not (root / ".blender-cli" / "session.lock").exists()
        assert not (root / ".blender-cli" / "session.pid").exists()
        assert not Path(endpoint).exists(), "Daemon cwd changes must not redirect endpoint cleanup"
        saved = call("session", "open", "--file", root / "explicit.blend")
        quiet()
        saved_autosave = root / ".blender-cli" / f'autosave-{saved["session"]}.blend'
        wait_autosave(saved_autosave)
        execute("import os; os._exit(3)", ok=False)
        call("session", "open", "--file", saved_autosave)
        quiet()
        with saved_autosave.with_suffix(".json").open() as stream:
            metadata = json.load(stream)
        assert metadata["filepath"] == str(root / "explicit.blend"), metadata
        assert metadata["dirty"] is False, metadata
        assert execute(f"(bpy.data.filepath, bpy.data.is_dirty) == {metadata['filepath'], False!r}")["value"] == "True"
        assert call("session", "save")["file"] == metadata["filepath"]
        call("session", "close")
        assert saved_autosave.exists() and saved_autosave.with_suffix(".json").exists()

        # Labels are synchronous disk checkpoints, survive stale close, and resolve newest-first.
        call("session", "open")
        quiet()
        execute("bpy.ops.wm.read_factory_settings(use_empty=True); bpy.ops.mesh.primitive_cube_add()")
        first_label = call("session", "snapshot", "--label", "durable-fit")["snapshot"]
        execute("bpy.ops.object.delete(); bpy.ops.mesh.primitive_uv_sphere_add(); agent.snapshot('durable-fit')")
        last_label = next(event["snapshot"] for event in reversed(history())
                          if event["label"] == "durable-fit")
        assert first_label != last_label
        index_path = root / ".blender-cli/snapshots/index.json"
        index_before = index_path.read_bytes()
        for snapshot_id in (first_label, last_label):
            assert (index_path.parent / (snapshot_id.removeprefix("sha256:") + ".blend")).is_file()
        execute("import os; os._exit(3)", ok=False)
        assert call("session", "close")["stale"]
        assert index_path.read_bytes() == index_before
        call("session", "open")
        quiet()
        durable_history = [event for event in history() if event["label"] == "durable-fit"]
        assert [event["snapshot"] for event in durable_history] == [first_label, last_label]
        assert all(event["durable"] and event["bytes"] > 0 for event in durable_history)
        call("session", "rollback", "durable-fit")
        sphere, = call("inspect")["objects"]
        assert sphere["name"] == "Sphere" and sphere["mesh"]["vertices"] > 8, sphere
        call("session", "rollback", first_label)
        assert vertices() == 8
        assert execute("bpy.data.filepath")["value"] == "''"
        execute(f"agent.rollback({last_label!r})")
        assert call("inspect")["objects"][0]["name"] == "Sphere"
        call("session", "close")
        assert index_path.read_bytes() == index_before
        if sys.platform != "win32":
            faulted = call("session", "open", "--file", saved_autosave)
            quiet()
            assert faulted["previous_autosave"] == str(saved_autosave), faulted
            crash_script = root / "fault.py"
            crash_script.write_text("# checkpoint crash regression\nimport os, signal; os.kill(os.getpid(), signal.SIGSEGV)\n")
            call("exec", crash_script, ok=False)
            dump = root / ".blender-cli" / f'session-{faulted["session"]}.crash.txt'
            text = dump.read_text()
            assert "# backtrace" in text and "# Agent request" in text, text
            assert "# checkpoint crash regression" in text and str(crash_script) in text and '"id"' in text, text
            assert str(dump) in (root / ".blender-cli/session.log").read_text()
            call("session", "close")
        # Exercise the real observation pipeline in one native process at a cheap tile size.
        # The unpatched 128px and 512px paths both leak ~1810 VMAs per render and die at ~35.
        require_device(executable)
        call("session", "open")
        quiet()
        try:
            result = execute("""
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=1)
import agent_observe as observation
source = bpy.context.scene
for render_index in range(120):
    with observation.isolated_data():
        scene, points, center, radius, framing = observation.render_scene(source, 128, None)
        near, far = observation.aim(scene, source, "front", points, center, radius)
        images = observation.render_passes(scene, 128, near, far)
        assert images["color"].shape == (128, 128, 3)
render_index + 1
""", timeout=1500)
            assert result["value"] == "120", result
            assert vertices() == 8
            assert execute("42")["value"] == "42"
        finally:
            call("session", "close")
    print("agent session: all assertions passed")


if __name__ == "__main__":
    main()
