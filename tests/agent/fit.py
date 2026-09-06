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

# A step that refuses part of the range, so a fit meets a failed evaluation.
FAILING = """# blender-cli program
P = {"sx": 1.0}
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
if P["sx"] > 1.2:
    raise ValueError("sx above 1.2 is not buildable")
bpy.data.objects['Cube'].scale = (P["sx"], 1.45, 1)
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
        self.opening = None

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
            if event["event"] == "session":
                # The channel's opening statement, not an answer to anything.
                self.opening = event
                continue
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
        call("observe", "--views", "front", "--passes", "silhouette", "--size", "512",
             "--out", root / "front.png", "--file", truth)
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
            assert fitted["evals"] <= 40 and fitted["applied"], fitted
            assert fitted["stopped"] == "budget", fitted
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
            # Under the default policy an event is sent only when the best
            # improves, so the stream is exactly the curve and nothing else.
            print(f"progress events={len(progress)} curve={len(curve)}", flush=True)
            assert len(progress) == len(curve), (progress, curve)
            assert [[event["eval"], event["best"]] for event in progress] == curve, progress
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

            # Under `fit bbox` the model is normalised exactly as the reference
            # is, so the model's own silhouette scores 1: the reference's 2D
            # bounding box and the model's projected 3D bounds are not the same
            # rectangle, and normalising only one displaces the optimum.
            own = channel.value("""
import json
json.dumps(agent.observe(views=('front',), passes=('silhouette',), size=512)['image'])
""")
            channel.done(op="target", action="set", name="self", ref=own, view="front",
                         mask="none", fit="bbox", metrics=["iou", "chamfer"])
            exact, = only(channel.request(op="exec", code="pass"), "objective")
            mine = exact["targets"]["self"]
            print("exact model under fit bbox:", json.dumps(mine), flush=True)
            assert mine["iou"] >= 0.98, mine
            assert mine["chamfer"] < 0.5, mine
            # Regions stay in the view's pixels: the worst cell has to sit
            # inside the model's silhouette bounding box in the budget view.
            box = channel.value("""
import json, numpy as np, agent_target
from agent_observe import isolated_data
with isolated_data():
    rendered, framing = agent_target.render_views(bpy.context.scene, ['front'], 256)
ys, xs = np.nonzero(rendered['front'][1])
json.dumps([int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1])
""")
            region = mine["worst"]["region"]
            print("worst region vs model bbox:", json.dumps([region, box]), flush=True)
            assert region[0] < box[2] and region[2] > box[0], (region, box)
            assert region[1] < box[3] and region[3] > box[1], (region, box)

            # A silhouette PNG is two-valued: it states its boundary exactly, so
            # `mask auto` has nothing to estimate and must not clean it up. The
            # same reference and model therefore score the same under either
            # policy, and a stencil along a thin boundary cannot move the score.
            channel.done(op="target", action="set", name="self-auto", ref=own, view="front",
                         mask="auto", fit="bbox", metrics=["iou", "chamfer"])
            both, = only(channel.request(op="exec", code="pass"), "objective")
            guessed, read = both["targets"]["self-auto"], both["targets"]["self"]
            print("two-valued reference, auto vs none:", json.dumps(
                [guessed["iou"], read["iou"]]), flush=True)
            assert guessed["iou"] == read["iou"], (guessed, read)
            assert guessed["chamfer"] == read["chamfer"], (guessed, read)
            assert guessed["worst"]["region"] == read["worst"]["region"], (guessed, read)
            channel.done(op="target", action="clear", name="self-auto")
            channel.done(op="target", action="clear", name="self")

            # The handoff the image provider reads at order 400. A provider of
            # our own reads it exactly where F's will, in the same request and
            # after the objective provider has run.
            channel.request(op="exec", code="""
import agent, agent_target


class Peek:
    name = "peek"
    order = 401

    def before(self, request, session):
        pass

    def after(self, request, session, emit):
        state = session.last_objective
        session.namespace['peeked'] = None if state is None else {
            "size": state["size"],
            "targets": {name: {"view": item["view"], "metric": item["metric"],
                               "delta": item["delta"], "worst": item["worst"],
                               "shapes": [list(item["reference"].shape),
                                          list(item["model"].shape)],
                               "dtypes": [item["reference"].dtype.name,
                                          item["model"].dtype.name],
                               "error": list(agent_target.error_image(
                                   item["reference"], item["model"]).shape)}
                        for name, item in state["targets"].items()}}


agent.register_provider(Peek())
""")
            channel.request(op="exec", code=RESET)
            handoff = channel.value("import json; json.dumps(peeked)")
            print("last_objective:", json.dumps(handoff), flush=True)
            assert handoff["size"] == 256, handoff
            entry = handoff["targets"]["top"]
            assert entry["view"] == "camera" and entry["metric"] == "iou", entry
            assert entry["shapes"] == [[256, 256], [256, 256]], entry
            assert entry["dtypes"] == ["bool", "bool"], entry
            assert entry["error"] == [256, 256, 3], entry
            assert set(entry["worst"]) == {"region", "iou", "missing", "extra"}, entry
            assert isinstance(entry["delta"], float), entry

            # A request that scores nothing must not leave the last one behind.
            channel.done(op="target", action="clear", name="top")
            channel.request(op="exec", code=RESET)
            assert channel.value("import json; json.dumps(peeked)") is None
            channel.done(op="target", action="set", name="top", ref="ref.png",
                         view="camera", mask="none", fit="none")

            # The other two progress policies: `all` is a rate-limited
            # heartbeat, `off` sends nothing at all.
            def short_fit(**budget):
                channel.request(op="exec", code=RESET)
                return channel.request(
                    op="fit", params=[{"path": X, "min": 0.4, "max": 1.9},
                                      {"path": Y, "min": 0.4, "max": 1.9}],
                    objective={"target": "top", "metric": "iou"},
                    budget={"seconds": 900, "size": 128, **budget})

            channel.done(op="session", action="feedback", feedback={"progress": "all"})
            beats = only(short_fit(evals=8), "progress")
            times = [event["at"] for event in beats]
            gaps = [second - first for first, second in zip(times, times[1:])]
            print(f"progress all: events={len(beats)} min gap={min(gaps):.3f}s", flush=True)
            assert len(beats) > 1 and min(gaps) >= 0.5, beats
            channel.done(op="session", action="feedback", feedback={"progress": "off"})
            assert not only(short_fit(evals=6), "progress")
            channel.done(op="session", action="feedback", feedback={"progress": "improvements"})
            assert channel.done(op="session", action="status")["feedback"]["progress"] == \
                "improvements"

            # A search that stops improving stops paying for renders. No
            # `patience` is given, so this is the derived one: two parameters
            # sit on the floor of 16. The same budget with a patience it cannot
            # exhaust runs to the end instead.
            patient = short_fit(evals=60)[-1]
            print("patience stop:", json.dumps(
                {key: patient[key] for key in ("evals", "stopped", "best")}), flush=True)
            assert patient["stopped"] == "patience" and patient["evals"] < 60, patient
            assert patient["best"]["score"] >= 0.99, patient
            spent = short_fit(evals=60, patience=10 ** 6)[-1]
            print("budget stop:", json.dumps(
                {key: spent[key] for key in ("evals", "stopped")}), flush=True)
            assert spent["stopped"] == "budget" and spent["evals"] == 60, spent
            assert patient["evals"] < spent["evals"], (patient, spent)
            assert spent["best"]["score"] - patient["best"]["score"] < 0.01, (patient, spent)

            # The derived patience grows with the search. Five parameters put it
            # at 25, so a 20-evaluation budget cannot reach it and the search
            # ends on `budget`; under a fixed 16 it would have stopped early.
            channel.request(op="exec", code=RESET)
            wide = channel.done(
                op="fit",
                params=[{"path": X, "min": 0.4, "max": 1.9},
                        {"path": Y, "min": 0.4, "max": 1.9},
                        {"path": 'objects["Cube"].scale[2]', "min": 0.4, "max": 1.9},
                        {"path": 'objects["Cube"].location[0]', "min": -0.3, "max": 0.3},
                        {"path": 'objects["Cube"].location[2]', "min": -0.3, "max": 0.3}],
                objective={"target": "top", "metric": "iou"},
                budget={"evals": 20, "seconds": 900, "size": 128})
            print("five parameters, derived patience:", json.dumps(
                {key: wide[key] for key in ("evals", "stopped")}), flush=True)
            assert wide["stopped"] == "budget" and wide["evals"] == 20, wide
            # An explicit patience still wins over the derived one.
            channel.request(op="exec", code=RESET)
            forced = channel.done(
                op="fit", params=[{"path": X, "min": 0.4, "max": 1.9}],
                objective={"target": "top", "metric": "iou"},
                budget={"evals": 60, "seconds": 900, "size": 128, "patience": 2})
            print("explicit patience:", json.dumps(
                {key: forced[key] for key in ("evals", "stopped")}), flush=True)
            assert forced["stopped"] == "patience" and forced["evals"] <= 16, forced

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
            assert cancelled["stopped"] == "cancel" and cancelled["applied"], cancelled
            # `stopped` says why on its own; a second field repeating it is gone.
            assert "cancelled" not in cancelled, cancelled
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
                                    "curve", "applied", "stopped", "error_map"}, in_code
            assert in_code["evals"] == 3 and set(in_code["best"]["params"]) == {"sx"}, in_code
            assert "cancelled" not in in_code, in_code

            # nelder-mead recovers the same truth as coordinate descent.
            channel.request(op="exec", code=RESET)
            simplex = channel.done(
                op="fit", method="nelder-mead",
                params=[{"path": X, "min": 0.4, "max": 1.9},
                        {"path": Y, "min": 0.4, "max": 1.9}],
                objective={"target": "top", "metric": "iou"},
                budget={"evals": 40, "seconds": 900, "size": 128})
            print("nelder-mead fit:", json.dumps(simplex["best"]), flush=True)
            assert simplex["method"] == "nelder-mead" and simplex["evals"] <= 40, simplex
            assert simplex["best"]["score"] >= 0.97, simplex
            assert abs(simplex["best"]["params"][X] - TRUTH[0]) < 0.1, simplex
            assert abs(simplex["best"]["params"][Y] - TRUTH[1]) < 0.1, simplex

            # A weighted objective over two views: one render per view per
            # evaluation, and a weighted mean of the two per-target scores.
            channel.done(op="target", action="set", name="face", ref="front.png",
                         view="front", mask="none", fit="none")

            def one_eval(**objective):
                channel.request(op="exec", code=RESET)
                return channel.done(
                    op="fit", params=[{"path": X, "min": 0.4, "max": 1.9}],
                    objective=objective, budget={"evals": 1, "size": 128})

            alone = one_eval(target="top", metric="iou")["best"]["score"]
            other = one_eval(target="face", metric="iou")["best"]["score"]
            mixed = one_eval(targets=["top", "face"], metric="iou", weights=[0.7, 0.3])
            print("weighted objective:", json.dumps(
                {"top": alone, "face": other, "weighted": mixed["best"]["score"]}), flush=True)
            assert mixed["objective"] == {"targets": ["top", "face"], "metric": "iou",
                                          "weights": [0.7, 0.3]}, mixed
            assert alone != other, (alone, other)
            assert abs(mixed["best"]["score"] - (0.7 * alone + 0.3 * other)) < 1e-9, mixed
            channel.done(op="target", action="clear", name="face")

            # A code objective is scored by agent code and returns no error map.
            channel.request(op="exec", code=RESET)
            coded = channel.done(
                op="fit", params=[{"path": X, "min": 0.4, "max": 1.9}],
                objective={"code": "agent.compare('ref.png', 'camera', metrics=('iou',),"
                                   " mask='none', fit='none', size=512)['iou']"},
                budget={"evals": 6, "seconds": 900, "size": 128})
            print("code objective:", json.dumps(coded["best"]), flush=True)
            assert "code" in coded["objective"] and "error_map" not in coded, coded
            assert 0 < coded["best"]["score"] <= 1 and coded["evals"] == 6, coded
            assert coded["best"]["score"] > alone, (coded, alone)

            # A step that raises costs its evaluation and is counted, and the
            # search keeps going instead of failing the request.
            channel.done(op="program", action="set", text=FAILING)
            broken = channel.done(
                op="fit", params=[{"name": "sx", "min": 0.4, "max": 1.9}],
                objective={"target": "top", "metric": "iou"},
                budget={"evals": 8, "seconds": 900, "size": 128})
            print("failed evaluations:", json.dumps(
                {key: broken[key] for key in ("evals", "failed", "best")}), flush=True)
            assert broken["failed"] >= 1 and broken["evals"] == 8, broken
            assert broken["best"]["score"] > 0 and broken["applied"], broken
            assert broken["best"]["params"]["sx"] <= 1.2, broken
            assert channel.done(op="program", action="get")["params"]["sx"] <= 1.2

            # Appearance metrics reach the objective event, and the first one
            # registered is the metric `best` tracks.
            channel.done(op="target", action="set", name="look", ref="ref.png", view="camera",
                         mask="none", fit="none", metrics=["ssim", "hist", "iou"])
            appearance, = only(channel.request(op="exec", code=RESET), "objective")
            look = appearance["targets"]["look"]
            print("appearance target:", json.dumps(look), flush=True)
            assert set(look) == {"ssim", "hist", "iou", "delta", "worst"}, look
            assert -1 <= look["ssim"] <= 1 and 0 <= look["hist"] <= 1, look
            assert set(appearance["best"]["look"]) == {"ssim", "snapshot", "step"}, appearance
            # `best` tracks the primary metric's best value so far, which this
            # reset moved away from, so it is ahead of the current score.
            assert appearance["best"]["look"]["ssim"] >= look["ssim"], appearance
            assert look["delta"]["ssim"] < 0, look
            channel.done(op="target", action="clear", name="look")

            cleared = channel.done(op="target", action="clear")
            assert cleared["cleared"] == ["top"], cleared
            assert not (root / ".blender-cli" / "targets" / "top").exists()
            # No targets, no objective event and an empty helper answer.
            assert not only(channel.request(op="exec", code="pass"), "objective")
            assert channel.value("import json; json.dumps(agent.objective())") == {
                "targets": {}, "best": {}}
            channel.request(op="target", action="clear", name="top", ok=False)

            metrics(channel, root)
        finally:
            channel.close()
    print("agent fit tests passed")


def metrics(channel, root):
    """`agent.compare` is what the objective computes; nothing else covers it.

    These assertions come from the deleted `tests/agent/compare.py`, which the
    removal of the `compare` verb took with it. The computation stayed, so its
    coverage lives here with the rest of workstream T.
    """
    # Real data from this binary; no committed fixtures and no replacement scene.
    channel.request(op="exec", code="""
bpy.data.objects['Cube'].scale = (0.6, 1, 1)
bpy.context.view_layer.update()
""")
    channel.value("""
import json
from pathlib import Path
import numpy as np
from agent_observe import png, bytes_rgb, resize
SIZE = 512
def load_rgb(path):
    image = bpy.data.images.load(str(Path(path).resolve()), check_existing=False)
    w, h = image.size
    pixels = np.empty(w * h * 4, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    rgb = bytes_rgb(pixels.reshape(h, w, 4)[::-1, :, :3])
    bpy.data.images.remove(image)
    return rgb
colour = agent.observe(views=('front',), passes=('color',), size=SIZE)['image']
shape = agent.observe(views=('front',), passes=('silhouette',), size=SIZE)['image']
rgb = load_rgb(colour)[2:-2, 2:-2]
silhouette = load_rgb(shape)[2:-2, 2:-2, 0] != 0
Path('self.png').write_bytes(png(rgb))
Path('mask.png').write_bytes(png(np.repeat(
    silhouette[:, :, None].astype(np.uint8) * 255, 3, axis=2)))
# A textured background the auto mask has to remove.
Path('colored.png').write_bytes(png(np.where(
    silhouette[:, :, None], rgb, np.array((35, 140, 210), dtype=np.uint8))))
# The same silhouette with 20% margins, which `fit=bbox` must normalise away.
ys, xs = np.nonzero(silhouette)
cropped = silhouette[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
scale = SIZE * 0.6 / max(cropped.shape)
w, h = round(cropped.shape[1] * scale), round(cropped.shape[0] * scale)
padded = np.zeros((SIZE, SIZE, 3), dtype=np.uint8)
x, y = (SIZE - w) // 2, (SIZE - h) // 2
padded[y:y + h, x:x + w] = np.repeat(
    (resize(cropped[:, :, None].astype(float), w, h) >= 0.5).astype(np.uint8) * 255, 3, axis=2)
Path('margins.png').write_bytes(png(padded))
# A portrait crop, a straight-alpha PNG, and the JPEG and WebP codecs.
Path('portrait.png').write_bytes(png(rgb[:, 64:-64]))
rgba = np.concatenate((rgb / 255, silhouette[:, :, None].astype(float)), axis=2)
image = bpy.data.images.new('Alpha reference', width=SIZE, height=SIZE, alpha=True)
image.pixels.foreach_set(rgba[::-1].astype(np.float32).ravel())
image.filepath_raw = str(Path('alpha.png').resolve())
image.file_format = 'PNG'
image.save()
bpy.data.images.remove(image)
composite = np.where(silhouette[:, :, None], rgb, np.array((35, 140, 210), dtype=np.uint8))
opaque = np.concatenate((composite / 255, np.ones((SIZE, SIZE, 1))), axis=2)
for extension, kind in (('jpg', 'JPEG'), ('webp', 'WEBP')):
    image = bpy.data.images.new('Codec reference', width=SIZE, height=SIZE, alpha=False)
    image.filepath_raw = str(Path('colored.' + extension).resolve())
    image.file_format = kind
    image.pixels.foreach_set(opaque[::-1].astype(np.float32).ravel())
    image.save()
    bpy.data.images.remove(image)
json.dumps({"ok": True})
""")

    def compare(reference, **kwargs):
        arguments = ", ".join(f"{key}={value!r}" for key, value in kwargs.items())
        return channel.value(
            f"import json; json.dumps(agent.compare({reference!r}, 'front',"
            f" metrics=('iou', 'chamfer', 'ssim', 'hist'){',' if arguments else ''}"
            f" {arguments}))")

    # A render compared against itself is the metrics' fixed point. The colour
    # tile is segmented by luminance, so it agrees to a rounding edge; the
    # silhouette tile is the mask itself and agrees exactly.
    same = compare("self.png", mask="none", fit="none")
    print("self compare:", json.dumps(same), flush=True)
    assert same["iou"] >= 0.98 and same["chamfer"] <= 1, same
    assert same["ssim"] >= 0.98 and same["hist"] <= 0.02, same
    exact = compare("mask.png", mask="none", fit="none")
    assert exact["iou"] == 1.0 and exact["chamfer"] == 0.0, exact

    # `mask=auto` removes a background that shares no colour with the object.
    composite = compare("colored.png", mask="auto", debug=str(root / "debug"))
    print("colored-background compare:", json.dumps(composite), flush=True)
    assert composite["iou"] >= 0.95, composite
    assert Path(composite["debug"]["reference_silhouette"]).is_file(), composite
    recovered = channel.value("""
import json
recovered = load_rgb('debug/reference-silhouette.png')[:, :, 0] != 0
json.dumps(float(np.count_nonzero(recovered & silhouette)
                 / np.count_nonzero(recovered | silhouette)))
""")
    assert recovered >= 0.95, recovered

    # `fit=bbox` removes reference margins; `fit=none` keeps them and scores badly.
    fitted, unfitted = compare("margins.png", mask="none"), compare("margins.png", mask="none",
                                                                   fit="none")
    print("margin reference bbox/none:", json.dumps([fitted, unfitted]), flush=True)
    assert fitted["iou"] >= 0.99 and unfitted["iou"] < 0.8, (fitted, unfitted)
    assert fitted["reference"]["fit"] == "bbox" and fitted["reference"]["bbox"] is not None

    # Every compiled-in codec, a non-square reference, and the size ladder.
    assert compare("portrait.png")["iou"] >= 0.98
    assert compare("alpha.png", mask="none")["iou"] >= 0.98
    for extension in ("jpg", "webp"):
        assert compare("colored." + extension)["iou"] >= 0.95, extension
    for size in (768, 1024):
        resized = channel.value(
            f"import json; json.dumps(agent.compare('self.png', 'front', size={size},"
            " mask='none', fit='none', frame='Cube'))")
        assert set(resized) == {"view", "iou", "reference"} and resized["iou"] >= 0.98, resized

    # Comparison reads the scene; it writes no file and changes no data.
    files = {str(path) for path in root.rglob("*.png")}
    before = channel.done(op="session", action="snapshot")["snapshot"]
    quiet = channel.request(op="exec", code="agent.compare('self.png', 'front')")
    assert only(quiet, "diff")[0]["added"] == [], quiet
    assert only(quiet, "diff")[0]["changed"] == [], quiet
    assert channel.done(op="session", action="snapshot")["snapshot"] == before
    assert files == {str(path) for path in root.rglob("*.png")}

    # The loop an agent would write by hand, which `fit` now runs in-process.
    start = time.perf_counter()
    loop = channel.value("""
import json
obj = bpy.data.objects['Cube']
scores = []
for index in range(20):
    value = round(0.2 + index * 0.04, 2)
    obj.scale.x = value
    scores.append((agent.compare('self.png', 'front', metrics=('iou',))['iou'], value))
score, best = max(scores)
obj.scale.x = best
json.dumps({"best": best, "iou": score})
""")
    print("20-iteration loop:", json.dumps(loop),
          "wall_s:", round(time.perf_counter() - start, 1), flush=True)
    assert loop["best"] == 0.6 and loop["iou"] >= 0.98, loop
    assert files == {str(path) for path in root.rglob("*.png")}


if __name__ == "__main__":
    main()
