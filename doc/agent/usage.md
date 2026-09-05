<!-- SPDX-FileCopyrightText: 2026 blender-cli Authors
     SPDX-License-Identifier: GPL-2.0-or-later -->

# Agent quick start

Use this as a working recipe; [design.md](design.md) owns the CLI contract,
response fields, metrics and recovery guarantees. Commands assume `blender-cli`
is on PATH. Outputs below are abbreviated: paths, hashes and timings vary.

## Perceive → decide → act → observe

Work in a dedicated directory and keep a session while iterating:

```sh
blender-cli session open --json
# {"session":"…","socket":"…/.blender-cli/session.sock"}
blender-cli inspect --json
# {"ok":true,"objects":[{"name":"Camera",…},{"name":"Cube",…},{"name":"Light",…}],…}
blender-cli describe bpy.types.Object.location --json
# {"ok":true,"kind":"property","type":"float","array_length":3,…}
blender-cli exec -c 'bpy.ops.wm.read_factory_settings(use_empty=True); bpy.ops.mesh.primitive_cube_add(); bpy.context.object.scale.x = 0.6; bpy.context.object.name' --json
# {"ok":true,"value":"'Cube'","diff":{"added":[…],"changed":[…],"removed":[…]},"snapshot":"sha256:…",…}
blender-cli observe --views front,persp --json
# {"ok":true,"image":"…/observe/….png","views":["front","persp"],"size":[516,1032],…}
```

Read the image, choose a change, execute it, and observe again. `value` is the
last expression's **repr string**, not its JSON serialization. `diff` identifies
added/removed datablocks and dependency update categories, not a property patch.
For longer edits put ordinary Python in `shape.py`, then use:

```sh
blender-cli exec shape.py --observe front,persp --json
blender-cli inspect --select 'objects["Cube"].location' 'objects["Cube"].scale' --json
# {"ok":true,"selected":{"objects[\"Cube\"].location":[0,0,0],"objects[\"Cube\"].scale":[0.6,1,1]}}
```

Selection starts at `bpy.data`: use `objects["Cube"].location`, not `location`
or `bpy.data.objects["Cube"].location`.

Without an open session, each call starts fresh. Use files to carry state:

```sh
blender-cli session save --file shape.blend --json
blender-cli session close --json
blender-cli exec -c 'bpy.data.objects["Cube"].location.z = 1' --file shape.blend --save --json
blender-cli inspect --file shape.blend --json
```

## Images and numeric fitting

Use a sheet for one image containing several views/passes; separate files are
convenient for feeding an individual view back as a reference. Inline output
is useful for a host that consumes base64 rather than files:

```sh
blender-cli session open --file shape.blend --json
blender-cli observe --views front,persp --passes color,wire --layout sheet --json
blender-cli observe --views front,persp --passes silhouette --layout separate --out refs --json
blender-cli observe --views front --inline --json
# {"ok":true,"base64":"iVBOR…","size":[516,516],…}
```

For exact **silhouette** self-comparison, take the front silhouette file path
from `images`, then use it with `--mask none`; do not compare a multi-tile sheet:

```sh
blender-cli compare --ref refs/front-silhouette.png --view front --metric iou,chamfer --mask none --json
# {"ok":true,"view":"front","iou":1.0,"chamfer":0.0}
```

Use the actual returned path (the filename above is illustrative). Color
references are useful for appearance metrics, but thresholded antialiased color
does not exactly equal native silhouette coverage. `--mask auto` can miss dark
shading near the background color; inspect `--debug-out masks` before trusting a
bad score. Thin features and foreground touching the border also need care.

Keep a parameter search inside one `exec` instead of launching for every trial:

```python
# fit.py — session already has Cube; reference is a single front silhouette tile.
cube = bpy.data.objects["Cube"]
scores = []
for x in (0.4, 0.6, 0.8):
    cube.scale.x = x
    scores.append((agent.compare("reference.png", "front", mask="none")["iou"], x))
best_iou, best_x = max(scores)
cube.scale.x = best_x
{"x": best_x, "iou": best_iou}
```

Run `blender-cli exec fit.py --json`. Automatic framing hides uniform scale
changes; vary proportions, use `--frame` with fixed bounds, or a scene camera.

## Discover, checkpoint, recover

```sh
blender-cli describe bpy.ops.mesh.bevel --json
blender-cli exec -c 'bpy.data.objects["Cube"].locaton' --json
# {"ok":false,"error":{"type":"AttributeError",…,"rna":{"struct":"Object","nearest":["location",…],"type":"float[3]"}},…}
blender-cli session snapshot --label before-bevel --json
# {"snapshot":"sha256:…","label":"before-bevel"}
blender-cli session history --json
# [{"snapshot":"sha256:…","label":null,"verb":"open","at":…},…]
blender-cli session rollback 'sha256:…' --json
blender-cli session rollback '~1' --json
```

Copy an actual hash from history. Rollback replaces Blender data, so reacquire
`cube = bpy.data.objects["Cube"]` afterwards; old held RNA references are invalid.
Snapshot hashes are process-local, not geometry identifiers you can use after a
restart. Save important milestones explicitly.

If native code terminates a session, the killed request reports `SessionError`.
The next command names the dead PID and, if available, an autosave:

```sh
blender-cli exec -c '42' --json
# {"ok":false,"error":{"type":"SessionError","message":"Session … exited unexpectedly; …"},"autosave":"…/.blender-cli/autosave-….blend"}
blender-cli session open --file /absolute/path/from/autosave-field.blend --json
# {"session":"…","socket":"…","previous_autosave":"…"}
blender-cli inspect --json
blender-cli session save --file recovered.blend --json
blender-cli session close --json
```

Recovery restores the last completed autosave, not the failed call or Python
variables. If no autosave is reported, reopen an explicit save, or discard the
dead session with `session close`. Closing a dead session keeps its crash file;
a clean close removes the new live session's autosave, not older crash files.

## Blender gotchas observed while iterating

- Factory startup includes a default cube. Start a new scene with
  `bpy.ops.wm.read_factory_settings(use_empty=True)` rather than accidentally
  creating `Cube.001` beside it.
- `mode_set(mode='EDIT')` enters multi-object edit for all selected compatible
  objects. Deselect everything, select the target and make it active first.
- Numeric assignment often silently clamps: `SubsurfModifier.levels = 99`
  becomes 11. Read back the value and consult `describe` for limits. Extreme
  evaluated geometry can abort the entire process; Python exception handling
  cannot catch a native allocator abort.
