# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Targets, the pushed objective and bounded fitting over the real event channel."""

import ast
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from gpu import require_device

# A fixed orthographic camera above the cube. Auto-framing removes uniform
# scale, so a fit loop needs either a fixed scene camera or a fixed frame
# object; with this camera both scale axes are identifiable.
SCENE = """
import bpy
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete()
bpy.ops.mesh.primitive_cube_add(size=2)
camera = bpy.data.objects.new('Fit camera', bpy.data.cameras.new('Fit camera'))
camera.data.type = 'ORTHO'
camera.data.ortho_scale = 4
camera.location = (0, 0, 6)
bpy.context.scene.collection.objects.link(camera)
bpy.context.scene.camera = camera
bpy.data.objects['Cube'].scale = ({0}, {1}, 1)
"""

# The same scene as a program, so `fit` can search `P` instead of RNA paths.
# A program starts from a factory-empty Main, so it builds rather than deletes.
PROGRAM = """# blender-cli program
P = {{"sx": {0}, "sy": {1}}}
# step 1
import bpy
bpy.ops.mesh.primitive_cube_add(size=2)
camera = bpy.data.objects.new('Fit camera', bpy.data.cameras.new('Fit camera'))
camera.data.type = 'ORTHO'
camera.data.ortho_scale = 4
camera.location = (0, 0, 6)
bpy.context.scene.collection.objects.link(camera)
bpy.context.scene.camera = camera
# step 2
bpy.data.objects['Cube'].scale = (P["sx"], P["sy"], 1)
"""

TRUTH = (1.70, 1.45)
START = (1.00, 1.00)
X = 'objects["Cube"].scale[0]'
Y = 'objects["Cube"].scale[1]'
RESET = f"bpy.data.objects['Cube'].scale = ({START[0]}, {START[1]}, 1)"


class Channel:
    """One `repl` process: JSON-line requests in, JSON-line events out."""

    def __init__(self, executable, cwd, blend):
        self.process = subprocess.Popen(
            [executable, "repl", "--standalone", "--file", str(blend)], cwd=str(cwd),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1)
        self.identifier = 0
        self.aside = []

    def send(self, **request):
        self.identifier += 1
        self.write(id=self.identifier, **request)
        return self.identifier

    def write(self, **request):
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()

    def read(self):
        line = self.process.stdout.readline()
        assert line, "the channel closed before the request ended"
        return json.loads(line)

    def events(self, identifier, on_event=None):
        """Every event of one request, in order, ending with its done or error.

        A `cancel` sent while this request runs is answered out of order on the
        same pipe; those events land in `aside`.
        """
        collected = []
        while True:
            event = self.read()
            if event["id"] != identifier:
                self.aside.append(event)
                continue
            event["at"] = time.perf_counter()
            collected.append(event)
            if on_event:
                on_event(event)
            if event["event"] in ("done", "error"):
                return collected

    def request(self, ok=True, on_event=None, **fields):
        events = self.events(self.send(**fields), on_event)
        assert events[-1]["event"] == ("done" if ok else "error"), events[-1]
        return events

    def done(self, **fields):
        return self.request(**fields)[-1]

    def value(self, code):
        """Run code whose last expression is JSON text and return the parsed value."""
        events = self.request(op="exec", code=code, record=False)
        value = next(event["value"] for event in events if event["event"] == "value")
        # The value event carries a Python repr, as the contract says.
        return json.loads(ast.literal_eval(value))

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=120)


def only(events, kind):
    return [event for event in events if event["event"] == kind]


def main():
    executable = str(Path(sys.argv[1]).resolve())
    require_device(executable)
    with tempfile.TemporaryDirectory(prefix="agent fit ") as directory:
        root = Path(directory)

        def call(*args):
            process = subprocess.run([executable, *map(str, args), "--json"], cwd=root,
                                     capture_output=True, text=True, timeout=900)
            assert process.returncode == 0, (args, process.stdout, process.stderr)
            return json.loads(process.stdout)

        # The reference is a silhouette this binary renders, not a committed fixture.
        truth, start, ref = root / "truth.blend", root / "start.blend", root / "ref.png"
        call("exec", "-c", SCENE.format(*TRUTH), "--save", truth)
        call("observe", "--views", "camera", "--passes", "silhouette", "--size", "512",
             "--out", ref, "--file", truth)
        call("exec", "-c", SCENE.format(*START), "--save", start)

        channel = Channel(executable, root, start)
        try:
            registered = channel.done(op="target", action="set", name="top", ref="ref.png",
                                      view="camera", mask="none", fit="none")
            print("target set:", json.dumps(registered), flush=True)
            assert registered["name"] == "top" and registered["view"] == "camera", registered
            assert registered["metrics"] == ["iou"], registered
            assert registered["mask"] == "none" and registered["fit"] == "none", registered
            stored = Path(registered["ref"])
            assert stored.parent == root / ".blender-cli" / "targets" / "top", registered
            assert stored.is_file() and Path(registered["silhouette"]).is_file(), registered
            assert registered["reference"]["occupancy"] > 0.5, registered
            first = registered["objective"]
            assert first["targets"]["top"]["delta"] is None, first
            assert 0 < first["targets"]["top"]["iou"] < 1, first

            listed = channel.done(op="target", action="list")
            assert [entry["name"] for entry in listed["targets"]] == ["top"], listed
            assert "silhouette" not in listed["targets"][0], listed

            # A state-changing request pushes the objective without being asked.
            events = channel.request(op="exec", code='bpy.data.objects["Cube"].scale.x = 1.4')
            objective, = only(events, "objective")
            print("objective event:", json.dumps(
                {key: value for key, value in objective.items() if key != "at"}), flush=True)
            # The documented order: value, diff, perception, objective, images, done.
            order = ["value", "diff", "perception", "objective", "image", "done"]
            kinds = [event["event"] for event in events]
            assert kinds == sorted(kinds, key=order.index), kinds
            assert kinds[-1] == "done" and kinds.count("objective") == 1, kinds
            top = objective["targets"]["top"]
            assert set(top) == {"iou", "delta", "worst"}, top
            assert 0 < top["iou"] < 1 and set(top["delta"]) == {"iou"}, top
            assert top["delta"]["iou"] > 0, top
            worst = top["worst"]
            assert set(worst) == {"region", "iou", "missing", "extra"}, worst
            assert len(worst["region"]) == 4 and worst["region"][2] > worst["region"][0], worst
            assert abs(worst["missing"] + worst["extra"] - 1) < 1e-9, worst
            best = objective["best"]["top"]
            assert set(best) == {"iou", "snapshot", "step"}, best
            assert best["iou"] == top["iou"] and best["snapshot"].startswith("sha256:"), best
            assert best["step"] >= 1, best

            # `agent.objective()` answers exactly what the next event carries: it
            # scores the same state and records nothing.
            helper = channel.value("import json; json.dumps(agent.objective())")
            repeated, = only(channel.request(op="exec", code="pass"), "objective")
            assert helper == {"targets": repeated["targets"], "best": repeated["best"]}, helper

            # A target on a budget view is scored from the perception provider's
            # render. The helper renders that view itself, so equal dicts prove
            # the shared tile is the same picture.
            channel.done(op="target", action="set", name="front", ref="ref.png",
                         view="front", mask="none", fit="none")
            fresh = channel.value("import json; json.dumps(agent.objective())")
            shared, = only(channel.request(op="exec", code="pass"), "objective")
            assert set(shared["targets"]) == {"front", "top"}, shared
            assert fresh == {"targets": shared["targets"], "best": shared["best"]}, (fresh, shared)
            channel.done(op="target", action="clear", name="front")

            # Fit both identifiable axes back to the reference.
            stamps = []
            events = channel.request(
                op="fit", method="coordinate",
                params=[{"path": X, "min": 0.4, "max": 1.9}, {"path": Y, "min": 0.4, "max": 1.9}],
                objective={"target": "top", "metric": "iou"},
                budget={"evals": 40, "seconds": 600, "size": 512},
                on_event=lambda event: stamps.append(event["at"]))
            fitted = events[-1]
            progress = only(events, "progress")
            print("fit progress[0]:", json.dumps(progress[0]), flush=True)
            print("fit progress[-1]:", json.dumps(progress[-1]), flush=True)
            print("fit done:", json.dumps(fitted), flush=True)
            assert fitted["evals"] <= 40 and fitted["applied"] and not fitted["cancelled"], fitted
            assert fitted["method"] == "coordinate", fitted
            assert fitted["objective"] == {"targets": ["top"], "metric": "iou",
                                           "weights": [1.0]}, fitted
            assert fitted["best"]["score"] >= 0.99, fitted
            assert fitted["best"]["snapshot"].startswith("sha256:"), fitted
            curve = fitted["curve"]
            assert [row[1] for row in curve] == sorted(row[1] for row in curve), curve
            assert curve[-1][1] == fitted["best"]["score"], curve
            assert Path(fitted["error_map"]["image"]).is_file(), fitted
            assert fitted["error_map"]["size"] == [512, 512], fitted
            assert fitted["error_map"]["target"] == "top", fitted
            # Progress is rate limited, never one event per evaluation regardless of cost.
            times = [event["at"] for event in progress]
            gaps = [second - first for first, second in zip(times, times[1:])]
            print(f"progress events={len(progress)} min gap={min(gaps):.3f}s", flush=True)
            assert min(gaps) >= 0.5, gaps
            for event in progress:
                assert set(event) >= {"event", "eval", "of", "best", "params"}, event
                assert event["of"] == 40 and set(event["params"]) == {X, Y}, event

            # Main is left at the best state, not at the last evaluation.
            applied = channel.value(
                "import json; json.dumps(list(bpy.data.objects['Cube'].scale))")
            wanted = fitted["best"]["params"]
            assert abs(applied[0] - wanted[X]) < 1e-6, (applied, wanted)
            assert abs(applied[1] - wanted[Y]) < 1e-6, (applied, wanted)
            assert abs(applied[0] - TRUTH[0]) < 0.05, applied
            assert abs(applied[1] - TRUTH[1]) < 0.05, applied
            scored = channel.value("import json; json.dumps(agent.objective())")
            assert scored["targets"]["top"]["iou"] >= 0.98, scored

            # Cancel keeps the best: fit ends with done, not Cancelled.
            channel.request(op="exec", code=RESET)
            identifier = channel.send(
                op="fit", method="coordinate",
                params=[{"path": X, "min": 0.4, "max": 1.9}, {"path": Y, "min": 0.4, "max": 1.9}],
                objective={"target": "top", "metric": "iou"},
                budget={"evals": 200, "seconds": 600, "size": 512})
            seen = 0

            def cancel_after_three(event):
                nonlocal seen
                if event["event"] == "progress":
                    seen += 1
                    if seen == 3:
                        channel.write(id=9001, op="cancel", target=identifier)

            events = channel.events(identifier, cancel_after_three)
            cancelled = events[-1]
            print("cancelled fit:", json.dumps(cancelled), flush=True)
            assert cancelled["event"] == "done" and cancelled["ok"], cancelled
            assert cancelled["cancelled"] and cancelled["applied"], cancelled
            assert cancelled["evals"] < 200, cancelled
            # The cancel itself was answered immediately, out of order.
            assert [event["id"] for event in channel.aside] == [9001], channel.aside
            kept = channel.value("import json; json.dumps(list(bpy.data.objects['Cube'].scale))")
            assert abs(kept[0] - cancelled["best"]["params"][X]) < 1e-6, (kept, cancelled)
            assert abs(kept[1] - cancelled["best"]["params"][Y]) < 1e-6, (kept, cancelled)

            # A seeded random search repeats exactly from the same state and budget.
            runs = []
            for _ in range(2):
                channel.request(op="exec", code=RESET)
                runs.append(channel.done(
                    op="fit", method="random",
                    params=[{"path": X, "min": 0.4, "max": 1.9},
                            {"path": Y, "min": 0.4, "max": 1.9}],
                    objective={"target": "top", "metric": "iou"},
                    budget={"evals": 12, "seconds": 600, "size": 128}))
            print("random fit:", json.dumps(runs[0]["best"]), flush=True)
            assert runs[0]["best"]["params"] == runs[1]["best"]["params"], runs
            assert runs[0]["best"]["score"] == runs[1]["best"]["score"], runs
            assert runs[0]["curve"] == runs[1]["curve"], runs
            assert runs[0]["evals"] == runs[1]["evals"] == 12, runs

            # A program parameter is fitted by re-executing the steps that read it.
            channel.done(op="program", action="set", text=PROGRAM.format(*START))
            programmed = channel.done(
                op="fit", method="coordinate",
                params=[{"name": "sx", "min": 0.4, "max": 1.9},
                        {"name": "sy", "min": 0.4, "max": 1.9}],
                objective={"target": "top", "metric": "iou"},
                budget={"evals": 24, "seconds": 900, "size": 256})
            print("program fit:", json.dumps(programmed["best"]), flush=True)
            assert set(programmed["best"]["params"]) == {"sx", "sy"}, programmed
            assert programmed["best"]["score"] > first["targets"]["top"]["iou"], programmed
            written = channel.done(op="program", action="get")["params"]
            assert written["sx"] == programmed["best"]["params"]["sx"], written
            assert written["sy"] == programmed["best"]["params"]["sy"], written

            # `agent.fit()` inside exec answers the same dict the request does.
            in_code = channel.value("""
import json
json.dumps(agent.fit([{"name": "sx", "min": 0.4, "max": 1.9}],
                     objective={"target": "top", "metric": "iou"},
                     budget={"evals": 3, "size": 128}))
""")
            print("agent.fit():", json.dumps(in_code["best"]), flush=True)
            assert set(in_code) >= {"method", "objective", "best", "evals", "failed",
                                    "curve", "applied", "cancelled", "error_map"}, in_code
            assert in_code["evals"] == 3 and set(in_code["best"]["params"]) == {"sx"}, in_code

            cleared = channel.done(op="target", action="clear")
            assert cleared["cleared"] == ["top"], cleared
            assert not (root / ".blender-cli" / "targets" / "top").exists()
            # No targets, no objective event and an empty helper answer.
            assert not only(channel.request(op="exec", code="pass"), "objective")
            assert channel.value("import json; json.dumps(agent.objective())") == {
                "targets": {}, "best": {}}
            channel.request(op="target", action="clear", name="top", ok=False)
        finally:
            channel.close()
    print("agent fit tests passed")


if __name__ == "__main__":
    main()
