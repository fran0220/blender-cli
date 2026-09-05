# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Real installed CLI/AF_UNIX session, memfile restoration and latency checks."""

import json
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import tempfile
import time


def main():
    executable = str(Path(sys.argv[1]).resolve())
    with tempfile.TemporaryDirectory(prefix="agent session ") as directory:
        root = Path(directory)

        def call(*args, ok=True):
            process = subprocess.run([executable, *map(str, args), "--json"], cwd=root,
                                     capture_output=True, text=True, timeout=30)
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

        opened = call("session", "open")
        endpoint = opened["socket"]
        assert endpoint == str(root / ".blender-cli" / "session.sock"), opened
        try:
            call("session", "open", ok=False)
            for index in range(10):
                code = "x = 0; x" if index == 0 else "x += 1; x"
                result = execute(code)
                assert result["value"] == str(index), result
                assert result["snapshot"].startswith("sha256:"), result
                if index in (0, 5, 9):
                    print(f"exec {index + 1}: {json.dumps(result)}", flush=True)
            assert execute("agent is __import__('agent')")["value"] == "True"
            assert execute("agent.observe()", ok=False)["error"]["type"] == "NotImplementedError"
            assert execute("agent.compare('ref.png', 'front')", ok=False)["error"]["type"] == "NotImplementedError"
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
            with socket.socket(socket.AF_UNIX) as running, socket.socket(socket.AF_UNIX) as control:
                running.settimeout(10)
                running.connect(endpoint)
                request = {"id": 9001, "verb": "exec", "args": {"argv": ["-c", "while True:\n    pass"]}}
                running.sendall((json.dumps(request) + "\n").encode())
                time.sleep(0.1)
                control.connect(endpoint)
                control.sendall(b'{"id":9001,"cancel":true}\n')
                response = json.loads(running.makefile("rb").readline())
                assert response["id"] == 9001 and response["result"]["error"]["type"] == "Cancelled", response
            assert execute("x")["value"] == "9"
            # Multiple complete lines on one connection retain order and matching IDs.
            with socket.socket(socket.AF_UNIX) as pipeline:
                pipeline.settimeout(10)
                pipeline.connect(endpoint)
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
        with socket.socket(socket.AF_UNIX) as hung:
            hung.connect(endpoint)
            request = {"id": 9200, "verb": "exec",
                       "args": {"argv": ["-c", "import time; time.sleep(30)"]}}
            hung.sendall((json.dumps(request) + "\n").encode())
            time.sleep(0.1)
            assert call("session", "close")["forced"] is True
        assert not Path(endpoint).exists()
        call("session", "open")
        execute("import os; os._exit(0)", ok=False)
        # Abrupt exit leaves a real stale endpoint. Open must clean it and restart.
        call("session", "open")
        call("session", "close")
    print("agent session: all assertions passed")


if __name__ == "__main__":
    main()
