# Tracking Blender upstream

Owner of: how this fork relates to `blender/blender`, which upstream files
it touches, and how upstream releases are brought in.

## Base and cadence

- Upstream: https://github.com/blender/blender (mirror of
  https://projects.blender.org/blender/blender).
- Base line: upstream `main` (the 5.3 development line at the time of the
  fork, commit `5c951f2e`). Upstream `main` is merged into the fork's `main`
  at a cadence chosen in `PLAN.md`, never less often than once per upstream
  release.
- Pinning to a release line (for example the next LTS) is one decision
  recorded in `PLAN.md`, performed as one merge of that tag, with the fork
  adapted to any `bpy`/RNA/build changes in the same commit series. There is
  never a period with two supported bases.

## Merge, never rebase

`main` is published. Upstream is brought in with

```
git fetch upstream --tags
git merge upstream/main
```

Conflicts are resolved toward upstream in upstream files and toward the
fork in `source/blender/agent/`. A merge commit is an ordinary checkpoint.
`main` is never force-pushed.

## Touched upstream files

Every touch is marked `/* blender-cli */` (or `# blender-cli` in
CMake/Python) on the line or block it adds. The full list:

| File | Touch |
|---|---|
| `CMakeLists.txt` | `option(WITH_AGENT …)` |
| `source/blender/CMakeLists.txt` | `add_subdirectory(agent)` under `WITH_AGENT` |
| `source/creator/CMakeLists.txt` | link `bf_agent` under `WITH_AGENT` |
| `source/creator/creator.cc` | one call registering the agent command before deferred command dispatch |
| `tests/CMakeLists.txt` | add `tests/agent` under `WITH_AGENT` after upstream's Python test helpers are defined, to register installed-launcher protocol tests |
| `README.md` | marked agent distribution section: required CLI extraction and unsigned macOS quarantine instructions; documentation-only exception |
| `source/blender/gpu/vulkan/vk_descriptor_pools.{cc,hh}` | explicit upstream-defect exception: enforce the pool's advertised `maxSets` and retire through existing timeline-safe recycling even when a driver permits overallocation |
| `source/creator/creator_signals.cc` | registered optional agent crash-log callback supplies the output path and preformatted request/Python context; no session policy in the upstream handler |

Adding a file to this table requires a reason in the same commit's message.
A touch that is not registration or build wiring is a design error to solve
inside `source/blender/agent/`.

The Vulkan descriptor-pool change is an upstream defect fix, a candidate for
submission upstream; drop it on merge once upstream fixes it. Descriptor pools
are owned by backend thread data, not an observation scene or Render, so the
agent cannot correctly release them by freeing its temporary IDs. Mesa 25.0.7
lavapipe permits unbounded descriptor-set allocation and maps each set's buffer;
waiting for `VK_ERROR_OUT_OF_POOL_MEMORY` exhausts Linux's virtual-memory-map
count. The driver-neutral counter retires a pool at its declared 250 sets and
lets the existing completed-GPU-timeline recycle call reset/free its sets.

## Fork-owned paths

```
AGENTS.md
PLAN.md
doc/agent/
source/blender/agent/
build_files/cmake/config/blender_agent.cmake
tests/agent/
.github/workflows/
```

The workflow directory owns the fork's native compiler gate and full-build CI;
it does not modify upstream's buildbot integration.
`source/blender/agent/packaging/` owns install-copy trimming, component accounting,
observation/startup color configuration extraction and archive creation. It is inside
the existing agent-owned subtree; upstream install rules remain untouched.

## Versioning

Releases are tagged `<upstream>-agent.N` where `<upstream>` is the upstream
version string of the merged base (for example `5.3.0-alpha`) and `N`
increments per release. `BKE_blender_version.h` is not modified;
`blender-cli --version` reports both numbers.

The first implementation reports upstream's human-readable runtime version and
a second line `blender-cli 5.3.0-alpha-agent.1`, constructed from the unmodified
upstream version macros. The cycle suffix is absent for a release base. This
identifies the fork revision series, not a claim that an artifact was published.

## Precompiled libraries

`lib/<platform>` submodules stay at the revision upstream pins for the base
tag. They are never modified, replaced or vendored differently; trimming is
done with CMake options and packaging, not by rebuilding dependencies.
