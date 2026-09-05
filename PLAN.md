# blender-cli — execution plan

This is the only execution-status document. Constraints are in `AGENTS.md`;
the contract is in `doc/agent/design.md`. Work is organised as workstreams
with disjoint file ownership so they run in parallel; each ends with a
runnable result and states what proves it. Status words: `todo`, `doing`,
`done`, `unverified` (landed, not yet proven on a product platform).

Base: upstream `main` (5.3 development line, forked at `5c951f2e`). Binary
name: `blender-cli`. Development evidence is produced in Linux orbs; macOS
and Windows verification is deferred until every workstream below is `done`
on Linux (owner: the platform workstream, last).

## Foundations that stay

These subsystems are kept as the implementation base of the request set.
They are complete on Linux and unchanged in intent; workstreams below may
edit them only where their row says so.

| Subsystem | Files | Status |
|---|---|---|
| Build profile, `WITH_AGENT`, `source/blender/agent/` wiring, orb setup, manual-only platform workflows | `build_files/cmake/config/blender_agent.cmake`, `.agents/setup`, `.github/workflows/agent-*.yml` | done — Linux configure/install `BUILD_EXIT=0`; macOS/Windows native compilation of `bf_agent` and `blender-cli` verified once, workflows are `workflow_dispatch` only |
| Command entry and launcher (`blender --command agent …`, `blender-cli` launcher, session auto-connect) | `agent_command.cc`, `launcher.cc`, `launcher_session.hh` | done — replaced in place by the kernel workstream (event streaming, `repl`) |
| Session daemon: `AF_UNIX` endpoint, main-thread loop, `BLI_timer_execute` pump, cancellation via `G.is_break`, memfile snapshot chain, isolated-snapshot autosave and dead-PID recovery, durable crash dumps | `agent_session.cc`, `agent_socket.hh`, `agent_transport.hh`, `agent_runtime.py` (`Session`) | done on Linux — first killed request reports autosave/recovery, filepath/dirty restoration, per-session crash dumps with request/source and render-time Python stack, and durable labelled checkpoints (newest label wins, older hashes retained, stale close preserves index) pass. Four CTests pass (632.54 s; median exec 6.108 ms under stress); manual label → 40 renders → crash → reopen → history → rollback restores geometry; macOS/Windows unverified; wire shape replaced by the kernel workstream |
| Synthetic context (window/screen/`VIEW_3D` adoption, `ED_editors_flush_edits` at every boundary) | `agent_context.cc` | done — unchanged |
| Observation renderer: EEVEE offscreen, fixed presets/lighting/color management, framing from converted geometry, `framing` in the response, GN instances, Vulkan descriptor-pool rollover so a session renders without bound | `agent_render.cc`, `agent_observe.py`, `vk_descriptor_pools.cc/.hh` (`/* blender-cli */`, exception in `upstream.md`) | done on Linux — pool rollover at 250 sets fixes ~1,810 extra maps/render (35 completed then crash at 65,530-map limit): 300 helper renders in 1,863.62 s (200 at 1,276.37 s) and 300 CLI renders in 1,449.46 s, warm maps bounded 1,301–1,543 and RSS ~1.08–1.20 GiB. OpenGL survives 50 renders in 403.29 s (~868 maps); four CTests including 120-render regression pass, deterministic hash `84ab1492…` retained; timings include concurrent stress; native product GPUs unverified |
| Metrics: IoU, Chamfer, SSIM, histogram distance; `--mask auto` classic-CV segmentation; `fit=bbox` reference normalisation with occupancy 1/1.1 | `agent_compare.py` | done — retained as the objective's computation; the `compare` verb is removed by the CLI workstream |
| RNA: `describe` for `bpy.*` and `agent.*`, corrective error records (`nearest`, `data.` hop, property/operator schemas) | `agent_rna.py` | done — extended by the describe workstream |
| Packaging: trimmed install, `tar.zst`, size tables | `packaging/package.py`, `tests/agent/package.py`, `doc/agent/build-profile.md` | done on Linux and macOS — Windows package unverified; re-measured by the platform workstream after all features land |

## Workstreams

Each workstream owns the files in its row exclusively until it reports
`done`. Anything outside its row is a request to the owning workstream (or
to the coordinator when no owner exists), never a direct edit. Every
workstream removes the code, tests and documentation that its work replaces
in the same commits; nothing is kept for compatibility. Every workstream
rebases on `origin/main` before pushing, builds in its own orb, and runs
`ctest --test-dir build/orb -R agent --output-on-failure` before every push.

### K — kernel: event channel, `repl`, provider registry

Done when: `blender-cli repl --standalone` reads one `exec` request line and
writes `log`, `value`, `diff` and `done` events in the documented order;
the session socket speaks the same protocol; `cancel` ends a running request
with `error` of type `Cancelled` while the transport thread keeps reading;
a one-shot verb prints exactly the folded envelope derived from those
events; and `agent.register_provider` runs a test provider's `after` hook
after every `exec` with its dict appearing as an event. All other
workstreams build on K's declarations in `design.md` and rebase onto K when
it lands.

Owns: `agent_command.cc`, `agent_session.cc`, `agent_socket.hh`,
`agent_transport.hh`, `launcher.cc`, `launcher_session.hh`, `agent.py`,
`agent_runtime.py` (request dispatch, `Session`, provider registry, envelope
folding), `tests/agent/protocol.py`, `tests/agent/session.py`.

| Item | Status |
|---|---|
| Request objects `{"id","op",…}` validated field by field against `agent_contract.py`; events streamed as JSON lines as they are produced (C++ writer, Python producer) | done — `agent_protocol` asserts `log`, `log`, `value`, `diff`, `done` in order, and `log` lines arriving one per `print` |
| `repl` stdio bridge (`--file`, `--standalone`); socket and stdio carry identical bytes | done — standalone in `agent_protocol`, bridged to the daemon in `agent_session`, sharing its namespace |
| `cancel` answered on the transport thread; running request ends with `Cancelled`; rollback to the pre-request snapshot on any failed request | done — `agent_session` cancels a running loop from a second connection and gets `{"target","cancelled":true}` at once; an inactive id answers `cancelled:false` |
| Folded envelope for one-shot verbs derived from the event list by one function; `--json` and human output both come from it | done — `fold()` in `agent_events.hh`, used by the launcher and the in-process verb |
| Provider registry: `Provider` protocol, orders, failure isolation (`log` event, never fatal), `agent.register_provider` | done — `register_provider`, `register_op`, `register_helper`, `register_record_hook`, `PROVIDER_MODULES`; the built-in `diff` provider is order 100 |
| `session status` / `session feedback`; `step` counter; `diff` event carries `snapshot` and `step`; durable labelled snapshots under `.blender-cli/snapshots/` and `rollback <label>` after recovery | done — a request that changes nothing takes no snapshot and does not advance `step` |
| Remove the old `{"id","verb","args"}` wire shape, `exec --observe`, the `compare` verb and the old response envelope; tests rewritten against the event stream | done — `tests/agent/compare.py` deleted with the verb |

### F — feedback: perception and image providers

Done when: after `exec -c 'bpy.ops.mesh.primitive_cube_add()'` in a session
the envelope carries a `perception` with counts, bounds, framing, changed
region and fraction, and silhouette delta; a second identical-state `exec`
produces no `image` event under the default threshold; moving the cube
produces one `image` event of kind `delta` whose `region` covers the
change; `session feedback image.mode=off` suppresses images; and
`agent.perceive()` returns the same dict as the event.

Owns: new `agent_feedback.py`, `tests/agent/feedback.py`,
`agent_observe.py` (only additions for the feedback size and delta
rendering; the renderer's determinism and `framing` contract are frozen).

| Item | Status |
|---|---|
| Perception provider: counts, bounds, framing, changed region/fraction, silhouette delta, symmetry, at 256 px front view by default | done on Linux — `agent_feedback.Perception`, order 200, proven by `tests/agent/feedback.py` |
| Image provider: delta/overlay/full/error kinds, threshold, budget views/pass/size, region crop; overlay against the previous state | done on Linux — `agent_feedback.Image`, order 400; a failed budget render is an `error` image event carrying its message, never a failed request |
| Perception caches the previous feedback render per view so deltas cost one render per action | done on Linux — an action costs exactly one budget render; see the measurements below |
| `agent.perceive()` helper; provider registration at session start | done on Linux — `agent_feedback.register(session)` runs from K's `PROVIDER_MODULES` and installs both providers and the `perceive` helper; `agent.perceive()` equals the request's own perception event |
| `inline` image payloads on the repl and socket transports | todo — the provider emits `inline` and omits `path` when `image.inline` is set; the policy field itself is K's `DEFS["image_policy"]` |

Feedback cycle cost, Linux orb at `origin/main` ≥ 7f47e733 (Release, xPack GCC
14.3.0, software Vulkan `lavapipe`, stock `vm.max_map_count` 65530), one action
at the default 256 px front budget view. "Action" is the `done` event's `ms`
for an `exec` that runs `pass`, so it is the feedback cycle plus a trivial
statement; "render" times `render_budget` alone inside the same session:

| Scene | Action (ms) | Budget render alone (ms) |
|---|---|---|
| Cube and two anchors, 92 vertices | 2435.4, 2417.5, 2296.6 | 2480.4 |
| `primitive_grid_add(x_subdivisions=1000, y_subdivisions=1000)` — 1,002,001 vertices | 24926.6, 25015.9, 24993.7 | 29651.6 |

The cycle is the render. Everything the providers add — the changed-region
scan, IoU, symmetry, the crops and the PNGs — stays inside the render's own
run-to-run variance. The cost is the observation renderer's, not the feedback
channel's, and on this device it is bound by EEVEE samples rather than by
pixels: at 32 samples the cube's budget render costs 2633.9/2708.6 ms at
256 px and 3395.4 ms at 512 px. Two levers exist, both needing
product-platform evidence before they are taken:

- EEVEE samples. The budget render inherits observation's 32 samples. On the
  cube: 32 samples 2633.9/2708.6 ms, 8 samples 818.1/780.0 ms, 1 sample
  455.5/329.1 ms. On the grid: 32 samples 25356.5/23760.9 ms, 8 samples
  9708.5 ms. This is a decision for the objective provider too, which scores
  targets from the same buffers.
- Skipping the render when Main did not change. The grid's remaining cost is
  `render_scene` 3.3 s (per-action Python conversion and the framing point
  list) and `aim` 1.2 s (1,002,001 `mathutils` projections) before EEVEE.
  An action whose memfile hash is unchanged cannot have changed the picture;
  that test needs the post-request snapshot to be taken before the perception
  provider runs, and the ID diff is not a safe substitute because an in-code
  rollback resets its accumulator.

### T — targets, objective and `fit`

Done when: `target set front --ref ref.png` followed by an `exec` yields an
`objective` event with per-target metrics, deltas against the previous
step, the worst 4×4 cell with `missing`/`extra`, and best-so-far
snapshot/step; `fit` over two program parameters with a 40-evaluation
budget streams `progress` at most every 0.5 s, returns the best parameters
and snapshot, and leaves `Main` at the best state; `cancel` during `fit`
keeps the best state; and a seeded `random` method is reproducible.

Owns: new `agent_target.py`, new `agent_fit.py`, `tests/agent/fit.py`,
`agent_compare.py` (only the shared metric functions; CLI parsing is
removed by W).

| Item | Status |
|---|---|
| Target storage in the session and on disk under `.blender-cli/targets/`; `target set/list/clear` | done on Linux — `session.targets` is the loaded set; `target set` copies the reference, writes `silhouette.png`/`target.json` and answers the first scoring in its `done`, so registering a target costs no extra round trip |
| Objective provider (order 300): per-target metrics at feedback size, deltas, worst cell, best-so-far | done on Linux — measured 2.61 s per action for one target at 256 px against 2.6 ms with none; a second target on the same view adds nothing (2.59 s), confirming one render per distinct view |
| `fit`: parameter specs (program params or RNA paths), objective forms, budget, methods `coordinate`, `nelder-mead`, `random`; evaluates through program re-run or RNA assignment plus objective scoring in-process | done on Linux — cube scale x/y against a binary-rendered silhouette reaches IoU 0.999975 in exactly 40 evaluations at 512 px (3.63 s per evaluation); program parameters go through `agent_program.attach(session).set_params`, and a step that raises costs one evaluation, scores worst and is counted in `failed` rather than failing the request |
| `progress` events, `cancel` semantics, `done` shape; `agent.fit()` and `agent.objective()` helpers | done on Linux — `progress` is rate limited to 0.5 s; a `cancel` on a second connection ends `fit` with `done` and `cancelled: true` after 4 of 200 evaluations with the best applied; seeded `random` repeats its params, score and curve exactly; the helpers are installed through `register_helper` and answer the event's dict |

### P — program model

Done when: a session with recording on turns three `exec` calls into
`.blender-cli/program/model.py` with a `P = {…}` block and `# step N`
blocks; `program set` with an edited parameter re-executes only the steps
after the first changed one (prefix cache keyed by sha256 of params +
steps), and the resulting `Main` hash equals a fresh full run; `program
history`/`rollback` move between `versions/<sha>.py`; and after `os._exit`
a `session open` rebuilds the scene from the program when the autosave is
older than the program's last version.

Owns: new `agent_program.py`, `tests/agent/program.py`, `agent_runtime.py`
(only the `record`/`program` hooks K declares as extension points; K owns
the file).

| Item | Status |
|---|---|
| Program file layout (`# base:` header, literal `P` block read by `ast.literal_eval`, `# step N` blocks), step recording, static `reproducible` verdict | done on Linux — `tests/agent/program.py` |
| `program get/set/patch/run/history/rollback/record`; versions and `index.json` | done on Linux — `register_op("program", …)`; driven through the real `blender-cli program` verb |
| Prefix-cached re-execution using snapshots per step; equality with a full run | done on Linux — a parameter change re-runs only its readers and later steps (13 ms of 66 ms), and the result has the same `digest` as a full run from the base |
| `agent_program.digest()` cost, measured on Linux (Release, five calls each, median; every scene hashed identically across all five) | done — empty scene 0.4 ms; 50 objects with 150 modifiers, 50 constraints and a geometry node group 40.5 ms; a 1,002,001-vertex grid alone 663.5 ms; both together 610.4 ms. Mesh buffers dominate: the RNA, node-tree and attribute walks cost ~40 ms at that object count and do not grow with vertex count. One `digest()` runs per program request, not per `exec` |
| Crash recovery via program replay when it is newer than the autosave; `agent.program()` helper | done on Linux — `os._exit`, reopen, `session status` reports `recovered_from: "program"` and the scene is rebuilt; an autosave newer than the program keeps the autosave path |
| Recording from the `exec` path | done on Linux — `register(session)` installs K's `register_record_hook`; three execs become three steps, and a failed exec, an empty diff, `--no-record` and `record off` are never recorded |

### D — describe schema and corrective errors

Done when: `describe channel` returns the request and event set with field
types; `describe schema` returns a JSON-schema projection of every request
suitable for a tool catalog; an `exec` whose misspelling has one certain
correction — the only candidate above difflib's 0.6 cutoff, or 0.85 with
more than 0.05 over the runner-up — carries `error.fix.code` that runs
successfully as-is; and ambiguous misspellings carry no `fix`.

Owns: `agent_rna.py`, `tests/agent/describe.py` (new; the RNA portions of
`tests/agent/protocol.py` move here in coordination with K).

| Item | Status |
|---|---|
| `describe channel` and `describe schema` generated from the request table K exposes | done on Linux — read from K's `agent_runtime.REQUESTS`/`EVENTS`/`DEFS`, never a second copy; nine ops, nine events; each op's schema is a self-contained draft 2020-12 document whose `$defs` hold only the shapes it reaches, and every op's `example` validates against it. `mutates` is dispatch policy and is not projected. Both exclusive choices the contract makes — `exec` code-or-script and `fit_param` name-or-path — are declared as `exactly_one_of` in K's table and project to `oneOf`, and the test asserts the projection is exactly as strict as the table, never stricter |
| `fix` on unambiguous attribute, enum and operator-keyword errors; never on ambiguous ones | done on Linux — `error_fields` contributes `rna` and `fix` to the real `error` event. `tests/agent/describe.py` fails a statement through the binary, then re-executes the `fix.code` it answers with: `locaton`→`location` (0.93), operator enum `type='MESHES'`→`'MESH'` (0.80, sole candidate), operator keyword `sise`→`size` (0.75, sole candidate), `bevel_dept`→`data.bevel_depth` (0.95) through the one-hop `data.` search, and a multibyte source line proving the rewrite is a UTF-8 byte edit at the failing position. `rotation_mode='XYZY'` scores 0.857 against both `XYZ` and `XZY` and carries no `fix`; neither does an identifier with no candidate at all |
| `describe` records for the new `agent` helpers | done on Linux — `describe agent` answers for all twelve helpers now present (`compare`, `describe`, `diff`, `fit`, `history`, `objective`, `observe`, `perceive`, `program`, `register_provider`, `rollback`, `snapshot`), each with a signature, docstring and parameter defaults from `inspect.signature`. The test asserts every record is well formed rather than a fixed list, so later helpers are covered without editing it |

### W — CLI projections, documentation, removal of the comparison verb

Done when: every request has exactly one CLI projection whose flags map
one-to-one to request fields; `blender-cli compare` no longer exists and
its tests are gone; `README.md`, `doc/agent/usage.md` and `doc/agent/design.md`
describe only the current surface; and a fresh orb can follow
`usage.md` from `repl` to a fitted model without consulting anything else.

Owns: `README.md`, `doc/agent/usage.md`, `doc/agent/design.md` (request
sections only; K owns *Channel protocol*), `tests/agent/compare.py`
(deletion), CLI argument tables in `agent_command.cc` help text (in
coordination with K).

| Item | Status |
|---|---|
| Remove `compare` verb, its parser and tests; `target set` is the only CLI entry to metrics | todo |
| `target`, `fit`, `program`, `repl`, `session status/feedback` CLI projections and help | todo |
| `usage.md` rewritten around the channel loop: repl, feedback budgets, targets, fit, program, recovery | todo |
| `README.md` reflects the current surface and quick start | todo |

### L — loop evidence

Done when: a Claude Opus thread with a painter-generated reference models the
object through `blender-cli repl` only, and the transcript shows: the
number of round trips, the objective trajectory, at least one `fit`, at
least one `program set`, one crash recovery, and zero requests whose only
purpose was to see the current state. Findings become items in the owning
workstreams, not fixes in this one.

Owns: `.amp/in/artifacts/` in its own orb; no repository files.

| Item | Status |
|---|---|
| Dogfood run on the completed Linux build; friction list filed to owners | todo |

### X — product platforms

Done when: all agent tests pass on macOS arm64 and Windows x64 from the
manual workflows, package sizes are re-measured after the feature work, and
`build-profile.md` records them. Not scheduled until every workstream above
is `done` on Linux.

Owns: `.github/workflows/agent-*.yml`, `doc/agent/build-profile.md`,
`packaging/package.py`, `tests/agent/package.py`.

| Item | Status |
|---|---|
| macOS arm64 full run of all agent tests on the final surface | todo |
| Windows x64 full run, including AF_UNIX/process-exit, handle inheritance, Vulkan loader probe and DLL/manifest trim | unverified — no native Windows run has passed the runtime tests |
| Re-measured package sizes on both product platforms | todo |

## Ordering

```diagram
┌───┐
│ K │ kernel: channel, repl, cancel, envelope, registry
└─┬─┘
  ├──────────┬──────────┬──────────┐
┌─▼─┐      ┌─▼─┐      ┌─▼─┐      ┌─▼─┐
│ F │      │ T │      │ P │      │ D │
└─┬─┘      └─┬─┘      └─┬─┘      └─┬─┘
  └──────────┴────┬─────┴──────────┘
                ┌─▼─┐
                │ W │ CLI projections, docs, removals
                └─┬─┘
                ┌─▼─┐      ┌───┐
                │ L │ ───▶ │ X │ platforms, last
                └───┘      └───┘
```

F, T, P and D start together against the declarations in `design.md`,
using local stubs for K's registry until K lands, then rebase. W starts when
K lands and finishes after F/T/P/D. L runs on the first build where W is
`done`. X runs last.

## Resolved decisions

- `WITH_OPENVDB` stays: voxel remesh is required. Its separately shipped Python
  SDK is trimmed, not the modelling implementation. Measurements and reasoning
  are in `doc/agent/build-profile.md`.
- Cycles CPU and Embree stay: EEVEE does not replace object/texture baking.
  The profile uses upstream's `WITH_EMBREE` spelling. Cycles' Python engine
  registration is preserved during packaging.
- Packaging retains factory-required Python modules outside `addons_core`, a
  monospaced filename alias for one font face, and the real startup AgX transform
  beside Standard. Literal deletion broke one-shot JSON or engine registration;
  do not patch upstream Python or disguise AgX as Standard to satisfy a size goal.
- Comparison is not a request. A target is registered once and the objective
  event scores it after every action; `agent.compare()` is the same
  computation for ad-hoc in-code use.
- Feedback defaults: perception and objective on, image mode `delta` with
  threshold 0.002, front view, 256 px, overlay on. Budgets are per session,
  overridable per request only for images.
- The program is the source of truth for reproducibility; the memfile
  snapshot chain is the source of truth for rollback speed. Both exist; the
  program is not derived from undo and undo is not derived from the program.
- Memfile snapshot IDs are process-local identities, not content hashes:
  measured on Linux, three identical full runs of one program produce three
  different IDs. Anything that must decide whether two states are the same
  scene uses `agent_program.digest()`, a sha256 over Main's content. Snapshot
  IDs remain the rollback handle and nothing else.
- Upstream exceptions beyond registration and build wiring are limited to the
  Vulkan descriptor-pool rollover and the crash-dump path hook, both recorded
  in `doc/agent/upstream.md`.
