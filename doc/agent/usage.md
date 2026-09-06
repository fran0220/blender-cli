<!-- SPDX-FileCopyrightText: 2026 blender-cli Authors
     SPDX-License-Identifier: GPL-2.0-or-later -->

# Working recipe

[design.md](design.md) owns the contract: the request set, the event shapes,
the metrics and the recovery guarantees. This page is the recipe, and every
command on it was run against the built binary with its output pasted. Paths,
hashes, PIDs and timings vary between runs; `…` marks where a long answer is
cut, never where one is invented.

## One pipe for the whole session

`repl` is the primary mode. Hold one pipe, write one request per line, read
its events back. Nothing pays for process start-up or shell quoting again:

```sh
blender-cli exec -c 'bpy.ops.wm.read_factory_settings(use_empty=True)' --save empty.blend --json
blender-cli repl --file empty.blend
```

Three requests in, and what came back on stdout:

```json
{"id": 1, "op": "exec", "code": "bpy.ops.mesh.primitive_cylinder_add(radius=0.4, depth=2.0)\nbpy.context.object.name = 'Handle'\nbpy.context.object.dimensions[:]"}
{"id": 2, "op": "exec", "code": "bpy.data.objects['Handle'].scale.x = 1.8"}
{"id": 3, "op": "exec", "code": "bpy.data.objects['Handle'].locaton"}
```

```json
{"id": null, "event": "session", "session": "162386", "file": "/tmp/u6/empty.blend", "dirty": false, "step": 0, "snapshot": "sha256:2bcb52bc…", "feedback": {"perception": true, "objective": true, "progress": "improvements", "image": {"mode": "delta", "threshold": 0.002, "views": ["front"], "pass": "color", "size": 256, "samples": 8, "overlay": true, "inline": false}}, "targets": [], "recovered_from": null}
{"id": 1, "event": "value", "value": "(0.800000011920929, 0.800000011920929, 2.0)"}
{"id": 1, "event": "diff", "added": [{"type": "MESH", "name": "Cylinder"}, {"type": "OBJECT", "name": "Handle"}], "changed": [{"type": "SCENE", "name": "Scene", "fields": ["selection", "base_flags"]}], "removed": [], "snapshot": "sha256:4e694aa7…", "step": 1}
{"id": 1, "event": "perception", "objects": 1, "verts": 64, "faces": 34, "bounds": {"low": [-0.4, -0.4, -1.0], "high": [0.4, 0.4, 1.0]}, "dims": [0.8, 0.8, 2.0], "framing": {"bounds": {…}, "center": [0.0, 0.0, 0.0], "radius": 1.1489125391646506, "objects": ["Handle"], "occupancy": 0.9090909090909091}, "changed": null, "symmetry": {"x": 0.9787234042553191, "y": null, "z": 1.0}}
{"id": 1, "event": "image", "kind": "full", "view": "front", "pass": "color", "size": [256, 256], "region": [0, 0, 256, 256], "path": "/tmp/u6/.blender-cli/feedback/94e15039….png"}
{"id": 1, "event": "done", "ok": true, "ms": 1500.149801999214}
{"id": 2, "event": "value", "value": null}
{"id": 2, "event": "diff", "added": [], "changed": [{"type": "OBJECT", "name": "Handle", "fields": ["transform", "copy_on_eval", "parameters"]}], "removed": [], "snapshot": "sha256:37766c21…", "step": 2}
{"id": 2, "event": "perception", "objects": 1, "verts": 64, "faces": 34, "bounds": {"low": [-0.72, -0.4, -1.0], "high": [0.72, 0.4, 1.0]}, "dims": [1.4399999380111694, 0.8, 2.0], "framing": {…, "radius": 1.2955307658126247, "objects": ["Handle"], "occupancy": 0.9090909090909091}, "changed": {"objects": ["Handle"], "view": "front", "region": [43, 11, 213, 245], "fraction": 0.497772216796875, "silhouette_delta": 0.4464285714285714}, "symmetry": {"x": 1.0, "y": null, "z": 1.0}}
{"id": 2, "event": "image", "kind": "delta", "view": "front", "pass": "color", "size": [186, 250], "region": [35, 3, 221, 253], "path": "…/feedback/7c642250….png"}
{"id": 2, "event": "image", "kind": "overlay", "view": "front", "pass": "color", "size": [186, 250], "region": [35, 3, 221, 253], "path": "…/feedback/52413ccc….png"}
{"id": 2, "event": "done", "ok": true, "ms": 630.5600579999009}
{"id": 3, "event": "error", "ok": false, "type": "AttributeError", "message": "'Object' object has no attribute 'locaton'", "line": 1, "rna": {"struct": "Object", "nearest": ["location", "lock_rotation", "lock_location", "delta_location", "lock_rotation_w"], "type": "float[3]"}, "fix": {"code": "bpy.data.objects['Handle'].location", "reason": "Object has no 'locaton'; nearest 'location' (similarity 0.93)"}}
```

Read that transcript for what the loop costs. Nothing in it was asked for.

The channel greets you before it reads anything: a `session` event with
`id: null` carrying the whole of `session status` — which scene is open, the
step and snapshot it is at, the feedback policy in force, the registered
targets, and whether this session was recovered. There is never a reason to
open a conversation by asking what state it is in.

Request 1 answered with the value of its last expression, the datablocks it
added, the snapshot the scene is now at, its counts and world bounds, and the
first picture of the view. Request 2 scaled the handle and answered with the
region of the view that changed (`[43, 11, 213, 245]`, half the frame), how much
of the silhouette moved with it, and two crops of exactly that region — the
result and a before/after overlay — instead of a whole frame. Request 3
misspelled a property and came back with the five nearest identifiers, the type
of the right one, and a `fix.code` that runs as it stands. No `observe` and no
`compare` request appears anywhere: looking is what an action already answers.

`diff` names the datablocks the agent can act on. Blender's windows, screens,
workspaces, brushes and palettes change constantly and mean nothing to a model,
so they are not listed.

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
# {"session":"140957","socket":"/tmp/u5/.blender-cli/session.sock"}
blender-cli exec -c "bpy.data.objects['Knob'].scale = (1.6, 1.6, 0.6)" --json
# {"diff":{"added":[],"changed":[{"fields":["transform","copy_on_eval","parameters"],"name":"Knob","type":"OBJECT"}],"removed":[],"snapshot":"sha256:1d044ef4…","step":3},"ms":3.0733940002392046,"ok":true,"value":null}
blender-cli inspect --select 'objects["Knob"].scale' --json
# {"ms":0.04988100045011379,"ok":true,"selected":{"objects[\"Knob\"].scale":[1.0,1.0,1.0]}}
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

Every action's consequences come back on their own; what varies is how much they
cost. The policy is per session:

```sh
blender-cli session feedback image.size=128 image.mode=delta --json
# {"feedback":{"perception":true,"objective":true,"progress":"improvements","image":{"mode":"delta","threshold":0.002,"views":["front"],"pass":"color","size":128,"samples":8,"overlay":true,"inline":false}},"ms":0.039956999899004586,"ok":true}
```

A setting is a dotted path into the policy and its value is JSON when it parses
as JSON, so `image.views='["front","persp"]'` works and several settings merge
in one request. `session status` reports the policy in force. The knobs that
matter: `perception` and `image.mode` decide whether there is a budget render at
all, `image.threshold` is the changed-pixel fraction below which no picture is
worth sending, `image.size` and `image.samples` are what that render costs
(8 samples by default, against `observe`'s 32), and `progress` decides how much
a running `fit` says while it works.

`exec` and `program` take `--image` to override the picture for one request —
a whole frame when something needs looking at, nothing when the answer is
already known:

```sh
blender-cli exec -c "bpy.data.objects['Handle'].scale.z = 0.5" --image full --json
# {"ok":true,"images":[{"kind":"full","view":"front","pass":"color","region":[0,0,128,128],"size":[128,128],"path":"…/.blender-cli/feedback/b4eadf2e….png"}],"ms":713.6910519993762}
blender-cli exec -c "bpy.data.objects['Handle'].scale.y = 1.4" --image off --json
# {"ok":true,"ms":711.387843999546}
```

Those two cost the same, because one budget render feeds both channels and
`--image off` only stops the pixels coming back. Switching off both is what
removes the render:

```sh
blender-cli session feedback perception=false image.mode=off --json
blender-cli exec -c "bpy.data.objects['Handle'].scale.y = 1.1" --json
# {"ok":true,"ms":3.508742000121856}
blender-cli session feedback perception=true image.mode=delta --json
blender-cli exec -c "bpy.data.objects['Handle'].scale.z = 1.7" --json
# {"ok":true,"ms":688.5190940001849, … "images":[{"kind":"delta",…},{"kind":"overlay",…}]}
```

Use that for a run of edits whose outcome is already known — building a rig,
importing, renaming — and switch back on for the change that needs looking at.
An action that changes nothing is not charged for it either way: there is
nothing to re-render, so it answers in under a millisecond and its perception
still arrives, with the deltas at zero.

```sh
blender-cli exec -c "len(bpy.data.objects)" --json
# {"ok":true,"value":"1","ms":0.6380220002029091,
#  "perception":{…,"changed":{"view":"front","objects":[],"region":null,"fraction":0.0,"silhouette_delta":0.0}}}
```

Pictures come back in four kinds. `delta` is the changed region cropped out of
the budget view and `overlay` is the same region before and after (before red,
after cyan, agreement white); `full` is the whole frame. Once a target is
registered a fourth arrives with every scoring — `error`, that target's
silhouette error over its worst cell, missing red and extra blue — so the
picture says which way the model is wrong rather than only that it moved:

```sh
blender-cli exec -c 'bpy.data.objects["Knob"].scale = (1.0, 1.0, 1.0)' --json
# {"ok":true,…,"images":[{"kind":"full","view":"front","region":[0,0,128,128],"size":[128,128],…},
#                        {"kind":"error","view":"front","region":[56,56,136,136],"size":[80,80],…}]}
```

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
# base: file /tmp/u5/empty.blend
P = {"radius": 0.4, "height": 2.0}

# step 1
bpy.ops.mesh.primitive_cylinder_add(radius=P["radius"], depth=P["height"])
bpy.context.object.name = "Handle"

# step 2
bpy.ops.mesh.primitive_uv_sphere_add(radius=P["radius"] * 1.6,
                                     location=(0, 0, P["height"] / 2))
bpy.context.object.name = "Knob"
```

Editing a program re-executes it, so this session ran with
`session feedback perception=false image.mode=off` — the point here is what ran,
not what it looks like:

```sh
blender-cli program set --text @model.py --json
# {"cached":0,"ran":[1,2],"from_step":1,"steps":2,"reproducible":true,"digest":"sha256:510e884d…","version":"sha256:7b78b83b…","ms":13.148849000572227,"ok":true,"diff":{"added":[{"name":"Cylinder","type":"MESH"},{"name":"Sphere","type":"MESH"},{"name":"Handle","type":"OBJECT"},{"name":"Knob","type":"OBJECT"}],…,"step":1}}
blender-cli program patch --old '"height": 2.0' --new '"height": 3.0' --json
# {"cached":0,"ran":[1,2],"from_step":1,"steps":2,"reproducible":true,"digest":"sha256:8e2638d1…","version":"sha256:a533bbbd…","ms":16.626302000076976,"ok":true,"diff":{…,"step":2}}
blender-cli program run --json
# {"cached":2,"ran":[],"from_step":3,"steps":2,"reproducible":true,"digest":"sha256:8e2638d1…","version":"sha256:a533bbbd…","ms":1.5858120004850207,"ok":true,"diff":{"added":[],"changed":[],"removed":[],…}}
```

`ran` is the steps that actually executed and `cached` the ones the prefix cache
supplied. The patch changed `P["height"]`, which both steps read, so both re-ran
— in 17 ms; the following `run` found nothing to do and answered in 1.6 ms with
an empty diff and the same `digest`. That digest is content, not timing: a
prefix-cached re-execution that lands where a full run would lands on the same
value, which is what makes editing the text safe.

`patch` requires its `--old` to match exactly once, which makes an edit that
silently hits the wrong place impossible. Every change writes a version:

```sh
blender-cli program history --json
# {"current":"sha256:a533bbbd…","ok":true,"versions":[
#  {"version":"sha256:7b78b83b…","parent":null,"message":"set","label":null,"steps":2,"reproducible":true,"failed":false,"at":1788651129.85441},
#  {"version":"sha256:a533bbbd…","parent":"sha256:7b78b83b…","message":"patch","label":null,"steps":2,"reproducible":true,"failed":false,"at":1788651129.8751857}]}
blender-cli program rollback 'sha256:7b78b83b…' --json
```

A version is named by its hash, by a `--label` given when it was made, or by a
hash prefix; `program rollback` takes it as its argument, the way
`session rollback` takes a snapshot.

`program record off` stops recording without stopping execution, and
`exec --no-record` skips one statement — use them for the throwaway probes that
should not become part of the model.

## From `repl` to a fitted model

Everything above composes into the loop this process exists for: register what
the model should look like, let every action say how far it is, and hand the
numeric part to the process. Start with a reference image bound to a view:

```sh
blender-cli target set front --ref reference.png --view front --metrics iou,chamfer --json
# {"ok":true,"name":"front","view":"front","mask":"auto","fit":"bbox","metrics":["iou","chamfer"],
#  "ref":"/tmp/f5/.blender-cli/targets/front/reference.png","silhouette":"…/targets/front/silhouette.png",
#  "reference":{"bbox":[87,11,169,244],"occupancy":0.91015625,"fit":"bbox"},
#  "objective":{"targets":{"front":{"iou":0.6985361145369725,"chamfer":11.768228939327805,"delta":null,
#    "worst":{"region":[64,64,128,128],"iou":0.5493951612903226,"missing":0.0,"extra":1.0}}},
#   "best":{"front":{"iou":0.6985361145369725,"snapshot":"sha256:…","step":1}}}}
```

Registering already scores: `iou` 0.70, and the worst 4×4 cell is the
bottom-right quadrant with `extra: 1.0` — every pixel wrong there is model that
the reference does not have. Every action from here carries an `objective`, so
the agent never asks how it is doing.

The score is absolute, not just comparable with itself: the model is normalised
the way the reference is, so a model that matches scores `iou` 1.0 and
`chamfer` 0.0. (Rendering the default cube, registering that render, and
scoring it against the cube it came from gives exactly that.) A thin silhouette
costs a little to resampling — the cylinder above, matched exactly, scores
0.974 rather than 1.0 — so near-1.0 is a match, and the remainder can be the
reference's resolution rather than the model.

The numeric part is `fit`. Name the program parameters to search, the objective
to optimise and a budget, and the search runs inside the process. Over the
channel it reports as it goes:

```json
{"id": 1, "op": "fit", "params": [{"name": "height", "min": 1.0, "max": 4.0}], "objective": {"target": "front", "metric": "iou"}, "budget": {"evals": 40}}
{"id": 1, "event": "progress", "eval": 1, "of": 40, "best": 0.7054329371816639, "params": {"height": 2.0}}
{"id": 1, "event": "progress", "eval": 2, "of": 40, "best": 0.9287510477787091, "params": {"height": 2.75}}
{"id": 1, "event": "progress", "eval": 6, "of": 40, "best": 0.9493263034563562, "params": {"height": 2.9375}}
{"id": 1, "event": "progress", "eval": 7, "of": 40, "best": 0.9510324483775812, "params": {"height": 3.03125}}
{"id": 1, "event": "progress", "eval": 11, "of": 40, "best": 0.9516651930445034, "params": {"height": 3.0078125}}
{"id": 1, "event": "done", "ok": true, "method": "coordinate", "evals": 23, "failed": 0, "stopped": "patience", "applied": true, …
 "best": {"params": {"height": 3.0078125}, "score": 0.9516651930445034, "snapshot": "sha256:217ef370…"},
 "curve": [[1, 0.7054329371816639], [2, 0.9287510477787091], [6, 0.9493263034563562], [7, 0.9510324483775812], [11, 0.9516651930445034]],
 "error_map": {"target": "front", "view": "front", "image": "…/.blender-cli/fit/642f4e8c….png", "size": [128, 128], "region": [32, 32, 64, 64]},
 "objective": {"targets": ["front"], "metric": "iou", "weights": [1.0]}, "ms": 39945.02247499986}
```

Twenty-three evaluations of a budget of forty, five `progress` events, `iou`
0.705 → 0.952, and the answer is a number the agent never had to guess: the
model was built with `height` 2.0 and the reference was rendered from 3.0. The
best parameters are applied to the live scene and written into the program's
`P` block, so `program get` now reports `{"height": 3.0078125, "radius": 0.4}`
and the program still reproduces the scene.

Three fields say what the search did with the money. `stopped` is why it ended
— `budget`, `seconds`, `cancel` or `patience`; this one converged, so it
stopped at 23 rather than spending all 40. `curve` records only the evaluations
that improved the best value, so it is the trajectory and not the transcript,
and a flat tail means the budget was enough. `error_map` is a picture of what
is still wrong, at the 4×4 cell contributing most of it.

`patience` is the convergence rule: the search gives up after that many
evaluations without a real improvement. It has no fixed default, because the
right value depends on the search — a cyclic coordinate descent probes `2n`
points per cycle before it can halve its step — so `fit` derives it as
`max(16, 5 × parameters)` unless the request sets one. Raise it to spend the
whole budget on a stubborn fit; lower it to stop paying for renders sooner.

`progress` follows the session's `progress` policy, `improvements` by default:
an event only when the best value moves, because an evaluation that changed
nothing is a token you cannot act on. `all` makes it a heartbeat at most every
0.5 s and `off` silences it — and loses nothing, since every improvement is in
`done.curve` anyway. A one-shot `blender-cli fit …` folds all of this into one
document.

Afterwards the objective keeps scoring, so a change that undoes the progress
says so immediately:

```sh
blender-cli exec -c 'bpy.data.objects["Knob"].scale = (1.35, 1.35, 1.35)' --json
# {"ok":true,"ms":609.7895390012127,…,"objective":{"targets":{"front":{"iou":0.8174197773411919,"chamfer":6.722321428571428,
#   "delta":{"iou":-0.14427467962140395,"chamfer":5.038742664988529},
#   "worst":{"region":[64,64,128,128],"iou":0.6474645030425964,"missing":0.04487917146144994,"extra":0.9551208285385501}}},
#  "best":{"front":{"iou":0.9616944569625958,"snapshot":"sha256:217ef370…","step":2}}}}
```

That cost 0.14 of `iou`, and `best` still names the snapshot that scored 0.96,
so returning to it is `session rollback 'sha256:217ef370…'` — the process did
the bookkeeping. (The fit scored 0.952 at its 128 px budget size and the pushed
objective scores 0.962 at the 256 px feedback size; compare a score with others
taken at the same size.)

`--params` also takes RNA paths (`{"path": "objects[\"Knob\"].scale[0]", "min":
0.5, "max": 2}`) for values that are not program parameters, `--objective` takes
several targets with weights or a `code` expression, and `--method` is
`coordinate`, `nelder-mead` or `random`. Cancelling a long search keeps what it
paid for: it stops, applies its best parameters, and ends with `done` and
`stopped: "cancel"` rather than an error.

## Checkpoints and rollback

Rollback is the only control this process has; there is no confirmation step
anywhere. A labelled snapshot is written to disk and survives a crash:

```sh
blender-cli session snapshot --label two-parts --json
# {"label":"two-parts","snapshot":"sha256:50ced3d3…","version":"sha256:a533bbbd…","ms":8.388486000512785,"ok":true}
blender-cli exec -c "bpy.data.objects['Knob'].scale = (1.6, 1.6, 0.6)" --json
blender-cli session rollback two-parts --json
# {"diff":{"added":[],"changed":[],"removed":[],"snapshot":"sha256:50ced3d3…","step":3},"ms":4.163031999269151,"ok":true,"snapshot":"sha256:50ced3d3…"}
blender-cli inspect --select 'objects["Knob"].scale' --json
# {"ms":0.04988100045011379,"ok":true,"selected":{"objects[\"Knob\"].scale":[1.0,1.0,1.0]}}
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

Native code can terminate the session, and the agent does not have to do
anything about it. The pipe outlives the process behind it: every request still
outstanding is answered with an `error` of type `Crashed`, the session is
reopened, and the channel greets you again with the state it came back at.

```json
{"id": 4, "event": "objective", "targets": {"front": {"iou": 0.9800367922599986, "delta": {"iou": 0.17515740025647397}, …}}, "best": {"front": {"iou": 0.9800367922599986, "snapshot": "sha256:a1a7c07b…", "step": 3}}}
{"id": 5, "op": "exec", "code": "import os; os._exit(1)"}
{"event": "error", "id": 4, "type": "Crashed", "ok": false, "message": "Session 161358 exited during this request; see .blender-cli/session.log. The session was reopened and this pipe still serves it", "recovered_from": "program", "snapshot": "sha256:6cb355a9…", "step": 0}
{"event": "error", "id": 5, "type": "Crashed", "ok": false, …}
{"event": "error", "id": 6, "type": "Crashed", "ok": false, …}
{"event": "session", "id": null, "session": "161459", "step": 0, "snapshot": "sha256:6cb355a9…", "targets": ["front"], "recovered_from": "program", …}
```

The next request is answered by the recovered session, and it is the scene that
was there before the kill:

```json
{"id": 7, "op": "exec", "code": "agent.objective()['targets']['front']['iou']", "record": false}
{"id": 7, "event": "value", "value": "0.9800367922599986"}
{"id": 8, "op": "program", "action": "run"}
{"id": 8, "event": "done", "ok": true, "steps": 3, "cached": 3, "ran": []}
```

`0.9800367922599986` before the kill and `0.9800367922599986` after it, digit
for digit, with the registered target still registered and nothing left for
`program run` to replay. `repl` exits non-zero only when the recovery itself
fails.

Recovery always recovers, and the newest source wins. The program and the
autosave are both on disk; whichever was written last is the one that is used,
and `recovered_from` names it. One-shot verbs get the same treatment from a
plain `session open` — there is never a second open naming a file:

```sh
blender-cli exec -c 'import os; os._exit(1)' --json
# {"ok":false,"error":{"type":"SessionError","message":"Session 183161 exited unexpectedly; see .blender-cli/session.log. `session open` recovers it from the newest of its program and autosave; `session close` discards it"},"autosave":"/tmp/f5/.blender-cli/autosave-183161.blend"}
blender-cli session open --json
# {"session":"183606","socket":"/tmp/f5/.blender-cli/session.sock","recovered_from":"autosave"}
blender-cli exec -c 'agent.objective()["targets"]["front"]["iou"]' --no-record --json
# {"ok":true,"value":"0.9736367733213159"}
```

That session was idle long enough for its autosave to be written after its last
program version, so the autosave won; the `repl` transcript above ended on a
burst of edits, so the program did. Each came back at the objective its own
session had. The distinction is worth knowing only because `recovered_from`
reports it, not because it changes what you do.

Neither source restores the failed call or Python variables, and an autosave is
the last *completed* write rather than every acknowledged edit. A recovered
session keeps the original live filepath rather than the recovery file, so it
will not overwrite its own lifeboat — keep the autosave's adjacent `.json`
sidecar, which is where that filepath and the dirty flag live. `session.log`
names the request that was running and the crash file
`.blender-cli/session-<pid>.crash.txt`; a native rendering crash also carries
the Python stack captured before the renderer took the GIL. `os._exit` and
SIGKILL bypass the handlers, so they leave recovery files but no dump. Closing
a dead session discards it and keeps its crash file.

## Looking at the scene

Pictures arrive on their own, at budget size and cropped to what moved.
`observe` is for the times that is not what you need: a bigger frame, another
view, a different pass, or a file to feed back as a reference. It renders
offscreen with fixed cameras, a fixed light rig and fixed colour management, so
the same scene state always produces the same PNG — the two runs behind this
page, in different directories, wrote the same `060bef51…` bytes. It is
deterministic, not fast: a 512 px pair on a software Vulkan device took 4.8 s.

```sh
blender-cli observe --views front,persp --json
# {"image":"/tmp/u5/.blender-cli/observe/060bef51….png","views":["front","persp"],"passes":["color"],"size":[516,1032],"ms":4782.478429000548,"ok":true,
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
`agent.objective()["targets"]["front"]` has the same `iou`, `chamfer`, `delta`
and `worst` the event does, `agent.perceive()` the same counts, bounds,
framing, changed region and symmetry, and `agent.program()["version"]` and
`agent.history()[-1]["op"]` read what `program get` and `session history`
return. Keeping a loop inside one `exec` — score, adjust, score again — costs
one round trip instead of one per iteration, which is the same reason `fit`
exists as a request.

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
  way. `target set --fit bbox` (the default) centres the reference's foreground
  at observation's occupancy `1/1.1`, which removes reference margins but not a
  different viewpoint or perspective.
- Automatic framing removes uniform scale, so a `fit` over a parameter that only
  scales the whole model measures nothing. Search a proportion, or fix the
  framing with `--frame OBJECT` or a scene camera.
- `--mask auto` is deterministic classic CV, not segmentation: it can fail on a
  textured background, on foreground touching the border, or on shading close to
  the background colour. `target set` writes the silhouette it derived beside
  the reference — look at it before trusting a bad score.

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
