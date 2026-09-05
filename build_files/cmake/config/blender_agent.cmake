# SPDX-FileCopyrightText: 2026 blender-agent Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

# blender-agent: headless, agent-serving build.
#
# Keeps what an agent modelling by code needs: Python, EEVEE offscreen
# rendering, Cycles CPU for baking, remesh/retopology, booleans, subdivision,
# glTF/FBX/OBJ IO. Trims GUI-only, pipeline-only and GPU-render-only
# dependencies. Rationale and keep/trim tables: doc/agent/build-profile.md.
#
# Example usage:
#   cmake -S . -B build -C build_files/cmake/config/blender_agent.cmake
#
# macOS: this is a normal (non-headless) build run in background mode so the
# Cocoa offscreen path and Metal are reachable. Linux uses WITH_HEADLESS.

set(WITH_AGENT               ON  CACHE BOOL "" FORCE)

# Never a GUI product; on Linux drop the window systems entirely.
if(UNIX AND NOT APPLE)
  set(WITH_HEADLESS          ON  CACHE BOOL "" FORCE)
endif()
set(WITH_BLENDER_THUMBNAILER OFF CACHE BOOL "" FORCE)
set(WITH_INTERNATIONAL       OFF CACHE BOOL "" FORCE)
set(WITH_INPUT_NDOF          OFF CACHE BOOL "" FORCE)
set(WITH_INPUT_IME           OFF CACHE BOOL "" FORCE)
set(WITH_XR_OPENXR           OFF CACHE BOOL "" FORCE)
set(WITH_GHOST_SDL           OFF CACHE BOOL "" FORCE)

# No audio, no video.
set(WITH_AUDASPACE           OFF CACHE BOOL "" FORCE)
set(WITH_CODEC_FFMPEG        OFF CACHE BOOL "" FORCE)
set(WITH_CODEC_SNDFILE       OFF CACHE BOOL "" FORCE)
set(WITH_COREAUDIO           OFF CACHE BOOL "" FORCE)
set(WITH_JACK                OFF CACHE BOOL "" FORCE)
set(WITH_OPENAL              OFF CACHE BOOL "" FORCE)
set(WITH_PULSEAUDIO          OFF CACHE BOOL "" FORCE)
set(WITH_PIPEWIRE            OFF CACHE BOOL "" FORCE)
set(WITH_SDL_AUDIO           OFF CACHE BOOL "" FORCE)
set(WITH_WASAPI              OFF CACHE BOOL "" FORCE)
set(WITH_RUBBERBAND          OFF CACHE BOOL "" FORCE)

# Cycles: CPU only, for baking. Observation renders are EEVEE.
set(WITH_CYCLES              ON  CACHE BOOL "" FORCE)
set(WITH_CYCLES_EMBREE       ON  CACHE BOOL "" FORCE)
set(WITH_CYCLES_DEVICE_CUDA  OFF CACHE BOOL "" FORCE)
set(WITH_CYCLES_DEVICE_OPTIX OFF CACHE BOOL "" FORCE)
set(WITH_CYCLES_DEVICE_HIP   OFF CACHE BOOL "" FORCE)
set(WITH_CYCLES_DEVICE_HIPRT OFF CACHE BOOL "" FORCE)
set(WITH_CYCLES_DEVICE_METAL OFF CACHE BOOL "" FORCE)
set(WITH_CYCLES_DEVICE_ONEAPI OFF CACHE BOOL "" FORCE)
set(WITH_CYCLES_OSL          OFF CACHE BOOL "" FORCE)
set(WITH_CYCLES_PATH_GUIDING OFF CACHE BOOL "" FORCE)
set(WITH_CYCLES_HYDRA_RENDER_DELEGATE OFF CACHE BOOL "" FORCE)
set(WITH_LLVM                OFF CACHE BOOL "" FORCE)
set(WITH_OPENIMAGEDENOISE    OFF CACHE BOOL "" FORCE)

# Pipeline formats the agent does not export.
set(WITH_USD                 OFF CACHE BOOL "" FORCE)
set(WITH_HYDRA               OFF CACHE BOOL "" FORCE)
set(WITH_MATERIALX           OFF CACHE BOOL "" FORCE)
set(WITH_ALEMBIC             OFF CACHE BOOL "" FORCE)
set(WITH_IO_GREASE_PENCIL    OFF CACHE BOOL "" FORCE)
set(WITH_IMAGE_CINEON        OFF CACHE BOOL "" FORCE)
set(WITH_IMAGE_OPENJPEG      OFF CACHE BOOL "" FORCE)

# Simulation, tracking, line art, PDF.
set(WITH_LIBMV               OFF CACHE BOOL "" FORCE)
set(WITH_FREESTYLE           OFF CACHE BOOL "" FORCE)
set(WITH_MOD_FLUID           OFF CACHE BOOL "" FORCE)
set(WITH_MOD_OCEANSIM        OFF CACHE BOOL "" FORCE)
set(WITH_BULLET              OFF CACHE BOOL "" FORCE)
set(WITH_HARU                OFF CACHE BOOL "" FORCE)

# Kept explicitly so the intent is visible in the cache.
set(WITH_PYTHON              ON  CACHE BOOL "" FORCE)
set(WITH_OPENVDB             ON  CACHE BOOL "" FORCE)
set(WITH_MOD_REMESH          ON  CACHE BOOL "" FORCE)
set(WITH_QUADRIFLOW          ON  CACHE BOOL "" FORCE)
set(WITH_GMP                 ON  CACHE BOOL "" FORCE)
set(WITH_MANIFOLD            ON  CACHE BOOL "" FORCE)
set(WITH_OPENSUBDIV          ON  CACHE BOOL "" FORCE)
set(WITH_POTRACE             ON  CACHE BOOL "" FORCE)
set(WITH_IMAGE_WEBP          ON  CACHE BOOL "" FORCE)
set(WITH_IO_FBX              ON  CACHE BOOL "" FORCE)
set(WITH_IO_WAVEFRONT_OBJ    ON  CACHE BOOL "" FORCE)
set(WITH_IO_PLY              ON  CACHE BOOL "" FORCE)
set(WITH_IO_STL              ON  CACHE BOOL "" FORCE)
set(WITH_DRACO               ON  CACHE BOOL "" FORCE)
set(WITH_MESHOPTIMIZER       ON  CACHE BOOL "" FORCE)
set(WITH_UV_SLIM             ON  CACHE BOOL "" FORCE)
