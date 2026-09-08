<!-- SPDX-FileCopyrightText: 2026 blender-cli Authors
     SPDX-License-Identifier: GPL-2.0-or-later -->

# Production recipes

[design.md](design.md) owns the contract; [build-profile.md](build-profile.md)
owns retained capabilities. These are current-interface examples, not pasted
test transcripts or timing claims. Check [PLAN.md](../../PLAN.md) for actual
build, workflow and platform evidence. Replace `/absolute/...` paths with paths
on the machine running Blender; create output directories before use.

## One process, one channel

Hold `blender-cli repl` open and send newline-delimited JSON requests. The
greeting is a `session` event with `id: null`, including the scene, snapshot,
device, feedback policy, targets and recovery source. Wait for each request's
`done` or `error` before deciding the next action. Structural, perceptual and
objective feedback is pushed between those boundaries, subject to the session
budget and device availability.

```json
{"id":1,"op":"scene","action":"reset"}
{"id":2,"op":"object","action":"create","name":"Body","primitive":"cube"}
{"id":3,"op":"object","action":"transform","name":"Body","scale":[1,0.5,2]}
{"id":4,"op":"data","action":"get","path":"objects[\"Body\"].location"}
```

The shell projection sends the same requests to a daemon in the current
directory. Without a session, one-shot requests do not share live state:

```sh
blender-cli session open --json
blender-cli capabilities --json
blender-cli scene reset --json
blender-cli object create Body --primitive cube --json
blender-cli object transform Body --scale 1,0.5,2 --json
blender-cli data get 'objects["Body"].location' --json
```

`scene reset` is empty by default; `scene reset --no-empty` restores Blender's
factory cube, camera and light. An ordinary `session open` without a file or
recovery starts with factory defaults, so reset explicitly when constructing
an empty scene. `session open --file /absolute/scene.blend` opens the named
scene rather than replaying a different program over it.

CLI conventions:

- Actions are positional: `object create`, `data set`, `render frame`.
  Object/rig/pose/simulation/operator names are the next positional argument;
  data paths occupy that position for `data`. Other fields use flags.
- `--location`, `--rotation`, `--scale`, `--head` and `--tail` accept comma
  triples or JSON arrays. Euler rotations use radians; bone endpoints are
  armature-local coordinates. `--objects` accepts comma-separated names.
- `--properties`, `--arguments`, `--value`, `--steps` and other structured
  fields take JSON. Shell-quote JSON and RNA paths. RNA paths start at Main,
  e.g. `objects["Body"].scale`, not `bpy.data.objects["Body"].scale`.
- `@FILE` reads a structured/text value from a file; `-` reads stdin; `@@`
  escapes an initial at sign. `program set --text @model.json` and
  `exec -c @extension.py` avoid shell quoting of a whole program.
- `--json` prints a compact folded response. Native results are structured
  JSON; only explicit Python `exec` returns its last expression as a repr
  string. Do not parse a native result with `ast.literal_eval`.
- `blender-cli --help`, `describe channel` and `describe schema` derive from
  the registry. `describe bpy.types.Object.rotation_euler` reads live RNA.

## Creation, materials and generic native operations

Dedicated commands establish their own object context. Generic native
operators expose upstream properties without generating Python:

```sh
blender-cli operator describe object.modifier_add --json
blender-cli operator call object.modifier_add --objects Body --active Body --properties '{"type":"BEVEL"}' --json
blender-cli data set 'objects["Body"].modifiers["Bevel"].width' --value 0.08 --json
blender-cli data call materials.new --arguments '{"name":"Finish"}' --json
blender-cli data set 'materials["Finish"].diffuse_color' --value '[0.2,0.4,0.6,1]' --json
blender-cli data set 'materials["Finish"].node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value' --value '[0.2,0.4,0.6,1]' --json
blender-cli data call 'objects["Body"].data.materials.append' --arguments '{"material":{"path":"materials[\"Finish\"]"}}' --json
blender-cli inspect --object Body --full --json
```

Creation returns actual names; use them if Blender disambiguates a requested
name. `data call` preserves native RNA output names. For `materials.new`, the
result includes `value.material`, a reference with `name`, `type` and `path`.
A function with a differently named RNA output uses that name, not a guessed
generic `result`. Pointer arguments and assignments use `{"path":"RNA path"}`.

The same APIs create node trees and links, edit modifier settings and manage
compositing or sequencer data. Discover each function's actual parameter and
output names through RNA instead of guessing them. Material viewport color
does not replace a production shader graph: set shader node inputs when
building a shaded deliverable.

Use `batch` for related native steps with one feedback boundary. Name a result
with `as` and address it with `$ref`:

```json
{"id":5,"op":"batch","steps":[
  {"op":"object","action":"create","name":"Detail","primitive":"uv_sphere","as":"detail"},
  {"op":"object","action":"transform","name":{"$ref":"detail.name"},"location":[0,0,2.5],"scale":[0.5,0.5,0.5]}
]}
```

Send that JSON as one line on the channel, or save the `steps` array to a file
and use `blender-cli batch --steps @steps.json`. Nested batches and control
requests are rejected. Failure rolls scene data back, not already written files.

## Rigging, posing and animation

For the `Body` mesh created above, create an armature and one bone, bind it,
and keyframe a pose through native commands:

```sh
blender-cli rig create Skeleton --json
blender-cli rig bone Skeleton --bone Root --head 0,0,-2 --tail 0,0,2 --json
blender-cli rig bind Skeleton --objects Body --weights automatic --json
blender-cli pose set Skeleton --bone Root --rotation 0,0,0 --json
blender-cli animation key --path 'objects["Skeleton"].pose.bones["Root"].rotation_euler' --frame 1 --json
blender-cli pose set Skeleton --bone Root --rotation 0,0,0.4 --json
blender-cli animation key --path 'objects["Skeleton"].pose.bones["Root"].rotation_euler' --frame 24 --json
blender-cli scene frame --frame 12 --json
blender-cli inspect --object Skeleton --full --json
```

`rig bone --parent NAME` creates a hierarchy. Weighting choices are
`automatic`, `envelope` and `empty`; empty groups require explicit weights
before useful deformation. `animation delete` removes keys from a path;
`animation bake --objects NAME --start 1 --end 24 --step 1` samples evaluated
object transforms. It is not a promise to bake every constraint, simulation
cache or pose channel into a complete interchange rig.

Rigify is a Python add-on, so its metarig and generation operations belong in
an explicit extension, not `operator call`. Reset before enabling it:

```python
# Save as rigify_extension.py, then run: blender-cli exec rigify_extension.py --json
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.preferences.addon_enable(module="rigify")
bpy.ops.object.armature_human_metarig_add()
bpy.ops.pose.rigify_generate()
len(bpy.data.objects["rig"].data.bones)
```

This extension replaces the scene; run it as a separate recipe, not as a
continuation that is expected to preserve `Body`. Add-on preferences reset
with factory settings. Native `operator call` rejects Python-defined operators
rather than secretly invoking an extension implementation.

## Simulation and external caches

This separate example resets the scene, adds a rigid body and evaluates a frame:

```sh
blender-cli scene reset --json
blender-cli object create Falling --location 0,0,3 --json
blender-cli simulation add Falling --type RIGID_BODY --start 1 --end 24 --json
blender-cli scene frame --frame 12 --json
blender-cli simulation bake Falling --type RIGID_BODY --start 1 --end 24 --json
blender-cli simulation free Falling --type RIGID_BODY --json
```

The simulation surface also covers `CLOTH`, `SOFT_BODY`, `FLUID` and `OCEAN`.
Adding a modifier/domain is configuration, not a finished physical setup:
configure collisions, fluid flows/effectors, domain resolution or ocean
parameters with `data set` and native operators. Inspect the resulting RNA.
For disk caches, supply an absolute directory through `--path` where supported
by the simulation's cache implementation, and inspect the returned cache status.
For example, a fluid bake must have a configured domain and its intended flow
objects before `simulation bake NAME --type FLUID --path /absolute/cache/fluid`.

Scene rollback cannot restore overwritten cache files, exported assets or
rendered frames. A `free` operation can deliberately invalidate external caches;
do not describe it as reversible merely because scene data has snapshots.
Changing scene parameters can stale a cache. Re-evaluate/bake the intended frame
range and verify outputs independently of program replay. Long jobs report
progress as `phase` and `fraction` (0–1), at most once per 0.5 seconds plus
completion, unless the session's progress policy is off. Animation loops and
point-cache/ocean baking can observe cancellation during work. Fluid and
production-render execution currently check at operation boundaries, not
necessarily during an upstream bake/render. Partial external files can remain
after cancellation or failure; inspect `external_effects` when reported in an
error as well as checking the output directory.

## Production rendering and interchange

`render` uses the scene camera, lights, world, render engine and compositor.
It does **not** substitute observation's fixed light rig or automatic framing.
Here factory defaults provide a camera and light for an independent render recipe:

```sh
blender-cli scene reset --no-empty --json
blender-cli data set 'objects["Camera"].data.lens' --value 50 --json
blender-cli object create Fill --type LIGHT --location -3,-4,5 --json
blender-cli data set 'objects["Fill"].data.energy' --value 600 --json
blender-cli operator call scene.new_compositor_effect_node_group --json
blender-cli data get 'scenes["Scene"].compositor_effects' --json
blender-cli render frame --frame 1 --engine CYCLES --format PNG --width 640 --height 640 --samples 32 --path /absolute/output/frame.png --no-record --json
blender-cli render animation --start 1 --end 24 --engine CYCLES --format PNG --path /absolute/output/frame_ --no-record --json
blender-cli scene save --path /absolute/output/scene.blend --no-record --json
blender-cli operator call wm.usd_export --properties '{"filepath":"/absolute/output/scene.usdc"}' --no-record --json
blender-cli operator call wm.alembic_export --properties '{"filepath":"/absolute/output/cache.abc","start":1,"end":24,"as_background_job":false}' --no-record --json
```

The compositor command creates and attaches upstream's default compositor
effect node group. Use its returned/discovered node-group name with `data call`
for `nodes.new` and `links.new`, passing socket pointers by path, to add the
intended grading or effects. Merely creating an unconnected node changes no
output. The active camera can likewise be assigned through a native pointer,
e.g. `data set 'scenes["Scene"].camera' --value '{"path":"objects[\"Camera\"]"}'`.

FFmpeg video formats and sound mixing remain production capabilities. First set
`scenes["Scene"].render.image_settings.media_type` to `"VIDEO"` to initialize
video defaults, then configure `scenes["Scene"].render.ffmpeg` through native data and discover
`sound.mixdown` for audio-file delivery. USD/Alembic, native OBJ/PLY/STL/FBX
import, Grease Pencil SVG/PDF and the normal image codecs are retained, not
reported as intentionally missing. `capabilities` reports the actual build;
profile intent alone is not proof that a dependency or GPU is available.

Python add-on IO remains explicit, including glTF and FBX export:

```sh
blender-cli exec -c "bpy.ops.export_scene.gltf(filepath='/absolute/output/scene.glb', export_format='GLB')" --no-record --json
blender-cli exec -c "bpy.ops.export_scene.fbx(filepath='/absolute/output/scene.fbx')" --no-record --json
blender-cli operator call wm.usd_import --properties '{"filepath":"/absolute/input/scene.usdc"}' --json
```

Agree on axis/unit conversions, selection flags and material limitations at
both ends. Alembic carries geometry/cache data rather than Blender shader
graphs; glTF may split vertices at attribute seams and triangulate polygons.
Keep texture/MTL/bin sidecars. Record absolute immutable input paths for replay;
`//` is relative to a blend file, not the JSON program. Export to new paths and
use `--no-record` for delivery-only work that should not repeat during rebuild.

## The scene is a JSON program

Save this as `model.json`:

```json
{
  "base": "factory-empty",
  "params": {"height": 2.0, "width": 0.4},
  "steps": [
    {"op":"object","action":"create","name":"Handle","primitive":"cylinder","as":"handle"},
    {"op":"object","action":"transform","name":{"$ref":"handle.name"},"scale":[{"$param":"width"},{"$param":"width"},{"$param":"height"}]}
  ]
}
```

`base` is `factory-empty`, `factory-default`, or an absolute blend-file path.
`params` contains literal JSON. `$param` substitutes a named parameter and
`$ref` reads a prior named result, not Python syntax or an arithmetic expression.
Use explicit values/parameters for relationships the request does not compute.
An advanced or add-on extension is a step with `{"op":"exec","code":"..."}`;
its namespace exposes the parameter dictionary as `P`. Native steps never
generate Python and replay through the same native command entry.

```sh
blender-cli program set --text @model.json --json
blender-cli program patch --old '"height": 2.0' --new '"height": 3.0' --json
blender-cli program run --json
blender-cli program get --json
blender-cli program history --json
```

The persisted record is `.blender-cli/program/model.json`; `program get.text`
is serialized JSON. `cached`, `ran` and `from_step` report prefix reuse;
`digest` identifies scene content rather than allocation-dependent snapshot
bytes. `patch` requires exactly one textual match. Use `program rollback
VERSION_OR_LABEL` to check out a version, or `program record off|on` to change
automatic recording. Successful mutating native commands and explicit `exec`
steps participate in the program; failed requests do not become successful steps.

The JSON artifact rebuilds without its original memfile cache, provided its
base and external inputs remain available. It does not embed external assets.
Pure native prefixes retain serialized named results. Replay starts a fresh
Python namespace and does not skip explicit extension steps or external-effect
operations just because their Main snapshot exists; their suffix re-runs.
After ordinary session rollback, reacquire Python RNA references: cached Main
is not a snapshot of arbitrary Python variables. A failed program edit keeps the edited text and
failure metadata available for correction while restoring pre-request scene data.

## Observation, targets and fitting

For the handle program, register a reference and let numeric search vary a
proportion. These operations need a usable observation device:

```sh
blender-cli observe --views front --passes silhouette --size 256 --out /absolute/output/reference.png --json
blender-cli target set front --ref /absolute/output/reference.png --view front --mask none --metrics iou,chamfer --json
blender-cli program patch --old '"height": 3.0' --new '"height": 1.5' --json
blender-cli fit --params '[{"name":"height","min":1,"max":4}]' --objective '{"target":"front","metric":"iou"}' --budget '{"evals":40,"size":256}' --json
```

`fit` applies its best parameters to `params` while preserving unrelated
parameters and program steps. Program-parameter evaluations re-execute affected
prefixes; RNA-path parameters assign properties directly. A helper call to
`agent.fit(...)` inside explicit `exec --no-record` uses the same machinery.
Progress follows the session policy; final results report the curve, best
parameters, evaluations and stop reason. Cancellation keeps the best result
for `fit`, unlike ordinary transactional edit cancellation.

Automatic framing hides uniform scale: fit proportions, fix `--frame OBJECT`,
or use a suitable camera view. A 256-pixel silhouette reference matches the
objective's fixed resolution. Different raster sizes or reference masking can
change a score even for unchanged geometry. `--mask auto` is deterministic
classic image processing, not semantic segmentation; inspect its derived mask.

```sh
blender-cli session feedback image.size=128 image.mode=delta --json
blender-cli session feedback perception=false image.mode=off --json
blender-cli session feedback perception=true image.mode=delta --json
blender-cli inspect --select 'objects["Handle"].scale' --json
```

Images are full frames, changed-region crops, before/after overlays or target
error maps. `image.mode=off` suppresses returned pixels, not a render still
needed by perception/objectives. Color-image samples do not change silhouette
metric samples. A `device: null` greeting means observation is unavailable:
`observe`/perceptual `fit` report `NoDevice`, not fabricated pictures or scores.
That does not equate to disabling native scene editing or Cycles CPU production
rendering. See the design for exact budgets, view presets and metric semantics.

## Checkpoints and recovery

```sh
blender-cli session snapshot --label checkpoint --json
blender-cli session history --json
blender-cli session rollback checkpoint --json
blender-cli session close --json
```

`session rollback '~1'` selects a relative snapshot; program rollback selects
a program version. Neither undoes external writes or restores Python variables.
Labelled checkpoints persist on disk. `session close` is an explicit end of
the live session, not a command to leave a daemon running in the background.

If the native process dies, the `repl` bridge reports `Crashed` for outstanding
requests, attempts to reopen the session, and greets the same pipe again if
recovery succeeds. `session open` also recovers an abandoned session. Recovery
chooses the newer usable program/autosave according to the design and reports
`recovered_from`; unavailable inputs or corrupt recovery files can still fail.
There is no guarantee that a killed in-flight operation completed. Autosaves
represent completed writes, and a program can only rebuild available inputs.
Keep autosave sidecars; they preserve original filepath/dirty state. Inspect
`.blender-cli/session.log` and native crash diagnostics when recovery fails.
