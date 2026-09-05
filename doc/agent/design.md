# blender-cli design

Owner of: the process model, the six verbs, their wire shapes, the session
protocol, observation determinism and the comparison metrics. Constraints
are in `AGENTS.md`; status is in `PLAN.md`.

## Why this shape

An agent modelling from a reference image runs one loop: look at the scene,
write `bpy` code, execute it, look at the result, compare with the
reference, repeat. Every layer between "write code" and "look at the result"
is cost. `blender-mcp` pays for an MCP server, a socket, a GUI Blender, an
add-on timer polling every 50 ms, and a viewport screenshot that depends on
GUI state. blender-cli removes all of it:

```
agent ──(code)──▶ one process: Python namespace + Main + eval + offscreen
                  render + metrics ──(JSON + one image)──▶ agent
```

Two further cuts shorten the loop more than anything else:

1. **Quantitative comparison inside the process.** Whether a silhouette lines
   up is a number, not a judgment. `compare` and its in-process twin
   `agent.compare()` let a script fit parameters numerically in one round
   trip; the agent's eyes are used for qualitative acceptance only.
2. **Self-description and RNA-aware errors.** The most common wasted round
   trip is a hallucinated `bpy` identifier. `describe` answers from live RNA,
   and an error carries the nearest valid names.

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

- **One-shot**: `blender-cli <verb> … [--file scene.blend] [--save]`.
  Loads, runs, optionally writes, exits. State lives in the `.blend` file.
- **Session**: `blender-cli session open [--file scene.blend]` starts a
  daemon bound to `<cwd>/.blender-cli/session.sock`. Any verb run in that
  directory connects to it; without a session the verb runs one-shot. State
  lives in the process; `session save` writes the `.blend`.

Both modes run the same registry; the daemon adds only the endpoint and the
persistent namespace.

## The six verbs

All verbs take `--json` to emit exactly one JSON document on stdout; human
output is the default. Images are files whose paths appear in the JSON, or
inline base64 with `--inline`. Exit code 0 is success; non-zero carries an
`error` object.

### `session`

```
session open  [--file F]        start daemon for cwd; answers {"session": id, "socket": path}
session save  [--file F]        write the .blend
session close                   write nothing, stop the daemon
session snapshot [--label L]    {"snapshot": "sha256:…", "label": L}
session rollback <id|~N>        restore; {"snapshot": current}
session history                 [{"snapshot", "label", "verb", "at"}]
```

Snapshots are memfile undo states keyed by the content hash of the memfile.
`rollback` never asks; it restores.

### `exec`

```
exec -c CODE | exec FILE.py [--observe VIEWS] [--timeout S]
```

Result:

```json
{
  "ok": true,
  "stdout": "…", "stderr": "…",
  "value": <repr of the last expression, if any>,
  "diff": {
    "added":   [{"type": "OBJECT", "name": "Cube"}, …],
    "changed": [{"type": "MESH", "name": "Cube", "fields": ["geometry", "copy_on_eval"]}],
    "removed": []
  },
  "snapshot": "sha256:…",
  "observe": {"image": "path.png", "views": ["front", "persp"]},
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
    "rna": {"struct": "Object", "nearest": ["location", "rotation_euler"], "type": "float[3]"}
  },
  "stdout": "…", "stderr": "…"
}
```

The namespace persists across `exec` calls in a session. It is preloaded
with `bpy`, `bmesh`, `mathutils`, `math` and the `agent` helper module.

#### Phase 1 one-shot contract

At the Phase 1 boundary, only `exec` and `inspect` were implemented; the other four verbs answered
`NotImplemented` and exit 1. Session snapshots, observations and RNA error
suggestions are absent, not placeholder fields. One-shot namespaces are fresh
and preload `bpy`, `bmesh`, `mathutils`, `math`; `agent` arrives with Phase 2.
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
undo pushes/restores inside arbitrary code can reset the accumulator; Phase 1
does not promise a mutation journal across those boundaries.

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

#### Phase 2 session contract

`session open [--file F]` detaches the sibling Blender executable and waits up
to 10 seconds for its local endpoint to accept. POSIX uses `fork`, `setsid`,
`/dev/null` stdin, and append-only `.blender-cli/session.log` stdout/stderr;
Windows uses Unicode `CreateProcessW` with `DETACHED_PROCESS` and redirected
handles. `.blender-cli/session.pid` records the daemon PID, also used as the
returned session ID. A process-held `.blender-cli/session.lock` serializes
open/forced-close operations. The directory is owner-only on POSIX. Opening
an already-live session fails; a dead PID permits stale socket cleanup.
`session close` does not save. It requests normal loop termination through
the command handler and `WM_exit`; if the daemon cannot answer within two
seconds, the launcher terminates it (SIGTERM then SIGKILL on POSIX,
TerminateProcess on Windows) and reports `forced: true`.

Other CLI calls connect directly when the endpoint accepts. They do not
launch Blender. A missing/dead endpoint falls back to the original one-shot
invocation; a live PID with an unavailable endpoint reports an error rather
than silently editing a different scene. Common `--json` and human output
remain compact/indented JSON respectively. Session result objects without an
`ok` field and the history array are successful; `ok: false` exits 1.

The wire mapping is deliberately argv-based: `verb` is the first CLI word;
`args.argv` is the remaining string array, without shell re-parsing. Script
paths resolve in the session's original working directory. Thus Python's
existing argument parser is the source of truth in both modes. Raw clients
must use distinct numeric request IDs across outstanding requests in the
session. Each line has one matching response. Requests queue by completed-line
arrival at the transport reader; simultaneously ready connections have no
cross-connection ordering guarantee. Closing abandons later queued requests.
Partial lines over 16 MiB disconnect. The endpoint is local trusted-code
access, not a sandbox or a multi-user authentication boundary.

Cancellation is an out-of-band line on a **second connection** with the
running request's ID (the same connection is also accepted). It has no
separate response. Unknown/inactive IDs have no effect. The transport thread
only moves/parses protocol bytes and sets an atomic cancellation flag;
Python trace checkpoints on the main thread copy it into `G.is_break` and
raise `Cancelled`. This avoids a data race on upstream's plain Boolean.
Native calls cannot be preempted: cancellation is noticed when they return
to Python. Code that catches the exception or disables tracing is not
forcibly interrupted. Failed/cancelled execs do not create a snapshot and
may have partially changed data; rollback is the recovery operation.

The main thread executes one request, pumps `BLI_timer_execute`, and answers.
While idle it pumps timers at roughly 10 ms intervals, releasing the GIL
while waiting for transport. Timers never run Blender API code on a transport
thread. A long-running request delays timers until it returns.

The persistent namespace preloads `bpy`, `bmesh`, `mathutils`, `math`, and
`agent`; one-shot namespaces now also preload `agent`. `agent.snapshot`,
`rollback`, `diff`, and `history` require a session. `observe` and `compare`
have the signatures below but raise `NotImplementedError` explicitly naming
Phase 3 and Phase 4. `describe` remains unimplemented.

Snapshots restore Blender Main data, **not Python variables or external
files**. Reacquire RNA references from `bpy.data` after rollback: saved Python
references into old Main may become invalid. Every successful session exec
adds a history event and a `snapshot` field; `inspect` does not. Initial state
has an `open` event. Manual snapshots have optional labels. `at` is Unix time
in seconds. `agent.diff()` samples the current exec boundary, with the Phase 1
ID-tag semantics (explicit undo/snapshot operations can reset accumulated tags).
`--file` loads only at `session open`; `session save --file F` writes without
reloading, and bare save uses the current Blender filepath.

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

### `inspect`

```
inspect [--object NAME] [--full] [--select PATH…]
```

Emits scene state from RNA: objects (type, transform, bounds, parent,
modifiers, materials, vertex/edge/face counts, UV layers), materials
(node tree summary), armatures (bones), cameras, lights, collections.
`--full` expands node trees and modifier settings. `--select` takes RNA
paths for a targeted read. Never truncated.

The Phase 1 response is `{ok, scene, objects, materials, armatures, cameras,
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

### `compare`

```
compare --ref IMG --view V [--metric M,…] [--mask auto|none]
```

Metrics: `iou` (silhouette intersection-over-union), `chamfer` (edge
distance, pixels), `ssim`, `hist` (color-histogram distance). `--mask auto`
removes the reference background with classic CV before comparison.

Result: `{"view": "front", "iou": 0.83, "chamfer": 4.2, "ssim": 0.71, "hist": 0.12}`.

The same computation is `agent.compare(ref, view, metrics=…)` inside `exec`,
returning a dict.

### `describe`

```
describe bpy.ops.mesh.bevel | describe bpy.types.Object.location | describe Modifier
```

Answers from live RNA: signature, properties with types, ranges, enum items
and descriptions, and for operators the poll requirements the synthetic
context satisfies.

## The `agent` helper module

Preloaded into every `exec` namespace. Fixed in Phase 2; the intended
surface:

```python
agent.observe(views=("front",), passes=("color",), size=512, ref=None) -> {"image": path, ...}
agent.compare(ref, view, metrics=("iou",), mask="auto") -> {"iou": …}
agent.snapshot(label=None) -> "sha256:…"
agent.rollback(snapshot_id) -> None
agent.diff() -> {"added": …, "changed": …, "removed": …}   # since last exec boundary
agent.history() -> [{"snapshot": …, "label": …, "verb": …, "at": …}, …]
```

## Synthetic context

Many `bpy.ops` operators poll for a window, screen, `VIEW_3D` area and
region. In background mode none exist. The fork constructs one
`wmWindow`/`bScreen` with a single `VIEW_3D` area and `WINDOW` region at
session start, without GHOST and without drawing, and installs it as the
default context for `exec`. Operators that genuinely need a GPU viewport
(e.g. GPU-based selection) report that in their error rather than failing
opaquely.

## Wire protocol (session)

JSON lines over `AF_UNIX`. One request, one response, in order:

```
→ {"id": 1, "verb": "exec", "args": {"argv": ["-c", "…"]}}
← {"id": 1, "result": {…}}
```

Cancellation: `{"id": 1, "cancel": true}` sets `G.is_break`; the running
request answers with `"ok": false, "error": {"type": "Cancelled"}`.

## What is deliberately absent

- No asset library, generation or download verbs. Files arrive; `exec`
  imports them.
- No curated operator wrappers (`gameready`, `rig`, `retarget`). The agent
  writes code; recipes belong in the agent's own skill documents.
- No typed tool catalog derived from RNA. `describe` serves RNA on demand
  instead of enumerating it.
- No MCP, HTTP or add-on socket. See `AGENTS.md`.
