# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Native batch transaction, references, recording and cancellation on real pipes."""

import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading


def main():
    executable = str(Path(sys.argv[1]).resolve())
    with tempfile.TemporaryDirectory(prefix="agent batch ") as directory:
        root = Path(directory).resolve()
        steps = [{"op": "object", "action": "create", "name": "OneShot", "type": "EMPTY"}]
        one = subprocess.run([executable, "batch", "--steps", json.dumps(steps), "--json"],
                             cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=120)
        assert one.returncode == 0, (one.stdout, one.stderr)
        assert not (root / ".blender-cli/program/model.json").exists(), "One-shot wrote a program"

        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errors:
            process = subprocess.Popen([executable, "repl", "--standalone"], cwd=root,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                                       text=True, encoding="utf-8", bufsize=1)
            events = queue.Queue()

            def read():
                for line in process.stdout:
                    events.put(json.loads(line))
                events.put(None)

            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            identifier = 0

            def send(op, **fields):
                nonlocal identifier
                identifier += 1
                process.stdin.write(json.dumps({"id": identifier, "op": op, **fields}) + "\n")
                process.stdin.flush()
                return identifier

            def receive():
                event = events.get(timeout=120)
                assert event is not None, "Native session exited unexpectedly"
                return event

            def call(op, ok=True, **fields):
                current = send(op, **fields)
                answer = []
                while True:
                    event = receive()
                    assert event["id"] == current, event
                    answer.append(event)
                    if event["event"] in {"done", "error"}:
                        assert event["ok"] == ok, (op, fields, answer)
                        return event, answer

            try:
                assert receive()["event"] == "session"
                call("session", action="feedback", feedback={
                    "perception": False, "objective": False, "image": {"mode": "off"}})
                call("scene", action="reset")
                steps = [
                    {"op": "object", "action": "create", "name": "Body", "as": "body"},
                    {"op": "object", "action": "transform", "name": {"$ref": "body.name"},
                     "location": [1, 2, 3]},
                    {"op": "data", "action": "get", "path": 'objects["Body"].location'},
                ]
                result, stream = call("batch", steps=steps)
                assert result["results"][2]["value"] == [1, 2, 3], result
                assert sum(event["event"] == "diff" for event in stream) == 1, stream
                program, _ = call("program", action="get")
                assert json.loads(program["text"])["steps"][-1] == {"op": "batch", "steps": steps}
                original = program["text"]

                failed, _ = call("batch", ok=False, steps=[
                    {"op": "object", "action": "transform", "name": "Body", "location": [9, 9, 9]},
                    {"op": "object", "action": "transform", "name": "Missing", "scale": [2, 2, 2]},
                ])
                assert failed["step"] == 2, failed
                assert call("data", action="get", path='objects["Body"].location')[0]["value"] == [1, 2, 3]
                assert call("program", action="get")[0]["text"] == original
                for invalid in [[], [{"op": "batch", "steps": []}],
                                [{"op": "session", "action": "close"}],
                                [{"op": "object", "action": "create", "bogus": 1}],
                                [{"op": "object", "action": "create", "as": 7}]]:
                    call("batch", steps=invalid, ok=False)
                assert call("program", action="get")[0]["text"] == original

                # File effects are explicit and survive a failed scene transaction.
                saved = root / "external.blend"
                failed, _ = call("batch", ok=False, steps=[
                    {"op": "scene", "action": "save", "path": str(saved)},
                    {"op": "object", "action": "transform", "name": "Missing", "scale": [2, 2, 2]},
                ])
                assert saved.is_file() and failed["external_effects"], failed

                # A main-thread native loop observes a cancel read concurrently by
                # the transport thread. No Python loop stands in for the native job.
                job = send("animation", action="bake", objects=["Body"], start=1, end=1000000)
                while True:
                    event = receive()
                    assert event["id"] == job and event["event"] != "error", event
                    if event["event"] == "progress":
                        break
                cancel = send("cancel", target=job)
                terminals = {}
                while len(terminals) != 2:
                    event = receive()
                    if event["event"] in {"done", "error"}:
                        terminals[event["id"]] = event
                assert terminals[cancel]["cancelled"] is True, terminals
                assert terminals[job]["event"] == "error" and terminals[job]["type"] == "Cancelled", terminals
                assert call("data", action="get", path='objects["Body"].location')[0]["value"] == [1, 2, 3]
                assert call("program", action="get")[0]["text"] == original

                call("session", action="feedback", feedback={"progress": "off"})
                _, silent = call("animation", action="bake", objects=["Body"], start=1, end=2)
                assert not any(event["event"] == "progress" for event in silent), silent
                call("session", action="close")
                process.stdin.close()
                assert process.wait(timeout=30) == 0
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                errors.seek(0)
                diagnostics = errors.read()
                if process.returncode:
                    print(diagnostics[-12000:], file=sys.stderr)
    print("agent batch: native references, atomic scene rollback, external effects and cancellation passed")


if __name__ == "__main__":
    main()
