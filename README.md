<!--
Keep this document short & concise,
linking to external resources instead of including content in-line.
See 'release/text/readme.html' for the end user read-me.
-->

<!-- blender-cli -->
## blender-cli

blender-cli is Blender with its GUI entry replaced by a request loop. One
process holds the scene, the Python namespace, offscreen rendering, the
comparison metrics and the parameter search; an agent holds one channel to it,
sends one statement at a time, and gets that statement's consequences back as a
stream of events — what changed in the data, what changed in the picture, and
how far the scene now is from its target.

```sh
blender-cli repl                       # the channel: JSON-line requests in, events out
blender-cli session open               # or a daemon, and one verb per request
blender-cli exec -c 'bpy.ops.mesh.primitive_cube_add()' --json
blender-cli --help                     # every verb with every flag
```

The Python API is upstream's `bpy`, unwrapped: there is no DSL, no operator
wrapper layer and no typed tool catalog. What the fork adds around it is the
persistent channel, feedback pushed with every action, a re-executable program
as the record of the scene, in-process parameter search, self-description from
live RNA, and errors that name the nearest valid identifier.

Start with [the working recipe](doc/agent/usage.md); the contract is
[design.md](doc/agent/design.md), the constraints are [AGENTS.md](AGENTS.md),
and [build and packaging details](doc/agent/build-profile.md) cover the build
profile. Product artifacts are `blender-cli-<version>-macos-arm64.tar.zst` and
`blender-cli-<version>-windows-x64.zip`; Linux archives are development evidence only.
Check [PLAN.md](PLAN.md) for which platforms have actually passed verification.

Extract the entire archive, then run `./blender-cli --version` (macOS) or
`blender-cli.exe --version` (Windows). The macOS tree is a plain directory:
the top-level CLI symlink points into `bin/`, beside `Resources/`. Keep those
directories together; their relative paths preserve upstream resource and dylib lookup.
No `.app`, installer, signing, or notarization is provided. On a trusted download,
remove macOS quarantine with `xattr -dr com.apple.quarantine <extracted-directory>`
before first use; unsigned artifacts are not Gatekeeper-approved releases.

Native full-build CI uploads archives, not GitHub Releases. Rendering tests that
report a missing Metal/Vulkan device are **skipped**, not evidence that rendering
works on that platform. See the run's diagnostics and package measurement JSON.
<!-- /blender-cli -->

Blender
=======

Blender is the free and open source 3D creation suite.
It supports the entirety of the 3D pipeline—modeling, rigging, animation, simulation, rendering, compositing,
motion tracking and video editing.

![Blender screenshot](https://code.blender.org/wp-content/uploads/2018/12/springrg.jpg "Blender screenshot")

Project Pages
-------------

- [Main Website](https://www.blender.org)
- [Reference Manual](https://docs.blender.org/manual/en/latest/index.html)
- [User Community](https://www.blender.org/community/)

Development
-----------

- [Build Instructions](https://developer.blender.org/docs/handbook/building_blender/)
- [Code Review & Bug Tracker](https://projects.blender.org)
- [Developer Forum](https://devtalk.blender.org)
- [Developer Documentation](https://developer.blender.org/docs/)


License
-------

Blender as a whole is licensed under the GNU General Public License, Version 3.
Individual files may have a different but compatible license.

See [blender.org/about/license](https://www.blender.org/about/license) for details.
