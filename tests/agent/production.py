# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Native production commands against real Blender, including evaluated results.

Python exec is used for assertions/measurements, never to implement the commands
under test. The process builds every scene itself. No scene fixtures or mocks.
"""

import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile


def main():
    executable = str(Path(sys.argv[1]).resolve())
    with tempfile.TemporaryDirectory(prefix="agent production ") as directory:
        root = Path(directory)
        requests = []
        expected_errors = set()

        def request(op, ok=True, **fields):
            requests.append({"id": len(requests) + 1, "op": op, **fields})
            if not ok:
                expected_errors.add(len(requests))

        def check(code):
            request("exec", code=code)

        def data(path, value):
            request("data", action="set", path=path, value=value)

        def create(name, **fields):
            request("object", action="create", name=name, **fields)

        request("session", action="feedback",
                feedback={"perception": False, "image": {"mode": "off"}})
        request("scene", action="reset")
        create("Body", type="MESH", primitive="cube", scale=[0.25, 0.25, 1])
        request("rig", action="create", name="Rig")
        request("rig", action="bone", name="Rig", bone="Root",
                head=[0, 0, -1], tail=[0, 0, 1])
        request("rig", action="bone", name="Rig", bone="Tip", parent="Root",
                head=[0, 0, 1], tail=[0, 0, 2])
        request("pose", action="set", name="Missing", bone="Root", ok=False)
        request("pose", action="set", name="Rig", bone="Missing", location=[0, 0, 0], ok=False)
        request("pose", action="set", name="Rig", bone="Root", ok=False)
        request("animation", action="key", path='objects["Rig"].location', frame=-1048575, ok=False)
        request("animation", action="key", path='objects["Rig"].location', frame=1048575, ok=False)
        request("animation", action="key", path='objects["Rig"].location', index=-2, ok=False)
        request("animation", action="delete", path='objects["Rig"].location', index=3, ok=False)
        request("animation", action="key", path='objects["Rig"].name', ok=False)
        request("animation", action="key", path='objects["Missing"].location', ok=False)
        # Blender supports negative animation frames; reject out-of-range values,
        # not the valid negative timeline.
        request("animation", action="key", path='objects["Rig"].location', frame=-2)
        request("animation", action="delete", path='objects["Rig"].location', frame=-2)
        request("rig", action="bind", name="Rig", objects=["Body"], weights="automatic")
        check("""
body = bpy.data.objects['Body']
rig = bpy.data.objects['Rig']
assert body.parent == rig
assert any(m.type == 'ARMATURE' and m.object == rig for m in body.modifiers)
assert any(g.weight > 0 for v in body.data.vertices for g in v.groups)
assert rig.data.bones['Tip'].parent == rig.data.bones['Root']
def vertices():
    bpy.context.view_layer.update()
    ob = bpy.data.objects['Body'].evaluated_get(bpy.context.evaluated_depsgraph_get())
    return [tuple(ob.matrix_world @ v.co) for v in ob.data.vertices]
rest = vertices()
""")
        bone_path = 'objects["Rig"].pose.bones["Root"].rotation_euler'
        request("pose", action="set", name="Rig", bone="Root", rotation=[0, 0, 0])
        request("animation", action="key", path=bone_path, frame=1)
        request("pose", action="set", name="Rig", bone="Root", rotation=[0, 0.7, 0])
        check("posed = vertices(); assert max(abs(a-b) for u,v in zip(rest,posed) for a,b in zip(u,v)) > 0.1")
        request("animation", action="key", path=bone_path, frame=5)
        request("scene", action="frame", frame=1)
        check("first = vertices(); assert max(abs(a-b) for u,v in zip(rest,first) for a,b in zip(u,v)) < 1e-5")
        request("scene", action="frame", frame=5)
        check("last = vertices(); assert max(abs(a-b) for u,v in zip(first,last) for a,b in zip(u,v)) > 0.1")
        request("animation", action="bake", objects=["Rig"], start=1, end=5, step=1)
        request("scene", action="frame", frame=5)
        check("assert max(abs(a-b) for u,v in zip(last,vertices()) for a,b in zip(u,v)) < 1e-4")
        request("scene", action="save", path=str(root / "rig.blend"))
        request("scene", action="reset")
        request("scene", action="open", path=str(root / "rig.blend"))
        request("scene", action="frame", frame=1)
        check("reopened_first = vertices()")
        request("scene", action="frame", frame=5)
        check("assert max(abs(a-b) for u,v in zip(reopened_first,vertices()) for a,b in zip(u,v)) > 0.1")
        # Native USD animation interchange; unlike Python-defined glTF/FBX exporters
        # this remains available through the native operator surface.
        data('scenes["Scene"].frame_start', 1)
        data('scenes["Scene"].frame_end', 5)
        request("operator", action="call", name="wm.usd_export",
                properties={"filepath": str(root / "motion.usdc"), "export_animation": True})
        request("scene", action="reset")
        request("operator", action="call", name="wm.usd_import",
                properties={"filepath": str(root / "motion.usdc")})
        check("""
assert any(o.type == 'MESH' for o in bpy.context.scene.objects)
def all_points():
    bpy.context.view_layer.update()
    deps = bpy.context.evaluated_depsgraph_get()
    return [tuple(o.evaluated_get(deps).matrix_world @ v.co)
            for o in sorted(bpy.context.scene.objects, key=lambda o:o.name) if o.type == 'MESH'
            for v in o.evaluated_get(deps).data.vertices]
""")
        request("scene", action="frame", frame=1)
        check("import_first = all_points()")
        request("scene", action="frame", frame=5)
        check("assert max(abs(a-b) for u,v in zip(import_first,all_points()) for a,b in zip(u,v)) > 0.1")

        # Visual bake must not apply a parent or constraints twice on playback.
        request("scene", action="reset")
        create("Target", type="EMPTY")
        create("Parent", type="EMPTY", location=[3, 2, 1])
        create("Follower", type="EMPTY")
        data('objects["Follower"].parent', {"path": 'objects["Parent"]'})
        request("data", action="call", path='objects["Follower"].constraints.new',
                arguments={"type": "COPY_LOCATION"})
        data('objects["Follower"].constraints["Copy Location"].target',
             {"path": 'objects["Target"]'})
        request("animation", action="key", path='objects["Target"].location', frame=1)
        data('objects["Target"].location', [2, 4, 6])
        request("animation", action="key", path='objects["Target"].location', frame=5)
        check("""
visual_before = {}
for frame in range(1, 6):
    bpy.context.scene.frame_set(frame)
    visual_before[frame] = tuple(bpy.data.objects['Follower'].matrix_world.translation)
""")
        request("animation", action="bake", objects=["Follower"], start=1, end=5)
        check("""
follower = bpy.data.objects['Follower']
assert follower.parent is None and follower.constraints[0].mute
for frame in range(1, 6):
    bpy.context.scene.frame_set(frame)
    assert max(abs(a-b) for a,b in zip(visual_before[frame], follower.matrix_world.translation)) < 1e-4
""")
        request("animation", action="delete", path='objects["Target"].location', frame=5, index=2)
        request("scene", action="frame", frame=5)
        check("assert abs(bpy.data.objects['Target'].location.z) < 1e-5")

        request("scene", action="reset")
        create("Falling", type="MESH", primitive="cube", location=[0, 0, 5])
        request("simulation", action="add", name="Falling", type="RIGID_BODY", start=1, end=12)
        request("simulation", action="bake", name="Falling", type="RIGID_BODY", start=1, end=12)
        request("scene", action="frame", frame=1)
        check("z_start = bpy.data.objects['Falling'].evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world.translation.z")
        request("scene", action="frame", frame=12)
        check("""
ob = bpy.data.objects['Falling'].evaluated_get(bpy.context.evaluated_depsgraph_get())
assert ob.matrix_world.translation.z < z_start - 0.2
assert bpy.context.scene.rigidbody_world.point_cache.is_baked
""")
        request("simulation", action="free", name="Falling", type="RIGID_BODY")
        check("assert not bpy.context.scene.rigidbody_world.point_cache.is_baked")

        # Cloth actually falls, not merely an operator reporting FINISHED.
        create("Cloth", type="MESH", primitive="plane", location=[4, 0, 4])
        request("operator", action="call", name="mesh.subdivide", objects=["Cloth"],
                active="Cloth", mode="EDIT", properties={"number_cuts": 3})
        request("simulation", action="add", name="Cloth", type="CLOTH", start=1, end=5)
        request("simulation", action="bake", name="Cloth", type="CLOTH", start=1, end=5)
        request("scene", action="frame", frame=5)
        check("""
ob = bpy.data.objects['Cloth'].evaluated_get(bpy.context.evaluated_depsgraph_get())
assert min(v.co.z for v in ob.data.vertices) < -0.01
assert bpy.data.objects['Cloth'].modifiers[0].point_cache.is_baked
""")
        request("simulation", action="free", name="Cloth", type="CLOTH")
        create("Soft", type="MESH", primitive="cube", location=[8, 0, 4])
        request("simulation", action="add", name="Soft", type="SOFT_BODY", start=1, end=5)
        data('objects["Soft"].soft_body.use_goal', False)
        request("simulation", action="bake", name="Soft", type="SOFT_BODY", start=1, end=5)
        request("scene", action="frame", frame=5)
        check("""
ob = bpy.data.objects['Soft'].evaluated_get(bpy.context.evaluated_depsgraph_get())
assert min(v.co.z for v in ob.data.vertices) < -1.01
""")
        request("simulation", action="free", name="Soft", type="SOFT_BODY")
        create("Ocean", type="MESH", primitive="plane", location=[20, 0, 0])
        request("simulation", action="add", name="Ocean", type="OCEAN", start=1, end=2,
                path=str(root / "ocean"))
        data('objects["Ocean"].modifiers["Ocean"].resolution', 2)
        data('objects["Ocean"].modifiers["Ocean"].viewport_resolution', 2)
        check("""
ob = bpy.data.objects['Ocean'].evaluated_get(bpy.context.evaluated_depsgraph_get())
assert len(ob.data.vertices) > 4
assert max(v.co.z for v in ob.data.vertices) - min(v.co.z for v in ob.data.vertices) > 0.01
""")
        request("simulation", action="bake", name="Ocean", type="OCEAN", start=1, end=2,
                path=str(root / "ocean"))
        check(f"""
from pathlib import Path
assert bpy.data.objects['Ocean'].modifiers['Ocean'].is_cached
cache_files = sorted(Path({str(root / 'ocean')!r}).glob('disp_*.exr'))
assert len(cache_files) == 2 and all(p.stat().st_size > 100 for p in cache_files), cache_files
""")
        request("simulation", action="free", name="Ocean", type="OCEAN")
        create("Domain", type="MESH", primitive="cube", location=[30, 0, 0])
        request("simulation", action="add", name="Domain", type="FLUID", start=1, end=2,
                path=str(root / "fluid"))
        data('objects["Domain"].modifiers["Fluid"].domain_settings.resolution_max', 16)
        create("Smoke", type="MESH", primitive="uv_sphere", location=[30, 0, 0], scale=[0.3]*3)
        request("operator", action="call", name="object.modifier_add", objects=["Smoke"],
                active="Smoke", properties={"type": "FLUID"})
        data('objects["Smoke"].modifiers["Fluid"].fluid_type', "FLOW")
        request("scene", action="save", path=str(root / "sim.blend"))
        request("simulation", action="bake", name="Domain", type="FLUID", start=1, end=2,
                path=str(root / "fluid"))
        check("assert bpy.data.objects['Domain'].modifiers['Fluid'].domain_settings.has_cache_baked_data")
        request("simulation", action="free", name="Domain", type="FLUID")

        # Use a real scene camera and native CPU-capable production renderer.
        request("scene", action="reset")
        create("Visible", type="MESH", primitive="cube")
        # Camera add otherwise inherits Blender's view alignment. Aim down -Z.
        create("Camera", type="CAMERA", location=[0, 0, 6], rotation=[0, 0, 0])
        data('scenes["Scene"].camera', {"path": 'objects["Camera"]'})
        create("Key", type="LIGHT", location=[1, 2, 4])
        data('objects["Key"].data.energy', 1000)
        request("render", action="frame", frame=1, path=str(root / "frame.png"),
                engine="CYCLES", format="PNG", width=64, height=64, samples=4)
        check("""
image = bpy.data.images.load(bpy.context.scene.render.filepath)
pixels = list(image.pixels)
assert max(pixels[0::4]) - min(pixels[0::4]) > 0.05
bpy.data.images.remove(image)
""")
        request("render", action="animation", start=1, end=2, path=str(root / "anim-"),
                engine="CYCLES", format="PNG", width=32, height=32, samples=1)
        data('scenes["Scene"].render.ffmpeg.format', "MPEG4")
        data('scenes["Scene"].render.ffmpeg.codec', "H264")
        request("render", action="animation", start=1, end=2, path=str(root / "clip.mp4"),
                engine="CYCLES", format="FFMPEG", width=32, height=32, samples=1)
        # Decode using Blender's bundled movie support so Windows/macOS tests
        # do not depend on an independently installed ffprobe executable.
        check(f"""
from pathlib import Path
movies = list(Path({str(root)!r}).glob('clip*.mp4'))
assert len(movies) == 1 and movies[0].stat().st_size > 100
movie = bpy.data.images.load(str(movies[0]))
assert movie.source == 'MOVIE'
assert movie.frame_duration == 2, movie.frame_duration
assert tuple(movie.size) == (32, 32), tuple(movie.size)
assert max(movie.pixels[0::4]) - min(movie.pixels[0::4]) > 0.01
bpy.data.images.remove(movie)
""")
        request("scene", action="save", path=str(root / "render.blend"))

        process = subprocess.run(
            [executable, "repl", "--standalone"], cwd=root,
            input="".join(json.dumps(item) + "\n" for item in requests),
            capture_output=True, text=True, encoding="utf-8", timeout=1200)
        assert process.returncode == 0, (process.stdout[-12000:], process.stderr[-12000:])
        events = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
        for item in requests:
            terminal = [event for event in events if event.get("id") == item["id"] and
                        event.get("event") in {"done", "error"}]
            diagnostic = (item, terminal,
                          [event for event in events if event.get("id") == item["id"] and
                           event.get("event") == "log"], process.stderr[-12000:])
            if item["id"] in expected_errors:
                assert any(event.get("event") == "error" for event in terminal), diagnostic
            else:
                assert terminal and all(event.get("ok", False) for event in terminal), diagnostic
        image = (root / "frame.png").read_bytes()
        assert image[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack(">II", image[16:24]) == (64, 64)
        assert (root / "anim-0001.png").is_file() and (root / "anim-0002.png").is_file()
        assert (root / "motion.usdc").stat().st_size > 100
        if events[0].get("device") is not None:
            eevee_requests = [
                {"id": 1, "op": "session", "action": "feedback",
                 "feedback": {"perception": False, "image": {"mode": "off"}}},
                {"id": 2, "op": "render", "action": "frame", "frame": 1,
                 "path": str(root / "eevee.png"), "engine": "BLENDER_EEVEE",
                 "format": "PNG", "width": 32, "height": 32, "samples": 4},
                {"id": 3, "op": "exec", "code":
                 "assert bpy.context.scene.eevee.taa_render_samples == 4"},
            ]
            eevee = subprocess.run(
                [executable, "repl", "--standalone", "--file", str(root / "render.blend")],
                cwd=root, input="".join(json.dumps(item) + "\n" for item in eevee_requests),
                capture_output=True, text=True, encoding="utf-8", timeout=240)
            assert eevee.returncode == 0, (eevee.stdout, eevee.stderr)
            eevee_events = [json.loads(line) for line in eevee.stdout.splitlines() if line.strip()]
            assert not any(event["event"] == "error" for event in eevee_events), eevee_events
            assert {event["id"] for event in eevee_events if event["event"] == "done" and
                    event.get("ok")} == {1, 2, 3}, eevee_events
            assert (root / "eevee.png").is_file()
            print("production: native EEVEE samples path verified")
        else:
            print("production: no native device; EEVEE remains unverified (CPU checks ran)")
        print(f"production: {len(requests)} native/measurement requests passed; deformation, "
              "animation round-trip, rigid body, cloth, soft body, ocean, fluid, render and video verified")


if __name__ == "__main__":
    main()
