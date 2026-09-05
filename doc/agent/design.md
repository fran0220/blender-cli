# blender-agent design

Owner of: the process model, the six verbs, their wire shapes, the session
protocol, observation determinism and the comparison metrics. Constraints
are in `AGENTS.md`; status is in `PLAN.md`.

## Why this shape

An agent modelling from a reference image runs one loop: look at the scene,
write `bpy` code, execute it, look at the result, compare with the
reference, repeat. Every layer between "write code" and "look at the result"
is cost. `blender-mcp` pays for an MCP server, a socket, a GUI Blender, an
add-on timer polling every 50 ms, and a viewport screenshot that depends on
GUI state. blender-agent removes all of it:

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
┌───────────────────────── blender-agent process ─────────────────────────┐
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
  ordinary command. `blender-agent` is a launcher for that invocation.
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

- **One-shot**: `blender-agent <verb> … [--file scene.blend] [--save]`.
  Loads, runs, optionally writes, exits. State lives in the `.blend` file.
- **Session**: `blender-agent session open [--file scene.blend]` starts a
  daemon bound to `<cwd>/.blender-agent/session.sock`. Any verb run in that
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
    "changed": [{"type": "MESH", "name": "Cube", "fields": ["vertices", "polygons"]}],
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

### `inspect`

```
inspect [--object NAME] [--full] [--select PATH…]
```

Emits scene state from RNA: objects (type, transform, bounds, parent,
modifiers, materials, vertex/edge/face counts, UV layers), materials
(node tree summary), armatures (bones), cameras, lights, collections.
`--full` expands node trees and modifier settings. `--select` takes RNA
paths for a targeted read. Never truncated.

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
→ {"id": 1, "verb": "exec", "args": {"code": "…", "observe": ["front"]}}
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
