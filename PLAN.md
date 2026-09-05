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
| Request objects `{"id","op",…}` with strict per-op field validation; events streamed as JSON lines as they are produced (C++ writer, Python producer) | todo |
| `repl` stdio bridge (`--file`, `--standalone`); socket and stdio carry identical bytes | todo |
| `cancel` answered on the transport thread; running request ends with `Cancelled`; rollback to the pre-request snapshot on any failed request | todo |
| Folded envelope for one-shot verbs derived from the event list by one function; `--json` and human output both come from it | todo |
| Provider registry: `Provider` protocol, orders, failure isolation (`log` event, never fatal), `agent.register_provider` | todo |
| `session status` / `session feedback`; `step` counter; `diff` event carries `snapshot` and `step`; durable labelled snapshots under `.blender-cli/snapshots/` and `rollback <label>` after recovery | todo |
| Remove the old `{"id","verb","args"}` wire shape, `exec --observe`, and the old response envelope; tests rewritten against the event stream | todo |

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
| Perception provider: counts, bounds, framing, changed region/fraction, silhouette delta, symmetry, at 256 px front view by default | todo |
| Image provider: delta/overlay/full/error kinds, threshold, budget views/pass/size, region crop; overlay against the previous state | todo |
| Perception caches the previous feedback render per view so deltas cost one render per action | todo |
| `agent.perceive()` helper; provider registration at session start | todo |

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
| Target storage in the session and on disk under `.blender-cli/targets/`; `target set/list/clear` | todo |
| Objective provider (order 300): per-target metrics at feedback size, deltas, worst cell, best-so-far | todo |
| `fit`: parameter specs (program params or RNA paths), objective forms, budget, methods `coordinate`, `nelder-mead`, `random`; evaluates through program re-run or RNA assignment plus objective scoring in-process | todo |
| `progress` events, `cancel` semantics, `done` shape; `agent.fit()` and `agent.objective()` helpers | todo |

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
| `program get/set/patch/run/history/rollback/record`; versions and `index.json` | done on Linux — driven through `agent_program.request`; K's `program` op and W's CLI projection call the same function |
| Prefix-cached re-execution using snapshots per step; hash equality with a full run | done on Linux — a parameter change re-runs only its readers and later steps; the resulting snapshot equals a full run from the base |
| Crash recovery via program replay when it is newer than the autosave; `agent.program()` helper | unverified — `agent_program.on_session_open` rebuilds and reports `recovered_from: "program"` under test; K must call it from `Session.__init__` and add the `agent.program()` wrapper |
| Recording from the `exec` path | unverified — `agent_program.record_from_exec` is proven with the exec's own code, diff and snapshots; K must call it from the exec path and add `exec --no-record` |

### D — describe schema and corrective errors

Done when: `describe channel` returns the request and event set with field
types; `describe schema` returns a JSON-schema projection of every request
suitable for a tool catalog; an `exec` with a misspelled property whose
nearest match has similarity ≥ 0.85 carries `error.fix.code` that runs
successfully as-is; and ambiguous misspellings carry no `fix`.

Owns: `agent_rna.py`, `tests/agent/describe.py` (new; the RNA portions of
`tests/agent/protocol.py` move here in coordination with K).

| Item | Status |
|---|---|
| `describe channel` and `describe schema` generated from the request table K exposes | todo |
| `fix` on unambiguous attribute, enum and operator-keyword errors; never on ambiguous ones | todo |
| `describe` records for the new `agent` helpers | todo |

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
- Comparison is not a request. A target plus the objective event replaces the
  former comparison verb; `agent.compare()` remains for ad-hoc in-code use.
- Feedback defaults: perception and objective on, image mode `delta` with
  threshold 0.002, front view, 256 px, overlay on. Budgets are per session,
  overridable per request only for images.
- The program is the source of truth for reproducibility; the memfile
  snapshot chain is the source of truth for rollback speed. Both exist; the
  program is not derived from undo and undo is not derived from the program.
- Upstream exceptions beyond registration and build wiring are limited to the
  Vulkan descriptor-pool rollover and the crash-dump path hook, both recorded
  in `doc/agent/upstream.md`.
