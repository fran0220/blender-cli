# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Pushed feedback on real scenes: perception fields, delta images, budgets and cost."""

import ast
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from gpu import require_device
from observe import read_png

RENDER = """
import time
from agent_observe import render_budget
_start = time.perf_counter()
render_budget(['front'], 256, 8)
(time.perf_counter() - _start) * 1000
"""

SCENE = """
bpy.ops.mesh.primitive_ico_sphere_add(radius=0.5, location=(-3, 0, 0))
bpy.ops.mesh.primitive_ico_sphere_add(radius=0.5, location=(3, 0, 0))
"""

NUDGE = """
bpy.data.objects['Cube'].location.x += 0.001
bpy.data.objects['Cube'].location.x -= 0.001
"""


def covers(outer, inner, slack=0):
    return (outer[0] <= inner[0] + slack and outer[1] <= inner[1] + slack and
            outer[2] >= inner[2] - slack and outer[3] >= inner[3] - slack)


def project(framing, x0, x1, z0, z1, size=256):
    """Pixel box of a world-space X/Z box in the front view, derived from the framing record.

    The observation contract fixes the framing: an orthographic preset centers the
    world-space bounds and fits their longest extent at `occupancy` of the tile.
    """
    bounds, center = framing["bounds"], framing["center"]
    half = max(bounds["high"][0] - bounds["low"][0], bounds["high"][2] - bounds["low"][2]) / 2
    scale = size * framing["occupancy"] / (2 * half)
    return [size / 2 + (x0 - center[0]) * scale, size / 2 - (z1 - center[2]) * scale,
            size / 2 + (x1 - center[0]) * scale, size / 2 - (z0 - center[2]) * scale]


def main():
    executable = str(Path(sys.argv[1]).resolve())
    require_device(executable)
    with tempfile.TemporaryDirectory(prefix="agent feedback ") as directory:
        root = Path(directory)

        def call(*args, ok=True):
            process = subprocess.run([executable, *map(str, args), "--json"], cwd=root,
                                     capture_output=True, text=True, timeout=900)
            assert process.returncode == (0 if ok else 1), (args, process.returncode,
                                                            process.stdout, process.stderr)
            envelope = json.loads(process.stdout)
            assert envelope.get("ok", True) == ok, envelope
            return envelope

        def execute(code, *args, ok=True):
            return call("exec", "-c", code, *args, ok=ok)

        def stream(request):
            """One request over `repl`, answered as the event stream itself."""
            process = subprocess.Popen([executable, "repl"], cwd=root, text=True,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            events = []
            try:
                process.stdin.write(json.dumps(request) + "\n")
                process.stdin.flush()
                for line in process.stdout:
                    event = json.loads(line)
                    if event["event"] == "session":
                        continue  # The channel's opening statement of its own state.
                    events.append(event)
                    if event["event"] in ("done", "error"):
                        break
            finally:
                process.stdin.close()
                process.wait(timeout=120)
            assert all(event["id"] == request["id"] for event in events), events
            return events

        def picture(event):
            assert ("path" in event) != ("inline" in event), event
            data = (base64.b64decode(event["inline"]) if "inline" in event
                    else Path(event["path"]).read_bytes())
            width, height, _ = read_png(data)
            assert event["size"] == [width, height], event
            x0, y0, x1, y1 = event["region"]
            assert [width, height] == [x1 - x0, y1 - y0], event
            return data

        call("session", "open")
        try:
            defaults = call("session", "status")["feedback"]
            assert (defaults["perception"], defaults["objective"]) == (True, True), defaults
            assert defaults["image"]["mode"] == "delta", defaults
            assert defaults["image"]["threshold"] == 0.002, defaults
            assert defaults["image"]["views"] == ["front"], defaults
            assert defaults["image"]["pass"] == "color", defaults
            assert defaults["image"]["size"] == 256, defaults
            assert defaults["image"]["overlay"] is True, defaults
            assert defaults["image"]["inline"] is False, defaults
            assert defaults["image"]["samples"] == 8, defaults

            # An agent that has seen nothing gets a whole frame, and no delta to compare with.
            first = execute("bpy.ops.wm.read_factory_settings(use_empty=True)")
            empty = first["perception"]
            assert empty["changed"] is None, empty
            assert (empty["objects"], empty["verts"], empty["faces"]) == (0, 0, 0), empty
            assert empty["bounds"] == {"low": [-1, -1, -1], "high": [1, 1, 1]}, empty
            assert empty["dims"] == [2, 2, 2], empty
            assert empty["framing"]["objects"] == [], empty
            assert empty["symmetry"] == {"x": 1.0, "y": None, "z": 1.0}, empty
            whole, = first["images"]
            assert (whole["kind"], whole["view"], whole["pass"]) == ("full", "front", "color"), whole
            assert whole["region"] == [0, 0, 256, 256] and whole["size"] == [256, 256], whole
            assert Path(whole["path"]).parent == root / ".blender-cli" / "feedback", whole
            picture(whole)

            added = execute("bpy.ops.mesh.primitive_cube_add(size=1)")
            cube = added["perception"]
            assert (cube["objects"], cube["verts"], cube["faces"]) == (1, 8, 6), cube
            assert cube["bounds"] == {"low": [-0.5, -0.5, -0.5], "high": [0.5, 0.5, 0.5]}, cube
            assert cube["framing"]["objects"] == ["Cube"], cube
            assert cube["changed"]["objects"] == ["Cube"], cube
            assert cube["changed"]["view"] == "front", cube
            assert cube["changed"]["fraction"] > 0.5, cube
            # The previous silhouette was empty: no overlap at all.
            assert cube["changed"]["silhouette_delta"] == 1.0, cube
            assert cube["symmetry"]["x"] > 0.999 and cube["symmetry"]["z"] > 0.999, cube
            assert cube["symmetry"]["y"] is None, cube
            assert [event["kind"] for event in added["images"]] == ["delta", "overlay"], added
            print("cube add perception:", json.dumps(cube), flush=True)
            print("cube add images:", json.dumps(added["images"]), flush=True)

            # Anchors fix the automatic framing so a later move is a local change, not a rescale.
            anchored = execute(SCENE)
            counts = anchored["perception"]
            state = call("inspect")
            assert counts["objects"] == len(state["objects"]), (counts, state["scene"])
            assert counts["verts"] == sum(obj["mesh"]["vertices"] for obj in state["objects"]), counts
            assert counts["faces"] == sum(obj["mesh"]["faces"] for obj in state["objects"]), counts
            # Icosphere vertices sit on the radius; the anchors span slightly under ±3.5.
            assert counts["bounds"]["low"][1:] == [-0.5, -0.5], counts
            assert counts["bounds"]["high"][1:] == [0.5, 0.5], counts
            assert -3.5 < counts["bounds"]["low"][0] < -3.4, counts
            assert 3.4 < counts["bounds"]["high"][0] < 3.5, counts
            assert set(counts["changed"]["objects"]) == {"Icosphere", "Icosphere.001"}, counts

            # An action that changes nothing still reports perception, with zero deltas.
            # It also cannot have changed the picture, so it reuses the buffers instead
            # of rendering them again, and the facts it reports are the rendered ones.
            unchanged = execute("pass")
            same = unchanged["perception"]
            assert same["changed"] == {"objects": [], "view": "front", "region": None,
                                       "fraction": 0.0, "silhouette_delta": 0.0}, same
            assert "images" not in unchanged, unchanged
            assert ({key: value for key, value in same.items() if key != "changed"} ==
                    {key: value for key, value in counts.items() if key != "changed"}), (same, counts)
            assert unchanged["ms"] < anchored["ms"] / 10, (unchanged["ms"], anchored["ms"])
            print("unchanged perception:", json.dumps(same), flush=True)
            print("settled action ms against the rendering one:", unchanged["ms"],
                  anchored["ms"], flush=True)

            # Moving inside the anchors' bounds leaves the framing alone, so this is a
            # local change: the cube's old and new pixels, and nothing else.
            moved = execute("bpy.data.objects['Cube'].location.x += 0.5")
            shifted = moved["perception"]
            assert shifted["changed"]["objects"] == ["Cube"], shifted
            assert shifted["bounds"] == counts["bounds"], shifted
            assert 0.002 < shifted["changed"]["fraction"] < 0.2, shifted
            assert shifted["symmetry"]["x"] < counts["symmetry"]["x"], (shifted, counts)
            union = project(shifted["framing"], -0.5, 1.0, -0.5, 0.5)
            region = shifted["changed"]["region"]
            assert covers(region, union, slack=3), (region, union)
            assert covers(union, region, slack=3), (region, union)
            delta, overlay = moved["images"]
            assert delta["kind"] == "delta" and overlay["kind"] == "overlay", moved["images"]
            assert covers(delta["region"], region), (delta, region)
            assert covers(delta["region"], union, slack=3), (delta, union)
            assert delta["region"] == overlay["region"], (delta, overlay)
            assert delta["size"][0] < 256 and delta["size"][1] < 256, delta
            assert picture(delta) != picture(overlay)
            print("cube move perception:", json.dumps(shifted), flush=True)
            print("cube move images:", json.dumps(moved["images"]), flush=True)
            print("cube pixels before and after the move:", [round(value, 1) for value in union],
                  flush=True)

            # A change under the threshold is a perception number, not an image.
            call("session", "feedback", "image.threshold=0.5")
            quiet = execute("bpy.data.objects['Cube'].location.x -= 0.5")
            assert quiet["perception"]["changed"]["fraction"] > 0.002, quiet
            assert "images" not in quiet, quiet

            call("session", "feedback", "image.threshold=0.002")
            policy = call("session", "feedback", "image.mode=off")
            assert policy["feedback"]["image"]["mode"] == "off", policy
            assert policy["feedback"]["image"]["threshold"] == 0.002, policy
            silent = execute("bpy.data.objects['Cube'].location.z += 0.5")
            assert silent["perception"]["changed"]["fraction"] > 0.002, silent
            assert "images" not in silent, silent
            call("session", "feedback", "image.mode=delta")

            # One request may ask for more picture than the session policy pushes,
            # over the channel the policy override is defined on.
            asked = stream({"id": 31, "op": "exec", "feedback": {"mode": "full"},
                            "code": "bpy.data.objects['Cube'].location.z -= 0.5"})
            frame, = [event for event in asked if event["event"] == "image"]
            assert frame["kind"] == "full" and frame["size"] == [256, 256], frame
            assert [event["event"] for event in asked[-4:]] == ["diff", "perception", "image",
                                                                "done"], asked
            picture(frame)
            assert call("session", "status")["feedback"]["image"]["mode"] == "delta"

            # A host without a shared filesystem takes the bytes instead of a path.
            written = set((root / ".blender-cli" / "feedback").iterdir())
            carried = stream({"id": 32, "op": "exec", "feedback": {"inline": True},
                              "code": "bpy.data.objects['Cube'].location.z += 0.5"})
            crop, blend = [event for event in carried if event["event"] == "image"]
            assert (crop["kind"], blend["kind"]) == ("delta", "overlay"), carried
            assert "path" not in crop and "inline" in crop, crop
            assert picture(crop) != picture(blend)
            assert set((root / ".blender-cli" / "feedback").iterdir()) == written, \
                "an inline image must not also write a file"

            # agent.perceive() samples without advancing what the agent last saw.
            paired = execute("bpy.ops.mesh.primitive_cone_add(radius1=0.4, location=(0, 0, 1))\n"
                             "import json; json.dumps(agent.perceive())")
            assert json.loads(ast.literal_eval(paired["value"])) == paired["perception"], paired
            print("perceive matches the event:", json.dumps(paired["perception"]["changed"]),
                  flush=True)

            # A budget view the scene cannot render is an image event, never a failed request.
            call("session", "feedback", 'image.views=["camera"]')
            broken = execute("bpy.data.objects['Cube'].location.x += 0.25")
            failure, = broken["images"]
            assert failure["kind"] == "error" and failure["view"] == "camera", failure
            assert "scene.camera" in failure["message"], failure
            assert "path" not in failure and "region" not in failure, failure
            assert "perception" not in broken, broken
            assert "provider perception" in broken["stderr"], broken
            print("render failure:", json.dumps(failure), broken["stderr"].strip(), flush=True)

            # A failed render never advanced the baseline, so the next one reports the
            # change the agent was not shown, rather than losing it.
            call("session", "feedback", 'image.views=["front"]')
            recovered = execute("pass")
            assert recovered["perception"]["changed"]["fraction"] > 0.002, recovered
            assert [event["kind"] for event in recovered["images"]] == ["delta", "overlay"], recovered
            assert execute("pass")["perception"]["changed"]["fraction"] == 0.0

            # With a target registered, every action also pictures what is still wrong,
            # not only what the last action moved.
            reference = call("observe", "--views", "front", "--passes", "silhouette",
                             "--layout", "separate")["image"]
            registered = call("target", "set", "front", "--ref", reference)
            # Model and reference go through the same normalisation and the reference is
            # resampled once, so what is left between this model and its own silhouette is
            # that a 512 px rasterisation resampled to 256 is not the 256 px rasterisation
            # of the same geometry. That costs a thin, broken shape a few points of IoU.
            baseline = registered["objective"]["targets"]["front"]["iou"]
            print("target self-score:", baseline, flush=True)
            assert baseline > 0.95, registered
            aimed = execute("bpy.data.objects['Cube'].location.z += 0.4")
            scored = aimed["objective"]["targets"]["front"]
            assert scored["iou"] < baseline - 0.002, (scored, baseline)
            assert scored["delta"]["iou"] < -0.002, scored
            maps = [event for event in aimed["images"] if event["kind"] == "error"]
            assert len(maps) == 1, aimed["images"]
            wrong = maps[0]
            assert wrong["target"] == "front", wrong
            assert wrong["pass"] == "silhouette" and wrong["view"] == "front", wrong
            x0, y0, x1, y1 = scored["worst"]["region"]
            assert wrong["region"] == [max(0, x0 - 8), max(0, y0 - 8),
                                       min(256, x1 + 8), min(256, y1 + 8)], (wrong, scored)
            _, _, rows = read_png(picture(wrong))
            colours = {row[index:index + 3] for row in rows for index in range(0, len(row), 3)}
            assert len(colours) > 1, colours
            assert {bytes((220, 40, 40)), bytes((40, 90, 220))} & colours, sorted(colours)
            print("target error map:", json.dumps(wrong), flush=True)
            print("worst cell:", json.dumps(scored["worst"]), flush=True)

            # Every region on the channel is budget-view pixels. The error map's model
            # side must therefore be exactly the budget view's silhouette inside the same
            # region: if the objective pictured its normalised comparison tile instead,
            # the two would disagree while both looked plausible on their own.
            paired = stream({"id": 33, "op": "exec",
                             "feedback": {"mode": "full", "pass": "silhouette"},
                             "code": "bpy.data.objects['Cube'].location.z += 0.3"})
            pictures = {event["kind"]: event for event in paired if event["event"] == "image"}
            assert set(pictures) == {"full", "error"}, paired
            _, _, budget = read_png(picture(pictures["full"]))
            _, _, mapped = read_png(picture(pictures["error"]))
            x0, y0, x1, y1 = pictures["error"]["region"]
            model_colours = (bytes((255, 255, 255)), bytes((40, 90, 220)))
            for row in range(y1 - y0):
                for column in range(x1 - x0):
                    index = column * 3
                    colour = mapped[row][index:index + 3]
                    lit = budget[y0 + row][(x0 + column) * 3] == 255
                    assert (colour in model_colours) == lit, (row, column, colour, lit)
            print("error map agrees with the budget view over",
                  (x1 - x0) * (y1 - y0), "pixels", flush=True)

            # A target whose score did not move is a number, not a picture.
            still = execute("pass")
            assert still["objective"]["targets"]["front"]["delta"]["iou"] == 0.0, still
            assert "images" not in still, still
            call("target", "clear")

            # NUDGE changes a datablock and returns it, so it renders but shows nothing.
            costs = {"cube settled": [execute("pass")["ms"] for _ in range(3)],
                     "cube rendered": [execute(NUDGE)["ms"] for _ in range(3)],
                     "cube render only": ast.literal_eval(execute(RENDER)["value"])}
            grid = execute("bpy.ops.object.select_all(action='SELECT')\n"
                           "bpy.ops.object.delete(use_global=False)\n"
                           "bpy.ops.mesh.primitive_grid_add(x_subdivisions=1000, y_subdivisions=1000)\n"
                           "bpy.context.object.rotation_euler[0] = 0.6\n")
            assert grid["perception"]["verts"] == 1002001, grid["perception"]
            costs["grid settled"] = [execute("pass")["ms"] for _ in range(3)]
            costs["grid rendered"] = [execute(NUDGE.replace("Cube", "Grid"))["ms"] for _ in range(3)]
            costs["grid render only"] = ast.literal_eval(execute(RENDER)["value"])
            print("action ms including one feedback cycle:", json.dumps(costs), flush=True)
        finally:
            call("session", "close")
    print("agent feedback: all assertions passed", flush=True)


if __name__ == "__main__":
    main()
