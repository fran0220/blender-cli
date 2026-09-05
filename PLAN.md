# blender-cli — execution plan

This is the only execution-status document. Constraints are in `AGENTS.md`;
the contract is in `doc/agent/design.md`. Each phase ends with a runnable
result and states what proves it. Status words: `todo`, `doing`, `done`,
`unverified` (landed, not yet proven on a product platform).

Base: upstream `main` (5.3 development line, forked at `5c951f2e`). Binary name: `blender-cli`.

## Phase 0 — project

| Item | Status |
|---|---|
| `AGENTS.md`, `PLAN.md`, `doc/agent/{design,build-profile,upstream}.md` | done |
| `build_files/cmake/config/blender_agent.cmake` build profile | done — configures and builds on Linux x86_64 (Debian 12, xPack GCC 14.3.0); `cmake --build build/orb --target install` exits 0 |
| GitHub fork `fran0220/blender-cli` of `blender/blender` with `main` = upstream `main` + this plan | done — Amp project `doufunao/blender-cli` |
| Amp project mapped to the fork | done — `doufunao/blender-cli` |
| Orb setup (`.agents/setup`): Debian packages + xPack GCC 14.3.0 and runtime, upstream LFS fallback, `lib/linux_x64` at the checkout's pin | done — Debian 12 x86_64: setup twice (20s / 2s), clean login selects GCC 14.3.0; upstream requires GCC/libstdc++ 14, replacing the configure-only Clang recipe |
| CI: `macos-15` (arm64) and `windows-2022` configure + build with the agent profile | todo |

## Phase 1 — a process that answers

Done when: starting with an empty `s.blend`,
`blender-cli exec -c 'import bpy; bpy.ops.mesh.primitive_cube_add()' --file s.blend --save --json`
followed by `blender-cli inspect --file s.blend --json` prints the cube as JSON,
on Linux. Proven on Debian 12 x86_64 with GCC 14.3.0; product-platform evidence
remains Phase 5.

| Item | Status |
|---|---|
| `WITH_AGENT` CMake option; `source/blender/agent/` subdirectory wired from `source/blender/CMakeLists.txt` | done — agent-profile configure, compile, link and install pass |
| `agent` `CommandHandler` registered from `creator.cc` (`BKE_blender_cli_command_register`), dispatching the six verbs; `blender-cli` is a launcher that runs `blender --command agent …` | done — installed `--help`, `--version` and six-verb protocol checks pass; future verbs return `NotImplemented` |
| `exec` one-shot: run code on the main thread, capture stdout/stderr, return the ID diff (added / changed / removed datablocks) | done — real cube add/remove, evaluated transform/geometry tags, no-op, captures, final expression, exceptions and cooperative timeout pass |
| `inspect` from RNA: scene, objects, materials, modifiers, armatures; `--object`, `--full`; never truncated | done — saved cube reports 8 vertices / 12 edges / 6 faces; full nodes/modifiers, bones, cameras, lights, collections and scalar/array RNA selection pass |
| `--file` load / `--save` write around a one-shot call | done — real installed-process round-trip, paths with spaces, factory startup, missing-file error and no save after failure pass |
| First protocol test in `tests/agent/` | done — `ctest --test-dir build/orb -R agent --output-on-failure`: agent_protocol passes (11.67s) |

## Phase 2 — session

Done when: `session open` starts a daemon, ten `exec` calls share a
namespace and variables, `snapshot` and `rollback` restore geometry, and the
round trip for a trivial `exec` is under 10 ms on Linux.

| Item | Status |
|---|---|
| Session endpoint: `AF_UNIX` socket (macOS, Linux, Windows 10 1803+), JSON lines, one request in flight | todo |
| Main loop: dequeue → execute → `BLI_timer_execute` → answer; cancellation via `G.is_break` | todo |
| Persistent Python namespace per session (`agent` helper module preloaded) | todo |
| Snapshot chain on memfile undo; `snapshot`, `rollback <id>`, `history`; `session save` writes the `.blend` | todo |
| `blender-cli <verb>` auto-connects to the session for the current directory when one exists, else runs one-shot | todo |

## Phase 3 — observation

Done when: `observe --views front,side,top,persp` returns one contact sheet
whose bytes are identical across two runs on the same platform, and
`bpy.ops.mesh.*` edit-mode operators succeed inside `exec` without a GUI.

| Item | Status |
|---|---|
| Synthetic `wmWindow` / `bScreen` / `VIEW_3D` area so context-dependent operators run headless | todo |
| Offscreen EEVEE render through `WM_init_gpu_offscreen`; Metal on macOS via a normal background build, Vulkan on Windows | todo |
| Camera presets (front, back, left, right, top, bottom, persp, `camera`), auto-framing on the scene or a named object | todo |
| Built-in lighting rig, fixed view transform, fixed resolution ladder (512 / 768 / 1024) | todo |
| Passes: color, wireframe, silhouette, normal, depth | todo |
| Contact sheet composition; `--ref` side-by-side and overlay | todo |
| `exec --observe` returns the image in the same round trip | todo |

## Phase 4 — closing the loop inside the process

Done when: agent code inside `exec` calls `agent.compare(ref, "front")` in
a loop over a parameter range and returns the best IoU without a single
image leaving the process.

| Item | Status |
|---|---|
| `compare --ref --view [--metric]`: silhouette IoU, edge Chamfer distance, SSIM, color-histogram distance; same functions exposed in the `agent` module | todo |
| Reference preprocessing in-process: background removal (classic CV), silhouette extraction | todo |
| RNA-aware errors: on `AttributeError` / wrong enum / out-of-range, answer with the nearest valid identifiers and types from RNA | todo |
| `describe <rna path>`: signature, properties, enum items, ranges, from live RNA | todo |

## Phase 5 — size, packaging, platforms

Done when: measured package sizes on both product platforms are recorded in
`doc/agent/build-profile.md`, and the Phase 1–4 protocol tests pass on
macOS arm64 and Windows x64.

| Item | Status |
|---|---|
| Configure and build the agent profile on macOS arm64 and Windows x64 | todo |
| Per-component size measurement; adjust the profile from numbers, not guesses | todo |
| Packaging trim: `addons_core` → glTF, FBX, Rigify; Python stdlib pruning; datafiles (one font, one studio light, no locale, no icons) | todo |
| Release artifacts: `blender-cli-<version>-macos-arm64.tar.zst`, `…-windows-x64.zip` | todo |

## Phase 6 — hosts (not started, not scheduled)

- Sophon mounts `blender-cli` as a local stdio Service once Sophon's
  Services support a stdio transport. That change lives in Sophon's own
  repository; nothing here.
- An MCP adapter is added only when a shell-less host is actually in use.
  Recording that host here comes before any adapter code.

## Open decisions

- Whether `WITH_OPENVDB` stays in the profile (voxel remesh needs it; it is
  large). Default: stays until Phase 5 numbers say otherwise.
- Whether Cycles CPU stays for baking or Phase 5 shows EEVEE baking covers
  the need. Default: stays.
- The `agent` Python helper module's exact surface (`observe`, `compare`,
  `snapshot`) is fixed in Phase 2 and recorded in `doc/agent/design.md`.
