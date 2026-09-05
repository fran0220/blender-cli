# Blender in Amp orbs

Setup installs Debian build prerequisites and Clang 19, then hydrates source LFS
assets and the exact `lib/linux_x64` revision pinned by this checkout. Amp caches
these in its project snapshot. Warm setup checks installed packages and reuses
Git/LFS objects; it does not update the source branch or build Blender. Initial
library downloads are substantial and require network access to GitHub and
projects.blender.org. No application secrets or backing services are required.
Source assets are fetched from Blender's upstream LFS remote because this GitHub
fork does not host those objects. The fallback remote has pushing disabled.

New login shells inside this checkout select `clang-19` / `clang++-19` via `CC`
and `CXX`. Existing CMake build directories retain their original compiler; use a
fresh build directory when switching compilers. Resume installs nothing.

For a headless development build with C++ tests:

```sh
cmake -S . -B build/orb -G Ninja \
  -C build_files/cmake/config/blender_headless.cmake \
  -DCMAKE_BUILD_TYPE=Release -DWITH_GTESTS=ON \
  -DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache
cmake --build build/orb --target install --parallel 2
build/orb/bin/blender --background --factory-startup --python-exit-code 1 \
  --python-expr 'import bpy; print(bpy.app.version_string)'
ctest --test-dir build/orb -N
```

Compilation can be expensive; select parallelism appropriate for the orb's RAM.
Run selected tests with `ctest --test-dir build/orb --output-on-failure -R PATTERN`.
The tracked `tests/files` LFS fixtures are included. GPU execution requires GPU
hardware and is not provided by setup.
