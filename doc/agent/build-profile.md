# blender-cli build profile

Owner of what is compiled in, what packaging removes, and how size is measured.
Execution status and platform validation belong in `PLAN.md`.

## Complete production, no GUI execution

`build_files/cmake/config/blender_agent.cmake` includes upstream's
`blender_release.cmake`, then applies the agent-specific exceptions below.
The scope is complete creation, characters, animation, simulation, lighting,
rendering, compositing and delivery, not modelling with a few export formats.
Native production requests are primary; upstream Python is an explicit
extension. No production feature is removed merely because observation uses
EEVEE or because a dedicated command has not been written yet.

| Retained capability | Build/dependency rationale |
|---|---|
| EEVEE and Cycles, Embree, OSL, path guiding, OpenImageDenoise | Production rendering and baking, not only deterministic observation. LLVM dependency selection follows upstream OSL/platform discovery; it is not forcibly disabled. |
| Fluid/Mantaflow, ocean/FFTW, Bullet | Fluids, smoke, ocean and rigid-body simulation. Cloth and soft body remain upstream-native. |
| Audaspace, FFmpeg, libsndfile, Rubber Band, OpenAL | Sequencer/video delivery, audio IO, mixing and time stretching. Platform audio devices use upstream release defaults. |
| USD, Alembic, MaterialX, Hydra, Cycles Hydra delegate | Scene/cache interchange, material translation and render delegates. Keep accompanying Python bindings and plugin resources. |
| glTF/Draco/meshoptimizer, FBX, OBJ, PLY, STL, BVH, SVG, blend | Native and upstream add-on interchange; retain add-on discovery across factory reset. |
| libmv/Ceres, Freestyle, Grease Pencil IO, Haru, Potrace | Tracking, stylized lines, drawing, SVG/PDF export and tracing. |
| Cineon, JPEG2000, OpenEXR, WebP and upstream image codecs | Production texture, film and image interchange, not only PNG previews. |
| OpenVDB/NanoVDB, remesh, QuadriFlow, GMP, Manifold, OpenSubdiv, UV SLIM, IK solvers | Geometry, volumes, deformation, rigging and UV workflows. |
| International text, all fonts and assets | Text objects and creation resources are production inputs, even without translated interactive UI. |
| Python and complete installed standard library/bindings | Explicit extensions, bundled add-ons, tooling and native engine registration. No generated Python behind production commands. |

## All exclusions and platform decisions

The former feature-specific OFF list is removed in full. The remaining choices
are narrowly about desktop interaction or upstream toolchain availability:

- `WITH_BLENDER_THUMBNAILER=OFF`: desktop thumbnail integration is not a
  production operation.
- `WITH_INPUT_NDOF=OFF`, `WITH_INPUT_IME=OFF`, `WITH_XR_OPENXR=OFF`,
  `WITH_GHOST_SDL=OFF`: no interactive devices, XR session or SDL window loop.
  GUI source otherwise stays compiled where upstream needs it. The agent entry
  never calls `WM_main`; CMake and packaging do not replace that runtime guard.
- Linux development uses `WITH_HEADLESS=ON`. Apple Silicon macOS and Windows
  explicitly use `WITH_HEADLESS=OFF` for Cocoa/Metal and Vulkan offscreen paths,
  without making GUI execution reachable.
- macOS uses upstream Metal Cycles support and CoreAudio. Windows x64 uses
  upstream release GPU settings, including HIPRT, oneAPI and precompiled
  CUDA/HIP/oneAPI kernels, plus WASAPI. Release builders must provide the
  upstream SDK/toolchain dependencies; a disabled or missing target capability
  is a validation gap, not grounds to silently narrow the product.
- Linux uses upstream normal developer GPU defaults: CUDA and HIP runtime
  support on, OptiX requested and subject to upstream SDK discovery, HIPRT and
  oneAPI off, no ahead-of-time GPU binaries. This avoids imposing release-farm
  CUDA/ROCm/oneAPI compiler SDKs on development. It is not evidence that the
  release GPU kernels or devices work.
- SDL audio and PipeWire follow upstream release defaults (off); they are
  alternative device backends, not the audio processing implementation.
  JACK/PulseAudio are requested on Linux and may be disabled by upstream when
  system development libraries are absent. FFmpeg, libsndfile and Audaspace
  remain required. Do not confuse unavailable playback hardware with missing
  audio-file support.
- Debug/test executables and experimental developer options otherwise follow
  upstream defaults. There is no source-level editor-removal project here.

Read CMake's configure output and generated compile definitions, not only
`CMakeCache.txt`: upstream can disable an unavailable dependency with a normal
variable while leaving the requested cache option ON.

## Packaging policy

`python3 source/blender/agent/packaging/package.py <install> <new-tree>
--platform <macos-arm64|windows-x64|linux-x64> --archive` copies its input and
refuses existing or nested destinations. It retains:

- Every installed runtime library and its aliases, USD/Hydra plugins, Windows
  side-by-side manifest and DLL layout; no name-based library deletion.
- Every installed Python binding, including USD, MaterialX, OpenVDB, OSL,
  OpenImageIO and OpenColorIO, plus SDK archives and installed stdlib packages.
  Python extensions can use these; lack of a current agent import is not proof
  they are removable.
- All add-ons in upstream locations, including Cycles, Hydra Storm, Rigify,
  pose library, Node Wrangler and hidden core import/export modules. No
  relocation or allow-list pruning.
- Complete assets, sculpt brushes, fonts, studio lights, presets, icons and
  locale resources. Small desktop data is kept rather than guessed expendable.
- The exact upstream OCIO config, all view transforms (including Filmic and
  AgX), LUTs and ICC profiles. Deterministic observation's Standard setting
  does not constrain production color management.
- Redistribution notices and licenses.

The only removals are build-time generators (`datatoc`, `makesdna`, `makesrna`,
`shader_tool`, `zstd_compress`), the standalone install-root `tests/` executables,
root-level Windows PDB debug symbols and regenerable `__pycache__` directories.
These do not implement scene capabilities. Python/add-on test directories and
SDK resources are not recursively stripped. Validation retains the original
install and runs the agent protocol scripts against the packaged executable.

macOS keeps the established plain directory layout: `bin/`, `Resources/` and
a top-level `blender-cli` symlink, preserving upstream loader/resource paths.
Windows retains the root CLI and `blender.shared` assembly. Unix archives use
tar+zstd; Windows uses ZIP. Packaging never rewrites binaries or DLL manifests.
Artifacts are unsigned/not notarized; product validation must use their actual
installed/extracted layout on the target platform.

## Validation and measurement

Product validation runs on Amp-connected Apple Silicon macOS and Windows 11
runners, never GitHub Actions. Use a dedicated checkout and one persistent build
directory per machine, preserving unrelated projects. Fetch `origin/main` and
initialize the exact pinned platform library submodule and LFS assets. Record
the source revision, library revision, OS, compiler and actual GPU in `PLAN.md`.
Use the complete profile with `WITH_GTESTS=ON`; do not disable capabilities to
obtain a green build. Missing devices remain unverified, not hardware passes.

On macOS use native arm64 Xcode tools, CMake and zstd (Make or Ninja). On Windows
provide VS 2022 MSVC 14.44, upstream-pinned CUDA 12.8 and HIP 7.1 compilers,
OptiX 9 headers and standalone Intel ocloc. Pinned `lib/windows_x64` supplies
DPC++, HIPRT and Level Zero, but not ocloc. Its Level Zero directory is
`level-zero`: pass `LEVEL_ZERO_INCLUDE_DIR` pointing to its `include` and
`LEVEL_ZERO_LIBRARY` to `lib/ze_loader.lib`. Preserve ocloc's companion IGC DLLs
and licenses. Obtain approval before privileged SDK installation on a runner.
Verify generated CUDA/OptiX/HIP/HIPRT/oneAPI kernel targets and compiled device
definitions, not merely ON cache values. Compiler provisioning is not a reason
to install or replace GPU drivers.

Configure, build and install warnings-visible, run all CMake-registered agent
tests, then create the package, run its integrity smoke below and repeat all
registered protocol scripts against the packaged binary. Measure archive size
and check extraction on that same platform. Keep diagnostics on the runner;
building and testing do not publish packages or upload them to object storage.

```sh
cmake -S . -B build/orb -C build_files/cmake/config/blender_agent.cmake
cmake --build build/orb --target install
ctest --test-dir build/orb -R agent --output-on-failure
python3 source/blender/agent/packaging/package.py build/orb/bin build/release --platform linux-x64 --archive
python3 tests/agent/package.py build/orb/bin/blender-cli build/release/blender-cli
```

Run every CMake-registered agent protocol script against the packaged tree as
well. `tests/agent/package.py` hashes installed runtime resources before/after,
requires production build options, imports bindings, enables production add-ons
and resolves operators after repeated factory resets. It checks engine and
color/image-format registration and retains byte-identical observation checks
when a device exists. Capability failures fail the test; device absence only
marks rendering unverified, never missing production modules as acceptable.

`tests/agent/io.py` performs real mesh round trips through blend, OBJ, native
and add-on FBX, STL, PLY, glTF/GLB, USD/USDA/USDC/USDZ and Alembic. It verifies
topology, bounds (absolute tolerance 1e-5 Blender units), UVs and materials where
the format carries shader data. Alembic tests geometry/UVs, not shader graphs.
It copies only `model.json` to a different directory and replays in a fresh
process; no Python-program compatibility or snapshot dependency is permitted.
References to external assets remain explicit dependencies, not magically
embedded into the JSON artifact.

Packaging writes a sibling JSON containing actual CLI version, logical input
and output bytes (excluding symlinks), per-component sizes, every removal with
reason/presence/byte count, and actual compressed bytes. Measure the same
revision, dependency pins and profile before making size claims; warm Python
caches can inflate an installed tree. A symbol-size attribution is not a
counterfactual uninstall saving.

Linux development measurement (2026-09-08, GCC 14.3 Release, pinned Linux libs,
`WITH_GTESTS=ON`, code through the native production acceptance series):

| Measurement | Bytes | MiB |
|---|---:|---:|
| Installed logical files | 1,696,132,995 | 1,617.56 |
| Packaged logical files before regenerated bytecode | 1,676,000,998 | 1,598.36 |
| `tar.zst`, zstd level 19, two workers | 435,931,363 | 415.74 |

This development tree includes the 389,611,312-byte test-binary directory;
it is not a macOS/Windows release-size prediction. The archive was captured
after smoke testing could regenerate Python bytecode. All 12 installed and
12 packaged tests passed; 5,622 runtime resources were byte-identical, as was
the deterministic observation image. Archive integrity, extraction and the
extracted executable's production feature flags were checked. The complete
measurement JSON is `build/production-package.json` in the acceptance orb.

After excluding standalone test binaries, the same Linux install produces
1,286,389,686 logical bytes (1,226.80 MiB) before regenerated bytecode and a
339,364,298-byte archive (323.64 MiB, zstd level 19, two workers, after smoke).
The targeted package smoke again verifies 5,622 runtime files and observation
bytes identical; `zstd -t` passes. Report: `build/distribution-package.json`.

Apple Silicon measurement (2026-09-08, M4 Pro, native arm64 Release,
`WITH_GTESTS=ON`, source `70fa7bf0733`, pinned macOS libraries
`a76ef917b4849ba2b1b1deb1a643e131a884a63b`, standalone test binaries excluded):

| Measurement | Bytes | MiB |
|---|---:|---:|
| Installed logical files | 1,244,352,131 | 1,186.71 |
| Packaged logical files before regenerated bytecode | 958,486,860 | 914.08 |
| `tar.zst` | 244,590,804 | 233.26 |

Native installed tests pass after the heartbeat-test correction; all 12
packaged scripts pass. Package smoke verifies 5,475 byte-identical runtime
resources and the same deterministic observation image. Archive integrity,
extraction and extracted `--version` pass. On macOS extract with
`tar --use-compress-program 'zstd -d' -xf <archive.tar.zst>` if the system tar
does not automatically decompress zstd. Metal observation and explicit Cycles
GPU rendering pass; Vulkan-only fault injection is not Mac evidence.

Previous modelling-only sizes and trim percentages are not applicable.
Windows package size and full production acceptance remain unverified.
Linux configure/build results are development evidence only.
