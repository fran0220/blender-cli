# Tracking Blender upstream

Owner of: how this fork relates to `blender/blender`, which upstream files
it touches, and how upstream releases are brought in.

## Base and cadence

- Upstream: https://github.com/blender/blender (mirror of
  https://projects.blender.org/blender/blender).
- Base line: **5.2 LTS**, anchored at `v5.2.1`. Upstream maintains 5.2 with
  bug-fix releases until July 2028. Each `v5.2.x` tag is merged into `main`
  as it appears.
- Moving to the next LTS line (5.5 or whatever upstream names it) is one
  decision recorded in `PLAN.md`, performed as one merge of that tag, with
  the fork adapted to any `bpy`/RNA/build changes in the same commit series.
  There is never a period with two supported bases.

## Merge, never rebase

`main` is published. Upstream is brought in with

```
git fetch upstream --tags
git merge v5.2.x
```

Conflicts are resolved toward upstream in upstream files and toward the
fork in `source/blender/agent/`. A merge commit is an ordinary checkpoint.
`main` is never force-pushed.

## Touched upstream files

Every touch is marked `/* blender-agent */` (or `# blender-agent` in
CMake/Python) on the line or block it adds. The full list:

| File | Touch |
|---|---|
| `CMakeLists.txt` | `option(WITH_AGENT …)` |
| `source/blender/CMakeLists.txt` | `add_subdirectory(agent)` under `WITH_AGENT` |
| `source/creator/CMakeLists.txt` | link `bf_agent` under `WITH_AGENT` |
| `source/creator/creator.cc` | one call registering the agent command before deferred command dispatch |

Adding a file to this table requires a reason in the same commit's message.
A touch that is not registration or build wiring is a design error to solve
inside `source/blender/agent/`.

## Fork-owned paths

```
AGENTS.md
PLAN.md
doc/agent/
source/blender/agent/
build_files/cmake/config/blender_agent.cmake
tests/agent/
```

## Versioning

Releases are tagged `v5.2.1-agent.N` where `5.2.1` is the upstream base and
`N` increments per release. `BKE_blender_version.h` is not modified;
`blender-agent --version` reports both numbers.

## Precompiled libraries

`lib/<platform>` submodules stay at the revision upstream pins for the base
tag. They are never modified, replaced or vendored differently; trimming is
done with CMake options and packaging, not by rebuilding dependencies.
