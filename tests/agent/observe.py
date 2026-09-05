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
            inline = call("observe", "--views", "front", "--passes", "silhouette", "--inline")
            assert "image" not in inline and image(inline)[1:3] == (516, 516)
            assert image(execute("1 + 1", "--observe", "front")["observe"])[1:3] == (516, 516)
            helper = execute("agent.observe(); agent.diff()")
            assert ast.literal_eval(helper["value"]) == {"added": [], "changed": [], "removed": []}, helper
            assert helper["diff"] == {"added": [], "changed": [], "removed": []}, helper
            # Failure cleanup also preserves Main and Python callbacks.
            before = call("session", "snapshot")["snapshot"]
            call("observe", "--views", "front", "--ref", root / "missing.png", ok=False)
            assert call("session", "snapshot")["snapshot"] == before
            execute("bpy.context.scene.camera = None")
            assert "scene.camera" in call("observe", "--views", "camera", ok=False)["error"]["message"]
            call("observe", "--size", "256", ok=False)
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
            execute("bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete(use_global=False)")
            empty = call("observe", "--views", "front", "--passes", "silhouette", "--inline")
            _, _, _, rows = image(empty)
            assert set(b"".join(row[6:1542] for row in rows[2:514])) == {0}
        finally:
            call("session", "close")
        assert "observe" in execute("42", "--observe", "front", "--file", blend)
        execute("bpy.context.scene.camera = None", "--save", root / "no-camera.blend")
        call("observe", "--views", "camera", "--file", root / "no-camera.blend", ok=False)
    print("agent observation: all assertions passed", flush=True)


if __name__ == "__main__":
    main()
