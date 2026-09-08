# SPDX-FileCopyrightText: 2026 blender-cli Authors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Copy a complete production install and optionally archive it.

Never edits the install input. Measurements are logical bytes (symlinks excluded).
Run tests/agent/package.py and the installed protocol suite before distribution.
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
              "before": components(resources), "removed": [], "components": {}}
    for pattern in ("lib/*", "blender.shared/*", "*.dll", "*.exe", version_dir.name + "/datafiles/*",
                    version_dir.name + "/scripts/addons_core/*",
                    version_dir.name + "/python/lib/python3.*/*",
                    version_dir.name + "/python/lib/*"):
        for path in sorted(resources.glob(pattern)):
            report["components"][str(path.relative_to(resources))] = size(path)

    def remove(path, reason):
        report["removed"].append({"path": str(path.relative_to(output)), "bytes": size(path),
                                  "present": path.exists() or path.is_symlink(), "reason": reason})
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)

    # No feature-based pruning: libraries, their manifests and Python bindings,
    # add-ons, assets, fonts and the complete OCIO tree remain upstream-exact.
    # Standalone installed test executables are validation tools, not resources.
    # Do not recursively strip Python or add-on directories named tests.
    remove(output / "tests", "standalone test binaries, not runtime resources")
    # These executables only generate build inputs; Blender never runs them.
    binaries = output / "bin" if platform == "macos-arm64" else output
    for name in ("datatoc", "makesdna", "makesrna", "shader_tool", "zstd_compress"):
        remove(binaries / (name + (".exe" if platform == "windows-x64" else "")),
               "build-time code/data generator")
    for path in sorted(output.glob("*.pdb")):
        remove(path, "debug symbols, not runtime code")
    result = subprocess.check_output([str(executable), "--version"], text=True)
    version = re.search(r"^blender-cli (\S+)$", result, re.M)[1]
    # Python regenerates these from retained sources. Do not strip static SDK
    # archives or stdlib packages: explicit Python extensions may need them.
    for path in sorted(output.rglob("__pycache__")):
        if path.exists():
            remove(path, "regenerable Python bytecode")
    report.update(version=version, trimmed_bytes=size(output), after=components(resources))
    if archive:
        name = "blender-cli-" + version + "-" + platform
        if platform == "windows-x64":
            artifact = Path(shutil.make_archive(str(output.parent / name), "zip", output.parent, output.name))
        else:
            artifact = output.parent / (name + ".tar.zst")
            subprocess.run(["tar", "--use-compress-program", "zstd -19", "-cf", str(artifact), "-C", str(output.parent),
                            output.name], check=True)
        report.update(artifact=artifact.name, compressed_bytes=artifact.stat().st_size)
    output.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("install", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--platform", required=True, choices=("macos-arm64", "windows-x64", "linux-x64"))
    parser.add_argument("--archive", action="store_true")
    args = parser.parse_args()
    package(args.install, args.output, args.platform, args.archive)
