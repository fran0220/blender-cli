# blender-cli build profile

Owner of: what the agent build compiles in, what it trims, and how size is
measured. The profile itself is
`build_files/cmake/config/blender_agent.cmake`; this document explains it.

## Where the size is

Official numbers for reference (compressed):

| Package | macOS arm64 | Windows x64 | Windows arm64 |
|---|---|---|---|
| Blender 4.4 release | 304 MB | 385 MB | 390 MB |
| `bpy` 5.2.1 wheel (no GUI entry) | 245 MB | 339 MB | 211 MB |

Two facts follow. Dropping the GUI entry saves about 15 %; the mass is in
dependencies and data files, not editor code. Windows x64 carries ~130 MB
that Windows arm64 does not: the precompiled Cycles GPU kernels
(CUDA / OptiX / HIP / oneAPI). macOS compiles Metal kernels at run time.

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

| Off | Saves | Cost |
|---|---|---|
| `WITH_CYCLES_DEVICE_CUDA/OPTIX/HIP/HIPRT/ONEAPI` | ~130 MB on Windows x64 | Cycles is CPU only; observation is EEVEE anyway |
| `WITH_CYCLES_OSL`, `WITH_LLVM` | tens of MB | no OSL shaders |
| `WITH_USD`, `WITH_HYDRA`, `WITH_MATERIALX`, `WITH_ALEMBIC` | large | glTF/FBX only |
| `WITH_OPENIMAGEDENOISE`, `WITH_CYCLES_PATH_GUIDING` | tens of MB | no denoising of observation renders |
| `WITH_CODEC_FFMPEG`, `WITH_CODEC_SNDFILE`, all audio backends, `WITH_AUDASPACE` | tens of MB | no video, no audio |
| `WITH_INTERNATIONAL`, `WITH_XR_OPENXR`, `WITH_INPUT_NDOF`, `WITH_INPUT_IME` | locale + input devices | none |
| `WITH_LIBMV`, `WITH_FREESTYLE`, `WITH_MOD_FLUID`, `WITH_MOD_OCEANSIM`, `WITH_BULLET`, `WITH_HARU`, `WITH_IO_GREASE_PENCIL`, `WITH_BLENDER_THUMBNAILER` | medium | none for the agent |
| `WITH_IMAGE_CINEON`, `WITH_IMAGE_OPENJPEG` | small | none |

Tier 2 — packaging, zero source changes:

- `scripts/addons_core`: keep `io_scene_gltf2`, `io_scene_fbx`, `rigify`;
  drop the rest (`bl_pkg`, `hydra_storm`, `node_wrangler`, `pose_library`,
  `ui_translate`, `viewport_vr_preview`, `io_anim_bvh`, `io_curve_svg`,
  `io_mesh_uv_layout`).
- Python standard library: drop `test`, `idlelib`, `tkinter`, `ensurepip`,
  `lib2to3`, `turtledemo`.
- `release/datafiles`: one font, one studio light (the built-in observation
  rig), no locale, no icons, no GUI themes, color-management LUTs limited to
  the Standard view transform.

Tier 3 — source-level removal of editors: not done. The `bpy` wheel shows
the ceiling is ~15 %, and every upstream merge would pay for it. GUI code is
compiled and unreachable.

## Expected result

Estimated, to be replaced by measurement in Phase 5: macOS arm64
120–150 MB, Windows x64 130–170 MB, compressed. Some options drag others
(OSL brings LLVM; OpenVDB brings Blosc and NanoVDB); the boundary is found
by building, not by reading.

## Measuring

```
cmake -S . -B build -C build_files/cmake/config/blender_agent.cmake
cmake --build build --target install
du -sh build/bin/*                              # per top-level component
du -sh build/bin/5.3/python/lib/python3.13/*    # stdlib
du -sh build/bin/5.3/scripts/addons_core/*
```

Record the numbers per platform in this file, then adjust the profile.
