# blender-cli build profile

Owner of: what the agent build compiles in, what it trims, and how size is
measured. The profile itself is
`build_files/cmake/config/blender_agent.cmake`; this document explains it.

## Where the size is

Measure this profile, not a different Blender release or a `bpy` wheel. Those
packages differ in version, dependencies and build flags, so their sizes cannot
isolate the cost of the GUI. CMake options disable compilation but upstream's
install rules still copy unused shared libraries and Python VFX bindings.
Packaging must remove that payload separately. No product size forecast is used.

## What the agent needs

The agent models by writing `bpy` code, observes through offscreen EEVEE,
bakes through Cycles CPU, and exports glTF or FBX. That fixes the keep list:

| Keep | Why |
|---|---|
| Python 3.13 + numpy | the interface language |
| EEVEE, GPU module, Vulkan (Windows) / Metal (macOS) | `observe` |
| Cycles CPU, Embree | baking |
| OpenVDB, `WITH_MOD_REMESH`, QuadriFlow | voxel remesh and retopology are high-frequency in code modelling |
| GMP, Manifold | exact booleans |
| OpenSubdiv | subdivision modifier |
| Potrace | image → curves, directly useful when modelling from a picture |
| OpenColorIO, OpenImageIO, PNG/JPEG/WebP | deterministic color-managed observation, texture IO |
| glTF (Draco, meshoptimizer), FBX, OBJ | export to games |
| Rigify | Phase 2 characters |
| Freetype | text objects |

## What is trimmed

Tier 1 — CMake options, zero source changes (see the profile):

| Off | Cost |
|---|---|
| `WITH_CYCLES_DEVICE_CUDA/OPTIX/HIP/HIPRT/ONEAPI/METAL` | Cycles is CPU only; observation is EEVEE |
| `WITH_CYCLES_OSL`, `WITH_LLVM` | no OSL shaders |
| `WITH_USD`, `WITH_HYDRA`, `WITH_MATERIALX`, `WITH_ALEMBIC` | no pipeline scene interchange |
| `WITH_OPENIMAGEDENOISE`, `WITH_CYCLES_PATH_GUIDING` | no denoising of observation renders |
| `WITH_CODEC_FFMPEG`, `WITH_CODEC_SNDFILE`, all audio backends, `WITH_AUDASPACE` | no video, no audio |
| `WITH_INTERNATIONAL`, `WITH_XR_OPENXR`, `WITH_INPUT_NDOF`, `WITH_INPUT_IME` | no translated UI or interactive input devices |
| `WITH_LIBMV`, `WITH_FREESTYLE`, `WITH_MOD_FLUID`, `WITH_MOD_OCEANSIM`, `WITH_BULLET`, `WITH_HARU`, `WITH_IO_GREASE_PENCIL`, `WITH_BLENDER_THUMBNAILER` | no tracking, simulation, line-art, PDF or desktop thumbnailing |
| `WITH_IMAGE_CINEON`, `WITH_IMAGE_OPENJPEG` | no Cineon/JPEG2000 |

Tier 2 — packaging, zero source changes:

- `scripts/addons_core`: exactly `io_scene_gltf2`, `io_scene_fbx`, `rigify`.
  **Required retention:** `cycles`, `bl_pkg`, `pose_library`, `io_anim_bvh`,
  `io_curve_svg`, `io_mesh_uv_layout` move unchanged to `scripts/modules`.
  Factory startup imports these modules; deleting them pollutes one-shot JSON
  with import errors and removes Cycles engine registration. Relocated bytes
  are not claimed as savings. Other optional add-ons are deleted.
- Python standard library: drop `test`, `idlelib`, `tkinter`, `ensurepip`,
  `lib2to3`, `turtledemo`. Linux already lacks all except `ensurepip`; CPython
  3.13 removed lib2to3, and upstream install excludes the other five except
  ensurepip. Remove both copies of the static libpython archive and generated
  bytecode. Keep the Python executable, NumPy, requests and zstandard; the
  latter two are imported by upstream HTTP and blend-file metadata modules.
- Remove Python VFX SDK bindings (USD, MaterialX, OSL, OpenVDB, OpenImageIO,
  OpenColorIO) and Cython. These are not `bpy`, and retained add-ons/agent modules
  do not import them. Keep Blender's actual IO/color/volume libraries.
- Remove shared libraries belonging to the disabled USD, OSL, MaterialX,
  tracking, HIPRT, OpenXR and SDL subsystems, plus build helpers and Windows
  PDBs. Preserve all license notices. SYCL/UR stay: the pinned Embree build
  causes actual dynamic dependencies even with all Cycles GPU devices off.
- Datafiles: one Inter font face, with the required `DejaVuSansMono.woff2`
  filename alias (symlink on Unix; same-face copy in Windows ZIP). BLF loads
  both names during background startup. One `studio/basic.sl` remains; it is
  **not** the observation rig, which creates SUN lights itself. Keep sculpt
  brush assets for code modelling. Drop locale, disk icons, GUI theme presets
  and macOS application icons.
- Color management: extract upstream Standard/sRGB transforms verbatim.
  **Required retention:** factory startup references AgX before the agent
  command starts, so retain the actual AgX/sRGB transform and its single
  `AgX_Base_sRGB.cube` LUT (2.6 MiB), not a misleading Standard alias. A strictly
  Standard-only config emits an upstream warning into stdout and breaks the
  protocol test. Keep the two referenced ICC profiles and an ACEScc built-in
  for OCIO's required log roles. Remove Filmic and all other LUTs. Observation
  remains Standard, with exact before/after byte equality tested.

Tier 3 — no source-level editor removal. GUI code remains compiled and
unreachable; no claimed percentage saving justifies additional upstream drift.

## Decisions from measurement

- **OpenVDB stays.** Its installed Linux library occupies 56 MiB. Voxel remesh
  is part of the modelling keep list; remove the separate 4.9 MiB Python binding,
  not the volume/remesh implementation used by `bpy`.
- **Cycles CPU and Embree stay.** The ELF symbol-size sum for `ccl::` symbols is
  8,163,977 bytes; this is attribution, not a counterfactual uninstall saving.
  Embree occupies 27 MiB and requires approximately 21 MiB of SYCL/UR libraries
  from the pinned package. EEVEE is not a replacement for Cycles object/texture
  baking. Removing the Cycles Python registration made `engine='CYCLES'` fail
  in a real exec; packaging now preserves it. The profile spells upstream's
  actual `WITH_EMBREE` option, replacing the ineffective `WITH_CYCLES_EMBREE`.
- No other engine switch changes: the measurements locate the removable mass
  in copied SDK/library/development payloads, not in required modelling engines.

## Packaging and CI

`python3 source/blender/agent/packaging/package.py <install> <new-tree>
--platform <macos-arm64|windows-x64|linux-x64> --archive` never edits its input
and refuses an existing output. It writes a sibling JSON with logical bytes,
per-component sizes, every removal (including absent paths), retained module
relocations, version parsed from the actual CLI, and compressed archive size.
`python3 tests/agent/package.py <original-cli> <trimmed-cli>` exercises all six
verbs, Cycles registration and byte-identical observation. Run all four existing
protocol scripts against the new tree as well before distributing it.

macOS uses a plain directory with `bin/` and `Resources/`, preserving upstream
`@loader_path/../Resources/lib` and application resource lookup; a top-level
`blender-cli` symlink gives a stable CLI path. No binary rewriting or `.app` is
needed. Windows uses the upstream flat layout. Archives are unsigned and not
notarized; quarantine instructions are in the root README.

The push gate compiles `bf_agent` and `blender-cli` on AppleClang/MSVC, including
their unavoidable generated DNA/shader dependency closure (3,806/3,761 Ninja
edges in the first native gates). It does not link Blender. Fatal warnings are
target-local (`-Werror` or `/WX`), never global. Linux builds and tests the full
profile with a 360-minute job budget. Each platform caches pinned libraries and a
2 GiB sccache store. The dispatch/tag workflow builds, installs, tests and
archives. Native device absence returns CTest skip code 77 with an explicit
Metal/Vulkan reason; render errors are never converted to skips. A skipped
render is not product-platform rendering evidence; execution status lives in PLAN.

## Measured — Linux x86_64 (dev)

Debian 12, xPack GCC 14.3.0, Release, pinned upstream libraries, Mesa 25.0.7
software Vulkan; `build/orb` is the shared build directory. `du -h` below reports
allocated MiB/KiB (rounded), unlike the packaging JSON's exact logical bytes.
Python startup generates bytecode, so warm test trees are larger than a fresh
install; those caches are removed from archives.

The untrimmed install compressed with `tar -I 'zstd -19' -cf - -C build/orb/bin .
| wc -c` is **238,784,479 bytes**. No macOS/Windows size is inferred from this.

### Installed components (`du -sh build/orb/bin/*`)

| Component | Allocated size |
|---|---:|
| `5.3` | 447 MiB (437 MiB before Python startup caches) |
| `blender` | 185 MiB |
| `lib` | 336 MiB |
| `makesrna` | 3.3 MiB |
| `shader_tool` | 2.6 MiB |
| `zstd_compress` | 516 KiB |
| `license` | 412 KiB |
| `makesdna` | 252 KiB |
| `blender-cli` | 188 KiB |
| `datatoc` | 28 KiB |
| `blender.desktop`, `readme.html` | 8 KiB each |
| `blender-launcher`, `blender-symbolic.svg`, `blender-system-info.sh`, `blender.svg` | 4 KiB each |

### Python stdlib top 20

Command: `du -sh build/orb/bin/5.3/python/lib/python3.*/* | sort -rh | head -20`.

| Component | Allocated size |
|---|---:|
| site-packages | 140 MiB |
| config-3.13-x86_64-linux-gnu | 81 MiB |
| lib-dynload | 22 MiB |
| __pycache__ | 2.1 MiB |
| ensurepip | 1.8 MiB |
| encodings | 1.8 MiB |
| email | 704 KiB |
| asyncio | 573 KiB |
| pydoc_data | 544 KiB |
| urllib | 364 KiB |
| xml | 349 KiB |
| multiprocessing | 336 KiB |
| importlib | 312 KiB |
| http | 300 KiB |
| logging | 288 KiB |
| unittest | 280 KiB |
| _pyrepl | 240 KiB |
| zipfile | 237 KiB |
| _pydecimal.py | 224 KiB |
| re | 220 KiB |

The separate `python/lib/libpython3.13.a` duplicates the 84,181,888-byte archive
inside `config-*`. Neither is a runtime dependency. The standalone Python
executable is 40,029,432 bytes and remains. Within site-packages, the largest
items are USD `pxr` 64 MiB, NumPy 29 MiB warm, Cython 12 MiB, MaterialX 7.4 MiB,
pip 6.2 MiB, OpenVDB binding 4.9 MiB, PyOpenColorIO 4.4 MiB, setuptools 3.9 MiB,
docutils 2.5 MiB and OpenImageIO 2.0 MiB. NumPy stays for the measured comparison
implementation; build/SDK bindings go, small general Python tooling remains.

### Add-ons and datafiles

Commands: `du -sh build/orb/bin/5.3/scripts/addons_core/*` and
`du -sh build/orb/bin/5.3/datafiles/*`.

| Add-on | Allocated size | Action |
|---|---:|---|
| bl_pkg | 1.2 MiB | relocate required runtime |
| cycles | 3.7 MiB warm | relocate required engine registration |
| hydra_storm | 20 KiB | remove |
| io_anim_bvh | 72 KiB | relocate required runtime |
| io_curve_svg | 88 KiB | relocate required runtime |
| io_mesh_uv_layout | 40 KiB | relocate required runtime |
| io_scene_fbx | 584 KiB | keep |
| io_scene_gltf2 | 1.9 MiB | keep |
| node_wrangler | 273 KiB | remove |
| pose_library | 136 KiB | relocate required runtime |
| rigify | 1.9 MiB | keep |
| ui_translate | 52 KiB | remove |
| viewport_vr_preview | 340 KiB | remove |

| Datafiles | Allocated size | Action |
|---|---:|---|
| assets | 12 MiB | keep sculpt brush assets |
| colormanagement | 20 MiB | Standard + factory AgX only |
| fonts | 15 MiB | Inter face + required filename alias |
| icons | 660 KiB | remove |
| studiolights | 4.0 MiB | retain basic.sl |

### Installed binary and shared libraries, descending

Command: `du -sh build/orb/bin/blender build/orb/bin/lib/* | sort -rh`.
Zero-byte symlink aliases are omitted; each actual library is counted once.

| File (version suffix shortened) | Allocated size | Action |
|---|---:|---|
| blender | 185 MiB | keep |
| libusd_ms | 76 MiB | remove |
| liboslexec | 61 MiB | remove |
| libopenvdb | 56 MiB | keep voxel remesh |
| liboslcomp | 48 MiB | remove |
| libembree4 | 27 MiB | keep CPU BVH |
| libur_loader | 17 MiB | required by pinned SYCL |
| libOpenImageIO | 17 MiB | keep image IO |
| libOpenColorIO | 6.9 MiB | keep color management |
| libceres | 5.4 MiB | remove tracking dependency |
| libsycl | 3.9 MiB | required by pinned Embree |
| libSDL3 | 2.8 MiB | remove |
| libdraco | 2.0 MiB | keep glTF |
| libhiprt0200564 | 1.7 MiB | remove |
| libur_adapter_level_zero_v2 | 1.4 MiB | retain pinned UR runtime adapter |
| libOpenEXR | 1.3 MiB | keep image IO dependency |
| libMaterialXCore | 1.2 MiB | remove |
| libOpenImageIO_Util | 1.1 MiB | keep |
| libMaterialXGenShader | 904 KiB | remove |
| libosdCPU | 820 KiB | keep subdivision |
| libosdGPU | 816 KiB | keep subdivision |
| libopenxr_loader | 772 KiB | remove |
| libMaterialXRender | 584 KiB | remove |
| libvulkan | 552 KiB | keep observation backend |
| libIex | 520 KiB | keep image IO dependency |
| libopenjph | 492 KiB | keep image IO dependency |
| libMaterialXRenderGlsl | 420 KiB | remove |
| libMaterialXGenMdl | 416 KiB | remove |
| libMaterialXGenGlsl | 408 KiB | remove |
| libOpenEXRCore | 396 KiB | keep |
| libMaterialXFormat | 320 KiB | remove |
| libImath | 308 KiB | keep |
| libMaterialXGenMsl | 300 KiB | remove |
| libtbb | 292 KiB | keep threading |
| liboslquery | 228 KiB | remove |
| libOpenEXRUtil | 208 KiB | keep |
| libMaterialXGenOsl | 184 KiB | remove |
| libmeshoptimizer | 152 KiB | keep glTF |
| libtbbmalloc | 136 KiB | keep allocator |
| libMaterialXRenderOsl | 92 KiB | remove |
| libbf_intern_draco_bridge | 72 KiB | keep glTF runtime bridge |
| liboslnoise | 52 KiB | remove |
| libIlmThread | 48 KiB | keep |
| libtbbmalloc_proxy | 28 KiB | keep |
| libMaterialXRenderHw | 20 KiB | remove |
| libblender_cpu_check, libbf_intern_meshopt_bridge | 16 KiB each | keep |

## Measuring

```
cmake -S . -B build -C build_files/cmake/config/blender_agent.cmake
cmake --build build --target install
du -sh build/bin/*                              # per top-level component
du -sh build/bin/5.3/python/lib/python3.13/*    # stdlib
du -sh build/bin/5.3/scripts/addons_core/*
```

Record the numbers per platform in this file, then adjust the profile.
