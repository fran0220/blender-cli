<!-- SPDX-FileCopyrightText: 2026 blender-cli Authors
     SPDX-License-Identifier: GPL-2.0-or-later -->

# Working recipe

[design.md](design.md) owns the contract: the request set, the event shapes,
the metrics and the recovery guarantees. This page is the recipe, and every
command on it was run against the built binary. Paths, hashes, PIDs and
timings vary between runs; nothing else does.

## One pipe for the whole session

`repl` is the primary mode. Hold one pipe, write one request per line, read
its events back. Nothing pays for process start-up or shell quoting again:

```sh
blender-cli exec -c 'bpy.ops.wm.read_factory_settings(use_empty=True)' --save empty.blend --json
blender-cli repl --file empty.blend
```

Five requests in, and what came back on stdout:

```json
{"id": 1, "op": "exec", "code": "bpy.ops.mesh.primitive_cylinder_add(radius=0.4, depth=2.0)\nbpy.context.object.name = 'Handle'\nbpy.context.object.dimensions[:]"}
{"id": 2, "op": "exec", "code": "bpy.data.objects['Handle'].locaton"}
{"id": 3, "op": "inspect", "select": ["objects[\"Handle\"].dimensions"]}
{"id": 4, "op": "program", "action": "get"}
{"id": 5, "op": "session", "action": "status"}
```

```json
{"id": 1, "event": "value", "value": "(0.800000011920929, 0.800000011920929, 2.0)"}
{"id": 1, "event": "diff", "added": [{"type": "MESH", "name": "Cylinder"}, {"type": "OBJECT", "name": "Handle"}], "changed": [{"type": "SCENE", "name": "Scene", "fields": ["selection", "base_flags"]}], "removed": [], "snapshot": "sha256:8640b6e2…", "step": 1}
{"id": 1, "event": "done", "ok": true, "ms": 3.2875520000743563}
{"id": 2, "event": "error", "ok": false, "type": "AttributeError", "message": "'Object' object has no attribute 'locaton'", "line": 1, "rna": {"struct": "Object", "nearest": ["location", "lock_rotation", "lock_location", "delta_location", "lock_rotation_w"], "type": "float[3]"}}
{"id": 3, "event": "done", "ok": true, "ms": 0.03810699990935973, "selected": {"objects[\"Handle\"].dimensions": [0.800000011920929, 0.800000011920929, 2.0]}}
{"id": 4, "event": "done", "ok": true, "ms": 0.7545570001639135, "text": "# blender-cli program\n# base: file /tmp/usage/empty.blend\nP = {}\n\n# step 1\nbpy.ops.mesh.primitive_cylinder_add(radius=0.4, depth=2.0)\nbpy.context.object.name = 'Handle'\nbpy.context.object.dimensions[:]\n", "params": {}, "steps": [{"n": 1, "code": "bpy.ops.mesh.primitive_cylinder_add(radius=0.4, depth=2.0)\nbpy.context.object.name = 'Handle'\nbpy.context.object.dimensions[:]", "reproducible": true}], "version": "sha256:930f2a7e…", "base": "file /tmp/usage/empty.blend", "record": true, "digest": "sha256:1b7c2eb9…", "reproducible": true}
{"id": 5, "event": "done", "ok": true, "ms": 0.01797599998099031, "session": "76247", "file": "/tmp/usage/empty.blend", "dirty": false, "step": 1, "snapshot": "sha256:8640b6e2…", "feedback": {"perception": true, "objective": true, "image": {"mode": "delta", "threshold": 0.002, "views": ["front"], "pass": "color", "size": 256, "overlay": true, "inline": false}}, "targets": [], "recovered_from": null}
```

Read that transcript for what the loop costs. Request 1 answered in 3 ms with
the value of its last expression, the datablocks it added and changed, and the
snapshot the scene is now at — nothing had to be asked for. Request 2 misspelled
a property and came back with the five nearest identifiers and the type of the
right one, so the correction needs no lookup. Neither of them ran `observe`.

`--file` names the scene the session opens; without it the session starts from
Blender's factory startup, **including its default cube**. `--standalone` runs
the loop in the same process instead of connecting to a daemon; the bytes are
identical either way.

## One-shot verbs are the same requests

Anything the channel can do, a verb can do. Each verb is one request, its flags
are that request's fields, and it prints the same events folded into one
document. Run in a directory with a live session, a verb is a socket round trip
of a few milliseconds; run without one it loads, executes, optionally saves and
exits:

```sh
blender-cli session open --file empty.blend --json
# {"session":"76247","socket":"/tmp/usage/.blender-cli/session.sock"}
blender-cli exec -c "bpy.data.objects['Knob'].scale = (1.6, 1.6, 0.6)" --json
# {"diff":{"added":[],"changed":[{"fields":["transform","copy_on_eval","parameters"],"name":"Knob","type":"OBJECT"}],"removed":[],"snapshot":"sha256:889d913a…","step":4},"ms":3.220897000119294,"ok":true,"value":null}
blender-cli inspect --select 'objects["Knob"].scale' --json
# {"ms":0.060753999605367426,"ok":true,"selected":{"objects[\"Knob\"].scale":[1.0,1.0,1.0]}}
```

`blender-cli --help` prints every verb with every flag it has; that list is
generated from the request table, so it is never out of date. Three
conventions are worth knowing before reading it:

- a value written `@FILE` is read from that file and `-` is read from stdin, so
  `exec -c @edit.py` and `program set --text @model.py` never fight the shell
  (`@@` starts a literal value that really begins with an at sign);
- `--json` prints one compact line; the default is the same document indented;
- `--select` paths start at `bpy.data`: `objects["Cube"].location`, not
  `location` and not `bpy.data.objects["Cube"].location`.

`value` is the **repr string** of the statement's last expression, not its JSON
serialization: `"(0.8, 0.8, 2.0)"` is text, and `ast.literal_eval` turns it back
into a tuple. `diff` names datablocks and depsgraph update categories, not a
property patch.

## Feedback budgets

Every action's consequences come back on their own; what varies is how much.
The policy is per session, not per request:

```sh
blender-cli session feedback image.mode=off image.size=128 --json
# {"feedback":{"image":{"inline":false,"mode":"off","overlay":true,"pass":"color","size":128,"threshold":0.002,"views":["front"]},"objective":true,"perception":true},"ms":0.04950099992129253,"ok":true}
```

A setting is a dotted path into the policy and its value is JSON when it parses
as JSON, so `image.views='["front","persp"]'` works and several settings can be
merged in one request. `session status` reports the policy in force. `exec` and
`program` take `--image delta|full|off` to override the image budget for one
request.

## The scene is a program

Every `exec` that changes data is recorded as the next step of
`.blender-cli/program/model.py`. That file, not the `.blend`, is the record: the
agent reads it, edits it, and the process re-executes it from the longest cached
prefix. A parameter block named `P` is the surface a search drives.

```sh
cat model.py
```

```python
# blender-cli program
# base: file /tmp/usage/empty.blend
P = {"radius": 0.4, "height": 2.0}

# step 1
bpy.ops.mesh.primitive_cylinder_add(radius=P["radius"], depth=P["height"])
bpy.context.object.name = "Handle"

# step 2
bpy.ops.mesh.primitive_uv_sphere_add(radius=P["radius"] * 1.6,
                                     location=(0, 0, P["height"] / 2))
bpy.context.object.name = "Knob"
```

```sh
blender-cli program set --text @model.py --json
# {"cached":0,"ran":[1,2],"from_step":1,"steps":2,"reproducible":true,"digest":"sha256:9e601296…","version":"sha256:1a917ead…","ms":16.61481399969489,"ok":true,"diff":{…,"step":2}}
blender-cli program patch --old '"height": 2.0' --new '"height": 3.0' --json
# {"cached":0,"ran":[1,2],"from_step":1,"steps":2,"reproducible":true,"digest":"sha256:bc1cd566…","version":"sha256:01f704a7…","ms":15.738618000341376,"ok":true,"diff":{…,"step":3}}
blender-cli program run --json
# {"cached":2,"ran":[],"from_step":3,"steps":2,"reproducible":true,"digest":"sha256:bc1cd566…","version":"sha256:01f704a7…","ms":1.572357999975793,"ok":true,"diff":{"added":[],"changed":[],"removed":[],…}}
```

`ran` is the steps that actually executed and `cached` the ones the prefix cache
supplied. The patch changed `P["height"]`, which both steps read, so both re-ran
in 16 ms; the following `run` found nothing to do and answered in 1.6 ms with an
empty diff. `digest` is content, not timing: a prefix-cached re-execution that
lands where a full run would lands on the same digest.

`patch` requires its `--old` to match exactly once, which makes an edit that
silently hits the wrong place impossible. Every change writes a version:

```sh
blender-cli program history --json
# {"current":"sha256:01f704a7…","ok":true,"versions":[
#  {"version":"sha256:930f2a7e…","parent":null,"message":"exec","label":null,"steps":1,"reproducible":true,"failed":false,"at":1788646419.7044904},
#  {"version":"sha256:1a917ead…","parent":"sha256:930f2a7e…","message":"set","label":null,"steps":2,"reproducible":true,"failed":false,"at":1788646429.7347088},
#  {"version":"sha256:01f704a7…","parent":"sha256:1a917ead…","message":"patch","label":null,"steps":2,"reproducible":true,"failed":false,"at":1788646429.7570257}]}
blender-cli program rollback 'sha256:1a917ead…' --json
```

`program record off` stops recording without stopping execution, and
`exec --no-record` skips one statement — use them for the throwaway probes that
should not become part of the model.

## Checkpoints and rollback

Rollback is the only control this process has; there is no confirmation step
anywhere. A labelled snapshot is written to disk and survives a crash:

```sh
blender-cli session snapshot --label two-parts --json
# {"label":"two-parts","snapshot":"sha256:9d093851…","version":"sha256:01f704a7…","ms":24.11225799960448,"ok":true}
blender-cli exec -c "bpy.data.objects['Knob'].scale = (1.6, 1.6, 0.6)" --json
blender-cli session rollback two-parts --json
# {"diff":{"added":[],"changed":[],"removed":[],"snapshot":"sha256:9d093851…","step":4},"ms":4.35534700000062,"ok":true,"snapshot":"sha256:9d093851…"}
blender-cli inspect --select 'objects["Knob"].scale' --json
# {"ms":0.060753999605367426,"ok":true,"selected":{"objects[\"Knob\"].scale":[1.0,1.0,1.0]}}
```

`session history` lists every snapshot with the op that produced it;
`session rollback '~1'` goes back one, `'~0'` restores the current one, and a
hash or a label goes straight there. Rollback replaces Blender data, so
reacquire `knob = bpy.data.objects["Knob"]` afterwards: Python references into
the old data may be invalid. Snapshot hashes identify memfiles inside one
process, not geometry, and are not comparable across processes. Reusing a label
selects the newest checkpoint while older ones stay reachable by their IDs, and
`.blender-cli/snapshots/` is yours to clean.

## When the process dies

Native code can terminate the session. The killed request says so, names the
dead PID and the autosave, and so does every request after it:

```sh
blender-cli exec -c 'import os; os._exit(1)' --json
# {"autosave":"/tmp/usage/.blender-cli/autosave-76247.blend","error":{"message":"Session 76247 exited unexpectedly; see .blender-cli/session.log. Recover with `session open --file <autosave>` or discard with `session close`","type":"SessionError"},"ok":false}
blender-cli session open --file .blender-cli/autosave-76247.blend --json
# {"previous_autosave":"/tmp/usage/.blender-cli/autosave-76247.blend","recovered_from":"autosave","session":"76902","socket":"/tmp/usage/.blender-cli/session.sock"}
blender-cli session status --json
# {"file":"/tmp/usage/empty.blend","dirty":false,"recovered_from":"autosave","session":"76902","snapshot":"sha256:b9d973da…","step":0,…}
blender-cli inspect --select 'objects["Knob"].location' --json
# {"ms":0.05549400020754547,"ok":true,"selected":{"objects[\"Knob\"].location":[0.0,0.0,1.5]}}
```

Recovery is explicit: `session open` on its own starts a **new empty session**
and only reports `previous_autosave`; naming the autosave is what restores the
scene, and `recovered_from` says which path was taken. The restored session
keeps the original live filepath (`empty.blend` above), not the recovery file,
so it will not overwrite its own lifeboat — keep the autosave's adjacent
`.json` sidecar, which is where that filepath and the dirty flag live.

Recovery restores the last completed autosave, not the failed call and not
Python variables. The program survives independently in
`.blender-cli/program/`, so the cheapest recovery is often to reopen and
`program run`. `session.log` names the request that was running and the crash
file `.blender-cli/session-<pid>.crash.txt`; a native rendering crash also
carries the Python stack captured before the renderer took the GIL. `os._exit`
and SIGKILL bypass the handlers, so they leave recovery files but no dump.
Closing a dead session discards it and keeps its crash file.

## Looking at the scene

`observe` renders offscreen with fixed cameras, a fixed light rig and fixed
colour management; the same scene state always produces the same PNG. It is
deterministic, not fast — a 512 px pair on a software Vulkan device took 7.7 s:

```sh
blender-cli observe --views front,persp --json
# {"image":"/tmp/usage/.blender-cli/observe/060bef51….png","views":["front","persp"],"passes":["color"],"size":[516,1032],"ms":7737.266684999668,"ok":true,
#  "framing":{"bounds":{"low":[-0.64,-0.64,-1.5],"high":[0.64,0.64,2.14]},"center":[0.0,0.0,0.32],"objects":["Handle","Knob"],"occupancy":0.9090909090909091,"radius":2.032633603644515}}
```

`framing` is world-space and free: bounds, centre, radius and the objects that
contributed them answer "is it the right size and in the right place" without
looking at the image. Views are `front back left right top bottom persp
camera`; passes are `color wire silhouette normal depth`; `--layout separate`
writes one file per view and pass, and `--inline` returns base64 instead of
files. Curves and modifiers are framed from their evaluated geometry, so there
is no need to `object.convert` first. Automatic framing hides uniform scale:
vary a proportion, pass `--frame OBJECT`, or use a scene camera.

`inspect` is the cheap read — objects with transforms, bounds, modifiers and
mesh counts, materials, armatures, cameras, lights and collections, never
truncated. `--full` expands node trees and modifier settings, and `--select`
reads exact RNA paths.

## Asking the process instead of guessing

`describe` answers from live RNA, so it is current for this build rather than
for some documentation:

```sh
blender-cli describe bpy.types.Object.rotation_mode --json
# {"animatable":true,"default":"XYZ","description":"The kind of rotation to apply, values from other rotation modes are not used",
#  "enum_items":[{"identifier":"QUATERNION","name":"Quaternion (WXYZ)","description":"No Gimbal Lock"},{"identifier":"XYZ","name":"XYZ Euler","description":"XYZ Rotation Order - prone to Gimbal Lock (default)"},…]}
```

`describe bpy.ops.mesh.bevel` gives an operator's keywords and whether its
`poll()` passes right now; `describe agent` and `describe agent.compare` give
the helper module; `describe channel` gives the request and event registry, and
`describe schema` the same as JSON Schema. Errors already carry the nearest
valid identifiers (`rna.nearest` in the transcript above), so `describe` is for
what you want to know before writing, not for repairing a typo.

Inside `exec` the same answers are one call away: the `agent` module is
preloaded beside `bpy`, `bmesh`, `mathutils` and `math`, and `describe agent`
lists it. Every helper returns the dict its request or event carries, so
`agent.program()["version"]` and `agent.history()[-1]["op"]` read what
`program get` and `session history` return. Keeping a loop inside one `exec`
costs one round trip instead of one per iteration.

## Blender gotchas worth knowing before you hit them

- Factory startup includes a default cube. Start from
  `bpy.ops.wm.read_factory_settings(use_empty=True)` or a saved empty file,
  or the next `primitive_cube_add` produces `Cube.001` beside it.
- `mode_set(mode='EDIT')` enters multi-object edit for every selected
  compatible object. Deselect, select the target, make it active, then switch.
- Numeric assignment often clamps silently: `SubsurfModifier.levels = 99`
  becomes 11. Read the value back, and consult `describe` for the range.
  Extreme evaluated geometry can abort the whole process, and a native
  allocator abort is not a Python exception you can catch.
- Set `rotation_mode` **before** assigning `rotation_euler`. Assigning Euler
  values in `QUATERNION` mode and then switching to `XYZ` converts from the
  quaternion and can zero what you just wrote.
- After `obj.scale = …`, `obj.dimensions` can be stale until
  `bpy.context.view_layer.update()` or the next evaluation. Update first, then
  read; never fit against a stale dimension.
- A reference image only compares usefully against a view that frames the same
  way. Observation centres geometry at occupancy `1/1.1`, which removes
  reference margins but not a different viewpoint or perspective.

### Rigify ships disabled

Factory startup enables glTF and FBX but not Rigify, and resetting the scene
resets add-on state, so enable it *after* the reset and in the same `exec`:

```python
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.preferences.addon_enable(module="rigify")
bpy.ops.object.armature_human_metarig_add()
bpy.ops.pose.rigify_generate()
len(bpy.data.objects["rig"].data.bones)
```

That answered `"706"` in 5.1 s on the Linux orb build. Avoid
`addon_utils.enable("rigify", default_set=False)`: Rigify's registration reads
the preferences entry that call does not create.
