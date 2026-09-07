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
| Packaging: trimmed install, `tar.zst`, size tables | `packaging/package.py`, `tests/agent/package.py`, `doc/agent/build-profile.md` | historical Linux/macOS evidence only — X is running the eight-test final surface and re-measuring both product packages; Windows remains unverified |

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
| `inline` image payloads on the repl and socket transports | done on Linux — the provider emits `inline` and omits `path` when `image.inline` is set, per session or per request |
| An action that changed nothing costs no render; the budget view renders at `image.samples`, default 8; the framing projection is vectorised | done on Linux — see the measurements below |

Feedback cycle cost, Linux orb (Release, xPack GCC 14.3.0, software Vulkan
`lavapipe`, stock `vm.max_map_count` 65530), one action at the default 256 px
front budget view. "Action" is the `done` event's `ms`: settled runs `pass`,
rendering nudges a value and puts it back, so it renders a picture that did
not move. "Render alone" times `render_budget` inside the same session:

| Scene | Settled action (ms) | Rendering action (ms) | Render alone (ms) |
|---|---|---|---|
| Cube and two anchors, 92 vertices | 0.389, 0.430, 0.386 | 687.4, 728.4, 716.1 | 694.9 |
| `primitive_grid_add(x_subdivisions=1000, y_subdivisions=1000)` — 1,002,001 vertices | 0.662, 0.501, 0.416 | 9091.0, 8940.0, 8968.7 | 9021.4 |

Against the first measurement of this channel — 2435.4 ms for the cube and
24926.6 ms for the grid, every action — three changes account for the
difference:

- **An action that changed nothing costs no render.** The diff provider has
  already settled whether any datablock changed by the time perception runs,
  so perception answers from the remembered buffers: 2435 ms → 0.39 ms on the
  cube, 24927 ms → 0.42 ms on the grid. The snapshot is part of the test
  because an in-code rollback moves `Main` without leaving a diff.
- **`image.samples`, default 8**, instead of observation's fixed 32. The
  budget render is bound by samples, not pixels, on this device: cube 32
  samples 2633.9/2708.6 ms, 8 samples 818.1/780.0 ms, 1 sample 455.5/329.1 ms;
  grid 32 samples 25356.5/23760.9 ms, 8 samples 9708.5 ms. Observation keeps
  32 and its determinism fixture is unchanged.
- **The framing projection is vectorised.** `render_scene` and `aim` built and
  projected a Python list of a million `mathutils` vectors: 3305.8 ms and
  1191.3 ms on the grid, now 101.7 ms and 31.9 ms — a batch that keeps
  upstream's own term order and single precision, so the observation
  determinism hash `84ab1492…` is byte-identical and `agent_observe` passes
  (and runs 147 s → 101 s).

What is left is EEVEE itself: 694.9 ms for a cube and 9.0 s for a million
vertices, on a software rasteriser. Both are the observation renderer's cost,
not the feedback channel's, and both want a product-platform measurement.

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
| Objective provider (order 300): per-target metrics at feedback size, deltas, worst cell, best-so-far | done on Linux — a target on a budget view is scored from the perception provider's render and costs no render at all (2.69 s per action against 2.72 s with no target); a target on another view costs one more (5.4 s), and a second target on that same view adds nothing |
| `fit`: parameter specs (program params or RNA paths), objective forms, budget, methods `coordinate`, `nelder-mead`, `random`; evaluates through program re-run or RNA assignment plus objective scoring in-process | done on Linux — cube scale x/y against a binary-rendered silhouette reaches IoU 0.999975 in exactly 40 evaluations at 512 px (3.63 s per evaluation); program parameters go through `agent_program.attach(session).set_params`, and a step that raises costs one evaluation, scores worst and is counted in `failed` rather than failing the request |
| `progress` events, `cancel` semantics, `done` shape; `agent.fit()` and `agent.objective()` helpers | done on Linux — a `cancel` on a second connection ends `fit` with `done`, `stopped: "cancel"` after 4 of 200 evaluations with the best applied; seeded `random` repeats its params, score and curve exactly; the helpers are installed through `register_helper` and answer the event's dict |
| Run-1 item: `progress` per the feedback policy, and a `patience` stop reported in `done.stopped` | done on Linux — under the default `improvements` a 40-evaluation fit emits exactly the 9 events of its curve where it used to emit 40; `all` stays a 0.5 s heartbeat and `off` emits none. On L's scene shape (5 program parameters, 60 evaluations) a fixed patience 16 ended the search at 24 of 60 but at IoU 0.959 against the 0.996658 the full budget reaches, and `4n` = 20 was no better. `patience` therefore has no fixed default: `fit` derives `max(16, 5 × parameters)`, which on that fit is 25 and reaches 0.996658 in 156.5 s against 153.3 s unbounded. An explicit `patience` still wins |
| Run-1 item: the objective provider leaves each target's silhouette masks for the image provider | done on Linux — `session.last_objective` carries the scored masks, worst cell, primary metric and its delta at the budget size, cleared in `before` so a stale scoring is never pictured; `agent_target.error_image` is the one composer `fit`'s `error_map` and F's `error` kind both draw |

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
| `program get/set/patch/run/history/rollback/record`; versions and `index.json` | done on Linux — `register_op("program", …)`; driven through the real `blender-cli program` verb. A failed `set` answers `step`, `version` and `cached_through` on the `error` event through the kernel's `agent_fields` merge, and `session snapshot --label L` answers the `version` it named, which `program history` then shows on that row |
| Prefix-cached re-execution using snapshots per step; equality with a full run | done on Linux — a parameter change re-runs only its readers and later steps, and the result has the same `digest` as a full run from the base. A full 3-step run from the base costs 64 ms inside the process; the whole `program set` request costs 583 ms once the feedback channel renders on it |
| `agent_program.digest()` cost, measured on Linux (Release, five calls each, median; every scene hashed identically across all five) | done — empty scene 0.4 ms; 50 objects with 150 modifiers, 50 constraints and a geometry node group 40.5 ms; a 1,002,001-vertex grid alone 663.5 ms; both together 610.4 ms. Mesh buffers dominate: the RNA, node-tree and attribute walks cost ~40 ms at that object count and do not grow with vertex count. One `digest()` runs per program request, not per `exec` |
| Crash recovery: the newest source wins and a reopen never comes back empty-handed; `agent.program()` helper | done on Linux — after `os._exit`, a plain reopen restores the program when its newest version is newer (`recovered_from: "program"`) and loads the recovery file otherwise (`recovered_from: "autosave"`), including when replaying the program raises. Both branches are forced by mtime in `tests/agent/program.py` and told apart by an unrecorded edit that exists only in the file. `recovered_from` is null only when there was nothing to recover |
| Recording from the `exec` path | done on Linux — `register(session)` installs K's `register_record_hook`; three execs become three steps, and a failed exec, an empty diff, `--no-record` and `record off` are never recorded |
| Re-execution when the snapshot store has evicted a prefix | done on Linux — 12 steps each adding a 361,201-vertex grid exhaust the 256 MiB budget: the first 8 prefixes become unrestorable while the last 5 stay. A `set` on a parameter only step 2 reads then falls back past the evicted prefix 1 to the base (`cached: 0`, `from_step: 1`, all 12 steps re-run) and lands on the same `digest` as a full run. 18.9 s of the test's 55 s |

### D — describe schema and corrective errors

Done when: `describe channel` returns the request and event set with field
types; `describe schema` returns a JSON-schema projection of every request
suitable for a tool catalog; an `exec` whose misspelling has one certain
correction — the only candidate above difflib's 0.6 cutoff, or 0.85 with
more than 0.05 over the runner-up — carries `error.fix.code` that runs
successfully as-is; and ambiguous misspellings carry no `fix`.

Owns: `agent_rna.py`, `tests/agent/describe.py` (new; the RNA portions of
`tests/agent/protocol.py` and of the deleted `tests/agent/compare.py` moved
here in coordination with K).

| Item | Status |
|---|---|
| `describe channel` and `describe schema` generated from the request table K exposes | done on Linux — read from K's `agent_runtime.REQUESTS`/`EVENTS`/`DEFS`, never a second copy; nine ops, nine events; each op's schema is a self-contained draft 2020-12 document whose `$defs` hold only the shapes it reaches, and every op's `example` validates against it. `mutates` is dispatch policy and is not projected. Both exclusive choices the contract makes — `exec` code-or-script and `fit_param` name-or-path — are declared as `exactly_one_of` in K's table and project to `oneOf`, and the test asserts the projection is exactly as strict as the table, never stricter. One-off external conformance evidence, outside ctest and adding no test dependency: `uv venv /tmp/schemacheck && uv pip install jsonschema` (4.26.0) in a throwaway venv, then `Draft202012Validator.check_schema` over all nine documents and `iter_errors(example)` for each — nine `check_schema OK`, nine `example valid`, and five malformed requests rejected, including exec with neither code nor script and a fit parameter that is neither name nor path |
| `fix` on unambiguous attribute, enum and operator-keyword errors; never on ambiguous ones | done on Linux — `error_fields` contributes `rna` and `fix` to the real `error` event. `tests/agent/describe.py` fails a statement through the binary, then re-executes the `fix.code` it answers with: `locaton`→`location` (0.93), operator enum `type='MESHES'`→`'MESH'` (0.80, sole candidate), operator keyword `sise`→`size` (0.75, sole candidate), `bevel_dept`→`data.bevel_depth` (0.95) through the one-hop `data.` search, and a multibyte source line proving the rewrite is a UTF-8 byte edit at the failing position. `rotation_mode='XYZY'` scores 0.857 against both `XYZ` and `XZY` and carries no `fix`; neither does an identifier with no candidate at all |
| `describe` records for the new `agent` helpers | done on Linux — `describe agent` answers for all twelve helpers now present (`compare`, `describe`, `diff`, `fit`, `history`, `objective`, `observe`, `perceive`, `program`, `register_provider`, `rollback`, `snapshot`), each with a signature, docstring and parameter defaults from `inspect.signature`. The test asserts every record is well formed rather than a fixed list, so later helpers are covered without editing it |

### W — CLI projections, documentation, removal of the comparison verb

Done when: every request has exactly one CLI projection whose flags map
one-to-one to request fields; `blender-cli compare` no longer exists and
its tests are gone; `README.md`, `doc/agent/usage.md` and `doc/agent/design.md`
describe only the current surface; and a fresh orb can follow
`usage.md` from `repl` to a fitted model without consulting anything else.

Owns: `README.md`, `doc/agent/usage.md`, `doc/agent/design.md` (request
sections only; K owns *Channel protocol*), `source/blender/agent/agent_cli.hh`,
`source/blender/agent/agent_cli_gen.py`, the codegen wiring in
`source/blender/agent/CMakeLists.txt`, `tests/agent/cli.py`.

| Item | Status |
|---|---|
| Remove `compare` verb, its parser and tests; `target set` is the only CLI entry to metrics | done — no `compare` verb, parser entry or test remains; `tests/agent/package.py` scores through `agent.compare` inside `exec` |
| Every request field has exactly one CLI projection, generated from `REQUESTS` | done on Linux — `agent_cli_gen.py` reads `agent_contract.py` at build time and emits `agent_cli_table.hh`; `agent_cli.hh` parses and prints `--help` from it. P's `from_step` field produced `--from-step` with no C++ edit. `tests/agent/cli.py` (47 s) checks the built binary's `describe channel` against the generated projection, that `--help` shows every flag, and that each flag reaches its field |
| `target`, `fit`, `program`, `repl`, `session status/feedback` CLI projections and help | done on Linux — `--help` is generated; `program rollback <version>` and `session rollback <id>` are both positional, `session feedback KEY=VALUE…`, `describe schema` (no `--schema`), `exec --no-record/--timeout/--image`, and `@FILE`/`-`/`@@` value sources |
| `usage.md` rewritten around the channel loop: repl, feedback budgets, targets, fit, program, recovery | done on Linux — every command on the page was run against the built binary and its output pasted, including a `repl` transcript whose second action answers with a changed region of `[43, 11, 213, 245]` and a delta crop, and a targets → `fit` walk-through where 12 evaluations over one program parameter took `iou` from 0.695 to 0.979 in 27 s and recovered a height of 2.97 against a reference rendered from 3.0 |
| `README.md` reflects the current surface and quick start | done |

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
| Dogfood run on the completed Linux build; friction list filed to owners | run 1 done on Linux at `b0b287bc`; items filed below; run 2 pending the fixes |
| Dogfood run 2 on the build with the run-1 items closed; token and round-trip delta against run 1 recorded here | done on Linux at `91a0ba2f` |

Run 1 (thread `T-01a073fe-9ff0-7699-9b6c-68d9e0d26220`; painter mug,
front elevation; cylinder body, x-scaled torus handle, Solidify wall): 11
channel requests — 9 that did work, 1 `session status` forced by the
recovery verdict not being pushed, 1 deliverable `observe` — plus 4
launcher invocations to open or recover the channel. Objective trajectory
(front, 256 px, mask auto, fit bbox): `target set` on the factory cube
IoU 0.626 → body 0.586 → handle 0.903 → hollow body 0.903 (no render,
`fraction` 1.5e-05) → `program set` promoting seven parameters 0.903 with a
byte-identical picture → `fit` coordinate, 60 evaluations, 148.9 s, 0.935 →
`fit` nelder-mead, 40 evaluations, 101.0 s, 0.936 → SIGKILL during a bevel
→ reopen `recovered_from: "program"`, `program run` `cached: 4, ran: []`,
IoU 0.935921258928053 digit for digit, the bevel from the killed request
preserved. Final IoU 0.9359, chamfer 2.28. Pushed events: 160, 49,705
bytes, ~12,426 tokens; ~58 % were tokens the agent could not use —
`progress` 42 % (13 of 100 events improved the best and those are already
in `done.curve`), GUI datablocks 71 % of `diff` bytes, a numpy subnormal
warning 741 tokens per session. No exception occurred, so no corrective
`fix` was observed. Items filed, with the ruling each owner builds to:

- K: a request killed with the process ends with an `error` of type
  `Crashed` on the same pipe; `repl` recovers a dead session itself by
  reopening through `Session.__init__` (the newest source wins, decided
  in P's `recover()`), keeps serving the pipe, and exits non-zero only
  when recovery itself fails; every channel opens with a `session` event
  carrying the `session status` shape, so the recovery verdict is pushed
  and never asked for; `repl`'s own failures are one
  compact line; `diff` never lists UI datablocks (`WINDOWMANAGER`,
  `SCREEN`, `WORKSPACE`, `BRUSH`, `PALETTE`, the `Render Result` and
  `Viewer Node` images); the numpy subnormal warning is silenced once in
  the runtime before any provider imports numpy; `progress` joins the
  feedback policy (`all|improvements|off`, default `improvements`) and
  `budget` gains `patience` and `tolerance`.
- T: `fit` emits `progress` per the policy and stops on `patience`
  evaluations without an improvement above `tolerance`, reporting
  `stopped` in `done`; the objective provider leaves each target's
  silhouette error masks at the budget size in session state for the
  image provider.
- F: the `error` image kind design.md already describes — the worst
  region's missing/extra map against the reference — is pushed after an
  action under the image budget, so the picture that settled run 1 (only
  `fit`'s `error_map` produced it) arrives with every scoring.
- W: `usage.md`'s crash section documents the plain reopen that rebuilds
  from the program and `repl`'s recovery behaviour once K lands it.
- P: `recover()` always recovers and the newest source wins. W found that
  a plain reopen after a crash recovers nothing when the autosave is
  newer than the program (`recovered_from: null`, empty scene, targets
  still registered; which branch runs was an 11 ms mtime race). Autosave
  newer → it is loaded, `recovered_from: "autosave"`; else the program
  replays, `"program"`; null only when nothing existed. `repl`'s own
  recovery reopens through the same code. `program get` is unchanged: it
  is the read half of editing the program, not a look at scene state.

Run 2 (thread `T-01a073fe-9ff0-7699-9b6c-68d9e0d26220`; same painter mug,
same plan, on `91a0ba2f` with every run-1 item closed): 11 channel
requests — none look-only — and 1 launcher invocation, against run 1's 11
requests with 1 look-only and 4 invocations. Pushed events 160 → 67,
49,705 → 24,495 bytes, ~12,426 → ~6,123 tokens (−51 %) for a run that did
more work: GUI datablocks 2,015 tokens → 0, the numpy warning 741 → 0,
`progress` 100 events/5,224 tokens → 12 events/592 tokens with all 12
improvements, so the share the agent could not use fell from ~58 % to
~10 %. `fit` derived `patience` 25 and both searches ended at the
coordinate step floor with `stopped: "patience"`: 102 evaluations and
249.9 s became 89 and 229.6 s. The trajectory agrees with run 1 step for
step in shape — handle +0.31, hollow and parameterisation exactly
0.000000, first fit +0.03 — on a new scale, since `fit bbox` now
normalises the model too. The `error` image changed one decision and it
is the difference between the runs: after fit #1 it showed a red bar of
constant width down the body's right edge, which a straight vertical edge
identifies as body width, not handle parameters; run 1 saw only `worst`'s
`missing`/`extra` numbers, searched the handle again and gained +0.0013
in 40 evaluations, while run 2 promoted the `radius=0.5` literal to
`P["body_r"]` in one `program set` and re-fitted for +0.0286 (IoU 0.9251
→ 0.9537, chamfer 2.65 → 1.57) in 42 evaluations — the same cost for 22×
the gain. The crash was answered on the pipe: the killed request ended
with `error` type `Crashed` carrying `recovered_from: "program"`, the
greeting re-announced it, targets survived, `repl` never exited, and
`program run` answered `cached: 4, ran: []` with the bevel from the
killed request in the scene, scoring 0.953755 against 0.953700 before
the crash. Byte-identical program text reproduced run 1's `version
d4d4d742…` and `digest 7805c4e8…` across builds and processes. Again no
exception occurred, so no corrective `fix` was observed. Evidence in the
run's orb at `.amp/in/artifacts/run2/`. One item filed, with the ruling:

- T: `fit`'s `done` sends `cancelled` beside `stopped`, and `cancelled`
  is exactly `stopped == "cancel"`; a field the agent can compute does
  not earn its tokens, so `cancelled` leaves the wire, the contract table
  and the tests in one commit, and `stopped: "cancel"` is the only way a
  cancelled search reports itself.

### I — model IO

Owns: `tests/agent/io.py`, `tests/agent/CMakeLists.txt`,
`tests/agent/package.py` (IO smoke), `agent_rna.py` (trimmed-format errors),
`packaging/package.py` (IO dependencies if needed), `doc/agent/usage.md`
(model IO recipe), and this workstream's evidence.

Done when: binary-built OBJ, FBX, STL, PLY, glTF/GLB and blend models survive
export → empty scene → import with format-appropriate geometry, material and
UV checks; recorded absolute-path imports reproduce the live digest in a fresh
process at another cwd; imported objects emit edit feedback and support RNA
target fitting; the trimmed package passes the same IO test and operator polls
after factory reset; USD/Alembic imports fail clearly without crashing; and
all agent CTests pass. No import/export verb or wire change.

| Item | Status |
|---|---|
| Protocol round trips, cross-cwd program replay, imported-object edits/fit and trimmed package IO | done on Linux — `agent_io` 232.07 s; all eight paths pass installed/trimmed with Vulkan and forced `device: null`; 13 operator polls and both add-on preferences survive factory reset |
| Tested usage recipe and format-preservation table | done — `usage.md` contains the actual default-normal GLB exchange (24 vertices / 12 triangles), recorded import, explicit selection export, new-process dimensions `(3.0, 2.0, 2.0)`, format table, loss tolerances and path/axis/unit/add-on/selection gotchas |
| Product-platform IO evidence | unverified — Linux development evidence does not prove macOS/Windows IO |

Linux orb evidence (2026-09-07, xPack GCC 14.3.0, software Vulkan/lavapipe):
`.agents/setup`, agent-profile configure and `cmake --build build/orb --target
install` exit 0, warnings visible. Full `ctest --test-dir build/orb -R agent
--output-on-failure`: **100% tests passed, 0 tests failed out of 9**,
1651.23 s. Individual seconds: protocol 37.90, describe 29.38, CLI 70.65,
session 470.71, program 54.84, IO 232.07, observe 111.29, feedback 86.96,
fit 557.40. The fetched comparison fix and platform-report changes were merged
without conflict; the installed Python was refreshed before the fit test ran.

`tests/agent/package.py` runs `io.py` against the output of the existing
packaging script, so it adds no second package-layout implementation. Installed
and trimmed sessions pass all 13 import/export polls after
`read_factory_settings(use_empty=True)`, including native `wm.fbx_import`
and the retained FBX add-on. Render equality SHA-256:
`9d5aaaa2a3fa70ae5c1779de339ea709bce8d07f86e360afd5de1e14352ba835`.
Forced `VK_DRIVER_FILES=/tmp/no-driver.json` runs pass both the installed IO
test and package smoke/trimmed IO with `device: null`: no pushed feedback,
`fit` returns `NoDevice`, but geometry, exports, program replay and errors are
all checked, exit 0 rather than skipping the non-render subject.

Each row builds its source file with a separate one-shot binary, imports it
through `repl`, checks counts/bounds/materials/UV mapping, copies only the
program directory to another cwd, and verifies fresh `session open` recovery
plus `program run` against the live digest. A transform emits `diff` and
`perception`, and nine RNA-fit evaluations against the imported model's own
reference improve IoU to >0.98. Another real transform is then exported and
read by another one-shot process. The same digests hold in installed/trimmed
and device/no-device runs:

| Path | Vertices / faces | Trimmed Vulkan wall (s) | Imported / fresh replay digest (sha256) |
|---|---|---|---|
| OBJ | 8 / 6 | 27.20 | `2a779d2da11239a7fc78a8779e44c0e8e0fd8fc8b423d3c89b17df277ee9c863` |
| FBX native | 8 / 6 | 28.03 | `5f64a0496657e2ded2f4d498760d9ce7ae0ca99014d9188ece1c73a39db5c894` |
| FBX add-on | 8 / 6 | 26.79 | `7cee89f413d7d2b212b61cecdf2e3f529d4bfd1067ea6377fae92413c4a93853` |
| STL | 8 / 12 | 27.69 | `b217afa8815dc90686d541197c13760f05d0ee8424beed5e8a326365911b4eda` |
| PLY | 8 / 6 | 27.46 | `8ed5a8fdc4cb25c2dd2e7357b97c0913cabe4a7dac09af38f3ab596a579f3748` |
| glTF | 8 / 12 | 28.39 | `ba98fe338dde7f5f452a2b859d2656e0fae717ba1adfe9a691f4bc0e18e8e4b8` |
| GLB | 8 / 12 | 28.82 | `ba98fe338dde7f5f452a2b859d2656e0fae717ba1adfe9a691f4bc0e18e8e4b8` |
| blend | 8 / 6 | 27.41 | `01c2ddad92473fc1c1afff21e5965c7330b3d453b00fe33bdd21de2a2563bde8` |

Defects/decisions: no missing importer, factory-reset add-on failure, package
dependency loss, import crash or digest mismatch was found. USD/Alembic
returned `AttributeError` with only “could not be found” and unrelated
STL/PLY/OBJ/FBX nearest identifiers. `agent_rna.py` now uses live
`bpy.app.build_options` to supply “support is not built in” in the existing
`rna.description`, with empty nearest and no spurious fix. The original
type/message/line remain unchanged, as the design requires; no wire shape,
operator wrapper or upstream file changed. The real agent recipe in
`usage.md` demonstrates the diagnostic now answers why the format failed
in that same response rather than requiring a build-options/describe probe.
Absolute external paths remain conservatively `reproducible: false`; the
test proves actual replay while the immutable input exists, not portability
after that file is removed. Normal/UV seams and triangulation are format
semantics, documented rather than treated as defects. Rigs, animation,
texture-file round trips and native macOS/Windows IO remain unverified.

### X — product platforms

Done when: all agent tests pass on macOS arm64 and Windows x64 from the
manual workflows, package sizes are re-measured after the feature work, and
`build-profile.md` records them. Not scheduled until every workstream above
is `done` on Linux. Linux work is complete; native verification is now running.

Owns: `.github/workflows/agent-*.yml`, `doc/agent/build-profile.md`,
`packaging/package.py`, `tests/agent/package.py`.

| Item | Status |
|---|---|
| macOS arm64 full run of all agent tests on the final surface | done — final [run 34114563805](https://github.com/fran0220/blender-cli/actions/runs/34114563805) at [6a276a08](https://github.com/fran0220/blender-cli/commit/6a276a08c0d3e42d586f5391a86adfdf67744bc4): 8/8 installed, 8/8 trimmed, Metal, package smoke and byte equality pass |
| Windows x64 full run, including AF_UNIX/process-exit, handle inheritance, Vulkan loader probe and DLL/manifest trim | software-Vulkan 8/8 installed and 8/8 trimmed pass at [5aac050c](https://github.com/fran0220/blender-cli/commit/5aac050c718b930ffbba63fb802f69ec234ebbca), [run 34101160262](https://github.com/fran0220/blender-cli/actions/runs/34101160262); final 6a276a08 run above pending; Windows 11 hardware unverified |
| Re-measured package sizes on both product platforms | macOS 34114563805 validated: installed 742,747,303 B, trimmed 346,212,598 B, tar.zst 72,837,002 B. Windows 34101160262 validated: installed 770,030,373 B, trimmed 290,678,527 B, ZIP 104,141,664 B; final Windows measurement pending |

The workflow's stale four-script package loop (including deleted `compare.py`)
is replaced with all eight scripts. Installed CTests and package verification
each have a 120-minute step budget inside the 360-minute job; CTest's individual
timeouts remain unchanged (fit: 3600 s). YAML parsing and every workflow shell
body's `bash -n` pass. Native completion and validated package measurements are pending;
X monitors Actions every 20 minutes and routes product defects to their owners.

First native results (2026-09-07, [run 34078946155](https://github.com/fran0220/blender-cli/actions/runs/34078946155)): macOS protocol 72.28 s, describe
40.04 s, cli 81.50 s, session 172.71 s, program 46.97 s, observe 50.60 s all
pass with no skips. Metal observation repeats hash `514eabae…` byte-for-byte.
Feedback fails at `tests/agent/feedback.py:129` (2.05 s), fit at
`tests/agent/fit.py:179` (13.53 s): returned paths resolve `/var` to
`/private/var`, but the tests compare against an unresolved temporary root.
F and T were asked to resolve their roots without weakening the location checks.
X's package smoke catches `CYCLES` missing from the trimmed session's render
engine enum. Upstream `addon_utils.reset_all()` enumerates add-on directories,
not general modules: preference-driven Cycles and pose_library must stay in
`addons_core`. The package layout and explicit factory-reset smoke assertion
are corrected, native verification pending. Trimmed suite did not run because
smoke failed.

Windows results from the same run: protocol fails 5.68 s, describe 8.50 s,
program 1.79 s on the first successful mutation/default feedback: Vulkan
instance initialization fails, the OpenGL fallback reports missing WGL
extensions, then `EXCEPTION_ACCESS_VIOLATION` (0xC0000005). K/F were notified
to make missing-device feedback safe rather than hiding it with test skips.
CLI fails 1.26 s (`KEY=VALUE` ellipsis becomes U+FFFD in the contract but not
help); session fails 1.18 s on the 600-character Chinese value round trip
after ten ASCII execs pass. K/W own the encoding diagnosis. Observe (0.20 s),
feedback (0.21 s), fit (0.19 s) skip with code 77 because the bundled Vulkan
loader reports `VK_ERROR_INCOMPATIBLE_DRIVER`; these are not passes or render
evidence. Package smoke's original session also crashes on mutation, so the
trimmed DLL/manifest runtime and complete session recovery remain unverified.
The hosted `windows-2022` runner has no usable Vulkan ICD; completing Windows
render evidence needs a Windows environment with one. Per coordinator ruling,
X adds a pinned, checksum-verified, cached mesa-dist-win 25.0.7 lavapipe ICD
outside the package, registered in the disposable runner's driver registry; a bundled-loader
probe must pass before the suite. This is software-Vulkan evidence, not
Windows 11 GPU-hardware evidence. Final Windows hardware and macOS Metal-device
rows remain unverified until the final revision is exercised on those devices.
K owns missing-device probing/errors plus both encoding defects and will request
native retries; final dispatch otherwise waits for the coordinator's all-landed
confirmation.

Retry 34083367870: X's environment-only ICD selection still returns no driver.
The next workflow uses native backslash paths and HKLM registration because
the Windows loader ignores environment overrides in elevated processes; loader
debug output is enabled on preflight to distinguish discovery/load failures.
The run does prove K's no-device safety and X's package Cycles fix: original
and trimmed smoke pass factory reset, Cycles assignment, inspect and describe;
render equality skips77. Trimmed describe and program pass, protocol fails
because provider NoDevice diagnostics join stderr, CLI passes the encoding
checks then fails its expected full image, and session passes Unicode/raw
AF_UNIX/20 round trips before failing the reopen after missing-file startup
(`tests/agent/session.py:314`, alive-but-unresponsive PID; sent to K).
Observe/feedback/fit skip77. Measured Windows bytes: installed 759,137,531,
trimmed 290,677,679, ZIP 104,141,306; still not validated rendering/package evidence.

X resumed on coordinator instruction: Windows-only [run 34100128906](https://github.com/fran0220/blender-cli/actions/runs/34100128906)
at [95169391](https://github.com/fran0220/blender-cli/commit/95169391efb8799e4b51ff64463a04605dd3ebed)
validates native-path/HKLM ICD discovery with loader debug output, and the
runtime greeting's `device` verdict. K's startup cleanup and W's no-device CLI
expectation fix are not prerequisites for this diagnostic run. Results are
recorded below; the final both-platform run follows the coordinator's all-landed
signal and covers macOS path fixes/Cycles plus all eight Windows tests.

K-requested Windows retry [34101160262](https://github.com/fran0220/blender-cli/actions/runs/34101160262)
at [5aac050c718](https://github.com/fran0220/blender-cli/commit/5aac050c718b930ffbba63fb802f69ec234ebbca)
adds failed-startup PID/endpoint cleanup and W's device-aware CLI assertions.
It passes all eight installed CTests with no CTest skips: protocol 222.08 s,
describe 118.31 s, cli 84.85 s, session 1108.22 s, program 135.47 s,
observe 334.13 s, feedback 150.58 s, fit 1309.31 s. All eight trimmed scripts
exit zero. Session reaches the complete startup and `repl` crash-recovery
assertions; program recovery and CLI status report `device: "vulkan"`.
Loader debug explicitly says elevated processes ignore `VK_DRIVER_FILES`,
locates the lavapipe JSON in HKLM and loads `.\vulkan_lvp.dll`. Package Cycles
and before/after byte equality pass (SHA-256 `9d5aaaa2…`). Size: installed
770,030,373 B, trimmed 290,678,527 B, ZIP 104,141,664 B, valid for this revision.
No-device subcases using an environment override cannot remove the registry
ICD on an elevated runner; this successful run is software-device evidence.

Earlier diagnostic 34100128906 also proves working ICD/rendering, but protocol
times out at 240.02 s and session at `session.py:125` times out its 30 s
`agent.compare('missing.png', 'front')` call. Describe 191.22 s, cli 122.74 s,
program 220.11 s, observe 541.37 s, feedback 242.85 s, fit 2063.50 s pass.
Trimmed scripts pass except the same session call timeout. This runner is
slower, and the timeout risk is reported to K/coordinator, not hidden by a skip.
The final both-platform run 34114563805 on 6a276a08 started immediately after
these runs completed, as requested; its results and final sizes are pending.

Its macOS job is now successful: protocol 69.23 s, describe 33.33 s,
cli 67.62 s, session 194.29 s, program 65.23 s, observe 70.43 s,
feedback 29.68 s, fit 367.60 s; all eight installed CTests pass without skips.
All eight trimmed scripts exit zero, with `device: metal`. Package Cycles
factory-reset smoke and original/trimmed byte equality pass (SHA-256
`9d5aaaa2a3fa70ae5c1779de339ea709bce8d07f86e360afd5de1e14352ba835`).
Validated macOS bytes: installed 742,747,303, trimmed 346,212,598,
tar.zst 72,837,002. F/T's canonical-path fixes and X's Cycles layout fix are
verified on macOS.

The Windows leg completed with the two known timeout causes: protocol reaches
its 240.00 s CTest budget; session fails at `session.py:125` when missing-reference
comparison exceeds the 30 s subprocess budget. Describe 190.58 s, cli 121.47 s,
program 221.48 s, observe 540.94 s, feedback 241.66 s and fit 2085.22 s pass.
Trimmed scripts pass 7/8 (only the same session timeout fails). Package smoke
and byte equality pass, but the full package gate fails; measured bytes
770,039,611 installed / 290,678,958 trimmed / 104,141,853 ZIP remain provisional.
Per coordinator's conditional authorization, Windows-only
[run 34128930729](https://github.com/fran0220/blender-cli/actions/runs/34128930729)
was dispatched at [26b6c839](https://github.com/fran0220/blender-cli/commit/26b6c839a49264d77186e9a864b039aa8a887e2e).
It includes K's measured-platform harness budgets and T's reference-load-before-render
fix; this retry supplies the final Windows number. macOS evidence stands unchanged.

Retry 34128930729 completes with protocol 294.64 s, describe 160.94 s,
cli 99.91 s, session 1281.04 s, program 176.59 s, observe 421.48 s and
feedback 191.57 s passing. Newly added `agent_io` also passes (1159.13 s).
Fit fails after 1363.24 s, not by timeout: T's new `fit.py:765` assertion
requires the missing-reference path to start with `/`, but Windows returns
the correct absolute `C:\Users\runneradmin\...\nothing-here.png` path.
The same assertion fails trimmed; the other seven trimmed scripts pass.
T is asked to assert the native resolved path; this is outside the authorized
fit-timeout retry, so another dispatch awaits coordinator authorization.
Package smoke/byte equality pass; measured installed 772,171,059 B,
trimmed 290,680,354 B and ZIP 104,142,404 B remain provisional because the
complete gate fails. The new IO CTest is not yet in the eight-script trimmed
loop; coordinator is notified of this additional surface before changing scope.

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
- Picture levers never move the score. `image.samples`, `image.size`, the
  mask policy and `fit`'s `budget.size` change what the agent looks at; the
  `objective` event at the fixed objective size (256 px, silhouette pass at
  a fixed sample count) is the only number it is measured against. `fit`'s
  `curve` is the search's own reading at its `budget.size`; `done` carries
  `best.params` and `best.snapshot`, no score. Four defects resolved the same
  way (samples on the unshared path, morphology on exact references, a
  256 reference at a 260 tile, `best.score` beside `curve`); a fifth goes
  here too. Morphology applies only to estimated segmentation: alpha and
  two-valued references skip it, so `mask auto` equals `mask none` on them.
- No device is a state, not a crash. `session.device` is `null`, `"vulkan"`
  or `"metal"` in the greeting and `session status`, probed once through the
  loader before any GPU context. When null, `exec`, `program`, `inspect` and
  `describe` are untouched, render-bearing providers push nothing (no
  `images`, no `objective`, no provider log), and `observe`, `fit` and
  `agent.perceive()` end as `error` type `NoDevice`. The feedback policy
  keeps saying what the session would send. The GL fallback is never
  entered. Tests assert both branches of this contract; exit 77 is reserved
  for tests whose whole subject is a render.
- Every byte the CLI writes is ASCII: events, the folded envelope, `repl`
  error lines, `--help`. No reader chooses an encoding; tests still decode
  UTF-8 explicitly rather than trust a console code page.
- Software Vulkan (lavapipe) on a hosted Windows runner is evidence for the
  Vulkan code path, not for a Windows 11 GPU; real-device rows stay
  `unverified` until run on one.
