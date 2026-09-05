# Blender in Amp orbs

Setup installs Debian build prerequisites and xPack GCC 14.3.0, then hydrates source LFS
assets and the exact `lib/linux_x64` revision pinned by this checkout. Amp caches
these in its project snapshot. Warm setup checks installed packages and reuses
Git/LFS objects; it does not update the source branch or build Blender. Initial
library downloads are substantial and require network access to GitHub and
projects.blender.org. No application secrets or backing services are required.
Source assets are fetched from Blender's upstream LFS remote because this GitHub
fork does not host those objects. The fallback remote has pushing disabled.

New login shells inside this checkout select `/opt/xpack-gcc/bin/gcc` and
`/opt/xpack-gcc/bin/g++` via `CC` and `CXX`. The matching libstdc++/libgcc_s runtime
is installed in `/usr/local/lib` and registered with `ldconfig`. Upstream now
requires GCC >= 14; Clang 19 with Debian 12's libstdc++ 12 fails on constexpr
`std::string` in `BLI_ustring.hh` and transitive includes in the shader tool.
The standalone xPack compiler uses Debian's existing system sysroot; no glibc
upgrade or upstream source workaround is needed. Clang tools remain installed
for formatting/editor integration, not as the build compiler.
Existing CMake caches retain their compiler: clear the old CMake cache when
switching, then keep one `build/orb` directory throughout development.
Resume installs nothing.

For a headless development build with C++ tests:

```sh
cmake -S . -B build/orb -G Ninja \
  -C build_files/cmake/config/blender_agent.cmake \
  -DCMAKE_BUILD_TYPE=Release -DWITH_GTESTS=ON \
  -DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache
amp orb service start blender-build --command 'cd /home/user/workspace/repo && set -o pipefail && { if cmake --build build/orb --target install --parallel 12 2>&1 | tee build/orb/build.log; then echo BUILD_SUCCEEDED; else echo BUILD_FAILED; fi; sleep infinity; }'
build/orb/bin/blender --background --factory-startup --python-exit-code 1 \
  --python-expr 'import bpy; print(bpy.app.version_string)'
ctest --test-dir build/orb -N
```

The build service stays idle after completion so supervision does not restart a
failed build repeatedly. Read `build/orb/build.log` and service logs for status.
`pipefail` preserves compiler failures through `tee`; the success/failure marker
does not depend on shell-variable expansion by the service manager.
Compilation can be expensive; 12 jobs fits a 16-core / 31 GB orb.
Run selected tests with `ctest --test-dir build/orb --output-on-failure -R PATTERN`.
The tracked `tests/files` LFS fixtures are included. Setup explicitly installs
`libgl1-mesa-dri`, `libegl-mesa0`, and `mesa-vulkan-drivers`: background EEVEE
can render using Mesa lavapipe's software Vulkan device without physical GPU
hardware. Mesa 22.3.6 supports Combined but crashes in `libvulkan_lvp.so` when
the native EEVEE Z pass is enabled (also reproduced with upstream's render
operator). Setup upgrades older Mesa to bookworm-backports, verified with
25.0.7, rather than adding a renderer workaround or changing Blender's pinned
libraries. This is Linux development evidence, not Metal/macOS or real-GPU
Vulkan/Windows verification.

## Setup verification

On a Debian 12 x86_64 orb, system packages took 11 seconds, initial source assets
about 4m50s, and library provisioning about 1m56s. Cold setup therefore needs
several minutes of network access; these downloads are retained in the snapshot.
Warm setup completed in 1.7 seconds without reinstalling packages, and resume in
3 milliseconds. Those original Clang checks only configured and ran the
`atomic_test` target (56 tests); they did not prove a full build. The GCC migration
is verified separately in `PLAN.md`; do not interpret the earlier configure-only
evidence as support for the old Clang/libstdc++ combination.
