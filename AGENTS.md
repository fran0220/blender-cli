# blender-cli repository guidance

blender-cli is a fork of Blender that runs as a **headless, agent-serving
process**: an agent sends one statement in over a persistent channel and gets
that statement's consequences back as a stream of events — what changed in
the data, what changed in the picture, and how far the scene now is from its
target. It is not a Blender add-on, not an MCP server, not an asset pipeline
and not a smaller Blender. It is Blender with its GUI entry replaced by a
request loop built so that the only slow step in the agent's
perceive → decide → act → observe cycle is the agent's own decision.

The design is derived from two facts about the agent: each of its decisions
costs seconds and tokens, and everything it can compute it should not have to
decide. Every rule below follows from **maximise information per round trip,
minimise round trips, and keep every computable step inside the process**.

**This file is the constraint authority.** Each fact class has exactly one
owning document; fix drift toward the owner instead of copying:

- `AGENTS.md` — constraints (this file);
- `PLAN.md` — the only execution-status document;
- `doc/agent/design.md` — process model, the CLI contract and its wire shapes;
- `doc/agent/build-profile.md` — what is compiled in, what is trimmed, and how
  size is measured;
- `doc/agent/upstream.md` — how the fork tracks Blender upstream.

Upstream is [`blender/blender`](https://github.com/blender/blender). The base
is upstream `main` (currently the 5.3 development line); the fork merges
upstream forward as `doc/agent/upstream.md` describes.

## Product model

- **One process, one boundary.** The agent's code, Blender's data, evaluation,
  offscreen rendering, comparison metrics and parameter search all live in
  one process. The only crossing is agent ↔ process. It carries requests in
  and an ordered event stream out; the stream is the same whether it runs
  over the session socket, over `blender-cli repl` stdio, or folded into one
  JSON document by a one-shot CLI verb.
- **The channel is persistent; a statement is the unit of action.** The
  agent holds one connection and sends one statement at a time into a
  namespace that persists for the session. Process start-up, shell quoting
  and re-connection are never paid per action. Results stream as they are
  produced: the value and the data diff first, the picture and the metrics
  as soon as they exist.
- **Feedback is pushed, never asked for.** Every action answers with three
  channels: the structural diff (which datablocks changed and how), the
  perceptual delta (what changed in the picture: changed region, bounds,
  counts, and an image of the change when the change is large enough), and
  the objective delta (how every registered target's metrics moved). The
  agent never has to decide to look; looking is a property of the answer.
  Budgets bound the cost of each channel; they are set per session, not per
  request.
- **Python is the interface language.** The agent drives Blender by writing
  `bpy` code. There is no DSL, no typed tool catalog and no operator wrapper
  layer standing between the agent and `bpy`. What the fork adds around
  Python is self-description (`describe`, from RNA), errors that carry the
  nearest valid identifier and an executable correction, a persistent
  namespace, structured results and the `agent` helper module.
- **The scene is a program.** The source of truth for a session is
  `model.py`, a re-executable Python program the agent reads and edits.
  Executing a statement that changes data records it as a step of the
  program; editing the program re-executes it from the longest cached prefix.
  History is the program's version tree, persisted on disk under
  `.blender-cli/`; rollback is checking out a version. Memfile snapshots are
  the evaluation cache that makes re-execution cheap, never the record.
- **The surface is the request set in `doc/agent/design.md`**, and only that
  set until `PLAN.md` says otherwise: `session`, `exec`, `program`,
  `target`, `fit`, `inspect`, `observe`, `describe`, `cancel`. CLI verbs are
  one-request projections of the same set plus `repl`, which is the channel
  itself on stdio; nothing exists in the CLI that does not exist on the
  channel. Comparison is not a request: a target is registered once and
  scored on every action; the metric functions are callable from agent code
  as `agent.compare()`.
- **Observation is deterministic.** A render is offscreen with fixed camera
  presets, a fixed built-in lighting rig, fixed color management and a
  resolution ladder sized for a vision model. The same scene state always
  yields the same image. There is no viewport, no screenshot and no
  dependence on GUI state.
- **The agent decides; the process computes.** Targets (reference images
  bound to views) are registered once and scored after every action.
  Metrics (silhouette IoU, edge distance, SSIM, color-histogram distance)
  come with the region that contributes most of the error. `fit` runs a
  bounded parameter search over program parameters or RNA paths inside the
  process, with progress events and cancellation, and returns the best
  parameters, the curve and the residual error map. Qualitative acceptance
  stays with the agent.
- **Perception is layered.** Structured facts (bounds, counts, changed
  region, metric deltas) are always returned and cost tens of tokens. Images
  are returned as deltas against what the agent already saw (changed-region
  crop, before/after overlay, error heat map) and only when the change
  exceeds the session's threshold or the agent asks. Full frames are on
  demand.
- **State is a session.** A session holds one `Main` in one process, one
  persistent Python namespace, one program with its version tree, its
  targets, and a content-addressed snapshot cache built on Blender's memfile
  undo. Rollback is the control; there is no confirmation step anywhere. A
  crash loses nothing the program and its versions can rebuild.

## Non-negotiables

- **No GUI path.** The binary never enters `WM_main`. GUI source stays
  compiled where trimming it costs more upstream drift than it saves; it is
  never reachable.
- **No network, no external services.** The process opens no sockets except
  its own local session endpoint. Asset libraries, generation services and
  anything else that lives on the network are the agent's business; their
  results arrive as files and enter through `exec`.
- **No MCP, no add-on socket server, no compatibility layer** with
  `blender-mcp` or any other protocol in the product. The channel's request
  and event schema is published by `describe --schema` from the same
  registry that serves the channel, so a function-calling host can project
  it; that projection is generated, never hand-maintained, and never wraps
  `bpy`. If a shell-less host ever needs MCP, it is a separate thin adapter
  over that schema, added only when that host exists and recorded in
  `PLAN.md` first.
- **Main-thread execution.** `bpy` and RNA run only on Blender's main thread.
  Transport threads move bytes; the main loop dequeues, executes, pumps
  `BLI_timer_execute`, and streams events. One request executes at a time
  per session; `cancel` is the only request answered out of order.
- **`bpy` stays `bpy`.** The Python API is upstream's, unmodified. Agent
  knowledge of `bpy` is the asset; the fork never renames, wraps or shadows
  it. Operators that need UI context are made to work through a synthetic
  window/screen/area, not through a replacement API.
- **Product targets are Apple Silicon macOS and x64 Windows 11.** Release
  artifacts exist for `macos_arm64` and `windows_x64` only; Linux is a
  development build. Metal is the macOS GPU path (a normal build in
  background mode, not `WITH_HEADLESS`); Vulkan is the Windows path.
- **New code lives in new places.** All fork code is under
  `source/blender/agent/`, its build profile under
  `build_files/cmake/config/blender_agent.cmake`, its docs under `doc/agent/`
  and its tests under `tests/agent/`. Touches to upstream files are limited
  to the list in `doc/agent/upstream.md`, each marked with a
  `/* blender-cli */` comment, and kept to registration and build wiring.
- **Upstream is merged, never rebased.** `main` is published; upstream
  `main` is merged forward (`git merge upstream/main`). The fork never
  carries a version shim, dual code path or "legacy" branch for an upstream
  API change: when upstream changes, the fork changes with it in one commit.
- **License is GPL-2.0-or-later.** Every new file carries the same SPDX
  header as upstream. Third-party additions go through `extern/` with their
  license recorded, exactly as upstream does.
- **Identifiers are neutral.** Wire fields, file names and CMake options say
  `agent`, `session`, `snapshot`; they never carry a host product's name.
- **Current only.** The repository has one design, the one in
  `doc/agent/design.md`, with no version labels. When the design changes,
  the change removes the previous implementation, its tests and its
  documentation in the same series of commits; nothing is kept for
  compatibility — no deprecated verbs, no aliases, no transitional wire
  fields, no "legacy" modes, no dual code paths. A request, event or field
  either exists in the current contract or it does not exist in the code.

## Execution rules

- Before writing code in an area of Blender you have not worked in this
  session, read the upstream code first. The upstream conventions
  (`BLI_`/`BKE_`/`RNA_` prefixes, `blender::` namespaces, C++20, clang-format
  with the repository `.clang-format`) are the style guide. Do not import
  another project's idioms.
- Every touch to an upstream file must be explainable as one of: register the
  agent command, add the `WITH_AGENT` option, add the subdirectory. Anything
  else is a design smell; solve it inside `source/blender/agent/` or record
  the exception in `doc/agent/upstream.md` in the same commit.
- Do not add features the request set does not need. Do not add asset
  tooling, pipeline commands or curated operator wrappers; the agent writes
  code.
- **Interfaces before parallel work.** A workstream in `PLAN.md` names the
  files it owns; two workstreams never own the same file. Anything one
  workstream needs from another is declared in `doc/agent/design.md` (a
  wire shape, an event, a provider hook) before either starts, and each
  builds against the declaration. Cross-cutting additions to a response go
  through the feedback provider registry, never through editing the
  envelope assembly directly.
- Every event and field an action returns must earn its tokens: it is
  either something the agent cannot compute, or something it would have had
  to ask for in a separate round trip. Anything else is left out.
- Commit to the smallest independently explainable unit and push directly to
  `main`; no pull requests. Fetch and reconcile before pushing; confirm
  `HEAD == origin/main` and a clean worktree after. Unfinished work lands as
  a real `wip(...)` commit stating what is unverified.

## Validation

1. **The build.** A change is done when it configures with
   `cmake -C build_files/cmake/config/blender_agent.cmake` and builds
   warnings-visible on the platform it targets. Use one shared build
   directory throughout development; never delete it as part of iteration.
   Keep `lib/` submodules at the revision upstream pins for the base tag.
2. **Protocol tests.** `tests/agent/` spawns the built binary and drives it
   through its real CLI, `repl` stdio and session endpoint, comparing event
   streams, JSON results and deterministic renders (with a stated
   tolerance). These run under upstream's `ctest` conventions. There are no
   mocks of Blender, no fixtures standing in for a scene the binary could
   build itself, and no unit tests of glue.
3. **Loop evidence.** A feature that changes what an action returns is done
   when a real agent run (a dogfood transcript against a reference image,
   recorded in `PLAN.md`) shows fewer round trips or fewer tokens for the
   same result, or a result that was previously unreachable.
4. **Platform evidence.** A claim about macOS or Windows behavior is backed by
   a run on that platform. A Linux build is development evidence only.
