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

# Every step reports the events one action produces and what producing them cost.
DRIVE = """
import json as _json, time as _time, agent_feedback as _feedback
_start = _time.perf_counter()
_events = _feedback.run({request})
_json.dumps({{"ms": (_time.perf_counter() - _start) * 1000, "events": _events}})
"""

SCENE = """
bpy.ops.mesh.primitive_ico_sphere_add(radius=0.5, location=(-3, 0, 0))
bpy.ops.mesh.primitive_ico_sphere_add(radius=0.5, location=(3, 0, 0))
"""


def images(payload):
    return [event for event in payload["events"] if event["event"] == "image"]


def sole(payload, name):
    matches = [event for event in payload["events"] if event["event"] == name]
    assert len(matches) == 1, (name, payload["events"])
    return matches[0]


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
            result = json.loads(process.stdout)
            assert result.get("ok", True) == ok, result
            return result

        def drive(code, request="None"):
            result = call("exec", "-c", "import agent_feedback\n" + code +
                          DRIVE.format(request=request))
            return json.loads(ast.literal_eval(result["value"]))

        def picture(event):
            data = (base64.b64decode(event["inline"]) if "inline" in event
                    else Path(event["path"]).read_bytes())
            width, height, _ = read_png(data)
            assert event["size"] == [width, height], event
            x0, y0, x1, y1 = event["region"]
            assert [width, height] == [x1 - x0, y1 - y0], event
            return data

        call("session", "open")
        try:
            # An agent that has seen nothing gets a whole frame, and no delta to compare with.
            first = drive("bpy.ops.wm.read_factory_settings(use_empty=True)")
            empty = sole(first, "perception")
            assert empty["changed"] is None, empty
            assert (empty["objects"], empty["verts"], empty["faces"]) == (0, 0, 0), empty
            assert empty["bounds"] == {"low": [-1, -1, -1], "high": [1, 1, 1]}, empty
            assert empty["dims"] == [2, 2, 2], empty
            assert empty["framing"]["objects"] == [], empty
            assert empty["symmetry"] == {"x": 1.0, "y": None, "z": 1.0}, empty
            whole = sole(first, "image")
            assert (whole["kind"], whole["view"], whole["pass"]) == ("full", "front", "color"), whole
            assert whole["region"] == [0, 0, 256, 256] and whole["size"] == [256, 256], whole
            assert Path(whole["path"]).parent == root / ".blender-cli" / "feedback", whole
            picture(whole)

            added = drive("bpy.ops.mesh.primitive_cube_add(size=1)")
            cube = sole(added, "perception")
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
            assert [event["kind"] for event in images(added)] == ["delta", "overlay"], added
            print("cube add:", json.dumps(cube), flush=True)
            print("cube add images:", json.dumps(images(added)), flush=True)

            # Anchors fix the automatic framing so a later move is a local change, not a rescale.
            anchored = drive(SCENE)
            state = call("inspect")
            counts = sole(anchored, "perception")
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
            unchanged = drive("pass")
            same = sole(unchanged, "perception")
            assert same["changed"] == {"objects": [], "view": "front", "region": None,
                                       "fraction": 0.0, "silhouette_delta": 0.0}, same
            assert images(unchanged) == [], unchanged
            assert same["symmetry"] == counts["symmetry"], (same, counts)
            print("unchanged perception:", json.dumps(same), flush=True)

            # Moving inside the anchors' bounds leaves the framing alone, so this is a
            # local change: the cube's old and new pixels, and nothing else.
            moved = drive("bpy.data.objects['Cube'].location.x += 0.5")
            shifted = sole(moved, "perception")
            assert shifted["changed"]["objects"] == ["Cube"], shifted
            assert shifted["bounds"] == counts["bounds"], shifted
            assert 0.002 < shifted["changed"]["fraction"] < 0.2, shifted
            assert shifted["symmetry"]["x"] < counts["symmetry"]["x"], (shifted, counts)
            union = project(shifted["framing"], -0.5, 1.0, -0.5, 0.5)
            region = shifted["changed"]["region"]
            assert covers(region, union, slack=3), (region, union)
            assert covers(union, region, slack=3), (region, union)
            delta, overlay = images(moved)
            assert delta["kind"] == "delta" and overlay["kind"] == "overlay", images(moved)
            assert covers(delta["region"], region), (delta, region)
            assert covers(delta["region"], union, slack=3), (delta, union)
            assert delta["region"] == overlay["region"], (delta, overlay)
            assert delta["size"][0] < 256 and delta["size"][1] < 256, delta
            assert picture(delta) != picture(overlay)
            print("cube move:", json.dumps(shifted), flush=True)
            print("cube move images:", json.dumps(images(moved)), flush=True)
            print("cube pixels before and after the move:", [round(value, 1) for value in union],
                  flush=True)

            # A change under the threshold is a perception number, not an image.
            quiet = drive("agent_feedback.configure({'image': {'threshold': 0.5}})\n"
                          "bpy.data.objects['Cube'].location.x -= 0.5")
            assert sole(quiet, "perception")["changed"]["fraction"] > 0.002, quiet
            assert images(quiet) == [], quiet

            silent = drive("agent_feedback.configure({'image': {'threshold': 0.002, 'mode': 'off'}})\n"
                           "bpy.data.objects['Cube'].location.z += 0.5")
            assert sole(silent, "perception")["changed"]["fraction"] > 0.002, silent
            assert images(silent) == [], silent

            # One request may ask for more picture than the session policy pushes.
            asked = drive("agent_feedback.configure({'image': {'mode': 'delta'}})\n"
                          "bpy.data.objects['Cube'].location.z -= 0.5",
                          request="{'feedback': {'image': {'mode': 'full'}}, 'inline': True}")
            frame = sole(asked, "image")
            assert frame["kind"] == "full" and frame["size"] == [256, 256], frame
            assert base64.b64decode(frame["inline"]) == Path(frame["path"]).read_bytes(), frame
            picture(frame)

            # agent_feedback.perceive() samples without advancing what the agent last saw.
            paired = drive("bpy.ops.mesh.primitive_cone_add(radius1=0.4, location=(0, 0, 1))\n"
                           "SAMPLE = agent_feedback.perceive()")
            sample = json.loads(ast.literal_eval(call(
                "exec", "-c", "import json; json.dumps(SAMPLE)")["value"]))
            event = sole(paired, "perception")
            assert sample == {key: value for key, value in event.items() if key != "event"}, \
                (sample, event)
            print("perceive matches the event:", json.dumps(sample["changed"]), flush=True)

            # A budget view the scene cannot render is an image event, never a failed request.
            broken = drive("agent_feedback.configure({'image': {'views': ['camera']}})")
            failure = sole(broken, "image")
            assert failure["kind"] == "error" and failure["view"] == "camera", failure
            assert "scene.camera" in failure["message"], failure
            assert "path" not in failure and "region" not in failure, failure
            log = sole(broken, "log")
            assert log["stream"] == "stderr" and "perception" in log["text"], log
            assert not [event for event in broken["events"] if event["event"] == "perception"], broken
            print("render failure:", json.dumps([failure, log]), flush=True)

            recovered = drive("agent_feedback.configure({'image': {'views': ['front']}})")
            assert sole(recovered, "perception")["changed"]["fraction"] == 0.0, recovered

            costs = {"cube": [drive("pass")["ms"] for _ in range(3)]}
            grid = drive("bpy.ops.object.select_all(action='SELECT')\n"
                         "bpy.ops.object.delete(use_global=False)\n"
                         "bpy.ops.mesh.primitive_grid_add(x_subdivisions=1000, y_subdivisions=1000)\n"
                         "bpy.context.object.rotation_euler[0] = 0.6\n")
            million = sole(grid, "perception")
            assert million["verts"] == 1002001, million
            costs["grid"] = [drive("pass")["ms"] for _ in range(3)]
            print("feedback cycle ms (1,002,001-vertex grid add):", grid["ms"], flush=True)
            print("feedback cycle ms:", json.dumps(costs), flush=True)
        finally:
            call("session", "close")
    print("agent feedback: all assertions passed", flush=True)


if __name__ == "__main__":
    main()
