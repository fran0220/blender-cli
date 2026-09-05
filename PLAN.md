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
| CI: `macos-15` (arm64) and `windows-2022` configure + build with the agent profile | done — [run 33964004642](https://github.com/fran0220/blender-cli/actions/runs/33964004642): AppleClang 17.0.0 and MSVC 19.44.35228 configure and build bf_agent + blender-cli successfully, with target-local fatal warnings |

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

Proven on Debian 12 x86_64 with GCC 14.3.0: agent-profile configure/install
returns `BUILD_EXIT=0`; both agent CTests pass. A separate 20-launcher-call
run measured median 5.453 ms, min 5.311 ms, max 5.775 ms (strict 10 ms median
bound, no added tolerance). Windows AF_UNIX/daemon code is structurally
present but unverified; macOS is also unverified. Neither is product-platform
evidence until run there.

| Item | Status |
|---|---|
| Session endpoint: `AF_UNIX` socket (macOS, Linux, Windows 10 1803+), JSON lines, one request in flight | done — Linux real endpoint, ordered pipelined IDs and full 200 KB response pass; product platforms unverified |
| Main loop: dequeue → execute → `BLI_timer_execute` → answer; cancellation via `G.is_break` | done — idle Python timer, second-connection cancellation and subsequent exec pass |
| Persistent Python namespace per session (`agent` helper module preloaded) | done — ten dependent execs and helper snapshot/rollback/diff/history pass; observation and comparison are delivered in Phases 3–4 below |
| Snapshot chain on memfile undo; `snapshot`, `rollback <id>`, `history`; `session save` writes the `.blend` | done — cube 8 → 26 → 8 vertices; branch retention, labels, ~N, operator undo coexistence, Main replacement and save/reload pass |
| `blender-cli <verb>` auto-connects to the session for the current directory when one exists, else runs one-shot | done — normal/forced close, duplicate-open refusal, stale recovery, file-open and one-shot fallback pass; median round trip 5.453 ms |

## Phase 3 — observation

Done when: `observe --views front,side,top,persp` returns one contact sheet
whose bytes are identical across two runs on the same platform, and
`bpy.ops.mesh.*` edit-mode operators succeed inside `exec` without a GUI.

Proven on Debian 12 x86_64, xPack GCC 14.3.0, software Vulkan with Mesa
25.0.7: agent-profile install returns `BUILD_EXIT=0`; all three agent CTests
pass (88.20s). Separate-process four-view PNGs are byte-identical (SHA-256
`84ab14926ce2ade4bc5b80e5ee0a6eb4504f209fea7b12c27984c66abe47cbd6`).
A separate session run measured first/subsequent five-pass front observations
at 3.538s / 3.470s with a warm driver shader cache. Mesa 22.3.6 rendered
Combined but crashed on upstream's native Z pass; setup now installs modern
Mesa from bookworm-backports. No upstream source changes were needed.
Metal/macOS and real-GPU Vulkan/Windows remain **unverified**.

| Item | Status |
|---|---|
| Synthetic `wmWindow` / `bScreen` / `VIEW_3D` area so context-dependent operators run headless | done — real subdivide/bevel/extrude/translate, retained edit-mode flush, fallback layout, rollback and explicit GPU-selection error pass |
| Offscreen EEVEE render through `WM_init_gpu_offscreen`; Metal on macOS via a normal background build, Vulkan on Windows | done on Linux — native full render, byte equality, unchanged memfile snapshots and empty helper diff pass; product platforms unverified |
| Camera presets (front, back, left, right, top, bottom, persp, `camera`), auto-framing on the scene or a named object | done — all presets, right-side alias, named framing, empty scene and missing-camera error pass |
| Built-in lighting rig, fixed view transform, fixed resolution ladder (512 / 768 / 1024) | done — fixed three-SUN rig and Standard/sRGB; all three tile sizes pass |
| Passes: color, wireframe, silhouette, normal, depth | done — 2580×516 five-pass sheet, nonempty tiles, binary silhouette checks and visual inspection of beveled cube/sphere pass |
| Contact sheet composition; `--ref` side-by-side and overlay | done — view×pass order, separate-file count, square/nonsquare reference dimensions, overlay and inline PNG checks pass |
| `exec --observe` returns the image in the same round trip | done — one-shot/session attachment and `agent.observe()` use the same renderer; pending user edits remain in diff |

## Phase 4 — closing the loop inside the process

Done when: agent code inside `exec` calls `agent.compare(ref, "front")` in
a loop over a parameter range and returns the best IoU without a single
image leaving the process.

Proven on Debian 12 x86_64, xPack GCC 14.3.0 and Mesa 25.0.7 software
Vulkan: agent-profile configure/install returns `BUILD_EXIT=0`. The real
`agent_compare` CTest passes (136.37s), as do the three earlier protocol,
session and observation regressions. No upstream files changed. Product
Metal/macOS and Vulkan/Windows remain **unverified**, owned by Phase 5.

The saved X-scale-0.6 cube front reference scores IoU 0.9999693439607603,
Chamfer 0.0013440860215053765 px, SSIM 0.9999552715896969 and histogram
distance 0.00003065603923968485. Bounds are IoU ≥ 0.98, Chamfer ≤ 1 px,
SSIM ≥ 0.98 and histogram ≤ 0.02. The non-perfect silhouette differs at
exactly four corner pixels: thresholded antialiased color has 130480 foreground
pixels, native depth/coverage has 130476. This is not resampling or camera drift.
A red-sphere reference scores 0.682835025523083 / 40.85824683145036 px /
0.7060183742050152 / 1.0 (bounds < 0.8 / > 10 px / < 0.9 / > 0.3).
The uniform blue-background composite recovers the native silhouette exactly:
IoU 1, Chamfer 0, SSIM 0.9999999997841629, histogram 0 (required IoU ≥ 0.95).

NumPy is retained deliberately: fresh reference loading, preprocessing and all
four pixel metrics average 120.998 ms over 20 runs on real rendered buffers,
versus 3318.741 ms for a warm all-metric comparison including render (~3.6%).
The 20-candidate X-scale loop (0.20 through 0.96, step 0.04) selects 0.60,
IoU 0.9999693439607603, with exec `ms=66474.6604`, wall 66.4818s. It emits
only numbers and creates no PNGs. Compare preserves snapshots and empty diffs
without edits and retains actual transform edits during fitting. Metric formulas,
mask limitations and resizing policy are defined only in `doc/agent/design.md`.

| Item | Status |
|---|---|
| `compare --ref --view [--metric]`: silhouette IoU, edge Chamfer distance, SSIM, color-histogram distance; same functions exposed in the `agent` module | done — self/wrong references, 20-candidate numeric fit, requested-only metrics, all sizes and named framing pass |
| Reference preprocessing in-process: background removal (classic CV), silhouette extraction | done — colored-background IoU 1, debug mask visually inspected, alpha/luminance policies, portrait centering and PNG/JPEG/WebP pass |
| RNA-aware errors: on `AttributeError` / wrong enum / out-of-range, answer with the nearest valid identifiers and types from RNA | done — instance/type/module typos, enum descriptions, integer overflow ranges, wrong array type and unknown operator keyword pass; unrelated errors have no RNA block |
| `describe <rna path>`: signature, properties, enum items, ranges, from live RNA | done — operator/property/struct/instance/module and helper pass; bevel poll false in object mode, true in edit mode, GPU-selection poll includes upstream reason |

## Phase 5 — size, packaging, platforms

Done when: measured package sizes on both product platforms are recorded in
`doc/agent/build-profile.md`, and the Phase 1–4 protocol tests pass on
macOS arm64 and Windows x64.

| Item | Status |
|---|---|
| Configure and build the agent profile on macOS arm64 and Windows x64 | doing — native gates green; [full run 33965180310](https://github.com/fran0220/blender-cli/actions/runs/33965180310) dispatched for full build, tests and packaging |
| Per-component size measurement; adjust the profile from numbers, not guesses | done — measured Linux install, stdlib top 20, add-ons, datafiles, sorted shared libraries and original/trimmed compression in build-profile.md; OpenVDB/Cycles decisions resolved |
| Packaging trim: `addons_core` → glTF, FBX, Rigify; Python stdlib pruning; datafiles (one font, one studio light, no locale, no icons) | done on Linux — four trimmed regressions pass; extracted archive passes six verbs and exact observation equality; required startup retention is documented below; native packages remain unverified |
| Release artifacts: `blender-cli-<version>-macos-arm64.tar.zst`, `…-windows-x64.zip` | unverified — archive stage landed; macOS uses plain bin/ + Resources/ preserving upstream relative lookup; unsigned, no notarization |

Linux final profile configure/install reports `BUILD_EXIT=0` with agent-local
fatal warnings enabled. `ctest --test-dir build/orb -R agent --output-on-failure`
passes all four tests (304.95s). GCC exposed equal EAGAIN/EWOULDBLOCK expressions;
the fix preserves errno semantics rather than disabling the warning. Measurement
and archive/equivalence evidence, including justified trim exceptions, are owned
by `doc/agent/build-profile.md`. The first native full run predates the Linux
warning fix and final factory-AgX packaging correction; final native packaging
will require the corrected revision, not a success claim against stale code.

## Phase 6 — hosts (not started, not scheduled)

- Sophon mounts `blender-cli` as a local stdio Service once Sophon's
  Services support a stdio transport. That change lives in Sophon's own
  repository; nothing here.
- An MCP adapter is added only when a shell-less host is actually in use.
  Recording that host here comes before any adapter code.

## Resolved decisions

- `WITH_OPENVDB` stays: voxel remesh is required. Its separately shipped Python
  SDK is trimmed, not the modelling implementation. Measurements and reasoning
  are in `doc/agent/build-profile.md`.
- Cycles CPU and Embree stay: EEVEE does not replace object/texture baking.
  Correct the profile's stale option spelling to upstream's `WITH_EMBREE`.
  Preserve Cycles' Python engine registration during packaging.
- Packaging retains factory-required Python modules outside `addons_core`, a
  monospaced filename alias for one font face, and the real startup AgX transform
  beside Standard. Literal deletion broke one-shot JSON or engine registration;
  do not patch upstream Python or disguise AgX as Standard to satisfy a size goal.
