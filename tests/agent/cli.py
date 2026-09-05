# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""The CLI is a projection of the request table, checked against the built binary.

The flag table is generated from `REQUESTS` at build time, so the property under
test is that the binary that shipped carries the projection of the contract that
shipped with it: every field of every request has exactly one flag, `--help`
shows it, and the parser turns it into that field of the request.
"""

import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "source" / "blender" / "agent"))
import agent_cli_gen  # noqa: E402


def main():
    executable = str(Path(sys.argv[1]).resolve())
    with tempfile.TemporaryDirectory(prefix="agent cli ") as directory:
        root = Path(directory)

        def raw(*args, stdin=None):
            return subprocess.run([executable, *map(str, args)], cwd=root, input=stdin,
                                  capture_output=True, text=True, timeout=300)

        def call(*args, ok=True, stdin=None):
            process = raw(*args, "--json", stdin=stdin)
            assert process.returncode == (0 if ok else 1), (args, process.stdout, process.stderr)
            result = json.loads(process.stdout)
            assert result.get("ok", True) is ok, (args, result)
            return result

        def message(*args):
            """The error of a request that is allowed to fail for its own reasons."""
            process = raw(*args, "--json")
            result = json.loads(process.stdout)
            return "" if result.get("ok", True) else result["error"]["message"]

        # The contract as the built binary serves it, not as the source tree has it.
        contract = call("describe", "channel")
        requests = contract["requests"]
        verbs = agent_cli_gen.table(contract["defs"], requests)
        assert verbs, contract

        help_text = raw("--help")
        assert help_text.returncode == 0, help_text
        usage = help_text.stdout

        # One flag per field, one field per flag, and --help shows every one.
        for verb in verbs:
            fields = requests[verb["name"]]["fields"]
            assert {entry["field"] for entry in verb["fields"]} == set(fields), verb
            flags = [entry["flag"] for entry in verb["fields"] if entry["flag"]]
            assert len(flags) == len(set(flags)), verb
            for entry in verb["fields"]:
                shown = entry["flag"] or entry["value"]
                assert shown in usage, (verb["name"], entry, usage)
        for op in requests:
            assert (op in {verb["name"] for verb in verbs}) == (op not in agent_cli_gen.NO_CLI), op
        for name, _, _ in agent_cli_gen.LAUNCHER_VERBS:
            assert name in usage, (name, usage)
        assert "cancel" in agent_cli_gen.NO_CLI
        assert "Unknown verb: cancel" in message("cancel")
        assert "Unknown verb: compare" in message("compare")
        assert "Unknown option for exec: --nosuchflag" in message("exec", "--nosuchflag")
        assert "Unknown option for observe: --metric" in message("observe", "--metric", "iou")

        # Every flag reaches its field. Each call names a scene that does not
        # exist, so the request is rejected for that reason after the command
        # line has been parsed: a verb whose handler is not installed yet still
        # proves its projection, and the failure is never the CLI's.
        parse_errors = ("Unknown option", "Unknown verb", "takes no positional",
                        "takes too many positional", "requires a value")
        for verb in verbs:
            for entry in verb["fields"]:
                if entry["position"] >= 0 or entry["kind"] in ("Flag", "NoFlag"):
                    continue
                sample = {"Json": "{}", "List": "front", "Int": "512", "Num": "1"}.get(
                    entry["kind"], entry["value"].split("|")[0] if "|" in entry["value"] else "x")
                action = requests[verb["name"]]["fields"].get("action", {}).get("enum")
                args = [verb["name"], *(action[:1] if action else []), entry["flag"], sample,
                        *(() if verb["name"] == "session" else ("--file", root / "absent.blend"))]
                assert not any(bad in message(*args) for bad in parse_errors), args

        blend = root / "start.blend"
        call("exec", "-c", "bpy.ops.wm.read_factory_settings(use_empty=True)", "--save", blend)
        call("session", "open", "--file", blend)
        try:
            # session feedback: dotted settings, JSON values, and the policy back.
            policy = call("session", "feedback", "perception=false", "image.mode=off",
                          'image.views=["front"]')["feedback"]
            assert policy["perception"] is False and policy["image"]["mode"] == "off", policy
            assert policy["image"]["views"] == ["front"], policy
            assert call("session", "status")["feedback"] == policy
            # exec: code, script, record, timeout and the image policy override.
            assert call("exec", "-c", "6 * 7")["value"] == "42"
            script = root / "step.py"
            script.write_text("bpy.ops.mesh.primitive_cube_add()\nlen(bpy.data.objects)\n")
            assert call("exec", script)["value"] == "1"
            assert call("exec", "-c", "@" + str(script))["value"] == "2"
            assert call("exec", "-c", "-", stdin="len(bpy.data.objects)\n")["value"] == "2"
            assert call("exec", "-c", "@@literal", ok=False)["error"]["type"] == "SyntaxError"
            steps = len(call("program", "get")["steps"])
            call("exec", "-c", "bpy.ops.mesh.primitive_cube_add()", "--no-record")
            assert len(call("program", "get")["steps"]) == steps, "--no-record must not record"
            assert call("exec", "-c", "while True:\n    pass", "--timeout", "0.01",
                        ok=False)["error"]["type"] == "TimeoutError"

            # --image is the per-request image policy, and it is the request's
            # `feedback` field: a whole frame when asked for one, nothing when not.
            # The first change establishes the view the next one is a delta of.
            call("session", "feedback", "perception=true", "image.mode=delta", "image.size=128")
            call("exec", "-c", "bpy.data.objects['Cube'].scale.z = 2.5")
            frames = call("exec", "-c", "bpy.data.objects['Cube'].scale.x = 2.0",
                          "--image", "full")["images"]
            assert [image["kind"] for image in frames] == ["full"], frames
            assert frames[0]["size"] == [128, 128], frames
            assert frames[0]["region"] == [0, 0, 128, 128], frames
            assert "images" not in call("exec", "-c", "bpy.data.objects['Cube'].scale.y = 1.5",
                                        "--image", "off")
            call("session", "feedback", "perception=false", "image.mode=off")

            # inspect: object, full and the space-separated RNA paths.
            full = call("inspect", "--object", "Cube", "--full")
            assert [obj["name"] for obj in full["objects"]] == ["Cube"], full
            selected = call("inspect", "--select", 'objects["Cube"].location',
                            'objects["Cube"].scale')
            assert set(selected["selected"]) == {'objects["Cube"].location',
                                                 'objects["Cube"].scale'}, selected

            # session: the action and its argument are words, the rest are flags.
            labelled = call("session", "snapshot", "--label", "checkpoint")
            assert labelled["label"] == "checkpoint", labelled
            assert call("session", "rollback", "checkpoint")["snapshot"], "label rollback"
            assert call("session", "rollback", "~0")["snapshot"], "offset rollback"
            saved = root / "saved.blend"
            call("session", "save", "--file", saved)
            assert saved.is_file()

            # program: the action is a word, the version is its argument, and the
            # program text is long enough to come from a file.
            assert call("program", "record", "off")["record"] is False
            assert call("program", "record", "on")["record"] is True
            assert "requires on|off" in message("program", "record")
            source = root / "model.py"
            source.write_text('# blender-cli program\n# base: factory\nP = {"size": 1.0}\n'
                              "\n# step 1\nbpy.ops.mesh.primitive_cube_add(size=P['size'])\n")
            version = call("program", "set", "--text", "@" + str(source))["version"]
            assert call("program", "get")["text"] == source.read_text()
            call("program", "patch", "--old", '"size": 1.0', "--new", '"size": 2.0')
            assert '"size": 2.0' in call("program", "get")["text"]
            assert call("program", "rollback", version)["version"] == version
            assert '"size": 1.0' in call("program", "get")["text"]

            # describe: the path is the verb's argument.
            assert call("describe", "bpy.types.Object.location")["array_length"] == 3
        finally:
            call("session", "close")
    print("agent cli: all assertions passed", flush=True)


if __name__ == "__main__":
    main()
