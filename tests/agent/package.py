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


def runtime_inventory(executable, root):
    """Hash actual installed resources, including libraries loaded on demand."""
    code = """
from pathlib import Path
import hashlib
resources = Path(bpy.utils.resource_path('LOCAL'))
inventory = {}
roots = {'version': resources}
for name in ('lib', 'blender.shared', 'license'):
    path = resources.parent / name
    if path.exists():
        roots[name] = path
for label, directory in roots.items():
    for path in sorted(directory.rglob('*')):
        if '__pycache__' in path.parts or path.suffix == '.pyc':
            continue
        key = label + '/' + path.relative_to(directory).as_posix()
        if path.is_symlink():
            inventory[key] = ('symlink', str(path.readlink()))
        elif path.is_file():
            with path.open('rb') as handle:
                inventory[key] = hashlib.file_digest(handle, 'sha256').hexdigest()
inventory
"""
    process = subprocess.run([str(executable), 'exec', '-c', code, '--no-record', '--json'],
                             cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=240)
    assert process.returncode == 0, (process.stdout, process.stderr)
    result = json.loads(process.stdout)
    assert result.get('ok', True), result
    inventory = ast.literal_eval(result['value'])
    assert any('/datafiles/colormanagement/' in key for key in inventory), inventory.keys()
    assert any('/scripts/addons_core/' in key for key in inventory), inventory.keys()
    return inventory


def smoke(executable, root, image, reference=None):
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
        gpu = call("session", "status")["device"] is not None
        capabilities = call("exec", "-c", """
import importlib
import addon_utils
required = ('bullet', 'codec_ffmpeg', 'codec_sndfile', 'cycles', 'cycles_osl',
            'freestyle', 'image_cineon', 'image_openjpeg', 'audaspace',
            'international', 'libmv', 'mod_oceansim', 'mod_remesh',
            'io_wavefront_obj', 'io_ply', 'io_stl', 'io_fbx', 'io_gpencil',
            'opencolorio', 'openvdb', 'alembic', 'usd', 'fluid', 'haru', 'potrace',
            'opensubdiv', 'image_webp')
missing = [name for name in required if not getattr(bpy.app.build_options, name)]
assert not missing, missing
import _cycles
assert _cycles.with_osl and _cycles.with_embree
assert _cycles.with_path_guiding and _cycles.with_openimagedenoise
# Repeat reset so enabled preferences cannot hide misplaced or missing add-ons.
for reset in range(2):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    for name in ('io_scene_gltf2', 'io_scene_fbx', 'cycles'):
        assert name in bpy.context.preferences.addons, name
    for name in ('rigify', 'pose_library', 'node_wrangler', 'hydra_storm'):
        assert addon_utils.enable(name, default_set=True) is not None, name
    for name in ('aud', 'pxr.Usd', 'pxr.UsdGeom', 'MaterialX', 'OpenImageIO',
                 'PyOpenColorIO', 'openvdb', 'oslquery', '_bpy_hydra',
                 'bl_pkg', 'io_anim_bvh', 'io_curve_svg', 'io_mesh_uv_layout'):
        importlib.import_module(name)
    operators = [bpy.ops.wm.obj_import, bpy.ops.wm.obj_export,
             bpy.ops.wm.fbx_import, bpy.ops.import_scene.fbx, bpy.ops.export_scene.fbx,
             bpy.ops.wm.stl_import, bpy.ops.wm.stl_export,
             bpy.ops.wm.ply_import, bpy.ops.wm.ply_export,
             bpy.ops.import_scene.gltf, bpy.ops.export_scene.gltf,
             bpy.ops.wm.open_mainfile, bpy.ops.wm.save_as_mainfile,
             bpy.ops.wm.usd_import, bpy.ops.wm.usd_export,
             bpy.ops.wm.alembic_import, bpy.ops.wm.alembic_export,
             bpy.ops.wm.grease_pencil_import_svg, bpy.ops.wm.grease_pencil_export_svg,
             bpy.ops.wm.grease_pencil_export_pdf, bpy.ops.fluid.bake_all,
             bpy.ops.rigidbody.object_add, bpy.ops.clip.track_markers,
             bpy.ops.sound.mixdown]
    # Poll can be false in an empty scene; RNA registration must still exist.
    assert all(operator.get_rna_type() for operator in operators)
    for engine in ('CYCLES', 'BLENDER_EEVEE', 'HYDRA_STORM'):
        bpy.context.scene.render.engine = engine
    for transform in ('Standard', 'AgX', 'Filmic', 'Raw'):
        bpy.context.scene.view_settings.view_transform = transform
    for format in ('CINEON', 'JPEG2000', 'OPEN_EXR', 'FFMPEG'):
        bpy.context.scene.render.image_settings.file_format = format
len(required)
""")
        assert int(capabilities["value"]) == 27, capabilities
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
        return gpu
    finally:
        call("session", "close")


if __name__ == "__main__":
    original, trimmed = (Path(arg).resolve() for arg in sys.argv[1:])
    with tempfile.TemporaryDirectory(prefix="agent package ") as directory:
        root = Path(directory).resolve()
        first, second = root / "original.png", root / "trimmed.png"
        original_inventory = runtime_inventory(original, root)
        packaged_inventory = runtime_inventory(trimmed, root)
        assert original_inventory == packaged_inventory, {
            'missing': sorted(original_inventory.keys() - packaged_inventory.keys()),
            'changed': sorted(key for key in original_inventory.keys() & packaged_inventory.keys()
                              if original_inventory[key] != packaged_inventory[key]),
            'added': sorted(packaged_inventory.keys() - original_inventory.keys()),
        }
        print('RUNTIME_BYTE_IDENTICAL', len(original_inventory), 'files', flush=True)
        gpu = smoke(original, root, first)
        assert smoke(trimmed, root, second, first) == gpu, "Packaging changed device availability"
        # Full IO round trips run once in the CMake-derived trimmed suite.
        # This smoke retains the distinct before/after registration checks above.
        if gpu:
            assert first.read_bytes() == second.read_bytes(), "Packaging changed observation bytes"
            print("BYTE_IDENTICAL", hashlib.sha256(first.read_bytes()).hexdigest())
        else:
            print("SKIP: package render equality and comparison unverified: no native GPU device")
