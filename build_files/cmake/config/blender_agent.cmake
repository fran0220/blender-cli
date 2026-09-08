# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

# Complete production Blender, with an agent request loop instead of WM_main.
# Inherit upstream's production dependency decisions, not a modelling subset.
# Usage: cmake -S . -B build/orb -C build_files/cmake/config/blender_agent.cmake
include("${CMAKE_CURRENT_LIST_DIR}/blender_release.cmake")

set(WITH_AGENT ON CACHE BOOL "" FORCE)
set(WITH_PYTHON ON CACHE BOOL "" FORCE)
set(WITH_CYCLES_HYDRA_RENDER_DELEGATE ON CACHE BOOL "" FORCE)

# Only desktop integration and interactive input are excluded. International
# fonts/text remain production dependencies, including for rendered text.
set(WITH_BLENDER_THUMBNAILER OFF CACHE BOOL "" FORCE)
set(WITH_INPUT_NDOF OFF CACHE BOOL "" FORCE)
set(WITH_INPUT_IME OFF CACHE BOOL "" FORCE)
set(WITH_XR_OPENXR OFF CACHE BOOL "" FORCE)
set(WITH_GHOST_SDL OFF CACHE BOOL "" FORCE)

if(WIN32)
  # Explicitly restore these when reconfiguring a formerly CPU-only cache;
  # upstream's release preset relies on their normal option defaults.
  set(WITH_CYCLES_DEVICE_CUDA ON CACHE BOOL "" FORCE)
  set(WITH_CYCLES_DEVICE_HIP ON CACHE BOOL "" FORCE)
endif()

if(UNIX AND NOT APPLE)
  set(WITH_HEADLESS ON CACHE BOOL "" FORCE)
  # Development builds use upstream's normal GPU defaults rather than the
  # release farm's ahead-of-time SDK toolchains. CUDA/HIP runtime compilation
  # remains available; OptiX is subject to upstream SDK discovery.
  set(WITH_CYCLES_DEVICE_CUDA ON CACHE BOOL "" FORCE)
  set(WITH_CYCLES_DEVICE_HIP ON CACHE BOOL "" FORCE)
  set(WITH_CYCLES_DEVICE_OPTIX ON CACHE BOOL "" FORCE)
  set(WITH_CYCLES_DEVICE_HIPRT OFF CACHE BOOL "" FORCE)
  set(WITH_CYCLES_DEVICE_ONEAPI OFF CACHE BOOL "" FORCE)
  set(WITH_CYCLES_CUDA_BINARIES OFF CACHE BOOL "" FORCE)
  set(WITH_CYCLES_HIP_BINARIES OFF CACHE BOOL "" FORCE)
  set(WITH_CYCLES_ONEAPI_BINARIES OFF CACHE BOOL "" FORCE)
else()
  # Cocoa/Metal and Windows Vulkan require the normal offscreen platform path.
  set(WITH_HEADLESS OFF CACHE BOOL "" FORCE)
endif()
