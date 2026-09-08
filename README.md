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
blender-cli scene reset --json         # empty scene; --no-empty keeps factory defaults
blender-cli object create Body --primitive cube --json
blender-cli object transform Body --scale 1,1,2 --json
blender-cli capabilities --json        # actual compiled production features
blender-cli --help                     # every verb with every flag
```

Typed native production commands cover scene creation, data and operators,
rigging, posing, animation, simulation and rendering. Generic RNA and native
operator access extend that surface without generating Python. Upstream `bpy`
is unchanged and available through explicit `exec`, including Python add-ons
such as Rigify, glTF and FBX export. The scene's record is `model.json`, a
structured command program with parameters, named results and a version tree.
Feedback is pushed after actions; numeric fitting runs inside the process.

The production scope includes assets, characters, animation, simulation,
lighting, rendering, compositing and interchange. Headless means no interactive
GUI, not a reduced Blender feature set. `render` uses the production scene's
camera and lights; `observe` uses deterministic observation presets.

Historical loop evidence: modelling a mug from a reference image took 11
requests over one channel and about 6,123 tokens of pushed feedback — two
parameter searches, a program edit, and a recovery from a killed process that
the channel answered without dropping the conversation. None of the 11 existed
only to look at the scene. This predates the native production surface and is
not a validation claim for it; the run and current status are in [PLAN.md](PLAN.md).

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
