# SPDX-FileCopyrightText: 2026 blender-cli Authors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Copy an installed agent build, trim it, and optionally archive it.

Never edits the install input. Measurements are logical bytes (symlinks excluded).
Run tests/agent/package.py before distributing the result.
"""

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess


def size(path):
    if path.is_symlink() or not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


def components(root):
    return {str(p.relative_to(root)): size(p) for p in sorted(root.iterdir())}


def standard_config(source):
    # Preserve upstream's exact transforms, rather than approximating Standard's
    # ACES reference conversion or sRGB transfer function. Fail on upstream drift.
    def block(section, kind, name):
        match = re.search(r"^" + section + r":\n(.*?)(?=^\S|\Z)", source, re.M | re.S)
        assert match, section
        for entry in re.split(r"(?=^  - !<" + kind + r">)", match[1], flags=re.M):
            if re.search(r"^    name: " + re.escape(name) + r"$", entry, re.M):
                files = re.findall(r"!<FileTransform> \{src: ([^,}]+)", entry)
                assert set(files) <= {"AgX_Base_sRGB.cube"}, (name, files)
                return entry.rstrip() + "\n"
        raise ValueError("Missing upstream OCIO block: " + name)

    header = """# SPDX-FileCopyrightText: 2026 blender-cli Authors
# SPDX-License-Identifier: GPL-2.0-or-later
# Transforms extracted unchanged from Blender's bundled OCIO config.
ocio_profile_version: 2.5
search_path: "icc:luts"
strictparsing: true
luma: [0.2126, 0.7152, 0.0722]
roles:
  reference: ACES2065-1
  scene_linear: Linear Rec.709
  rendering: Linear Rec.709
  default_byte: sRGB
  default_float: Linear Rec.709
  default_sequencer: sRGB
  color_picking: sRGB
  data: Non-Color
  aces_interchange: ACES2065-1
  cie_xyz_d65_interchange: Linear CIE-XYZ D65
  default: Linear Rec.709
  color_timing: ACEScc
  compositing_log: ACEScc
displays:
  sRGB:
    - !<View> {name: Standard, view_transform: Standard, display_colorspace: sRGB}
    - !<View> {name: AgX, view_transform: AgX Base Rec.1886, display_colorspace: sRGB}
    - !<View> {name: Raw, colorspace: Non-Color}
active_displays: [sRGB]
active_views: [Standard, AgX, Raw]
"""
    return (header + "display_colorspaces:\n" +
            block("display_colorspaces", "ColorSpace", "Linear CIE-XYZ D65") +
            block("display_colorspaces", "ColorSpace", "sRGB") +
            block("display_colorspaces", "ColorSpace", "Rec.1886") + "view_transforms:\n" +
            block("view_transforms", "ViewTransform", "Standard") +
            block("view_transforms", "ViewTransform", "AgX Base Rec.1886") + "colorspaces:\n" +
            "".join(block("colorspaces", "ColorSpace", name)
                    for name in ("Linear Rec.709", "ACES2065-1", "Non-Color", "ACEScc", "Linear FilmLight E-Gamut")))


def package(install, output, platform, archive):
    install, output = install.resolve(), output.resolve()
    if output == install or install in output.parents or output in install.parents:
        raise ValueError("Output and install must be separate, non-nested trees")
    if output.exists():
        raise FileExistsError(output)
    app = install / "Blender.app" / "Contents"
    if platform == "macos-arm64":
        output.mkdir(parents=True)
        shutil.copytree(app / "MacOS", output / "bin", symlinks=True)
        shutil.copytree(app / "Resources", output / "Resources", symlinks=True)
        (output / "blender-cli").symlink_to("bin/blender-cli")
        resources = output / "Resources"
        # Preserve upstream redistribution notices outside the .app as well.
        for path in install.iterdir():
            if path.is_file():
                shutil.copy2(path, output / path.name)
    else:
        shutil.copytree(install, output, symlinks=True)
        resources = output
    executable = output / ("blender-cli.exe" if platform == "windows-x64" else "blender-cli")
    versions = [p for p in resources.iterdir() if re.fullmatch(r"\d+\.\d+", p.name)]
    assert len(versions) == 1, versions
    version_dir = versions[0]
    report = {"platform": platform, "installed_bytes": size(install),
              "before": components(resources), "removed": []}
    report["components"] = {}
    for pattern in ("lib/*", "*.dll", "*.exe", version_dir.name + "/datafiles/*",
                    version_dir.name + "/scripts/addons_core/*",
                    version_dir.name + "/python/lib/python3.*/*",
                    version_dir.name + "/python/lib/*"):
        for path in sorted(resources.glob(pattern)):
            report["components"][str(path.relative_to(resources))] = size(path)

    def remove(path):
        amount = size(path)
        present = path.exists() or path.is_symlink()
        report["removed"].append({"path": str(path.relative_to(output)), "bytes": amount,
                                  "present": present})
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)

    addons = version_dir / "scripts" / "addons_core"
    # These are imported unconditionally by upstream's factory startup, or
    # register the retained Cycles engine. Preserve their original module names
    # and code in the standard module search path, not the optional add-on tree.
    report["relocated"] = []
    for name in ("bl_pkg", "io_anim_bvh", "io_curve_svg", "io_mesh_uv_layout", "cycles", "pose_library"):
        path = addons / name
        destination = version_dir / "scripts" / "modules" / name
        report["relocated"].append({"module": name, "bytes": size(path)})
        shutil.move(str(path), destination)
    keep = {"io_scene_gltf2", "io_scene_fbx", "rigify"}
    for path in sorted(addons.iterdir()):
        if path.name not in keep:
            remove(path)
    python = version_dir / "python"
    # Do not probe both layouts: Lib also matches lib on case-insensitive macOS.
    stdlibs = [python / "lib"] if platform == "windows-x64" else list(python.glob("lib/python3.*"))
    assert len(stdlibs) == 1 and stdlibs[0].is_dir(), stdlibs
    for name in ("test", "idlelib", "tkinter", "ensurepip", "lib2to3", "turtledemo"):
        remove(stdlibs[0] / name)
    for path in sorted(python.rglob("*.a")):
        remove(path)
    # Upstream's macOS/Windows Python install copies VFX SDK bindings even when
    # those engines are disabled. None are dependencies of the kept add-ons or
    # agent modules. Keep requests/zstandard used by upstream Python modules.
    site = stdlibs[0] / "site-packages"
    for pattern in ("pxr", "usd_core*", "MaterialX*", "materialx*", "oslquery*",
                    "OpenImageIO*", "openimageio*", "PyOpenColorIO*", "opencolorio*",
                    "openvdb*", "pyopenvdb*", "Cython*", "cython*"):
        for path in sorted(site.glob(pattern)):
            remove(path)
    for path in sorted(resources.glob("*.icns")) + list(resources.glob("Assets.car")):
        remove(path)
    data = version_dir / "datafiles"
    for path in sorted((data / "fonts").iterdir()):
        if path.name != "Inter.woff2":
            remove(path)
    # BLF initializes both filenames even in background mode. One font face,
    # two names; ZIP cannot preserve links, so Windows carries a second copy.
    mono = data / "fonts" / "DejaVuSansMono.woff2"
    if platform == "windows-x64":
        shutil.copy2(data / "fonts" / "Inter.woff2", mono)
    else:
        mono.symlink_to("Inter.woff2")
    for path in sorted((data / "studiolights").rglob("*")):
        if path.is_file() and path.relative_to(data / "studiolights").as_posix() != "studio/basic.sl":
            remove(path)
    for path in (data / "locale", data / "icons", version_dir / "scripts" / "presets" / "interface_theme"):
        remove(path)
    ocio = data / "colormanagement"
    config = ocio / "config.ocio"
    previous = size(config)
    config.write_text(standard_config(config.read_text()), encoding="utf-8")
    report["removed"].append({"path": str(config.relative_to(output)),
                              "bytes": previous - size(config), "present": True})
    # Factory startup still references AgX. Dropping that view emits a warning
    # before the agent command starts, corrupting one-shot JSON. Keep its real
    # transform, not an alias that would silently change AgX semantics.
    remove(ocio / "filmic")
    for path in sorted((ocio / "luts").iterdir()):
        if path.name != "AgX_Base_sRGB.cube":
            remove(path)
    for path in sorted((ocio / "icc").iterdir()):
        if path.name not in {"srgb_rec709_display.icc", "g24_rec709_display.icc"}:
            remove(path)
    # Build helpers share bin/ with the default CMake install prefix. They are
    # not runtime executables. Preserve licenses and the upstream engine itself.
    for name in ("datatoc", "makesdna", "makesrna", "shader_tool", "zstd_compress",
                 "blender-launcher", "blender.desktop", "blender.svg", "blender-symbolic.svg"):
        for suffix in ("", ".exe") if platform == "windows-x64" else ("",):
            remove(output / (name + suffix))
    for path in sorted(output.glob("*.pdb")):
        remove(path)
    # Upstream bundles all precompiled shared libraries regardless of disabled
    # profile options. These correspond to explicitly disabled engines/backends,
    # whose separate Python bindings were removed above. Retain SYCL/UR: the
    # pinned Embree dependency links them even with Cycles GPU devices disabled.
    libraries = resources / "lib" if platform != "windows-x64" else output
    for path in sorted(libraries.iterdir()):
        if re.match(r"(?:lib)?(?:usd|osl|MaterialX|ceres|hiprt|openxr|SDL)", path.name):
            remove(path)
    result = subprocess.check_output([str(executable), "--version"], text=True)
    version = re.search(r"^blender-cli (\S+)$", result, re.M)[1]
    # The version probe also initializes Python. Remove caches after that last
    # invocation so a fresh archive contains no generated bytecode.
    for path in sorted(output.rglob("__pycache__")):
        if path.exists():
            remove(path)
    report.update(version=version, trimmed_bytes=size(output), after=components(resources))
    if archive:
        name = "blender-cli-" + version + "-" + platform
        if platform == "windows-x64":
            artifact = Path(shutil.make_archive(str(output.parent / name), "zip", output.parent, output.name))
        else:
            artifact = output.parent / (name + ".tar.zst")
            # The long option is shared by GNU tar and macOS bsdtar; -I is not.
            subprocess.run(["tar", "--use-compress-program", "zstd -19", "-cf", str(artifact), "-C", str(output.parent),
                            output.name], check=True)
        report.update(artifact=artifact.name, compressed_bytes=artifact.stat().st_size)
    report_path = output.with_suffix(".json")
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("install", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--platform", required=True, choices=("macos-arm64", "windows-x64", "linux-x64"))
    parser.add_argument("--archive", action="store_true")
    args = parser.parse_args()
    package(args.install, args.output, args.platform, args.archive)
