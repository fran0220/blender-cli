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

The returned endpoint remains absolute. Where its absolute name exceeds the
platform's Unix-socket address limit, the launcher and daemon address the same
file relative to their initial working directory. Raw socket clients can likewise
connect to `.blender-cli/session.sock` from the session directory. Cleanup retains
the absolute name even if Python later changes the daemon's working directory.

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

At the Phase 1 boundary, only `exec` and `inspect` were implemented; the other
four verbs answered `NotImplemented` and exited 1. Snapshots, observations and
RNA error suggestions were absent, not placeholder fields. One-shot
namespaces are fresh and preload `bpy`, `bmesh`, `mathutils`, `math`; Phase 2
adds `agent` and the session contract below, preserving the one-shot behavior.
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
Common `--json` and human output
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
`rollback`, `diff`, and `history` require a session. Phase 3 implements
`observe` in both modes. `compare` still raises `NotImplementedError` naming
Phase 4; `describe` remains unimplemented.

Snapshots restore Blender Main data, **not Python variables or external
files**. Reacquire RNA references from `bpy.data` after rollback: saved Python
references into old Main may become invalid. Every successful session exec
adds a history event and a `snapshot` field; `inspect` does not. Initial state
has an `open` event. Manual snapshots have optional labels. `at` is Unix time
in seconds. `agent.diff()` samples the current exec boundary, with the Phase 1
ID-tag semantics (explicit undo/snapshot operations can reset accumulated tags).
`--file` loads only at `session open`; `session save --file F` writes without
reloading, and bare save uses the current Blender filepath.

The session maintains `<initial cwd>/.blender-cli/autosave-<pid>.blend` from
the current agent snapshot, not unsnapshotted live edits. Snapshot creation
(including open, successful exec and manual snapshot) and rollback mark it dirty.
The main thread writes after queuing a response if at least two seconds have
elapsed since the last write; otherwise the idle pump writes after one second
without a request. Explicit `session save` does not trigger a snapshot or a
pre-response autosave. A clean close deletes only that session's autosave;
crash files survive recovery and later closes. The most recent completed write
is recoverable; this is not synchronous durability for every acknowledged edit.
Write failures are logged and retried, without turning a successful exec into a
failed response.

Upstream 5.3 no longer supplies `BLO_memfile_write_file`: memfiles contain
out-of-stream shared arrays and cannot be dumped as blend files. The agent
decodes its retained memfile into an isolated Main using an empty old Main and
`BLO_READ_SKIP_UNDO_OLD_MAIN`, recomputes user counts, then uses upstream's
`BLO_write_file` recovery/compression path. It never saves through an operator,
replaces live Main, or changes live filepath/dirty state or Python references.
The active snapshot scene is placed first for loading without saved UI.
Non-memfile-undo data (UI and brushes) is not recovered; linked libraries are
reloaded from their files, and ordinary blend-save orphan rules apply. Python
variables/history/hashes do not survive a process crash. Both `session open
--file` and one-shot `--file` can load the autosave.

`session` without an action returns `ValueError` in both modes, with message
`session requires an action: open|save|close|snapshot|rollback|history`.

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

#### Phase 3 observation contract

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
world-space evaluated bounds with a 10% margin; perspective fits their
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
| `depth` | Camera depth d mapped to clamp(1 − (d − near)/(far − near), 0, 1), repeated in RGB; near/far are the min/max depths of the framing-bound corners (range at least 0.001). Depth is axial, or radial for a panoramic `camera`, matching EEVEE. Near is white, far/background black; the silhouette masks background. |

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
requested views/passes and actual output dimensions. `exec --observe VIEWS`
attaches this result as `observe`; `agent.observe(views, passes, size, ref)`
uses the same implementation and returns the dict directly.

PNG output has only IHDR, IDAT and IEND chunks, RGB8, filter 0 and zlib level 9:
no timestamps, metadata hashes, paths or render timing. Byte determinism is
scoped to the same scene state, same build, same platform, same Mesa/driver
(or product-platform GPU driver), and the same observation arguments. It is
not a cross-driver floating-point equivalence claim. Metal/macOS and
real-GPU Vulkan/Windows require their own platform runs; Linux software
Vulkan evidence cannot establish either.

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

#### Phase 4 comparison contract

CLI additionally accepts `--size 512|768|1024`, `--frame OBJECT` (bounds, not
timeline frame, exactly as observe), and `--debug-out DIR`. The helper accepts
the same settings as `size=512, frame=None, debug=False`; `debug=True` selects
a new temporary directory, or a path chooses the directory. Only requested
metric keys and `view` are returned (CLI adds `ok`). Debug alone adds
`debug: {reference_silhouette: path}` for an unbordered binary RGB PNG. Normal
comparison does not encode, write or return any image, even within `exec`.
Each call freshly loads the reference and freshly evaluates/renders the scene;
there is no stale image, scene, RNA-pointer or file-mtime cache.

Reference loading uses `bpy.data.images` / ImBuf and compiled-in codecs,
including PNG, JPEG and WebP. Byte buffers are straight display-referred sRGB;
float buffers are unpremultiplied, converted from linear to sRGB and clamped.
Alpha is resampled with premultiplied **display** RGB to avoid transparent-color
bleeding, then unpremultiplied for segmentation. Reference image datablocks and
render data are disposed under Phase 3's recalc/callback preservation boundary.

A square 516/772/1028 image whose entire two-pixel opaque outer border is
RGB(32,32,32) is recognized as a single observe tile and cropped by two pixels.
No other border is removed; multi-view/pass sheets should be cropped by the
caller. This makes observe→compare self-consistency independent of sheet chrome.
Then pixel-center bilinear interpolation aspect-fits and centers the image in
the chosen tile, just like Phase 3 overlay. Scaled dimensions are nearest
integers (Python round, minimum 1); odd padding puts the extra pixel at right
or bottom. Padding is background, never foreground. No silhouette alignment,
translation or scale optimization happens implicitly.

`mask=auto` estimates the background by componentwise median RGB of the fitted
image's four outer rows/columns (before padding). Let border RGB distances from
that median have median m and median absolute deviation d. Foreground is RGB
Euclidean distance > max(0.08, m + 6d), on channels normalized to [0,1]. If any
alpha is < 254/255, alpha ≥ 0.5 takes precedence. A 3×3 square opening followed
by closing, with edge-replicated padding, removes isolated noise and closes
one-pixel gaps. All surviving components are kept; large holes are retained,
not filled, because an object can have real holes in its Phase 3 silhouette.
This is deterministic classic CV, not semantic segmentation: textured borders,
foreground touching most of the border, background-colored objects, and thin
features can defeat it. Debug exposes that uncertainty instead of hiding it.

`mask=none` uses alpha ≥ 0.5 when the loaded image has an alpha channel (even
fully opaque alpha); otherwise display grayscale ≥ 0.5. It does no morphology.
Grayscale is 0.2126R + 0.7152G + 0.0722B. RGB under alpha is composited onto
the Phase 3 display background (rounded sRGB(0.035), 53/255). SSIM replaces
pixels outside each image's own foreground mask with this same background;
histograms exclude those pixels. Thus a removed colored background does not
dominate either appearance metric.

Let A and B be reference and exact Phase 3 render silhouettes:

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

### `describe`

```
describe bpy.ops.mesh.bevel | describe bpy.types.Object.location | describe Modifier
```

Answers from live RNA: signature, properties with types, ranges, enum items
and descriptions, and for operators the poll requirements the synthetic
context satisfies.

#### Phase 4 RNA contract

`describe` and `agent.describe(path)` resolve public attributes and literal
string/integer subscripts, never calls or arbitrary expressions. A bare name
is relative to `bpy.types`. Instance paths return the instance's live struct.
Results have `kind: property|struct|operator|module`; CLI adds `ok: true`.
Structs include `struct`, `description`, `base` (nullable), and `properties`
keyed by identifier. Property records include identifier, description, lowercase
type, subtype, animatable, readonly, and where applicable array_length, default,
hard_min/max, soft_min/max, fixed_type, and enum_items (identifier/name/description).
Operators add path, keyword signature, context note and actual `poll()` boolean;
`poll_reason` is present only when upstream supplies a failure message. Polls
run in the adopted Phase 3 context, not an invented edit mode. Module results
map every operator name to its one-line RNA description. Neither JSON nor
indented human output truncates properties or enum items.

Exec errors retain their original type/message/line. Attribute failures on RNA
instances/types and operator modules add `error.rna.struct` and up to five
`nearest` identifiers, ranked by Python difflib SequenceMatcher similarity
(cutoff 0.6, sorted candidate names for deterministic ties). Candidates come
from live properties/functions or module operators. A nearest property also
supplies compact type, e.g. `float[3]`. Property assignment TypeError/ValueError
adds the live property record (including enum descriptions and numeric ranges).
Operator argument failures add the operator struct's complete valid properties.
The failing bytecode's source position identifies the receiver in the original
traceback locals, including semicolon-separated statements; calls are never
replayed. If no RNA receiver can be recovered, `rna` is absent, not empty.
Blender silently clamps many numeric assignments; these remain successful
upstream operations, not synthetic errors. Descriptions still expose their ranges.

## The `agent` helper module

Preloaded into every `exec` namespace. The Phase 4 surface is:

```python
agent.observe(views=("front",), passes=("color",), size=512, ref=None) -> {"image": path, ...}
agent.compare(ref, view, metrics=("iou",), mask="auto", size=512, frame=None, debug=False) -> {"view": …, "iou": …}
agent.describe(path) -> {"kind": …, ...}
agent.snapshot(label=None) -> "sha256:…"
agent.rollback(snapshot_id) -> None
agent.diff() -> {"added": …, "changed": …, "removed": …}   # since last exec boundary
agent.history() -> [{"snapshot": …, "label": …, "verb": …, "at": …}, …]
```

## Synthetic context

Many `bpy.ops` operators poll for a window, screen, `VIEW_3D` area and
region. Background startup does not provide a reliable active UI area.
Phase 3 adopts the first loaded window's active screen and its first
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
