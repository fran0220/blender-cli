# blender-cli design

Owner of: the process model, the channel protocol and its events, the request
set and its CLI projections, the feedback provider registry, the program
model, targets and `fit`, the session protocol, observation determinism and
the comparison metrics. Constraints are in `AGENTS.md`; status is in
`PLAN.md`.

## Why this shape

An agent modelling from a reference image runs one loop: look at the scene,
write `bpy` code, execute it, look at the result, compare with the
reference, repeat. The agent's decision is the only step that must be slow
(seconds, tokens). Everything else is either computed inside the process or
it is waste:

```
agent ──(one statement)──▶ one process: namespace + Main + eval + offscreen
                           render + metrics + search
      ◀──(event stream: value, diff, perception, objective, image, done)──
```

Four cuts follow from that, in order of how much loop they remove:

1. **Feedback is pushed.** Each action answers with its own consequences on
   three channels — structural diff, perceptual delta, objective delta — so
   "observe" and "compare" are not separate decisions. Budgets keep the cost
   bounded; deltas keep the tokens low.
2. **The scene is a program.** `model.py` is the record; the agent edits
   text, the process re-executes from the longest cached prefix. State is
   fully visible, history is a version tree on disk, rollback is a checkout.
3. **Search runs inside.** `fit` evaluates parameters against registered
   targets in-process with progress and cancellation; the agent proposes a
   parameterisation and an objective, not a sequence of guesses.
4. **Self-description and corrective errors.** `describe` answers from live
   RNA and from the channel registry; an error carries the nearest valid
   identifier and, when unambiguous, the corrected statement.

## Channel protocol

One session speaks one protocol on every transport. Requests and events are
JSON objects, one per line, UTF-8, newline-terminated. Transports:

- the session socket (`.blender-cli/session.sock`, AF_UNIX);
- `blender-cli repl`: a stdio bridge to the session socket (opening the
  session if none exists; `--standalone` runs the loop in the same process
  without a daemon), so a host holds one pipe for the whole session;
- one-shot CLI verbs: each sends exactly one request and prints its folded
  envelope (below).

### Requests

```
{"id": 7, "op": "exec",     "code": "..." | "script": "/abs/path.py", "record": true|false,
                            "timeout": 30, "feedback": {…image policy…}}
{"id": 8, "op": "program",  "action": "get|set|patch|run|history|rollback|record",
                            "text": "...", "old": "...", "new": "...", "label": "...",
                            "version": "sha256:…|label", "on": true|false, "from_step": 3,
                            "feedback": {…image policy…}}
{"id": 9, "op": "target",   "action": "set|list|clear", "name": "front",
                            "ref": "path.png", "view": "front", "mask": "auto", "fit": "bbox",
                            "metrics": ["iou","chamfer","ssim","hist"]}
{"id":10, "op": "fit",      "params": [...], "objective": {...}, "budget": {...},
                            "method": "coordinate|nelder-mead|random"}
{"id":11, "op": "inspect",  "select": ["objects[\"Cube\"].location"], "object": "...", "full": false}
{"id":12, "op": "observe",  "views": ["front"], "passes": ["color"], "size": 512,
                            "ref": "path.png", "layout": "sheet|separate", "overlay": false,
                            "frame": "Handle", "out": "path", "inline": false}
{"id":13, "op": "describe", "path": "bpy.ops.mesh.bevel" | "agent.compare" | "channel" | "schema"}
{"id":14, "op": "session",  "action": "snapshot|rollback|history|save|close|feedback|status",
                            "label": "...", "snapshot": "sha256:…|~N", "file": "...", "feedback": {...}}
{"id":15, "op": "cancel",   "target": 10}
```

`id` is a client-chosen integer, unique within the connection. Fields not
listed for an `op` are rejected with `error` of type `ProtocolError`, naming
the field and the ones the op accepts, before anything runs. The request
table in `agent_runtime.py` (`REQUESTS`, `EVENTS`, `DEFS`) is that list: the
validator, `describe channel` and `describe schema` all read it, and each
field carries `type`, `required`, `default`, `enum`, `items`, `ref` and
numeric bounds. One request executes at a time; later requests queue in
arrival order. `cancel` is handled on the transport thread and answered
immediately with its own `done` — `{"target": N, "cancelled": true|false}`,
false when no request with that id is running — while it raises `G.is_break`
for the running request.

How that request then ends is the op's `cancels` outcome in the request
table. The default is `error`: the request ends with `error` of type
`Cancelled`, having restored the state it started from, because a half-applied
edit is worth nothing. `fit` declares `done` instead: a search that has
already paid for its evaluations keeps them, applies the best parameters and
ends with `ok: true` and `cancelled: true`. Discarding a paid-for search is
the opposite of what the search is for. An op's outcome is data, so
`describe schema` projects it rather than restating it.

Every CLI verb is exactly one request and its flags are that request's
fields; the mapping lives once, in `agent_cli.hh`, so the launcher and the
in-process one-shot verb build identical requests. `--file` and `--save`
are not request fields except where an op declares `file`: they select the
scene a one-shot verb loads and writes, and a session rejects them.

A request that fails restores `Main` to the snapshot the session held when
it started, so a partial edit never survives its own error. A request that
changes no datablock takes no snapshot and does not advance `step`; its
`diff` event still carries the state it left.

### Events

Every request produces an ordered sequence of events sharing its `id`, ending
in exactly one `done` or `error`:

```
{"id": 7, "event": "log",        "stream": "stdout|stderr", "text": "..."}
{"id": 7, "event": "value",      "value": "<repr>"}
{"id": 7, "event": "diff",       "added": [...], "changed": [...], "removed": [...],
                                 "snapshot": "sha256:…", "step": 12}
{"id": 7, "event": "perception", ...shape below...}
{"id": 7, "event": "objective",  ...shape below...}
{"id": 7, "event": "image",      "kind": "delta|full|overlay|error", "view": "front",
                                 "pass": "color", "path": "...", "inline": "<base64>",
                                 "size": [w, h], "region": [x0, y0, x1, y1]}
{"id":10, "event": "progress",   "eval": 37, "of": 200, "best": 0.913, "params": {...}}
{"id": 7, "event": "done",       "ok": true, "ms": 123.4, ...op-specific result...}
{"id": 7, "event": "error",      "ok": false, "type": "...", "message": "...", "line": 3,
                                 "rna": {...}, "fix": {"code": "..."}, "autosave": "..."}
```

Ordering within one request: `log` and `progress` as produced; `value`; then
`diff`; then `perception`; then `objective`; then zero or more `image`; then
`done`. `log` may interleave with anything before `done`. `done` carries the
op-specific result fields defined under each request below (`session
history` rows, `describe` records, `observe` paths). Human-readable output
is the folded envelope, indented.

### Folded envelope (one-shot CLI, `--json`)

A one-shot verb prints one JSON document: the `done`/`error` object with
`diff`, `perception`, `objective` and `images: [image…]` merged in and
`stdout`/`stderr` concatenated from `log`. The folded envelope is derived
from the event stream by one function, `fold()` in `agent_events.hh`; it has
no fields of its own. An `error` event's fields become the envelope's
`error` object; `stdout` and `stderr` appear only when something was written;
`progress` is transient and does not survive folding. Human output is that
envelope indented; `--json` is the same document, compact. Exit status is 0
for `ok: true`, 1 otherwise. Both the launcher talking to a session and the
in-process one-shot verb fold the same way, because they call the same
function.

### Feedback budgets

`session feedback` sets the per-session policy; it is returned by
`session status`:

```
{"perception": true,
 "objective":  true,
 "progress":   "all|improvements|off",
 "image": {"mode": "delta|full|off", "threshold": 0.002, "views": ["front"],
           "pass": "color", "size": 256, "samples": 8, "overlay": true,
           "inline": false}}
```

Defaults are the values above. `threshold` is the fraction of changed
pixels in the budget view below which no `image` event is sent. `exec` and
`program` requests may carry `"feedback"` to override the image policy for
one request; nothing else is per-request.

`progress` is what a search pushes while it runs. The default,
`improvements`, sends a `progress` event only for an evaluation that beat the
best; every evaluation is already in `done.curve`, so pushing them all spends
tokens repeating the answer. `fit`'s `budget` bounds the search the same way:
`patience` evaluations without an improvement greater than `tolerance` end
it, and `done.stopped` says which bound it hit — `budget`, `patience`,
`seconds` or `cancel`.

`samples` is the budget render's EEVEE sample count, deliberately below
observation's fixed 32: a delta is read for where the picture moved, not for
its finish, and samples are what the render's cost is made of. Observation
keeps 32 and stays byte-deterministic; the budget view is deterministic at
its own sample count, so an unchanged scene still produces an identical
buffer and a zero delta.

### Perception event

Always sent for `exec`, `program set|patch|run|rollback` and `session
rollback`; costs no render beyond the budget view at budget size.

```
{"event": "perception",
 "objects": 3, "verts": 1290, "faces": 1288,
 "bounds": {"low": [x,y,z], "high": [x,y,z]}, "dims": [x,y,z],
 "framing": {...observe framing record...},
 "changed": {"objects": ["Handle"],
             "view": "front", "region": [x0,y0,x1,y1], "fraction": 0.031,
             "silhouette_delta": 0.012},
 "symmetry": {"x": 0.98, "y": null, "z": 0.12}}
```

`objects`, `verts` and `faces` count the same converted, instanced geometry
that `framing` measures and the budget view renders — evaluated as rendered,
including geometry-nodes instances, excluding cameras, lights and
hidden-render objects. `bounds` repeats `framing.bounds` and `dims` is its
extent.

`changed` compares the budget view against the previous perception render of
the same session (kept in memory as the budget-size silhouette and color
buffers); it is `null` on the first action, and present with zero deltas when
an action changed nothing. `changed.objects` names the objects in the
request's ID diff, including the objects that use changed geometry
datablocks. A pixel counts as changed when any 8-bit channel moves by more
than 2 or the silhouette flips there; `region` is the changed pixels' bounds
in the budget view (top-left origin, exclusive high, `null` when nothing
changed) and `fraction` is their share of the view. `silhouette_delta` is
`1 − IoU` of the two silhouettes.

`symmetry` is silhouette IoU under mirroring about each world-axis plane
through the framing center, measured in the budget view. An orthographic
preset projects that center onto the image center, so mirroring the buffer
mirrors world space exactly; the axis along the view direction is invisible
to the silhouette and is reported as `null` rather than the trivial 1.0
(`front` measures x and z). Non-axis-aligned views (`persp`, `camera`)
report `null` for all three. Perception costs no render beyond the budget
views, whose buffers the image provider reuses.

An action that changed no datablock and left the same snapshot cannot have
changed the picture, so it costs no render at all: perception answers from
the remembered buffers with zero deltas, and no image is sent. The snapshot
is part of that test because a rollback inside the executed code moves
`Main` without leaving a diff behind.

`agent.perceive(view, size)` returns this payload without the `event` key
and without advancing the remembered state, so an action's own
`agent.perceive()` and its perception event describe the same state against
the same baseline. It renders once for that sample; only the action's
provider advances the baseline.

### Objective event

Sent after every action when at least one target is registered:

```
{"event": "objective",
 "targets": {"front": {"iou": 0.931, "chamfer": 3.1,
                       "delta": {"iou": +0.012, "chamfer": -0.4},
                       "worst": {"region": [x0,y0,x1,y1], "iou": 0.61,
                                 "missing": 0.7, "extra": 0.3}}},
 "best": {"front": {"iou": 0.931, "snapshot": "sha256:…", "step": 12}}}
```

Each target carries exactly the metrics it was registered with, scored at the
feedback size in its own view; targets sharing a view share one render.
`delta` is the change in each of those metrics since the previous scoring,
and is `null` the first time a target is scored, exactly as perception's
`changed` is `null` on the first action.

`worst` is the 4×4 grid cell of the view with the largest silhouette error
(the most disagreeing pixels; ties go to the first cell in row-major order),
split into reference-not-model (`missing`) and model-not-reference (`extra`)
as fractions of that cell's disagreement, so they sum to 1 (to 0 when the
cell agrees exactly). `region` is `[x0, y0, x1, y1]` in view pixels,
top-left origin, high coordinates exclusive — the same convention as
`reference.bbox`.

`best` remembers the best value of the target's **primary metric** — the
first metric it was registered with — seen this session, and the
snapshot/step that produced it, so an agent can return to it without
bookkeeping. Larger is better for `iou` and `ssim`, smaller for `chamfer`
and `hist`.

The objective provider records the values it reports, so the next `delta` is
measured against them. `agent.objective()` returns the same dict for the
current state without recording it, so calling it never changes what the
next event reports.

### Image event

`delta` is the changed region cropped out of the budget view with 8 px
padding, in the budget view's own pixels; its `region` is that padded crop,
so it always covers `perception.changed.region`. `overlay` is the same
region's silhouettes before and after (before red, after cyan, agreement
white, neither RGB 32); `error` is the target's silhouette error map
(missing red, extra blue) for the worst region, or, when the feedback render
itself failed, a `message` and no image at all; `full` is a whole frame at
budget size.

The image provider sends one image per budget view per action, and nothing
when the view's changed fraction is below `threshold`. A view the agent has
never seen has nothing to be a delta against and is sent `full`; `mode:
"full"` sends whole frames thereafter, `mode: "delta"` sends the crop and,
with `overlay` on, the overlay beside it. An image event carries `path` or
`inline`, never both: by default the PNG is written under
`.blender-cli/feedback/` and named by its content hash, and with the image
policy's `inline` set the base64 payload replaces it. The policy is per
session and per request, so a host without a shared filesystem asks for
inline once and a host with one never pays for base64.

## Feedback provider registry

The runtime assembles events for a request by calling registered providers
in a fixed order; workstreams add channels by registering, never by editing
the assembly. In `agent_runtime.py`:

```python
class Provider(Protocol):
    name: str                       # "diff", "perception", "objective", "image", ...
    order: int                      # ascending; diff=100, perception=200, objective=300, image=400
    def before(self, request: dict, session: Session) -> None: ...   # capture pre-state
    def after(self, request: dict, session: Session, emit) -> None: ... # emit(event_dict)

def register_provider(provider: Provider) -> None
def register_op(op: str, handler) -> None          # handler(request, session, emit) -> dict
def register_helper(name: str, function) -> None   # function(session, ...) backs agent.<name>
def register_record_hook(hook) -> None             # hook(session, code, step)

PROVIDER_MODULES = ["agent_feedback", "agent_target", "agent_program"]
```

`before` runs on the main thread before the request executes; `after` runs
after it, in `order`, and may call `emit` any number of times. Both run only
for a request that can change `Main` (`exec`, `fit`, `program
set|patch|run|rollback`, `session rollback`), because a request that changes
nothing has no consequences to push. A provider that raises is reported as a
`log` event on `stderr` and skipped; it never fails the request. Providers
read session state through `Session`; they do not call each other. The image
provider reads the perception provider's last result through
`session.last_perception`.

Each module named in `PROVIDER_MODULES` is imported once per session and must
expose `register(session)`, where it installs its providers, request handlers,
`agent` helpers and the record hook. A module that is not built is skipped; a
listed module without that hook fails the session, because a feedback channel
that silently contributes nothing is worse than a session that will not open.
A module that belongs to a session only, such as the program, registers
nothing when `"snapshot" not in session.native` — that is exactly the
one-shot case.

The built-in `diff` provider (order 100) is the kernel's: it samples the ID
state at both boundaries, advances `step` and takes the snapshot when
something changed, runs the record hook, and emits the `diff` event. It is
the only provider that decides what a state change is; everything else reads
`session.last_diff`.

The kernel publishes these session attributes for providers and handlers:
`last_diff`, `previous_snapshot` (the snapshot in force when the request
began), `last_perception`, `request_feedback` (the policy for this request,
including a per-request image override), `targets`, `recovered_from` and
`opened_file`. A session opened with an explicit file starts from that file:
nothing replays over it.

## Program model

`.blender-cli/program/model.py` is the session's program: a re-executable
Python file whose steps are the actions that produced the scene. Layout:

```python
# blender-cli program
# base: factory-empty
P = {"handle_x": 0.43, "body_r": 0.35}      # parameters (fit targets these by name)

# step 1
bpy.ops.mesh.primitive_cylinder_add(radius=P["body_r"], depth=1.0)

# step 2
bpy.data.objects["Cylinder"].location.x = P["handle_x"]
```

Everything before the first `# step N` line is the **header**; it is the
parameter block and it must not change `Main`, because it is re-executed at
the start of every run. `P` is a literal `dict` with string keys, read by
`ast.literal_eval`; the program is never executed to be parsed. Steps are
the text between `# step N` lines, renumbered on every write.

`# base:` names the state step 1 starts from and is written when the program
is created: `factory` for a session opened without `--file`, `file <path>`
for one opened with it, `factory-empty` when the line is absent. The base is
a re-executable statement (`wm.read_factory_settings`, `wm.open_mainfile`),
not a snapshot, so a program rebuilds its scene in any process.

- `program record on` (default on in a session) appends every `exec` whose
  diff is non-empty as the next `# step N` block; an `exec` with an empty
  diff, a failed `exec` and `exec` with `"record": false` are never
  recorded.
- `program get` returns `{text, params, steps, version, base, record,
  reproducible}`, where `steps` is `[{n, code, reproducible}…]`.
  `program set` replaces the text; `program patch` applies one `old`→`new`
  replacement and fails when the match count is not exactly one. Both
  re-execute.
- `program run` re-executes. `program history` lists the version tree;
  `program rollback <version|label>` checks a version out and re-executes
  it, taking a version, a `sha256:`-less digest prefix or a label.
  `program record on|off` answers `{record}`. `set`, `patch`, `run` and
  `rollback` take `--label` to name the version they create.
- `set`, `patch`, `run` and `rollback` answer
  `{version, steps, digest, from_step, cached, ran, reproducible}`.
- A step that raises ends the request with `error`, whose `type` is the
  step's own exception type, `line` is relative to that step, and `message`
  is prefixed `step N:`. It also carries `step`, the `version` holding the
  failing text, and `cached_through`, the last prefix still cached.

  `Main` returns to the pre-request state: the kernel's rule that a failed
  request leaves no partial edit holds here too, so a failed edit never
  becomes the live scene. The program keeps the failure in three places that
  are not `Main`. The text keeps the edit, because a file is not `Main` and
  the agent patches the text it can see. Its version row records
  `failed: true` with `step` and `line`. And the prefix cache keeps every
  step that did run, because a cache is not state — so a corrected `set` or
  `patch` resumes from `cached_through` at no extra cost.

### Prefix-cached re-execution

Re-execution runs from the longest prefix whose memfile snapshot is still
cached. The key of the state after `N` steps is

```
sha256( "# blender-cli program" ∥ base ∥ header-without-P ∥ step₁ … step_N
        ∥ canonical JSON of the parameters those texts read )
```

The parameters that enter the key are exactly the names appearing as
`P["name"]` in the header or in steps 1..N; any other use of `P` (a computed
key, `P.get`, passing `P` along) makes the prefix depend on every parameter.
Changing one parameter therefore invalidates the first step that reads it
and everything after it, and nothing before. Each executed step's snapshot
is cached under its prefix key, so the same prefix is never recomputed.
Rolling back to a cached prefix restores `Main`, **not** Python variables:
a step that reads a variable a skipped step defined sees that variable's
value from the last time the skipped step ran, and RNA references in it are
stale, exactly as after `session rollback`.

An evicted memfile drops every prefix that named it and re-execution falls
back to a shorter prefix, or to the base.

`set`, `patch`, `run`, `rollback` and `program get` answer with a `digest`:
`agent_program.digest()`, a sha256 over `Main`'s content. A partial re-run
and a full run from the base of the same program produce the same `digest`,
and that equality is what proves the prefix cache correct. Memfile snapshot
IDs cannot serve: they hash retained buffers including allocation state, so
two runs that build the same scene never share one. The `digest` is
comparable across runs and across processes; the snapshot ID is not. It is
the answer to "is this the same scene" everywhere in the process.

It covers every ID list; object transforms, relations and material slots;
mesh vertex, edge, loop and polygon buffers; material node graphs; cameras,
lights and collections; curve spline points, metaball elements, lattice
points and armature bones; the attribute domains of meshes, grease-pencil
drawings, point clouds and hair curves, read as values rather than as the
bare references RNA reports; and the RNA settings of every modifier, every
constraint and every non-mesh data ID. A mesh's `position` and its
dot-prefixed topology attributes are the geometry buffers and are not read
twice; everything else on the mesh — UV and colour layers, sharpness,
creases, and whatever geometry nodes stored by name — is.

Every node tree is content and is walked the same way, shader, geometry and
compositor alike: `bpy.data.node_groups`, material, world and compositor
trees contribute their nodes, each node's settings, every input socket's
`default_value` and linked state, the links, and the group interface with
its defaults. A Nodes modifier also keeps its group's input values in a
struct per socket, which the settings walk can only report as a reference,
so those are read directly. Two scenes whose geometry nodes differ in one
socket therefore have different digests even when nothing reaches a mesh.

The settings walk is `agent_runtime.settings`, the one `inspect --full`
already uses, so what an agent can read is what the digest distinguishes:
two scenes differing only in a modifier's numeric setting have different
digests even when the setting never reaches a mesh datablock. Only the
writable half is kept. Read-only RNA is derived from the rest, and some of
it is a measurement rather than a setting — a Nodes modifier reports its own
`execution_time`, which would otherwise make two runs of one program
disagree about the scene. A `Mesh` is the one ID that walk skips, because
its content is the geometry buffers and its attribute domains, and walking a
million-vertex collection as RNA references would cost far more and say
less.

Measured on Linux (Release, five calls each, median; every scene hashed
identically across all five): an empty scene costs 0.4 ms; 50 objects with
150 modifiers, 50 constraints and a geometry node group cost 40.5 ms; a
1,002,001-vertex grid alone costs 663.5 ms; both together cost 610.4 ms.
Mesh buffers dominate, and the RNA, node-tree and attribute walks do not
grow with vertex count. A `digest` is computed once per `program` request,
not per `exec`.

### Versions

Every `set|patch|rollback` and every recorded `exec` writes
`versions/<sha256 of the text>.py` and appends a row to `index.json`:
`{version, parent, label, at, steps, reproducible, message, failed}`, with
`step` and `line` added when `failed` is true. `program run` changes no text
and so creates no version; it re-executes the current one. `current` names
the checked-out version. Identical text reuses its version file and still
appends a row, so the tree records the move. `program rollback` takes a
version, a `sha256:`-less digest prefix or a label; `session snapshot
--label L` labels the current version.

### `reproducible`

`reproducible` is a static, conservative verdict per step and for the
program: a step is reproducible unless its source shows something a re-run
cannot replay. It is false when the step

- imports or names `time`, `datetime`, `uuid`, `secrets`, `socket`,
  `subprocess`, `urllib`, `requests`, `http`, `getpass`, `tempfile` or
  `webbrowser`, or names `os.urandom`, `os.environ`, `os.getpid` or
  `bpy.app.timers`;
- imports or names `random` or `numpy` and the program contains no `seed()`
  call with a literal argument;
- calls `input()`, or reads a file the program cannot carry: `open`,
  `bpy.ops.wm.open_mainfile|append|link|revert_mainfile`, `bpy.data.*.load`
  or a `bpy.ops` importer whose path argument is not a literal relative path
  inside the program directory (absolute paths, `//`-relative paths and
  computed paths all count as outside);
- fails to parse.

A program is reproducible when its header and every step are. The verdict is
recorded in each `index.json` row. It is only ever downgraded at run time:
when two full runs from the base of the same version land on different
`digest`s, that version becomes irreproducible for the rest of the session.
The observed digests are session state, so this dynamic downgrade does not
survive a restart; the static verdict does.

### Crash recovery

Recovery always recovers, and the newest source wins. A session directory
that holds work another process left behind compares the program's newest
version time with the modification time of that process's recovery file:

- the program is newer, or no recovery file survived: the scene is rebuilt
  by running the program from its base, and `recovered_from` is `"program"`;
- the recovery file is newer: it is loaded, sidecar included, exactly as
  `session open --file` would load it, and `recovered_from` is `"autosave"`.
  The program stays attached — its text is still the record — with an empty
  prefix cache, because the scene on screen is the file's and not a prefix of
  the program;
- replaying the program raises: the recovery file is loaded instead and the
  failure is reported on stderr. A program that no longer runs never leaves
  its directory unopenable, and never leaves the agent empty-handed.

`recovered_from` is null only when there was nothing to recover. It is
reported by `session open` and by `session status`. Which branch a reopen
takes is otherwise a race between two modification times, so an agent that
had to notice a recovery file and reopen a second time to get its work back
was the bug this rule removes.

A session opened on a file is not a recovery: the agent named what it
wanted, nothing is replayed over it, and a session opened explicitly on a
recovery file keeps `recovered_from: "autosave"`.

### Registration

`agent_program.register(session)` is the `PROVIDER_MODULES` entry point. It
installs the `program` request handler, backs `agent.program()`, installs the
record hook, and performs crash recovery.

A program belongs to a session, so `register` does nothing in one-shot mode,
where there is no snapshot store to cache prefixes in: a one-shot `exec`
leaves no `model.py` behind for the next session to replay, and the
`program` request there answers that it is not implemented. Recovery is also
skipped when the session was opened on a file. `session open --file F` asked
for `F`; the program is the truth only when recovering, never over a scene
the agent named.

The record hook is the runtime's
`register_record_hook(f)`: `f(session, code, step)` runs after an `exec`
whose diff was non-empty and whose `record` was not false, so a failed exec,
an exec that changed nothing and `exec --no-record` are never steps. The hook
reads the snapshot on each side of the request from the session's history;
the step's snapshot enters the prefix cache only when the pre-request
snapshot is the one the program's own prefix produced, so a recording made
after a rollback never poisons the cache.

An `exec` that itself drives the program — calling `Program.run` or
`set_params` — should carry `--no-record`, or the program comes to contain a
step that re-runs the program.

### Python API

`agent.program()` returns `{text, params, steps, version, reproducible}`.

`fit` drives program parameters through the session's `Program`:

```python
program = agent_program.attach(session)   # the session's Program, created on demand
program.params                            # the parsed P dict
program.set_params(values, label=None)    # rewrite P, commit, re-execute the affected suffix
program.version                           # current "sha256:…"
program.run()                             # re-execute from the longest cached prefix
```

`set_params` merges named values into `P`, rewrites only the `P = {…}`
statement in the header, commits a version and re-executes from the first
step that reads a changed parameter, leaving `Main` at the result. That is
one `fit` evaluation, and it costs one partial re-execution. It answers the
run result above, so `version` identifies the evaluated program and `digest`
identifies the scene it produced, and it raises `agent_program.StepError`
when a step fails. Setting a parameter no step reads re-executes nothing.

## Targets and `fit`

`target set` stores the reference under `.blender-cli/targets/<name>/` with
its preprocessed silhouette (mask policy and `fit` policy as in `compare`),
bound to one view. Registered targets are scored by the objective provider
after every state-changing request at the feedback size (256), and at
`budget.size` during `fit`. A target scores only the metrics it was
registered with, and targets sharing a view share one render, so the pushed
objective costs one render per distinct target view; a target registered
with `ssim` or `hist` pays for them on every action. A target bound to a
budget view is scored from the render the perception provider already made
for that request, so it costs nothing beyond the metrics.

`fit`:

```
{"op": "fit",
 "params": [{"name": "handle_x", "min": 0.2, "max": 0.6}          # program parameter
            {"path": "objects[\"Handle\"].scale[0]", "min": 0.5, "max": 2}],   # RNA path
 "objective": {"target": "front", "metric": "iou"}
            | {"targets": ["front", "side"], "metric": "iou", "weights": [0.7, 0.3]}
            | {"code": "agent.compare(...)['iou']"},
 "budget": {"evals": 200, "seconds": 120, "size": 128},   # seconds optional
 "method": "coordinate"}
```

Every parameter needs `min` and `max`; the search starts from the live value,
clamped into that interval, and works in the unit cube so a step means the
same thing on every parameter. Each evaluation sets the parameters (program
parameters re-execute from the parameter block's cached prefix; RNA paths
assign directly), updates the depsgraph, renders the objective's views at
`budget.size`, and scores. Repeated parameter vectors are answered from the
search's own cache and cost no render and no evaluation.

`objective` defaults to every registered target on `iou` with equal weights.
The metric's direction decides whether the search maximises (`iou`, `ssim`)
or minimises (`chamfer`, `hist`); a `code` objective is always maximised and
must return a number. `budget` defaults to `{"evals": 200, "size": 128}`;
`seconds` has no default, so the evaluation count is the only bound unless
the agent asks for a wall-clock one.

`progress` events are sent at most every 0.5 s and carry the best value and
its parameters, so an agent watching a long fit never has to ask. `cancel`
and an exhausted budget behave alike: the search stops, the best parameters
are applied, and the request ends with `done`, not `error` — a cancelled fit
that discarded its result would cost the agent everything it paid for. Only
`fit` answers a cancel this way; every other request ends with `Cancelled`.

`done` returns

```
{"ok": true, "ms": …, "method": "coordinate",
 "objective": {"targets": ["front"], "metric": "iou", "weights": [1.0]},
 "best": {"params": {"handle_x": 0.41}, "score": 0.994, "snapshot": "sha256:…"},
 "evals": 37, "failed": 0, "curve": [[1, 0.81], [4, 0.93], [19, 0.994]],
 "applied": true, "cancelled": false,
 "error_map": {"target": "front", "view": "front", "image": "…png",
               "size": [w, h], "region": [x0, y0, x1, y1]}}
```

`curve` records only the evaluations that improved the best value, so it is
the search's trajectory rather than its transcript. `error_map` is the first
objective target's silhouette error at `budget.size` — missing red, extra
blue, agreement white — with `region` naming its worst 4×4 cell; it is
absent for a `code` objective. The best parameters are applied to the live
scene and, for program parameters, written into `P`.

A parameter vector whose program step raises is a dead region of the search
space, not a failed request: the evaluation costs its budget, scores as the
worst possible value, is reported in `failed`, and the search continues. The
step's error is logged on `stderr`. `fit` fails only when nothing scored at
all.

Methods: `coordinate` (cyclic coordinate descent that walks an improving
direction until it stops improving and halves the step on a barren cycle),
`nelder-mead`, `random` (a seeded Latin hypercube over half the evaluation
budget, then coordinate refinement from its best sample). Determinism: same
program, same budget, same method ⇒ same result, whenever the evaluation
budget binds before the time budget.

## Describe and corrective errors

`describe channel` returns the request/event registry as records;
`describe schema` returns the same as JSON Schema for a function-calling host.
Both are ordinary `describe` paths, on the channel and on the CLI alike.
`describe agent`, `describe agent.<fn>` and `bpy.*` paths behave as in the
RNA contract below.

Both are **generated from the request table** the runtime validates against
(`REQUESTS`, `EVENTS` and the shared `DEFS` it references); neither is
hand-maintained, so a host's tool catalog cannot describe a request the
process would reject. A field spec carries `type`, `required`, `default`,
`enum`, `items`, `ref` (a `DEFS` name), `minimum`/`maximum` with an optional
`exclusive_minimum`, `exactly_one_of` on an object, and a one-line `doc`.
`describe channel` answers `{kind: "channel", requests, events, defs}`; each
request record is `{doc, fields, events, example}`, where `events` names the
events that op can produce and `example` is one concrete valid request.

`describe schema` answers `{kind: "schema", $schema, requests}` with one
document per op. Each document is self-contained — its own `$schema`, an
`$id` of `urn:blender-cli:request:<op>`, `title`, `description`, and a
`$defs` holding exactly the shared shapes it reaches — so a host can hand a
single op's schema to a model unchanged. `id` and `op` are part of the
projected request: `op` is a `const`, `additionalProperties` is `false`, and
`exactly_one_of` becomes `oneOf`. Every op record's `example` validates
against its own document; that is what keeps the table, the schema and the
request validator from drifting apart.

Errors add `fix` when a single correction is certain: an unknown attribute
or operator keyword, or an enum item that is not in the property's live
items. Candidates are ranked by the same difflib similarity as `nearest`,
including the `data.` hop, and a correction is **unambiguous** when it is
the only candidate above the 0.6 cutoff, or scores at least 0.85 and beats
the runner-up by more than 0.05. A lone candidate needs no threshold because
nothing else is close; a crowded neighbourhood needs both. `fix.code` is the
submitted code with that one identifier replaced at its source position and
nothing else changed, so the agent resubmits it directly — necessary because
a failed request rolls `Main` back, which makes any earlier statement in the
same code part of the correction. It is emitted only if it still compiles.
`fix.reason` names the struct, the rejected identifier, the replacement and
its similarity. When no single fix is certain, `fix` is absent, never a
guess. A value Blender clamps is not an error at all (see *RNA details*), so
it carries no `fix`; the property record's ranges expose the limit instead.

## Process model

```
┌───────────────────────── blender-cli process ─────────────────────────┐
│ transport thread(s)      main thread                                     │
│  AF_UNIX / stdio  ─────▶ queue ─▶ execute ─▶ BLI_timer_execute ─▶ answer │
│                                     │                                    │
│                          Python namespace ── bpy ── Main ── depsgraph    │
│                          offscreen GPU (EEVEE) ── metrics ── snapshots   │
└──────────────────────────────────────────────────────────────────────────┘
```

- Entry: `blender --command agent <verb> …`, registered through
  `BKE_blender_cli_command_register` from `creator.cc`. `--command`
  already forces background mode, runs after full initialization and only
  calls `WM_exit` when the handler returns, so a long-lived loop is an
  ordinary command. `blender-cli` is a launcher for that invocation.
- Everything that touches `bpy`, RNA or `Main` runs on the main thread.
  Transport threads only move bytes. One request is in flight per session;
  a second request waits in arrival order in memory and is dropped with the
  process. There is no durable queue.
- One process holds one `Main`. Multiple scenes are multiple processes.
- GPU: `WM_init_gpu_offscreen` on first `observe`. macOS builds are normal
  builds run in background mode so `createSystemBackground()` reaches the
  Cocoa offscreen path and Metal; Windows builds use Vulkan. `WITH_HEADLESS`
  is used on Linux only.

## Modes

- **Channel**: `blender-cli repl [--file scene.blend] [--standalone]` holds
  one stdio pipe to the session for the whole conversation: requests in,
  events out, as the channel protocol defines. This is the primary mode.
- **Session + one-shot verbs**: `blender-cli session open [--file
  scene.blend]` starts a daemon bound to `<cwd>/.blender-cli/session.sock`.
  Any verb run in that directory sends one request to it and prints the
  folded envelope. Without a session the verb runs one-shot: loads, runs,
  optionally writes (`--save`), exits, with state in the `.blend` file.

The returned endpoint remains absolute. Where its absolute name exceeds the
platform's Unix-socket address limit, the launcher and daemon address the same
file relative to their initial working directory. Raw socket clients can likewise
connect to `.blender-cli/session.sock` from the session directory. Cleanup retains
the absolute name even if Python later changes the daemon's working directory.

All modes run the same request registry; the daemon adds only the endpoint,
the persistent namespace and the program. One-shot verbs are projections of
the requests: each verb's flags are the request's fields, and its output is
the folded envelope.

## The requests

Every request is described here in its CLI projection; the JSON form is the
request shape under *Channel protocol*. All verbs take `--json` to emit
exactly one JSON document on stdout; human output is the default. Images are
files whose paths appear in the JSON, or inline base64 with `--inline`. Exit
code 0 is success; non-zero carries an `error` object.

A verb is a request and its flags are that request's fields, so the flag table
is generated from the request table rather than written a second time:
`agent_cli_gen.py` reads `REQUESTS` at build time and emits the table
`agent_cli.hh` parses with, which is why the launcher (talking to a session)
and the in-process one-shot verb build byte-identical requests. A field added
to the contract therefore arrives with its flag, and `blender-cli --help`
lists it, without a hand-written line anywhere. The naming rule is:

| Field | Flag |
|---|---|
| boolean, default true | `--no-<field>` clears it |
| boolean, otherwise | `--<field>` sets it |
| array of strings | `--<field> A,B` |
| number or integer | `--<field> N`, the enum values when bounded |
| string | `--<field> V`, the enum values when bounded |
| anything structured | `--<field> JSON` |

The projections that this rule does not produce are `exec -c CODE` and its
`SCRIPT.py` argument, `--image` for the per-request feedback override, and the
action of `session`, `program` and `target` with its argument, which read as
words rather than flags (`session rollback ~1`, not `session --action
rollback --snapshot ~1`). They are listed in one place, `IRREGULAR` in
`agent_cli_gen.py`, and nothing else deviates.

Wherever a value is a statement, a program or a policy rather than a word, it
may name its source instead: `@FILE` is that file's contents and `-` is stdin,
so `exec -c @edit.py` and `program set --text @model.py` do not depend on
shell quoting. A value that must begin with an at sign is written `@@`.

`cancel` has no CLI projection: it stops a request that is still running, which
needs the channel. `repl` and `session open` are the reverse — the launcher
answers them itself, so they are the only verbs that are not requests.

### `session`

```
session open  [--file F]        start daemon for cwd; answers {"session": id, "socket": path}
session status                  {"session", "file", "dirty", "step", "snapshot", "feedback", "targets"}
session feedback [KEY=VALUE…]   merge those settings into the policy; answers the policy
session save  [--file F]        write the .blend
session close                   write nothing, stop the daemon
session snapshot [--label L]    {"snapshot": "sha256:…", "label": L}
session rollback <id|~N|label>  restore; {"snapshot": current, "step": N}
session history                 [{"snapshot", "label", "op", "step", "at"}]
```

Snapshots are memfile undo states keyed by the content hash of the memfile.
Labelled snapshots are also written to `.blender-cli/snapshots/<hash>.blend`
with a metadata sidecar and `index.json`, and survive a process crash.
History marks imported entries `durable: true`; `rollback <id|label>` after
recovery reloads the indexed file, with the newest occurrence of a label winning.
`rollback` never asks; it restores. Every request that changes `Main`
advances `step` and produces a `diff` event carrying the new snapshot.

A setting is a dotted path into the policy and its value is JSON when it parses
as JSON, so `session feedback image.mode=off image.size=128
image.views='["front","persp"]'` sets three of them in one request. Settings
merge into the policy in force; the answer is the whole policy, which is also
what `session status` reports. `session open` is the launcher's own verb —
there is no session to ask yet — and every other action is one request to a
live one.

### `exec`

```
exec -c CODE | exec SCRIPT.py [--no-record] [--timeout S] [--image delta|full|off]
```

Runs the code, then pushes feedback: a `diff` event (added/changed/removed
IDs with the new snapshot and step), a `perception` event, an `objective`
event when targets exist, and `image` events under the feedback policy.
With recording on (the default in a session), the executed code is appended
to the program as the next step. Folded envelope:

```json
{
  "ok": true,
  "stdout": "…", "stderr": "…",
  "value": <repr of the last expression, if any>,
  "diff": {
    "added":   [{"type": "OBJECT", "name": "Cube"}, …],
    "changed": [{"type": "MESH", "name": "Cube", "fields": ["geometry", "copy_on_eval"]}],
    "removed": [],
    "snapshot": "sha256:…", "step": 12
  },
  "perception": {…},
  "objective": {…},
  "images": [{"kind": "delta", "view": "front", "path": "…", "region": [x0, y0, x1, y1]}],
  "ms": 12
}
```

On exception:

```json
{
  "ok": false,
  "error": {
    "type": "AttributeError",
    "message": "'Object' object has no attribute 'locaton'",
    "line": 3,
    "rna": {"struct": "Object", "nearest": ["location", "rotation_euler"], "type": "float[3]"},
    "fix": {"code": "bpy.data.objects[\"Cube\"].location = (1, 0, 0)"}
  },
  "stdout": "…", "stderr": "…"
}
```

A failed `exec` rolls `Main` back to the pre-request snapshot, so a partial
edit never persists and the failed code is never recorded. The namespace
persists across `exec` calls in a session. It is preloaded with `bpy`,
`bmesh`, `mathutils`, `math` and the `agent` helper module.

#### One-shot details

One-shot namespaces are fresh and preload `bpy`, `bmesh`, `mathutils`,
`math` and `agent`; the session contract below preserves one-shot behavior.
`value` is a string containing the final expression's `repr`, or JSON null if
there is no final expression. Both AST pieces compile before either executes.
`ms` measures compilation and execution, excluding file load/save and ID diff.
Python stdout/stderr are captured, including on `BaseException` (for example
`SystemExit`). Error `line` is the innermost user-code line, syntax-error line,
or null for argument/file errors. Native Blender reports go to process stderr.
C++ owns the command entry, GIL/context and response stream; the installed
`scripts/modules/agent_runtime.py` uses the Python C API's compiler/evaluator
and RNA through `bpy`, without wrapping or modifying `bpy`.

`--timeout S` is a positive, finite, cooperative wall-clock deadline checked
at Python trace events and after execution. Native calls cannot be forcibly
interrupted; an overdue call raises `TimeoutError` after returning to Python.
It is not a security sandbox: arbitrary code can disable tracing or terminate
the process. No file is saved after a failed verb.

The launcher locates its sibling from `/proc/self/exe`, `_NSGetExecutablePath`
or `GetModuleFileNameW` (with an `argv[0]` fallback on POSIX), then passes
`--factory-startup --disable-autoexec --command agent`. POSIX replaces itself
with `execv`; Windows uses Unicode `CreateProcessW`, CRT argument quoting and
exit-code propagation. `--file F` requires an existing file and loads without
UI or embedded-script execution. `--save F` creates/writes F; bare `--save`
writes the `--file` path. Neither creates missing parent directories. Without
`--file`, the scene is Blender's factory startup, including its default cube.
To create a cube named `Cube`, start from a saved empty scene or delete the
default object and mesh first. Human output is indented JSON; `--json` is one
compact JSON document. No inspection arrays are truncated.

ID diff compares Main's top-level ID lists by `session_uid`; `type` is the
uppercased upstream ID-type name (for example `OBJECT`, `MESH`, `NODETREE`).
Added/removed entries contain only type/name; surviving tagged IDs are changed.
The initial view layer is evaluated before the boundary. C++ samples original
`ID.recalc` at both boundaries and resets `recalc_after_undo_push` at the start;
the final mask is accumulated tags OR newly pending recalc bits. This retains
explicit update tags even when an operator evaluates and clears `ID.recalc`.
Renames are detected by comparing names and add the `name` group. Entries sort
by type/name. These are real depsgraph update categories, not property names or
byte-level equality: tagging an unchanged value may count, and untagged raw
memory edits do not. Embedded IDs are not separate Main-list entries. Explicit
undo pushes/restores inside arbitrary code can reset the accumulator; there
is no mutation journal across those boundaries.

The exact flag mapping (prefix `ID_RECALC_`) is:

| Flags | Field group |
|---|---|
| `TRANSFORM` | `transform` |
| `GEOMETRY` | `geometry` |
| `ANIMATION` | `animation` |
| `PSYS_REDO`, `PSYS_RESET`, `PSYS_CHILD`, `PSYS_PHYS` | `particles` |
| `SHADING` | `shading` |
| `SELECT` | `selection` |
| `BASE_FLAGS` | `base_flags` |
| `POINT_CACHE` | `point_cache` |
| `EDITORS` | `editors` |
| `SYNC_TO_EVAL` (upstream's current copy-on-evaluation flag) | `copy_on_eval` |
| `SEQUENCER_STRIPS` | `sequencer` |
| `FRAME_CHANGE` | `frame_change` |
| `AUDIO_FPS`, `AUDIO_VOLUME`, `AUDIO_MUTE`, `AUDIO_LISTENER`, `AUDIO` | `audio` |
| `PARAMETERS` | `parameters` |
| `SOURCE` | `source` |
| `TAG_FOR_UNDO` | `undo` |
| `NTREE_OUTPUT` | `node_output` |
| `HIERARCHY` | `hierarchy` |
| `COMPOSITOR` | `compositor` |

Reserved/provision bits do not name field groups. Combined upstream masks map
to each constituent group.

#### Session details

`session open [--file F]` detaches the sibling Blender executable and waits up
to 10 seconds for its local endpoint to accept. POSIX uses `fork`, `setsid`,
`/dev/null` stdin, and append-only `.blender-cli/session.log` stdout/stderr;
Windows uses Unicode `CreateProcessW` with `DETACHED_PROCESS` and redirected
handles. Its `STARTUPINFOEX` handle list includes only NUL and the log, not the
caller's capture pipes, so the detached daemon cannot hold their EOF open.
`.blender-cli/session.pid` records the daemon PID, also used as the
returned session ID. A process-held `.blender-cli/session.lock` serializes
open/forced-close operations. The directory is owner-only on POSIX. Opening
an already-live session fails; a dead PID permits stale socket cleanup. Opening
over a dead session reports `previous_autosave` (absolute path) when its recovery
file exists, and preserves that file. Closing a dead session reports
`{ok: true, stale: true, autosave?: path}`, removes the socket/PID/lock, and
preserves its recovery file.
`session close` does not save. It requests normal loop termination through
the command handler and `WM_exit`; if the daemon cannot answer within two
seconds, the launcher terminates it (SIGTERM then SIGKILL on POSIX,
TerminateProcess on Windows) and reports `forced: true`.

Other CLI calls connect directly when the endpoint accepts. They do not
launch Blender. Only absence of the PID file permits one-shot fallback. A dead
PID returns exit 1 and `{ok: false, error: {type: "SessionError", message: …},
autosave?: path}` for every verb except open/close. The message identifies the
PID, `.blender-cli/session.log`, `session open --file <autosave>` and
`session close`. A live PID with an unavailable endpoint also reports an error,
never edits a different scene. `autosave` is present only for an existing file.
If a connection drops during a request, the launcher waits at most two seconds
for process teardown and returns this same recovery error on the killed request
itself. A still-live process retains the disconnection error instead.
Before executing, `session.log` records the request ID, verb and first line of
each argument (at most 512 characters). For script files it also records the
filename and first code line. Native crash dumps handled by Blender go to
`.blender-cli/session-<pid>.crash.txt`, independent of the live blend filepath;
startup names this path in `session.log`. They include the current request.
Before offscreen rendering releases the GIL, the agent captures the Python
frame chain and appends it on a crash without calling Python from the handler.
Upstream's ordinary Python backtrace is empty while `PyThreadState` is detached;
the captured chain identifies the render entry, not a later native instruction.
SIGKILL and `os._exit` bypass crash handlers and cannot produce a native dump.
Common `--json` and human output
remain compact/indented JSON respectively. Session result objects without an
`ok` field and the history array are successful; `ok: false` exits 1.

A CLI verb reaches the endpoint as the request object *Channel protocol*
defines, byte for byte the same one `repl` and a raw client send; there is no
argv on the wire and no second parser. Script paths are absolute by the time
they leave the launcher, and any other relative path in a request resolves in
the session's original working directory.

The endpoint adds what a socket needs and nothing else. A raw client must use
distinct request IDs across its outstanding requests. Requests queue by
completed-line arrival at the transport reader; simultaneously ready
connections have no cross-connection ordering guarantee, and closing abandons
requests still queued behind the running one. A partial line over 16 MiB
disconnects. The endpoint is local trusted-code access, not a sandbox or a
multi-user authentication boundary.

`cancel` is an ordinary request, normally sent on a **second connection**
while the first is busy (the same connection is also accepted). It is
answered out of order, on the transport thread, with its own `done`:
`{"id": <the cancel's id>, "event": "done", "ok": true, "target": N,
"cancelled": true|false}`, where `cancelled` is false when no request with
that id is running. That answer says the cancel was delivered; how the
running request ends is its own terminal event, under *Channel protocol*.
The transport thread only moves and parses protocol bytes and sets an atomic
flag; Python trace checkpoints on the main thread copy it into `G.is_break`
and raise `Cancelled`. This avoids a data race on upstream's plain Boolean.
Native calls cannot be preempted: cancellation is noticed when they return
to Python. Code that catches the exception or disables tracing is not
forcibly interrupted. A cancelled or failed request takes no snapshot of the
state it abandoned; it is restored to the snapshot it began from, except
where the op's `cancels` outcome is `done` and it keeps what it produced.

The main thread executes one request, pumps `BLI_timer_execute`, and answers.
While idle it pumps timers at roughly 10 ms intervals, releasing the GIL
while waiting for transport. Timers never run Blender API code on a transport
thread. A long-running request delays timers until it returns.

Session and one-shot namespaces both preload `bpy`, `bmesh`, `mathutils`,
`math` and `agent`. `agent.snapshot`, `rollback`, `diff`, `history`,
`program`, `objective` and `fit` require a session, since they read or move
state that only a session keeps; `observe`, `compare`, `perceive` and
`describe` work in both.

Snapshots restore Blender Main data, **not Python variables or external
files**. Reacquire RNA references from `bpy.data` after rollback: saved Python
references into old Main may become invalid. Every successful session exec
adds a history event and a `snapshot` field; `inspect` does not. Initial state
has an `open` event. Manual snapshots have optional labels. `at` is Unix time
in seconds. `agent.diff()` samples the current exec boundary, with the
ID-tag semantics above (explicit undo/snapshot operations can reset accumulated tags).
`--file` loads only at `session open`; `session save --file F` writes without
reloading, and bare save uses the current Blender filepath.

The session maintains `<initial cwd>/.blender-cli/autosave-<pid>.blend` from
the current agent snapshot, not unsnapshotted live edits. Snapshot creation
(including open, successful exec and manual snapshot) and rollback mark it dirty.
The main thread writes after queuing a response if at least five seconds have
elapsed since the last write; otherwise the idle pump writes after one second
without a request. Explicit `session save` does not trigger a snapshot or a
pre-response autosave. A clean close deletes only that session's autosave;
crash files survive recovery and later closes. The most recent completed write
is recoverable; this is not synchronous durability for every acknowledged edit.
Write failures are logged and retried, without turning a successful exec into a
failed response.

Linux orb measurements (Release, xPack GCC 14.3.0, five writes each; elapsed
decode + user-count rebuild + compressed write/rename + isolated-Main cleanup):

| Snapshot | Samples (ms) | Median (ms) | Autosave bytes |
|---|---|---|---|
| Factory cube scene | 7.565, 7.200, 6.947, 9.996, 9.255 | 7.565 | 75,182 |
| `primitive_grid_add(x_subdivisions=1000, y_subdivisions=1000)` — 1,002,001 vertices | 88.886, 85.532, 86.020, 90.414, 85.068 | 86.020 | 31,807,615 |

An 86 ms main-thread write can delay the next queued round trip meaningfully.
The measured policy therefore increases the busy-session interval from the
initial two seconds to five (roughly 4.3% → 1.7% write duty at this mesh size),
while retaining the one-second idle trigger. This reduces write frequency, not
the maximum single-write stall; continuously busy sessions trade up to five
seconds of unpersisted work for less interference. Large linked assets or slower
storage can cost more. Each actual write logs its elapsed cost in `session.log`.

Upstream 5.3 no longer supplies `BLO_memfile_write_file`: memfiles contain
out-of-stream shared arrays and cannot be dumped as blend files. The agent
decodes its retained memfile into an isolated Main using an empty old Main and
`BLO_READ_SKIP_UNDO_OLD_MAIN`, recomputes user counts, then uses upstream's
`BLO_write_file` recovery/compression path. It never saves through an operator,
replaces live Main, or changes live filepath/dirty state or Python references.
External paths are made absolute in the isolated write so ordinary `--file`
loading from the recovery directory still finds the snapshot's assets.
The adjacent `autosave-<pid>.json` records the snapshot's original `filepath`
and `dirty` state. Session recovery restores those values after loading, before
creating its initial snapshot; an originally unsaved session still requires
`session save --file`, rather than overwriting its recovery file. Preserve this
sidecar together with the blend file. Clean close removes both files for the
current session only. One-shot loading remains ordinary file loading.
The active snapshot scene is placed first for loading without saved UI.
Non-memfile-undo data (UI and brushes) is not recovered; linked libraries are
reloaded from their files, and ordinary blend-save orphan rules apply. Python
variables and unlabelled history do not survive a process crash. Both `session open
--file` and one-shot `--file` can load the autosave.

A request that omits a required field, names a field its op does not declare,
or gives one a value outside its declared type or enum is rejected before any
of it runs, by the request table itself. That is a contract violation, not a
value error: `session` without an action returns `ProtocolError` in both
modes, with the message the table generates,
`session requires action: status|feedback|save|close|snapshot|rollback|history`.

The content-addressed store retains independent upstream memfiles, not a
linear undo tail: rolling back then executing does not delete the former
future. SHA-256 hashes the concatenated `MemFileChunk` buffers, including
upstream's pointer-based references to retained immutable shared storage.
These are process-local memfile identities, **not canonical geometry hashes**
and not comparable across processes. Duplicate hashes reuse stored data but
still append history events. Oldest-created unique hashes are evicted at a
256 MiB budget measured by upstream `MemFileUndoData.undo_size`; this includes
upstream's approximate shared-storage accounting and is not a hard RSS limit.
A single larger snapshot fails with `MemoryError`; its scene mutations are
not automatically reversed. History preserves events for evicted hashes;
rollback to one raises `KeyError`. `~N` selects an earlier history event
relative to the current snapshot; `~0` restores the current snapshot.

Labelled snapshots are additionally synchronous durable checkpoints. Both
`session snapshot --label X` and `agent.snapshot("X")` write the retained memfile
through the same isolated decode/write path to
`<initial cwd>/.blender-cli/snapshots/<hash-without-sha256-prefix>.blend` (and its
metadata sidecar), then atomically replace `snapshots/index.json`. The index is
an array with `id`, `label`, `parent`, `created` (Unix seconds), `bytes` (blend
file size), original `filepath` and `dirty`. A response acknowledges the label
only after both serialization and index replacement succeed. A failed write
leaves the prior index intact; an unindexed file may remain for manual cleanup.
This is process-crash durability, not a guarantee against power/storage loss.

Opening a session in the same directory imports these entries into history
with `durable: true`, before its new `open` event. Labelling again appends an
event; the newest occurrence of a label wins, while older IDs remain reachable.
Labels and disk files are not evicted by the in-memory budget. Rollback first
uses a retained memfile, or loads an indexed checkpoint through the normal
Main-replacement file path, restores its filepath/dirty state, and seeds a new
memfile chain. A disk rollback returns a new process-local snapshot ID and
appends a `rollback` event whose `parent` is the durable ID. Durable IDs identify
stored files across sessions, not canonical geometry hashes. `session close`,
including stale close, never removes the index or these files. The snapshots
directory belongs to the agent to clean; no deletion verb or close flag is added.

Long-lived render measurements on Linux (Mesa 25.0.7 lavapipe, concurrent
stress workloads; times are not isolated latency benchmarks):

| Path | Completed renders | Wall time | Process memory maps |
|---|---|---|---|
| Unpatched 512px helper loop | 35; crashed on 36 | 147.54 s including crash | 3,093 after render 1 → 64,644 after 35; limit 65,530 |
| Unpatched 128px observation pipeline | 35; crashed on 36 | 88.5 s through render 35 | 3,093 → 64,636 |
| Fixed 512px `agent.observe` loop, one exec | 300 | 1,863.62 s; render 200 at 1,276.37 s | 1,302–1,543 after warmup |
| Fixed 512px CLI `observe` round trips | 300 | 1,449.46 s | 1,301–1,542 after warmup |
| Explicit OpenGL backend, 512px helper loop | 50 | 403.29 s | 863 after render 1, 868 after 50 |

The failure was descriptor-set buffer mappings retained by the Vulkan thread's
active descriptor pool, not leaked disposable scenes or render results.
Respecting the declared 250-set capacity reaches the existing timeline-safe
pool reset, which frees those sets and mappings. The remaining partial pool
is bounded; its map count oscillates rather than growing with render count.
Warm RSS (renders 50–300) was 1,131,400–1,257,988 KiB for the helper and
1,136,988–1,224,832 KiB for CLI calls. Both sessions answered a subsequent exec
and closed normally. The regression uses 120 real 128px observation-pipeline
renders, since the unpatched map growth is the same ~1,810 per render at both
sizes. These measurements do not establish native macOS or Windows behavior.

`WM_init` already registers undo types, including in background mode;
`wm_file_read_post` skips stack initialization there. The agent initializes
`wm->runtime->undo_stack` with `BKE_undosys_stack_create`,
`BKE_undosys_stack_init_from_main`, and `_from_context` when needed. Each
agent snapshot also updates upstream's stack (two steps, 32 MiB accounting
target, upstream retains the minimum needed steps). Agent-owned memfiles
are independent of it, so operator undo cannot invalidate stored hashes.
Rollback decodes with old-Main reuse disabled, tears down/reinitializes editor
data, and resets the WM undo stack to the restored Main. No upstream file
changes are needed.

### `program`

```
program get                        {"text", "params", "steps", "version", "base",
                                    "record", "digest", "reproducible"}
program set   --text @model.py     replace the program and re-execute from the first change
program patch --old OLD --new NEW  replace text matching exactly once, then re-execute
program run                        re-execute from the longest cached prefix
program history                    {"versions": [{"version", "parent", "label", "at",
                                    "steps", "reproducible"}], "current"}
program rollback <version|label>   check out a version and re-execute
program record on|off              stop or resume recording executed statements
```

The program is the record of the scene and *Program model* above defines it:
what a step is, how a prefix is cached, when a version is written, and what
`reproducible` means. Only the projection is here. `--text` and the two patch
sides are text, so `@FILE` and `-` read them from a file or stdin rather than
from a shell word. `--label L` names the version that `set`, `patch`, `run` or
`rollback` creates. Those four change `Main`, so they answer with the same
`diff`, `perception`, `objective` and `image` events an `exec` does, and with
`ran`, `cached`, `from_step` and the content `digest` that proves a prefix-cached
re-execution landed where a full run would.

### `inspect`

```
inspect [--object NAME] [--full] [--select PATH…]
```

Emits scene state from RNA: objects (type, transform, bounds, parent,
modifiers, materials, vertex/edge/face counts, UV layers), materials
(node tree summary), armatures (bones), cameras, lights, collections.
`--full` expands node trees and modifier settings. `--select` takes RNA
paths for a targeted read. Never truncated.

The response is `{ok, scene, objects, materials, armatures, cameras,
lights, collections}`. Object `type` is RNA's object type (`MESH`, not ID type
`OBJECT`); `mesh` contains `vertices`, `edges`, `faces` counts and UV layer names.
Transforms include all rotation representations, world matrix and dimensions;
`bounds` are the eight local-space bounding-box corners. Parent/data/material
references use names. `--object` filters the objects array only; other datablock
arrays still describe the file. Node-tree summaries include node names/types
and a link count; `--full` supplies links with socket identifiers, socket default
values, and all RNA node/modifier settings. Pointer settings use typed name
references rather than recursively following cyclic graphs. Arrays/collections
are complete; non-finite RNA floats are strings (`nan`, `inf`, `-inf`) so JSON
remains valid. `--select` paths resolve relative to `bpy.data` (not `eval`) and
replace the scene response with `{ok: true, selected: {path: value, ...}}`.
Resolution failures retain `ValueError` and explain the base, for example
`--select "location" could not be resolved relative to bpy.data; use a path
such as objects["Cube"].location`.

### `observe`

```
observe [--views V,…] [--passes P,…] [--size 512|768|1024] [--ref IMG] [--layout sheet|separate] [--frame OBJECT]
```

- Views: `front back left right top bottom persp camera`. Default:
  `front,persp`.
- Passes: `color wire silhouette normal depth`. Default: `color`.
- Lighting is a built-in three-point rig and a neutral world; view transform
  is Standard; the resolution ladder is fixed. Nothing about the render
  depends on GUI, viewport or user preferences.
- `--ref` places the reference image beside the first view, and `--ref
  --overlay` blends it at 50 %.
- Default output is one contact sheet; `separate` writes one file per view
  and pass.

Result: `{"image": path, "views": [...], "passes": [...], "size": [w, h]}`.

#### Observation details

The full `RE_RenderFrame` EEVEE pipeline is the primary path, rather than
`ED_view3d_draw_offscreen_imbuf_simple`: it supplies native Combined, Normal
and Depth passes without borrowing viewport shading, camera state or GPU
selection buffers. The engine owns lazy `WM_init_gpu_offscreen` and its GPU
context lifecycle; the agent must not initialize that one-shot API again.
C++ copies the render-pass float buffers and frees the render. It does not
invoke the render operator, create a Render Result image, update the user's
scene frame or pause any viewport.

Observation builds a disposable scene from evaluated geometry and instance
world transforms of the current view layer, excluding hidden-render objects,
cameras and lights. Mesh-convertible objects are frozen to evaluated meshes;
other geometry data is copied. Existing materials are retained for color.
Mesh instances copy evaluated `object.data`, including its material slots:
`new_from_object(..., preserve_all_data_layers=True)` can re-evaluate a temporary
Geometry Nodes object's instancer instead of its instance mesh. Framing uses
the vertices of the actual converted/copied geometry, transformed by the
instance matrix, for all objects; only data without vertices falls back to
evaluated bounds. Modifiers and curve tessellation therefore frame as rendered.
The original scene's camera, world, render/color settings and object data are
not edited. All temporary IDs are removed even on failure. Render, frame and
depsgraph Python callbacks are suspended and restored, so observation does
not run user code that could mutate Main. Observation is not a snapshot or
rollback operation and does not invalidate the agent's RNA references.
Temporary ID creation/deletion invalidates unrelated dependency tags in
upstream. The observation boundary preserves and restores the pending recalc
masks of both top-level and embedded IDs (notably the scene master collection),
after completing deferred view-layer synchronization/evaluation. It preserves
real edits made before `agent.observe()` rather than blindly clearing tags.
Sessions evaluate at open and exec completion so snapshots describe settled
geometry; no memfile hash normalization or cached-snapshot substitution is used.

Axis cameras look toward the bounds center: front from −Y, back +Y, left −X,
right +X, top +Z, bottom −Z; `side` aliases **right**. All six are orthographic.
`persp` looks from normalized (1, −1, 0.8), azimuth −45° and elevation
approximately 29.5°, with a 50 mm lens and 36 mm sensor. Auto-framing uses
world-space geometry bounds with occupancy `1 / 1.1 = 0.9090909090909091`
(the longest orthographic extent, before pixel rounding); perspective fits their
bounding sphere. `--frame OBJECT` selects bounds, not rendered membership.
An empty scene uses bounds [−1, 1]³ and renders background. `camera` copies the
evaluated `scene.camera` projection/transform, disables depth of field, and
errors with `ValueError` when no camera is assigned.

Lighting is a built-in three-SUN key/fill/rim rig: directions (−3, −4, 6),
(4, −1, 2), (1, 4, 5), energies 3/1/2, 10° angular size, white light. This is
scale-independent and uses no preference studio lights. The neutral world
has linear RGB 0.05, strength 1; transparent film is composited over linear
RGB 0.035. EEVEE uses 32 render samples with a fresh sampling sequence starting
at sample zero (there is no user EEVEE seed), no compositor, sequencer, stamps
or dithering. The temporary scene uses the source frame/subframe so animated
materials are evaluated at the observed time, without advancing the source.
Standard/sRGB, exposure 0, gamma 1 and no look are fixed. Color is converted
from linear Combined with the standard sRGB transfer function and clamped to
RGB8; data passes bypass that transfer. Tile size is 512 by default, or
`--size 768|1024`.

Exact pass definitions (all RGB8):

| Pass | Definition |
|---|---|
| `color` | Antialiased EEVEE Combined, with the fixed lighting/background and Standard transfer above. |
| `wire` | Color darkened by up to 90% at evaluated triangle edges. A second EEVEE material-override render uses the upstream Wireframe shader, pixel size 1; its antialiased coverage supplies the overlay mask. This is a diagnostic tessellation wire, not original polygon-edge topology. |
| `silhouette` | Binary white (255) where native Depth is inside the camera far clip and Combined alpha ≥ 0.5; black (0) otherwise. No intermediate gray/antialiasing survives. Transparent surfaces follow this coverage rule, not a semantic object-ID mask. |
| `normal` | Native EEVEE world-space shading normal mapped componentwise by 0.5n + 0.5; black outside the silhouette. Normal/depth use EEVEE's nearest-to-pixel-center data sample, not color's antialiasing average. |
| `depth` | Camera depth d mapped to clamp(1 − (d − near)/(far − near), 0, 1), repeated in RGB; near/far are the min/max depths of the framing geometry points (range at least 0.001). Depth is axial, or radial for a panoramic `camera`, matching EEVEE. Near is white, far/background black; the silhouette masks background. |

Sheet rows follow requested **view order**, columns **pass order**. Each tile
has a 2-pixel RGB (32,32,32) border on every side: dimensions are
`passes × (size+4)` by `views × (size+4)`. `--ref IMG` adds a rightmost column
beside the first view row; the reference is bilinearly resized to tile height
with aspect preserved and the same border, leaving lower rows blank.
`--ref IMG --overlay` instead fits the reference inside the first view/first
pass tile, preserves aspect, centers it and blends at 50% in display space.

`--layout separate` produces one bordered PNG per view×pass, in that order;
only the first gets a reference/overlay. The result additionally has
`images: [{view, pass, image, size}, ...]`, with the first image also exposed
at the top level. Default session outputs are content-addressed files under
`<cwd>/.blender-cli/observe/`; one-shot outputs use a new system temporary
directory. `--out PATH` chooses a sheet filename, or a directory for separate
layout. `--inline` writes no files and substitutes a `base64` string for the
sheet's `image` path. It is mutually exclusive with `--out` and requires sheet
layout: separate layout writes files, never multiple image payloads across the
one-image boundary. All results include `ok: true`,
requested views/passes and actual output dimensions. Feedback `image` events
and `agent.observe(views, passes, size, ref)` use the same implementation;
the helper returns the dict directly.

Every observation also returns `framing: {bounds: {low: [x,y,z], high: [x,y,z]},
center: [x,y,z], radius: number, objects: [name,...], occupancy: number}`.
Bounds/center/radius are world-space, radius is half the bounds diagonal
(minimum 0.01), and contributing original object names are sorted and unique.
Empty scenes report the fallback bounds and no objects. Occupancy is the preset
fit constant, not a measured pixel fraction; perspective, scene cameras,
asymmetric geometry and raster rounding need not attain it.

PNG output has only IHDR, IDAT and IEND chunks, RGB8, filter 0 and zlib level 9:
no timestamps, metadata hashes, paths or render timing. Byte determinism is
scoped to the same scene state, same build, same platform, same Mesa/driver
(or product-platform GPU driver), and the same observation arguments. It is
not a cross-driver floating-point equivalence claim. Metal/macOS and
real-GPU Vulkan/Windows require their own platform runs; Linux software
Vulkan evidence cannot establish either.

### `target`

```
target set NAME --ref IMG [--view V] [--mask auto|none] [--fit bbox|none] [--metrics M,…]
target list
target clear [NAME]
```

A target binds a reference image to a preset view. While targets exist, every
state-changing request ends with an `objective` event scoring each target at
the feedback size; `fit` optimises against them. Metrics: `iou` (silhouette
intersection-over-union), `chamfer` (edge distance, pixels), `ssim`, `hist`
(color-histogram distance); the default is `iou`, and the first one listed is
the target's primary metric. `--mask auto` removes the reference
background with classic CV before comparison. There is no comparison verb:
the same computation is `agent.compare(ref, view, metrics=…)` inside `exec`,
returning
`{"view": "front", "reference": {…}, "iou": 0.83, "chamfer": 4.2, "ssim": 0.71, "hist": 0.12}`,
and the `objective` event carries it for every target.

A target name matches `[A-Za-z0-9][A-Za-z0-9._-]*`, because it is also a
directory name. `target set` copies the reference into
`.blender-cli/targets/<name>/reference.<ext>`, so moving or deleting the
original never changes what the session is fitting, and writes the
preprocessed binary silhouette beside it as `silhouette.png` and the record
as `target.json`. Setting an existing name replaces it and forgets its
previous and best values. A session reads every stored target at open, so a
target outlives a crash. `done`:

```
target set   {"ok": true, "name": "front", "ref": "…/reference.png", "view": "front",
              "mask": "auto", "fit": "bbox", "metrics": ["iou"],
              "reference": {"bbox": [x0,y0,x1,y1], "occupancy": 0.9, "fit": "bbox"},
              "silhouette": "…/silhouette.png",
              "objective": {"targets": {…}, "best": {…}}}
target list  {"ok": true, "targets": [ …one set record without `silhouette`… ]}
target clear {"ok": true, "cleared": ["front"]}
```

`target clear` without a name clears every target; with an unknown name it is
a `KeyError`, not a silent success. `reference` describes the preprocessed
reference at the feedback size, in that tile's pixels. `target` changes no
scene data, so no `objective` event follows it; instead `target set` carries
the first scoring in its `done` as `objective`, in the event's shape, so
registering a target already answers how far the scene is from it without a
second request.

#### Metric details

`agent.compare` accepts `size=512, frame=None, debug=False, fit="bbox",
mask="auto"`, `frame` meaning bounds, not timeline frame, exactly as observe;
`debug=True` selects a new temporary directory, or a path chooses the
directory. The debug directory is a helper argument, not a request field, so
it has no flag. Only requested
metric keys, `view`, and `reference: {bbox: [x0,y0,x1,y1], occupancy: number,
fit: "bbox"|"none"}` are returned (CLI adds `ok`). The bbox is the final
foreground's tile-pixel bounds, top-left origin and exclusive high coordinates;
occupancy is its longest extent divided by tile size. An empty foreground has
`bbox: null, occupancy: 0`. Debug alone adds
`debug: {reference_silhouette: path}` for an unbordered binary RGB PNG. Normal
comparison does not encode, write or return any image, even within `exec`.
Each call freshly loads the reference and freshly evaluates/renders the scene;
there is no stale image, scene, RNA-pointer or file-mtime cache.

Reference loading uses `bpy.data.images` / ImBuf and compiled-in codecs,
including PNG, JPEG and WebP. Byte buffers are straight display-referred sRGB;
float buffers are unpremultiplied, converted from linear to sRGB and clamped.
Alpha is resampled with premultiplied **display** RGB to avoid transparent-color
bleeding, then unpremultiplied for segmentation. Reference image datablocks and
render data are disposed under the observation recalc/callback preservation boundary.

A square 516/772/1028 image whose entire two-pixel opaque outer border is
RGB(32,32,32) is recognized as a single observe tile and cropped by two pixels.
No other border is removed; multi-view/pass sheets should be cropped by the
caller. This makes observe→compare self-consistency independent of sheet chrome.
Then pixel-center bilinear interpolation aspect-fits and centers the image in
the chosen tile, just like the observe overlay. Scaled dimensions are nearest
integers (Python round, minimum 1); odd padding puts the extra pixel at right
or bottom. Padding is background, never foreground. `--fit none` stops here,
preserving reference framing (use it for exact observe self-comparisons or
deliberately fixed scene-camera framing).

Default `--fit bbox` / `fit="bbox"` segments the reference first, crops RGB and
silhouette together to the foreground bounding box, then uniformly resizes
and centers them at observe's `OCCUPANCY = 1 / 1.1` (0.9090909090909091).
RGB and mask use pixel-center bilinear resizing; the mask is thresholded at
0.5. Dimensions round to nearest integers, so a 512 tile targets 465 pixels
while actual observation coverage can be 466 pixels. Empty foreground stays
empty. This removes reference margins, not viewpoint, perspective or shape
differences; it does not optimize against the rendered silhouette.

`mask=auto` estimates the background by componentwise median RGB of the fitted
image's four outer rows/columns (before padding). Let border RGB distances from
that median have median m and median absolute deviation d. Foreground is RGB
Euclidean distance > max(0.08, m + 6d), on channels normalized to [0,1]. If any
alpha is < 254/255, alpha ≥ 0.5 takes precedence. A 3×3 square opening followed
by closing, with edge-replicated padding, removes isolated noise and closes
one-pixel gaps. All surviving components are kept; large holes are retained,
not filled, because an object can have real holes in its rendered silhouette.
This is deterministic classic CV, not semantic segmentation: textured borders,
foreground touching most of the border, background-colored objects, and thin
features can defeat it. Debug exposes that uncertainty instead of hiding it.

`mask=none` uses alpha ≥ 0.5 when the loaded image has an alpha channel (even
fully opaque alpha); otherwise display grayscale ≥ 0.5. It does no morphology.
Grayscale is 0.2126R + 0.7152G + 0.0722B. RGB under alpha is composited onto
the observe display background (rounded sRGB(0.035), 53/255). SSIM replaces
pixels outside each image's own foreground mask with this same background;
histograms exclude those pixels. Thus a removed colored background does not
dominate either appearance metric.

Let A and B be reference and exact observe render silhouettes:

- **IoU** = |A ∩ B| / |A ∪ B|; both empty gives 1.
- **Chamfer** uses inner four-connected silhouette boundaries E(A), E(B),
  including foreground at tile edges (outside the tile is background).
  D(E,p) = min over q in E of |px−qx| + |py−qy|, an exact city-block distance
  transform computed by separable forward/backward minimum-prefix scans.
  Score = ½(mean over p in E(A) of D(E(B),p) + mean over p in E(B) of D(E(A),p)).
  Units are pixels, not normalized by size. Both empty gives 0; exactly one
  empty gives the tile's maximum city-block distance, 2(size−1).
- **SSIM** is the full-tile mean of
  ((2μxμy+C1)(2σxy+C2))/((μx²+μy²+C1)(σx²+σy²+C2)), with population moments
  from a uniform 7×7 window, reflect padding, L=1, K1=0.01, K2=0.03,
  C1=(K1L)², C2=(K2L)². Float64 integral images compute window means;
  roundoff-negative variances clamp to zero, final score to [−1,1].
- **Hist** is 1 − Σ min(hA,hB), a joint 16×16×16-bin display-sRGB histogram
  with uniform channel bins [k/16,(k+1)/16), inclusive at 1 in the last bin.
  Each image uses its own foreground-only probability histogram. Both empty
  gives 0; one empty gives 1. Range [0,1]; smaller is better, as for Chamfer.

Pixel work uses shipped NumPy, not Python pixel loops or a new dependency.
Distance transforms and SSIM are linear in tile pixel count. The small fixed
morphology stencil iterates nine vectorized arrays. Performance evidence and
real fit-loop timings are recorded in `PLAN.md`; rendering is the intended
dominant cost. Automatic framing removes uniform scale, so fit loops must vary
an identifiable parameter (e.g. cube X scale with fixed Z extent), use a fixed
`frame` object's bounds, or use the `camera` view.

### `fit`

```
fit --params JSON [--objective JSON] [--budget JSON] [--method coordinate|nelder-mead|random]
```

*Targets and `fit`* above defines the search: what a parameter is, what an
objective may be, how a budget bounds it, what `progress` carries and what
cancellation keeps. Only the projection is here. The three structured fields
are JSON, so `--params @params.json` reads one from a file:

```sh
blender-cli fit --params '[{"name": "handle_x", "min": 0.2, "max": 0.6}]' \
                --objective '{"target": "front", "metric": "iou"}' \
                --budget '{"evals": 40}' --json
```

### `describe`

```
describe bpy.ops.mesh.bevel | describe bpy.types.Object.location | describe Modifier
describe agent.compare | describe channel | describe schema
```

Answers from live RNA: signature, properties with types, ranges, enum items
and descriptions, and for operators the poll requirements the synthetic
context satisfies.

#### RNA details

`describe` and `agent.describe(path)` resolve public attributes and literal
string/integer subscripts, never calls or arbitrary expressions. A bare name
is relative to `bpy.types` (except `agent`). Instance paths return the instance's live struct.
Results have `kind: property|struct|operator|module|function`; CLI adds `ok: true`.
`describe agent.compare` and other public helpers use Python `inspect.signature`
and docstrings: `{kind: "function", signature: "agent.compare(...)", doc: string,
parameters: [{name: string, default: string|null}, ...]}`. Defaults are Python
repr strings; null means a required parameter. `describe agent` returns
`{kind: "module", path: "agent", doc: string, functions: {name: function_record}}`.
Helpers added by other workstreams appear there as soon as they exist; the
record comes from `inspect.signature`, never from a hand-written list.
Unresolvable paths raise `ValueError` naming the supported `bpy.*` and
`agent.*` roots, the `channel` and `schema` registries, and the supplied
path. CLI describe errors never include internal `line` fields.
Structs include `struct`, `description`, `base` (nullable), and `properties`
keyed by identifier. Property records include identifier, description, lowercase
type, subtype, animatable, readonly, and where applicable array_length, default,
hard_min/max, soft_min/max, fixed_type, and enum_items (identifier/name/description).
Operators add path, keyword signature, context note and actual `poll()` boolean;
`poll_reason` is present only when upstream supplies a failure message. Polls
run in the adopted synthetic context, not an invented edit mode. Module results
map every operator name to its one-line RNA description. Neither JSON nor
indented human output truncates properties or enum items.

Exec errors retain their original type/message/line. Attribute failures on RNA
instances/types and operator modules add `error.rna.struct` and up to five
`nearest` identifiers, ranked by Python difflib SequenceMatcher similarity
(cutoff 0.6, sorted candidate names for deterministic ties). Candidates come
from live properties/functions or module operators. If the struct has no near
match, its `data` pointer's live RNA properties/functions are searched one hop
and hints are prefixed with `data.`, e.g. `data.bevel_depth` on a curve Object.
A nearest property on the original struct also
supplies compact type, e.g. `float[3]`. Property assignment TypeError/ValueError
adds the live property record (including enum descriptions and numeric ranges).
Operator argument failures add the operator struct's complete valid properties.
The failing bytecode's source position identifies the receiver in the original
traceback locals, including semicolon-separated statements; calls are never
replayed. If no RNA receiver can be recovered, `rna` is absent, not empty.
Blender silently clamps many numeric assignments; these remain successful
upstream operations, not synthetic errors. Descriptions still expose their ranges.
That same source position is what `fix.code` rewrites, so a correction is a
byte-level edit of the submitted text, not a regenerated statement.
`agent_rna.error_fields(error, code, filename)` returns the fields this module
contributes — `rna`, `fix`, or neither — and the runtime merges them into the
error object. It never raises and never replaces `type`, `message` or `line`.

## The `agent` helper module

Preloaded into every `exec` namespace. The surface is:

```python
agent.observe(views=("front",), passes=("color",), size=512, ref=None, frame=None) -> {"image": path, "framing": …}
agent.compare(ref, view, metrics=("iou",), mask="auto", size=512, frame=None, debug=False, fit="bbox") -> {"view": …, "reference": …, "iou": …}
agent.perceive(view="front", size=256) -> perception dict (same shape as the event)
agent.objective() -> objective dict for all targets (same shape as the event)
agent.fit(params, objective=None, budget=None, method="coordinate") -> {"best": …, "params": …, "evals": …}
agent.describe(path) -> {"kind": …, ...}
agent.snapshot(label=None) -> "sha256:…"
agent.rollback(snapshot_id) -> None
agent.diff() -> {"added": …, "changed": …, "removed": …}   # since last exec boundary
agent.history() -> [{"snapshot": …, "label": …, "op": …, "step": …, "at": …}, …]
agent.program() -> {"text": …, "params": {…}, "steps": N, "version": "sha256:…"}
agent.register_provider(provider) -> None
```

Every helper returns the same dict its event or request carries; there is
no helper-only shape.

## Synthetic context

Many `bpy.ops` operators poll for a window, screen, `VIEW_3D` area and
region. Background startup does not provide a reliable active UI area.
The process adopts the first loaded window's active screen and its first
`VIEW_3D` area/`WINDOW` region. Background factory startup and file loading
already provide these data; constructing a duplicate hierarchy unconditionally
would change files unnecessarily. If missing, the agent allocates a data-only
window with `wm_window_new`, a workspace/layout with `BKE_workspace_add` /
`ED_workspace_layout_add`, and space/regions through the upstream space-type
constructor. It never calls `WM_window_open` (which tries to create a native
GHOST window) or initializes screen drawing.
`ED_screen_refresh`'s background-only branch installs the screen context
callback when missing; area/region type callbacks are resolved without drawing.
This is required for `selected_objects`, not just operator polls, particularly
after restoring a newly created layout.

C++ resolves the hierarchy at each `exec`, at session startup and after
memfile rollback. No window, screen, area or region pointer is cached across
Main replacement. Calls made by user code that replace Main follow upstream
context semantics within that call; the next exec reestablishes the default.
Screen-coordinate `view3d.select`, `select_box`, `select_circle`, and
`select_lasso` polls explicitly explain that GPU viewport selection is
unavailable in the undrawn context. Mesh/data selection remains upstream bpy.

Every exec boundary, including a failed exec, calls `ED_editors_flush_edits`:
edit mode stays active, while mesh RNA, inspection, snapshots and save see
current geometry. This uses upstream's editmode-load operation, not a forced
mode switch, and supports continuing an edit across requests.

## Wire protocol (session)

The session socket speaks exactly the channel protocol: JSON lines over
`AF_UNIX`, request objects in, event objects out, in request order. There
is no second wire shape; the `repl` bridge and one-shot verbs are clients of
this socket.

## What is deliberately absent

- No asset library, generation or download verbs. Files arrive; `exec`
  imports them.
- No curated operator wrappers (`gameready`, `rig`, `retarget`). The agent
  writes code; recipes belong in the agent's own skill documents.
- No typed tool catalog derived from RNA. `describe` serves RNA on demand
  instead of enumerating it; `describe schema` projects the request set,
  never `bpy`.
- No comparison verb. Comparison is an objective pushed after every action.
- No confirmation, dry-run or preview step. Rollback is the control.
- No MCP, HTTP or add-on socket. See `AGENTS.md`.
