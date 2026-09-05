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
            if isinstance(result, dict):
                assert result.get("ok", True) == ok, result
            return result

        def execute(code, **kwargs):
            return call("exec", "-c", code, **kwargs)

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
        endpoint = opened["socket"]
        assert endpoint == str(root / ".blender-cli" / "session.sock"), opened
        local_endpoint = str(Path(endpoint).relative_to(root))
        try:
            call("session", "open", ok=False)
            usage = call("session", ok=False)["error"]
            assert usage == {"type": "ValueError", "message":
                             "session requires an action: open|save|close|snapshot|rollback|history"}, usage
            for index in range(10):
                code = "x = 0; x" if index == 0 else "x += 1; x"
                result = execute(code)
                assert result["value"] == str(index), result
                assert result["snapshot"].startswith("sha256:"), result
                if index in (0, 5, 9):
                    print(f"exec {index + 1}: {json.dumps(result)}", flush=True)
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
            call("session", "rollback", baseline["snapshot"])
            assert vertices() == 8
            execute("branch = x + 100; bpy.data.objects['Cube'].location.x = 3")
            # The former future remains reachable after a new branch.
            call("session", "rollback", modified["snapshot"])
            assert vertices() > 8
            call("session", "rollback", baseline["snapshot"])
            history = call("session", "history")
            assert history[0]["verb"] == "open", history
            assert any(item["label"] == "before" for item in history), history
            assert [item["at"] for item in history] == sorted(item["at"] for item in history)
            assert execute("len(agent.history())")["value"] == str(len(history))
            execute("saved = agent.snapshot('helper'); bpy.data.objects['Cube'].location.x = 12")
            execute("agent.rollback(saved); bpy.data.objects['Cube'].location.x")
            assert execute("bpy.data.objects['Cube'].location.x")["value"] == "0.0"
            execute("bpy.data.objects['Cube'].location.x = 7; agent.diff()")
            assert execute("agent.diff()")["diff"] == {"added": [], "changed": [], "removed": []}
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

            # Cancellation is on a second connection while the original is executing.
            with connection(local_endpoint) as running, connection(local_endpoint) as control:
                request = {"id": 9001, "verb": "exec", "args": {"argv": ["-c", "while True:\n    pass"]}}
                running.sendall((json.dumps(request) + "\n").encode())
                time.sleep(0.1)
                control.sendall(b'{"id":9001,"cancel":true}\n')
                response = json.loads(running.makefile("rb").readline())
                assert response["id"] == 9001 and response["result"]["error"]["type"] == "Cancelled", response
            assert execute("x")["value"] == "9"
            # Multiple complete lines on one connection retain order and matching IDs.
            with connection(local_endpoint) as pipeline:
                for index, code in enumerate(("queued = 40", "queued += 2; queued")):
                    message = {"id": 9100 + index, "verb": "exec", "args": {"argv": ["-c", code]}}
                    pipeline.sendall((json.dumps(message) + "\n").encode())
                stream = pipeline.makefile("rb")
                first, second = (json.loads(stream.readline()) for _ in range(2))
                assert first["id"] == 9100 and second["id"] == 9101
                assert second["result"]["value"] == "42", second
            failed = execute("raise RuntimeError('intentional')", ok=False)
            assert "snapshot" not in failed
            before_count = len(call("session", "history"))
            call("inspect")
            assert len(call("session", "history")) == before_count
            blend = root / "out.blend"
            call("session", "save", "--file", blend)
            assert blend.is_file()
            # Timing includes process creation, transport, execution, snapshot and JSON printing.
            samples = []
            for _ in range(20):
                start = time.perf_counter()
                execute("1 + 1")
                samples.append((time.perf_counter() - start) * 1000)
            median = statistics.median(samples)
            print(f"20 CLI round trips (ms): median={median:.3f} min={min(samples):.3f} max={max(samples):.3f}", flush=True)
            if sys.platform.startswith("linux"):
                assert median < 10, samples
        finally:
            call("session", "close")
        assert not Path(endpoint).exists()
        assert not (root / ".blender-cli" / "session.pid").exists()
        assert not (root / ".blender-cli" / f'autosave-{opened["session"]}.blend').exists()
        assert call("inspect", "--file", blend)["ok"]
        fallback = execute("'x' in globals()")
        assert fallback["value"] == "False" and "snapshot" not in fallback, fallback
        call("session", "open", "--file", blend)
        try:
            assert vertices() == 8
        finally:
            call("session", "close")
        call("session", "open", "--file", root / "absent.blend", ok=False)
        # A native call cannot be preempted; close still has a bounded forced-exit path.
        call("session", "open")
        with connection(local_endpoint) as hung:
            request = {"id": 9200, "verb": "exec",
                       "args": {"argv": ["-c", "import time; time.sleep(30)"]}}
            hung.sendall((json.dumps(request) + "\n").encode())
            time.sleep(0.1)
            assert call("session", "close")["forced"] is True
        assert not Path(endpoint).exists()
        crashed = call("session", "open")
        autosave = root / ".blender-cli" / f'autosave-{crashed["session"]}.blend'
        first_write = wait_autosave(autosave)
        execute("bpy.ops.wm.read_factory_settings(use_empty=True); bpy.ops.mesh.primitive_cube_add(); "
                "bpy.context.object.name = 'RecoveredCube'; held = bpy.context.object; "
                "saved_state = (bpy.data.filepath, bpy.data.is_dirty)")
        # A failed edit must not contaminate the pending successful snapshot.
        execute("held.location.x = 9; raise RuntimeError('not a snapshot')", ok=False)
        wait_autosave(autosave, first_write)
        reader = root / "one-shot-reader"
        reader.mkdir()
        saved_cube, = call("inspect", "--file", autosave, cwd=reader)["objects"]
        assert saved_cube["name"] == "RecoveredCube" and saved_cube["location"][0] == 0, saved_cube
        assert execute("(bpy.data.filepath, bpy.data.is_dirty) == saved_state and held.location.x == 9")["value"] == "True"
        # Rollback itself dirties the autosave, even without another successful exec.
        stamp = autosave.stat().st_mtime_ns
        call("session", "rollback", "~1")
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
        assert "session open --file" in killed["error"]["message"], killed
        assert "session.log" in killed["error"]["message"], killed
        logged = [json.loads(line.removeprefix("Agent request: "))
                  for line in (root / ".blender-cli/session.log").read_text().splitlines()
                  if line.startswith("Agent request: ")]
        assert logged[-1]["id"] and logged[-1]["verb"] == "exec", logged[-1]
        assert "os._exit" in logged[-1]["args"]["argv"][1], logged[-1]
        # Abrupt exit leaves a real stale endpoint. Open must clean it and restart.
        for args in (("exec", "-c", "42"), ("session", "history")):
            dead = call(*args, ok=False)
            assert dead["error"]["type"] == "SessionError" and "exited unexpectedly" in dead["error"]["message"], dead
            assert dead["autosave"] == str(autosave), dead
        recovered = call("session", "open", "--file", autosave)
        assert recovered["previous_autosave"] == str(autosave), recovered
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
        saved_autosave = root / ".blender-cli" / f'autosave-{saved["session"]}.blend'
        wait_autosave(saved_autosave)
        execute("import os; os._exit(3)", ok=False)
        call("session", "open", "--file", saved_autosave)
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
        execute("bpy.ops.wm.read_factory_settings(use_empty=True); bpy.ops.mesh.primitive_cube_add()")
        first_label = call("session", "snapshot", "--label", "durable-fit")["snapshot"]
        execute("bpy.ops.object.delete(); bpy.ops.mesh.primitive_uv_sphere_add(); agent.snapshot('durable-fit')")
        history = call("session", "history")
        last_label = next(event["snapshot"] for event in reversed(history) if event["label"] == "durable-fit")
        assert first_label != last_label
        index_path = root / ".blender-cli/snapshots/index.json"
        index_before = index_path.read_bytes()
        for snapshot_id in (first_label, last_label):
            assert (index_path.parent / (snapshot_id.removeprefix("sha256:") + ".blend")).is_file()
        execute("import os; os._exit(3)", ok=False)
        assert call("session", "close")["stale"]
        assert index_path.read_bytes() == index_before
        call("session", "open")
        durable_history = [event for event in call("session", "history") if event["label"] == "durable-fit"]
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
