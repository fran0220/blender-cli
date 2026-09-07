# SPDX-FileCopyrightText: 2026 blender-cli Authors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Exercise the request set before/after packaging, including exact render equality."""

import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from gpu import require_device


def smoke(executable, root, image, reference=None, gpu=True):
    def call(*args):
        process = subprocess.run([str(executable), *map(str, args), "--json"], cwd=root,
                                 capture_output=True, text=True, encoding="utf-8", timeout=180)
        assert process.returncode == 0, (args, process.stdout, process.stderr)
        result = json.loads(process.stdout)
        assert result.get("ok", True), result
        print("SMOKE", *args[:2], "OK", flush=True)
        if process.stderr:
            print(process.stderr, file=sys.stderr)
        return result

    call("session", "open")
    try:
        operators = call("exec", "-c", """
bpy.ops.wm.read_factory_settings(use_empty=True)
assert 'io_scene_gltf2' in bpy.context.preferences.addons
assert 'io_scene_fbx' in bpy.context.preferences.addons
operators = [bpy.ops.wm.obj_import, bpy.ops.wm.obj_export,
             bpy.ops.wm.fbx_import, bpy.ops.import_scene.fbx, bpy.ops.export_scene.fbx,
             bpy.ops.wm.stl_import, bpy.ops.wm.stl_export,
             bpy.ops.wm.ply_import, bpy.ops.wm.ply_export,
             bpy.ops.import_scene.gltf, bpy.ops.export_scene.gltf,
             bpy.ops.wm.open_mainfile, bpy.ops.wm.save_as_mainfile]
assert all(operator.poll() for operator in operators)
len(operators)
""")
        assert operators["value"] == "13", operators
        call("exec", "-c", "import bpy, agent, agent_runtime, agent_observe, agent_compare, agent_rna; "
             "bpy.ops.wm.read_factory_settings(); "
             "bpy.data.objects['Cube'].scale.x = 0.6; "
             "bpy.context.scene.render.engine = 'CYCLES'; "
             "bpy.context.scene.render.engine = 'BLENDER_EEVEE'")
        result = call("inspect", "--object", "Cube")
        assert result["objects"][0]["mesh"]["vertices"] == 8, result
        call("describe", "bpy.types.Object")
        if gpu:
            call("observe", "--views", "front", "--out", image)
            # There is no comparison verb: the metrics are the objective's
            # computation, reached from code as `agent.compare`.
            scored = call("exec", "-c",
                          f"agent.compare({str(reference or image)!r}, 'front')")
            assert ast.literal_eval(scored["value"])["iou"] > 0.98, scored
    finally:
        call("session", "close")


if __name__ == "__main__":
    original, trimmed = (Path(arg).resolve() for arg in sys.argv[1:])
    gpu = True
    try:
        require_device(original)
    except SystemExit as error:
        if error.code != 77:
            raise
        gpu = False
    with tempfile.TemporaryDirectory(prefix="agent package ") as directory:
        root = Path(directory).resolve()
        first, second = root / "original.png", root / "trimmed.png"
        smoke(original, root, first, gpu=gpu)
        smoke(trimmed, root, second, first, gpu=gpu)
        # Exercise the actual trimmed importers/exporters, not merely their polls.
        subprocess.run([sys.executable, str(Path(__file__).with_name("io.py")), str(trimmed)],
                       check=True, timeout=1800)
        if gpu:
            assert first.read_bytes() == second.read_bytes(), "Packaging changed observation bytes"
            print("BYTE_IDENTICAL", hashlib.sha256(first.read_bytes()).hexdigest())
        else:
            print("SKIP: package render equality and comparison unverified: no native GPU device")
