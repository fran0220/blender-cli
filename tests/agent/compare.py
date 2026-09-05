# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Real reference images, numeric fitting and live RNA through the installed CLI."""

import ast
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def main():
    executable = str(Path(sys.argv[1]).resolve())
    with tempfile.TemporaryDirectory(prefix="agent compare ") as directory:
        root = Path(directory)

        def call(*args, ok=True):
            process = subprocess.run([executable, *map(str, args), "--json"], cwd=root,
                                     capture_output=True, text=True, timeout=900)
            assert process.returncode == (0 if ok else 1), (args, process.returncode, process.stdout, process.stderr)
            result = json.loads(process.stdout)
            assert result.get("ok", True) == ok, result
            return result

        def execute(code, *args, **kwargs):
            return call("exec", "-c", code, *args, **kwargs)

        def compare(ref, *args, **kwargs):
            return call("compare", "--ref", ref, "--view", "front", "--metric", "iou,chamfer,ssim,hist",
                        *args, **kwargs)

        blend, ref, wrong, colored = (root / name for name in ("scene.blend", "ref.png", "wrong.png", "colored.png"))
        execute("bpy.data.objects['Cube'].scale.x = 0.6", "--save", blend)
        call("observe", "--views", "front", "--out", ref, "--file", blend)
        same = compare(ref, "--file", blend)
        print("self compare:", json.dumps(same), flush=True)
        assert same["iou"] >= 0.98 and same["chamfer"] <= 1 and same["ssim"] >= 0.98 and same["hist"] <= 0.02, same
        execute("""
bpy.data.objects.remove(bpy.data.objects['Cube'], do_unlink=True)
bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16)
mat = bpy.data.materials.new('Red')
mat.node_tree.nodes.get('Principled BSDF').inputs['Base Color'].default_value = (0.5, 0.015, 0.015, 1)
bpy.context.object.data.materials.append(mat)
""", "--save", root / "wrong.blend")
        call("observe", "--views", "front", "--out", wrong, "--file", root / "wrong.blend")
        different = compare(wrong, "--file", blend)
        print("wrong compare:", json.dumps(different), flush=True)
        assert different["iou"] < 0.8 and different["chamfer"] > 10, different
        assert different["ssim"] < 0.9 and different["hist"] > 0.3, different

        print("session open:", json.dumps(call("session", "open", "--file", blend)), flush=True)
        try:
            # Real data from observe; no committed image fixtures or replacement scene mocks.
            call("observe", "--views", "front", "--passes", "silhouette", "--out", root / "mask.png")
            execute("""
from pathlib import Path
import numpy as np
from agent_observe import png, bytes_rgb
def load_rgb(path):
    image = bpy.data.images.load(str(Path(path).resolve()), check_existing=False)
    w, h = image.size
    pixels = np.empty(w * h * 4, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    rgb = bytes_rgb(pixels.reshape(h, w, 4)[::-1, :, :3])
    bpy.data.images.remove(image)
    return rgb
rgb = load_rgb('ref.png')[2:-2, 2:-2]
silhouette = load_rgb('mask.png')[2:-2, 2:-2, 0] != 0
composite = np.where(silhouette[:, :, None], rgb, np.array((35, 140, 210), dtype=np.uint8))
Path('colored.png').write_bytes(png(composite))
""")
            composite = compare(colored, "--mask", "auto", "--debug-out", root / "debug")
            print("colored-background compare:", json.dumps(composite), flush=True)
            assert composite["iou"] >= 0.95, composite
            assert Path(composite["debug"]["reference_silhouette"]).is_file()
            mask_iou = execute("""
recovered = load_rgb('debug/reference-silhouette.png')[:, :, 0] != 0
float(np.count_nonzero(recovered & silhouette) / np.count_nonzero(recovered | silhouette))
""")
            assert float(mask_iou["value"]) >= 0.95, mask_iou
            assert compare(root / "mask.png", "--mask", "none")["iou"] == 1.0
            execute("""
Path('portrait.png').write_bytes(png(rgb[:, 64:-64]))
rgba = np.concatenate((rgb / 255, silhouette[:, :, None].astype(float)), axis=2)
image = bpy.data.images.new('Alpha reference', width=512, height=512, alpha=True)
image.pixels.foreach_set(rgba[::-1].astype(np.float32).ravel())
image.filepath_raw = str(Path('alpha.png').resolve())
image.file_format = 'PNG'
image.save()
bpy.data.images.remove(image)
rgba = np.concatenate((composite / 255, np.ones((512, 512, 1))), axis=2)
for extension, format in (('jpg', 'JPEG'), ('webp', 'WEBP')):
    image = bpy.data.images.new('Codec reference', width=512, height=512, alpha=False)
    image.filepath_raw = str(Path('colored.' + extension).resolve())
    image.file_format = format
    image.pixels.foreach_set(rgba[::-1].astype(np.float32).ravel())
    image.save()
    bpy.data.images.remove(image)
""")
            assert compare(root / "portrait.png")["iou"] >= 0.98
            assert compare(root / "alpha.png", "--mask", "none")["iou"] >= 0.98
            for extension in ("jpg", "webp"):
                assert compare(root / ("colored." + extension))["iou"] >= 0.95
            for size in (768, 1024):
                resized = call("compare", "--ref", ref, "--view", "front", "--size", size, "--frame", "Cube")
                assert set(resized) == {"ok", "view", "iou"} and resized["iou"] >= 0.98, resized
            # Warm comparison cost includes fresh reference loading, evaluation and rendering.
            compare(ref)
            warm = execute("agent.compare('ref.png', 'front', metrics=('iou','chamfer','ssim','hist'))")
            print("warm all-metric exec:", json.dumps(warm), flush=True)
            before = call("session", "snapshot")["snapshot"]
            no_change = execute("agent.compare('ref.png', 'front'); agent.diff()")
            assert ast.literal_eval(no_change["value"]) == {"added": [], "changed": [], "removed": []}, no_change
            assert call("session", "snapshot")["snapshot"] == before
            files_before = {str(path) for path in root.rglob("*.png")}
            start = time.perf_counter()
            fit = execute("""
obj = bpy.data.objects['Cube']
scores = []
for index in range(20):
    value = round(0.2 + index * 0.04, 2)
    obj.scale.x = value
    scores.append((agent.compare('ref.png', 'front', metrics=('iou',))['iou'], value))
score, best = max(scores)
obj.scale.x = best
{'best': best, 'iou': score}
""")
            print("20-iteration fit:", json.dumps(fit), "wall_s:", time.perf_counter() - start, flush=True)
            assert ast.literal_eval(fit["value"])["best"] == 0.6, fit
            assert ast.literal_eval(fit["value"])["iou"] >= 0.98, fit
            assert files_before == {str(path) for path in root.rglob("*.png")}
            assert "observe" not in fit and "image" not in ast.literal_eval(fit["value"])
            assert any(item["name"] == "Cube" for item in fit["diff"]["changed"]), fit

            # Pixel-only cost on real rendered buffers, separate from render latency.
            benchmark = execute("""
import time
from agent_compare import reference, measure, METRICS
from agent_observe import isolated_data
with isolated_data():
    start = time.perf_counter()
    for _ in range(20):
        pixels, mask = reference('ref.png', 512, 'auto')
        measure(pixels, mask, rgb / 255, silhouette, METRICS)
    pixel_ms = (time.perf_counter() - start) * 1000 / 20
{'preprocess_and_metrics_ms': pixel_ms}
""")
            print("NumPy pixel cost:", benchmark["value"], flush=True)

            typo = execute("obj = bpy.data.objects['Cube']; obj.locaton = (0, 0, 0)", ok=False)
            print("attribute error:", json.dumps(typo), flush=True)
            assert "location" in typo["error"]["rna"]["nearest"] and typo["error"]["line"] == 1, typo
            enum = execute("obj.rotation_mode = 'NOT_AN_ENUM'", ok=False)
            print("enum error:", json.dumps(enum), flush=True)
            assert "XYZ" in {item["identifier"] for item in enum["error"]["rna"]["enum_items"]}, enum
            overflow = execute("bpy.context.scene.render.resolution_x = 2**40", ok=False)
            assert overflow["error"]["rna"]["hard_min"] <= 512 <= overflow["error"]["rna"]["hard_max"], overflow
            print("integer range error:", json.dumps(overflow), flush=True)
            wrong_type = execute("obj.location = 'wrong'", ok=False)
            assert wrong_type["error"]["rna"]["array_length"] == 3, wrong_type
            keyword = execute("bpy.ops.mesh.primitive_cube_add(unknown_keyword=1)", ok=False)
            print("operator keyword error:", json.dumps(keyword), flush=True)
            assert "size" in keyword["error"]["rna"]["properties"], keyword
            assert "rna" not in execute("int('not a number')", ok=False)["error"]
            assert "rna" not in execute("int(obj.location)", ok=False)["error"]
            type_typo = execute("bpy.types.Object.locaton", ok=False)
            assert "location" in type_typo["error"]["rna"]["nearest"], type_typo
            module_typo = execute("bpy.ops.mesh.primitive_cub_add()", ok=False)
            assert "primitive_cube_add" in module_typo["error"]["rna"]["nearest"], module_typo

            op = call("describe", "bpy.ops.mesh.bevel")
            print("describe bevel (trimmed first 20 lines):", "\n".join(json.dumps(op, indent=2).splitlines()[:20]), flush=True)
            assert op["kind"] == "operator" and op["poll"] is False and "offset" in op["properties"], op
            execute("bpy.ops.object.mode_set(mode='EDIT')")
            assert call("describe", "bpy.ops.mesh.bevel")["poll"] is True
            execute("bpy.ops.object.mode_set(mode='OBJECT')")
            blocked = call("describe", "bpy.ops.view3d.select")
            assert blocked["poll"] is False and "GPU viewport selection" in blocked["poll_reason"], blocked
            prop = call("describe", "bpy.types.Object.location")
            print("describe location:", json.dumps(prop), flush=True)
            assert prop["kind"] == "property" and prop["array_length"] == 3 and prop["animatable"], prop
            assert call("describe", "Modifier")["struct"] == "Modifier"
            assert call("describe", "bpy.types.BevelModifier")["base"] == "Modifier"
            execute("obj.modifiers.new('Bevel', 'BEVEL')")
            instance = call("describe", 'bpy.data.objects["Cube"].modifiers[0]')
            assert instance["struct"] == "BevelModifier" and "width" in instance["properties"], instance
            module = call("describe", "bpy.ops.mesh")
            assert module["kind"] == "module" and module["operators"]["bevel"], module
            assert ast.literal_eval(execute("agent.describe('bpy.types.Object.location')")["value"])["array_length"] == 3
            call("describe", "__import__('os').getcwd()", ok=False)
            for extra in (("--size", "256"), ("--mask", "bad"), ("--metric", "bad")):
                call("compare", "--ref", ref, "--view", "front", *extra, ok=False)
            compare(root / "missing.png", ok=False)
        finally:
            call("session", "close")
    print("agent compare: all assertions passed", flush=True)


if __name__ == "__main__":
    main()
