# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Real EEVEE renders, byte determinism, Main invariance and headless editing."""

import ast
import base64
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import zlib

from gpu import require_device


EDIT = """
bpy.context.view_layer.objects.active = bpy.data.objects['Cube']
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.select_all(action='SELECT')
bpy.ops.mesh.subdivide(number_cuts=1)
bpy.ops.mesh.bevel(offset=0.08, segments=2)
bpy.ops.object.mode_set(mode='OBJECT')
"""
SCENE = EDIT + """
bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, location=(2.5, 0, 0))
mat = bpy.data.materials.new('Blue')
mat.node_tree.nodes.get('Principled BSDF').inputs['Base Color'].default_value = (0.04, 0.2, 0.5, 1)
bpy.context.object.data.materials.append(mat)
"""

SCATTER = """
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_plane_add()
plane = bpy.context.object
tree = bpy.data.node_groups.new('Scatter', 'GeometryNodeTree')
tree.interface.new_socket(name='Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
tree.interface.new_socket(name='Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
input_node = tree.nodes.new('NodeGroupInput')
output_node = tree.nodes.new('NodeGroupOutput')
points = tree.nodes.new('GeometryNodeDistributePointsOnFaces')
points.inputs['Density'].default_value = 3
ico = tree.nodes.new('GeometryNodeMeshIcoSphere')
ico.inputs['Radius'].default_value = 0.15
ico.inputs['Subdivisions'].default_value = 1
material = bpy.data.materials.new('Red instances')
material.node_tree.nodes.get('Principled BSDF').inputs['Base Color'].default_value = (0.8, 0.015, 0.005, 1)
set_material = tree.nodes.new('GeometryNodeSetMaterial')
set_material.inputs['Material'].default_value = material
instances = tree.nodes.new('GeometryNodeInstanceOnPoints')
tree.links.new(input_node.outputs['Geometry'], points.inputs['Mesh'])
tree.links.new(ico.outputs['Mesh'], set_material.inputs['Geometry'])
tree.links.new(set_material.outputs['Geometry'], instances.inputs['Instance'])
tree.links.new(points.outputs['Points'], instances.inputs['Points'])
tree.links.new(instances.outputs['Instances'], output_node.inputs['Geometry'])
plane.modifiers.new('Scatter', 'NODES').node_group = tree
bpy.context.view_layer.update()
instance_points = []
for instance in bpy.context.evaluated_depsgraph_get().object_instances:
    if instance.is_instance:
        obj = instance.object
        assert len(obj.data.vertices) == 12
        for axis in range(3):
            vertices = [v.co[axis] for v in obj.data.vertices]
            corners = [c[axis] for c in obj.bound_box]
            # Upstream ico construction has small (~2e-6) conservative bounds.
            assert abs(min(vertices) - min(corners)) < 1e-5
            assert abs(max(vertices) - max(corners)) < 1e-5
        instance_points.extend(instance.matrix_world @ v.co for v in obj.data.vertices)
assert len(instance_points) > 12
from agent_observe import isolated_data, render_scene
with isolated_data():
    scene, framed_points, center, radius, framing = render_scene(bpy.context.scene, 512, 'Plane')
    for axis in range(3):
        assert abs(min(p[axis] for p in framed_points) - min(p[axis] for p in instance_points)) < 1e-5
        assert abs(max(p[axis] for p in framed_points) - max(p[axis] for p in instance_points)) < 1e-5
len(instance_points) // 12
"""


def read_png(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    offset, compressed, kinds = 8, b"", []
    while offset < len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        kind, chunk = data[offset + 4:offset + 8], data[offset + 8:offset + 8 + length]
        kinds.append(kind)
        if kind == b"IHDR":
            w, h, bits, color, _, _, _ = struct.unpack(">IIBBBBB", chunk)
            assert (bits, color) == (8, 2)
        if kind == b"IDAT":
            compressed += chunk
        offset += length + 12
    assert kinds == [b"IHDR", b"IDAT", b"IEND"], kinds
    raw = zlib.decompress(compressed)
    assert len(raw) == h * (w * 3 + 1)
    rows = [raw[y * (w * 3 + 1) + 1:(y + 1) * (w * 3 + 1)] for y in range(h)]
    assert all(raw[y * (w * 3 + 1)] == 0 for y in range(h))
    return w, h, rows


def main():
    executable = str(Path(sys.argv[1]).resolve())
    require_device(executable)
    with tempfile.TemporaryDirectory(prefix="agent observe ") as directory:
        root = Path(directory)

        def call(*args, ok=True):
            process = subprocess.run([executable, *map(str, args), "--json"], cwd=root,
                                     capture_output=True, text=True, timeout=600)
            assert process.returncode == (0 if ok else 1), (args, process.returncode, process.stdout, process.stderr)
            result = json.loads(process.stdout)
            assert result.get("ok", True) == ok, result
            return result

        def execute(code, *args, **kwargs):
            return call("exec", "-c", code, *args, **kwargs)

        def image(result):
            data = base64.b64decode(result["base64"]) if "base64" in result else Path(result["image"]).read_bytes()
            w, h, rows = read_png(data)
            assert result["size"] == [w, h], result
            return data, w, h, rows

        blend = root / "scene.blend"
        execute(SCENE, "--save", blend)
        cube = call("inspect", "--object", "Cube", "--file", blend)["objects"][0]
        assert cube["mesh"]["vertices"] > 26, cube
        print("one-shot edited mesh:", cube["mesh"], flush=True)
        execute("bpy.ops.object.mode_set(mode='EDIT'); bpy.ops.mesh.select_all(action='SELECT'); "
                "bpy.ops.mesh.extrude_region_move(TRANSFORM_OT_translate={'value': (0, 0, 0.2)}); "
                "bpy.ops.transform.translate(value=(0.1, 0, 0)); bpy.ops.object.mode_set(mode='OBJECT')")
        first = call("observe", "--views", "front,side,top,persp", "--file", blend, "--out", root / "a.png")
        second = call("observe", "--views", "front,side,top,persp", "--file", blend, "--out", root / "b.png")
        a, w, h, _ = image(first)
        b, _, _, _ = image(second)
        assert a == b, "Separate-process observation bytes differ"
        assert (w, h) == (516, 2064)
        print("determinism sha256:", hashlib.sha256(a).hexdigest(), hashlib.sha256(b).hexdigest(), flush=True)

        call("session", "open", "--file", blend)
        try:
            before = call("session", "snapshot")["snapshot"]
            times = []
            for _ in range(2):
                start = time.perf_counter()
                sheet = call("observe", "--views", "front", "--passes", "color,wire,silhouette,normal,depth")
                times.append(time.perf_counter() - start)
            print("session observe seconds (first/subsequent):", times, flush=True)
            after = call("session", "snapshot")["snapshot"]
            assert before == after, (before, after)
            print("unchanged snapshots:", before, after, flush=True)
            assert ast.literal_eval(execute("agent.diff()")["value"]) == {"added": [], "changed": [], "removed": []}
            _, w, h, rows = image(sheet)
            assert (w, h) == (2580, 516)
            for i in range(5):
                values = set(b"".join(row[(i * 516 + 2) * 3:(i * 516 + 514) * 3] for row in rows[2:514]))
                assert len(values) > 1, (i, values)
                if i == 2:
                    assert values == {0, 255}, values
            print("five-pass dimensions:", [w, h], flush=True)
            separate = call("observe", "--views", "front,side", "--passes", "silhouette,depth", "--layout", "separate")
            assert len(separate["images"]) == 4
            assert len({record["image"] for record in separate["images"]}) == 4
            for record in separate["images"]:
                assert image(record)[1:3] == (516, 516)
            ref = separate["images"][0]["image"]
            beside = call("observe", "--views", "front", "--ref", ref)
            assert image(beside)[1:3] == (1032, 516)
            over = call("observe", "--views", "front", "--ref", ref, "--overlay")
            assert image(over)[1:3] == (516, 516)
            assert image(over)[0] != image(call("observe", "--views", "front"))[0]
            wide_ref = call("observe", "--views", "front", "--ref", sheet["image"])
            assert image(wide_ref)[1:3] == (3080, 516)
            inline = call("observe", "--views", "front", "--passes", "silhouette", "--inline")
            assert "image" not in inline and image(inline)[1:3] == (516, 516)
            helper = execute("agent.observe(); agent.diff()")
            assert ast.literal_eval(helper["value"]) == {"added": [], "changed": [], "removed": []}, helper
            assert (helper["diff"]["added"], helper["diff"]["changed"],
                    helper["diff"]["removed"]) == ([], [], []), helper
            pending = execute("bpy.data.objects['Cube'].location.x += 0.1; agent.observe(); agent.diff()")
            assert any(item["name"] == "Cube" and "transform" in item["fields"]
                       for item in ast.literal_eval(pending["value"])["changed"]), pending
            # Every camera preset and resolution rung; camera settings cannot leak to later presets.
            execute("bpy.context.scene.camera.data.shift_x = 0.2")
            ordered = call("observe", "--views", "camera,front,back,left,right,bottom", "--passes", "silhouette")
            _, w, h, rows = image(ordered)
            assert (w, h) == (516, 3096)
            standalone = image(call("observe", "--views", "front", "--passes", "silhouette"))
            assert rows[516:1032] == standalone[3]
            assert image(call("observe", "--views", "persp", "--frame", "Cube", "--size", "768"))[1:3] == (772, 772)
            assert image(call("observe", "--views", "front", "--size", "1024", "--inline"))[1:3] == (1028, 1028)
            # Failure cleanup also preserves Main and Python callbacks.
            before = call("session", "snapshot")["snapshot"]
            call("observe", "--views", "front", "--ref", root / "missing.png", ok=False)
            assert call("session", "snapshot")["snapshot"] == before
            execute("bpy.context.scene.camera = None")
            assert "scene.camera" in call("observe", "--views", "camera", ok=False)["error"]["message"]
            call("observe", "--size", "384", ok=False)
            call("observe", "--overlay", ok=False)
            call("observe", "--views", "unknown", ok=False)
            call("session", "rollback", before)
            execute(EDIT)
            cube = call("inspect", "--object", "Cube")["objects"][0]
            print("session edited mesh:", cube["mesh"], flush=True)
            assert cube["mesh"]["vertices"] > 26
            execute("bpy.ops.object.mode_set(mode='EDIT'); bpy.ops.mesh.select_all(action='SELECT'); "
                    "bpy.ops.mesh.extrude_region_move(TRANSFORM_OT_translate={'value': (0, 0, 0.2)}); "
                    "bpy.ops.transform.translate(value=(0.1, 0, 0))")
            assert execute("bpy.context.mode")["value"] == "'EDIT_MESH'"
            assert call("inspect", "--object", "Cube")["objects"][0]["mesh"]["vertices"] > cube["mesh"]["vertices"]
            execute("bpy.ops.object.mode_set(mode='OBJECT')")
            failure = execute("bpy.ops.view3d.select(location=(10, 10))", ok=False)
            assert "GPU viewport selection" in failure["error"]["message"], failure
            # No VIEW_3D in the loaded active layout: next boundary creates a data-only fallback.
            execute("[setattr(area, 'type', 'CONSOLE') for area in bpy.context.window.screen.areas]")
            assert execute("bpy.context.area.type")["value"] == "'VIEW_3D'"
            call("session", "rollback", before)
            assert execute("bpy.context.area.type")["value"] == "'VIEW_3D'"
            deleted = execute("bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete(use_global=False); "
                              "len(bpy.context.scene.objects)")
            assert deleted["value"] == "0", deleted
            empty = call("observe", "--views", "front", "--passes", "silhouette", "--inline")
            _, _, _, rows = image(empty)
            assert set(b"".join(row[6:1542] for row in rows[2:514])) == {0}
            scatter = execute(SCATTER)
            print("GN scatter instances:", scatter["value"], flush=True)
            before = call("session", "snapshot")["snapshot"]
            silhouette = call("observe", "--views", "front", "--passes", "silhouette")
            _, _, _, rows = image(silhouette)
            white = sum(row[6:1542].count(255) for row in rows[2:514]) // 3
            assert white > 512 * 512 * 0.01, white
            framed = call("observe", "--views", "front", "--passes", "silhouette", "--frame", "Plane")
            assert image(framed)[0] == image(silhouette)[0], "Named framing lost GN instances"
            color = call("observe", "--views", "front", "--frame", "Plane")
            _, _, _, rows = image(color)
            pixels = b"".join(row[6:1542] for row in rows[2:514])
            red = sum(r > 80 and r > 2 * g and r > 2 * b
                      for r, g, b in zip(pixels[0::3], pixels[1::3], pixels[2::3]))
            assert red > white * 0.5, (red, white)
            assert call("session", "snapshot")["snapshot"] == before
            print("GN front pixels (white/red):", white, red, flush=True)
        finally:
            call("session", "close")
        for geometry in (
                "bpy.ops.curve.primitive_bezier_circle_add(); bpy.context.object.data.bevel_depth = 0.12",
                "bpy.ops.mesh.primitive_cube_add(); o = bpy.context.object; "
                "o.modifiers.new('Array', 'ARRAY').count = 3; "
                "o.modifiers.new('Displace', 'DISPLACE').strength = 0.3"):
            result = execute("bpy.ops.wm.read_factory_settings(use_empty=True); " + geometry + "\n"
                             "a = agent.observe(passes=('silhouette',))\n"
                             "bpy.ops.object.convert(target='MESH')\n"
                             "b = agent.observe(passes=('silhouette',))\n"
                             "assert a['framing'] == b['framing'], (a, b)\n"
                             "assert open(a['image'], 'rb').read() == open(b['image'], 'rb').read()\n"
                             "a['framing']")
            print("converted geometry framing:", result["value"], flush=True)
        execute("bpy.context.scene.camera = None", "--save", root / "no-camera.blend")
        call("observe", "--views", "camera", "--file", root / "no-camera.blend", ok=False)
    print("agent observation: all assertions passed", flush=True)


if __name__ == "__main__":
    main()
