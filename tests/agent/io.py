# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Real model IO, recorded imports, fresh-process replay and imported-object fitting."""

import ast
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


# A non-unit box with continuous UVs. Keeping UVs continuous lets the test
# distinguish format triangulation from vertex splitting at attribute seams.
MODEL = """
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
obj = bpy.context.object
obj.name = 'Model'
for vertex in obj.data.vertices:
    vertex.co.x *= 1.25
    vertex.co.y *= 0.75
    vertex.co.z *= 1.5
for loop in obj.data.loops:
    co = obj.data.vertices[loop.vertex_index].co
    obj.data.uv_layers.active.data[loop.index].uv = ((co.x + 1.25) / 2.5, (co.z + 1.5) / 3)
material = bpy.data.materials.new('Finish')
material.diffuse_color = (0.2, 0.4, 0.6, 1.0)
material.node_tree.nodes.get('Principled BSDF').inputs['Base Color'].default_value = (0.2, 0.4, 0.6, 1.0)
obj.data.materials.append(material)
"""

FACTS = """
bpy.context.view_layer.update()
objects = [obj for obj in bpy.context.scene.objects if obj.type == 'MESH']
assert len(objects) == 1, [obj.name for obj in objects]
obj = objects[0]
points = [obj.matrix_world @ vertex.co for vertex in obj.data.vertices]
facts = {
    'name': obj.name,
    'vertices': len(obj.data.vertices),
    'faces': len(obj.data.polygons),
    'bounds': [[min(p[i] for p in points) for i in range(3)],
               [max(p[i] for p in points) for i in range(3)]],
    'materials': [mat.name for mat in obj.data.materials],
    'uv': sorted(set(tuple(round(float(v), 5) for v in item.uv)
                     for item in obj.data.uv_layers.active.data)) if obj.data.uv_layers else [],
}
facts
"""

# Defaults carry matching axis conversions on both ends. glTF stores triangles
# and per-vertex attributes; omit normals to avoid flat-normal vertex splitting.
FORMATS = {
    'obj': ('wm.obj_export', 'wm.obj_import', '', '', 8, 6, True, True),
    'fbx': ('export_scene.fbx', 'import_scene.fbx', '', '', 8, 6, True, True),
    'stl': ('wm.stl_export', 'wm.stl_import', '', '', 8, 12, False, False),
    'ply': ('wm.ply_export', 'wm.ply_import', ', export_normals=False', '', 8, 6, False, True),
    'gltf': ('export_scene.gltf', 'import_scene.gltf',
             ", export_format='GLTF_SEPARATE', export_normals=False", '', 8, 12, True, True),
    'glb': ('export_scene.gltf', 'import_scene.gltf',
            ", export_format='GLB', export_normals=False", '', 8, 12, True, True),
    'blend': ('wm.save_as_mainfile', 'wm.open_mainfile', ', check_existing=False', '', 8, 6, True, True),
}


def main():
    executable = str(Path(sys.argv[1]).resolve())
    with tempfile.TemporaryDirectory(prefix='agent io ') as directory:
        root = Path(directory).resolve()

        def call(*args, cwd, ok=True):
            process = subprocess.run([executable, *map(str, args), '--json'], cwd=cwd,
                                     capture_output=True, text=True, encoding='utf-8', timeout=240)
            assert process.returncode == (0 if ok else 1), (args, process.stdout, process.stderr)
            result = json.loads(process.stdout)
            assert result.get('ok', True) == ok, result
            return result

        def stream(code, cwd, ok=True):
            process = subprocess.run([executable, 'repl'], cwd=cwd, capture_output=True,
                                     text=True, encoding='utf-8', timeout=240,
                                     input=json.dumps({'id': 1, 'op': 'exec', 'code': code}) + '\n')
            assert process.returncode == 0, (process.stdout, process.stderr)
            events = [json.loads(line) for line in process.stdout.splitlines()]
            assert events[0]['event'] == 'session', events
            assert all(event['id'] == 1 for event in events[1:]), events
            assert events[-1]['event'] == ('done' if ok else 'error'), events
            return events

        def facts(cwd):
            return ast.literal_eval(call('exec', '-c', FACTS, '--no-record', cwd=cwd)['value'])

        def check(actual, expected, vertices, faces, materials, uv, name):
            assert (actual['vertices'], actual['faces']) == (vertices, faces), actual
            # ASCII OBJ and float32 exchange formats round coordinates. 1e-5
            # Blender units is the absolute bound tolerance, not a percentage.
            assert all(abs(a - b) < 1e-5 for row, other in zip(actual['bounds'], expected['bounds'])
                       for a, b in zip(row, other)), (actual, expected)
            if materials:
                assert actual['materials'] == expected['materials'], actual
            else:
                assert actual['materials'] == [], actual
            assert actual['uv'] == (expected['uv'] if uv else []), actual
            assert actual['name'] == name, actual

        for extension, (exporter, importer, export_options, import_options,
                        vertices, faces, materials, uv) in FORMATS.items():
            started = time.monotonic()
            source, live, replay, reader = [root / extension / name
                                            for name in ('source', 'live', 'other-cwd', 'reader')]
            for path in (source, live, replay, reader):
                path.mkdir(parents=True)
            asset = source / ('asset.' + extension)
            export = f'bpy.ops.{exporter}(filepath={str(asset)!r}{export_options})'
            imported = f'bpy.ops.{importer}(filepath={str(asset)!r}{import_options})'
            original = call('exec', '-c', MODEL + '\n' + export + '\n' + FACTS, cwd=source)
            expected = ast.literal_eval(original['value'])
            assert asset.is_file() and asset.stat().st_size > 0, asset

            call('session', 'open', cwd=live)
            try:
                call('session', 'feedback', 'perception=false', 'image.mode=off', cwd=live)
                call('exec', '-c', 'bpy.ops.wm.read_factory_settings(use_empty=True)', cwd=live)
                assert call('exec', '-c', 'len(bpy.data.objects)', cwd=live)['value'] == '0'
                events = stream(imported, live)
                assert any(event['event'] == 'diff' and event['added'] for event in events), events
                name = 'asset' if extension in ('stl', 'ply') else 'Model'
                check(facts(live), expected, vertices, faces, materials, uv, name)
                program = call('program', 'get', cwd=live)
                assert program['steps'][-1]['code'].strip() == imported, program
                # External files are conservatively marked irreproducible even when
                # present: absolute paths remove cwd dependence, not file dependence.
                assert program['steps'][-1]['reproducible'] is False, program
                digest = program['digest']
                shutil.copytree(live / '.blender-cli/program', replay / '.blender-cli/program')
                call('session', 'open', cwd=replay)
                try:
                    rebuilt = call('program', 'run', cwd=replay)
                    assert rebuilt['digest'] == digest, (extension, digest, rebuilt)
                    check(facts(replay), expected, vertices, faces, materials, uv, name)
                finally:
                    call('session', 'close', cwd=replay)

                device = events[0]['device']
                call('session', 'feedback', 'perception=true', 'image.size=128', cwd=live)
                reference = live / 'reference.png'
                if device:
                    call('observe', '--views', 'front', '--passes', 'silhouette', '--size', '256',
                         '--out', reference, cwd=live)
                    call('target', 'set', 'front', '--ref', reference, '--view', 'front',
                         '--mask', 'none', '--metrics', 'iou', cwd=live)
                edit = f'bpy.data.objects[{name!r}].scale.x = 0.5'
                changed = stream(edit, live)
                assert any(event['event'] == 'diff' and
                           any(row['name'] == name for row in event['changed']) for event in changed), changed
                if device:
                    assert any(event['event'] == 'perception' for event in changed), changed
                else:
                    assert not any(event['event'] in ('perception', 'objective', 'image')
                                   for event in changed), changed
                params = json.dumps([{'path': f'objects[{name!r}].scale[0]', 'min': 0.5, 'max': 1.5}])
                fitted = call('fit', '--params', params, '--objective', '{"target":"front","metric":"iou"}',
                              '--budget', '{"evals":9,"size":256}', cwd=live, ok=bool(device))
                if device:
                    assert fitted['applied'] and fitted['failed'] == 0, fitted
                    assert fitted['curve'][-1][1] > fitted['curve'][0][1], fitted
                    assert fitted['objective']['targets']['front']['iou'] > 0.98, fitted
                else:
                    assert fitted['error']['type'] == 'NoDevice', fitted

                # Take a genuinely modified model out, not merely the original
                # recovered by fitting, and inspect it in another one-shot process.
                call('exec', '-c', f'bpy.data.objects[{name!r}].scale.x = 1.25', cwd=live)
                modified = facts(live)
                output = live / ('asset.' + extension)
                call('exec', '-c', f'bpy.ops.{exporter}(filepath={str(output)!r}{export_options})', cwd=live)
                roundtrip = call('exec', '-c', 'bpy.ops.wm.read_factory_settings(use_empty=True)\n'
                                 f'bpy.ops.{importer}(filepath={str(output)!r}{import_options})\n' + FACTS, cwd=reader)
                check(ast.literal_eval(roundtrip['value']), modified, vertices, faces, materials, uv, name)
                print(f'IO {extension}: {vertices} vertices, {faces} faces; digest={digest}; '
                      f'device={device}; {time.monotonic() - started:.2f}s', flush=True)
            finally:
                call('session', 'close', cwd=live)

        errors = root / 'errors'
        errors.mkdir()
        call('session', 'open', cwd=errors)
        try:
            for operator, label in (('usd_import', 'USD'), ('alembic_import', 'Alembic')):
                events = stream(f'bpy.ops.wm.{operator}(filepath={str(errors / "absent")!r})', errors, ok=False)
                error = events[-1]
                assert label.lower() in error['message'].lower() and 'not built in' in error['message'], error
                assert call('exec', '-c', '42', cwd=errors)['value'] == '42'
        finally:
            call('session', 'close', cwd=errors)
    print('agent io: all assertions passed')


if __name__ == '__main__':
    main()
