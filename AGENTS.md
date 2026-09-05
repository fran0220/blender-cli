# blender-cli repository guidance

blender-cli is a fork of Blender that runs as a **headless, agent-serving
process**: an agent sends Python code in, and gets JSON and one image out. It
is not a Blender add-on, not an MCP server, not an asset pipeline and not a
smaller Blender. It is Blender with its GUI entry replaced by a request loop
built for the agent's perceive → decide → act → observe cycle.

**This file is the constraint authority.** Each fact class has exactly one
owning document; fix drift toward the owner instead of copying:

- `AGENTS.md` — constraints (this file);
- `PLAN.md` — the only execution-status document;
- `doc/agent/design.md` — process model, the CLI contract and its wire shapes;
- `doc/agent/build-profile.md` — what is compiled in, what is trimmed, and how
  size is measured;
- `doc/agent/upstream.md` — how the fork tracks Blender upstream.

Upstream is [`blender/blender`](https://github.com/blender/blender). The base
is the **5.2 LTS** line, first anchored at tag `v5.2.1` (maintained upstream
until July 2028).

## Product model

- **One process, one boundary.** The agent's code, Blender's data, evaluation,
  offscreen rendering and comparison metrics all live in one process. The
  only crossing is agent ↔ process, and it carries text in and JSON plus at
  most one image out per round trip.
- **Python is the interface language.** The agent drives Blender by writing
  `bpy` code. There is no DSL, no typed tool catalog and no operator wrapper
  layer standing between the agent and `bpy`. What the fork adds around
  Python is self-description (`describe`, from RNA), RNA-aware error reports,
  a persistent namespace and structured results.
- **Six verbs**, and only six until `PLAN.md` says otherwise:
  `session`, `exec`, `inspect`, `observe`, `compare`, `describe`. Their
  contract is in `doc/agent/design.md`.
- **Observation is deterministic.** `observe` is an offscreen render with fixed
  camera presets, a fixed built-in lighting rig, fixed color management and a
  resolution sized for a vision model. The same scene state always yields the
  same image. There is no viewport, no screenshot and no dependence on GUI
  state.
- **Quantitative comparison lives in the process.** `compare` returns numbers
  (silhouette IoU, edge distance, SSIM, color-histogram distance) between a
  reference image and a preset view, and the same functions are callable from
  agent code inside `exec`, so a numeric fit runs in one round trip.
  Qualitative judgment stays with the agent.
- **State is a session.** A session holds one `Main` in one process, a
  persistent Python namespace and a content-addressed snapshot chain built on
  Blender's memfile undo. Rollback is the control; there is no confirmation
  step anywhere.

## Non-negotiables

- **No GUI path.** The binary never enters `WM_main`. GUI source stays
  compiled where trimming it costs more upstream drift than it saves; it is
  never reachable.
- **No network, no external services.** The process opens no sockets except
  its own local session endpoint. Asset libraries, generation services and
  anything else that lives on the network are the agent's business; their
  results arrive as files and enter through `exec`.
- **No MCP, no add-on socket server, no compatibility layer** with
  `blender-mcp` or any other protocol in the product. If a shell-less host
  ever needs MCP, it is a separate thin adapter over the same registry, added
  only when that host exists and recorded in `PLAN.md` first.
- **Main-thread execution.** `bpy` and RNA run only on Blender's main thread.
  Transport threads move bytes; the main loop dequeues, executes, pumps
  `BLI_timer_execute`, and answers. One request is in flight per session.
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
  release tags are merged forward (`git merge v5.2.x`). The fork never
  carries a version shim, dual code path or "legacy" branch for an upstream
  API change: when upstream changes, the fork changes with it in one commit.
- **License is GPL-2.0-or-later.** Every new file carries the same SPDX
  header as upstream. Third-party additions go through `extern/` with their
  license recorded, exactly as upstream does.
- **Identifiers are neutral.** Wire fields, file names and CMake options say
  `agent`, `session`, `snapshot`; they never carry a host product's name.

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
- Do not add features the six verbs do not need. Do not add asset tooling,
  pipeline commands or curated operator wrappers; the agent writes code.
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
   through its real CLI and session endpoint, comparing JSON results and
   deterministic renders (with a stated tolerance). These run under upstream's
   `ctest` conventions. There are no mocks of Blender, no fixtures standing
   in for a scene the binary could build itself, and no unit tests of glue.
3. **Platform evidence.** A claim about macOS or Windows behavior is backed by
   a run on that platform. A Linux build is development evidence only.
