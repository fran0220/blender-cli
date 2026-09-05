# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Generate the CLI projection table from the request table.

A one-shot CLI verb is one request: its flags are that request's fields. The
request table is the source of truth, so the flags are derived from it here and
compiled into `agent_cli.hh`, instead of being written twice. A workstream that
adds a field to `REQUESTS` gets its flag with no C++ edit.

The table is read out of the module source with `ast`, not imported: the runtime
module imports `bpy`, which only exists inside Blender, and this runs under the
plain CPython that CMake already uses. Point it at whichever module holds the
tables:

    python3 agent_cli_gen.py agent_contract.py agent_cli_table.hh

Naming rule, applied to every field that has no entry in `IRREGULAR`:

    boolean, default true    --no-<field>            clears it
    boolean, otherwise       --<field>               sets it
    (an underscore in a field name is a hyphen in its flag)
    array of strings         --<field> FIELD,…       comma-separated
    integer / number         --<field> FIELD         parsed as a number
    string                   --<field> FIELD         verbatim, `a|b` when bounded
    anything structured      --<field> JSON          literal JSON, or @FILE to read it

`IRREGULAR` is the complete list of fields whose projection is not that rule.
Every entry is a deliberate contract decision, and each one says why.
"""

import ast
import sys

# Requests with no CLI projection, and why.
NO_CLI = {
    # `cancel` needs a second connection to a request that is still running, so
    # it exists on the channel only. One-shot verbs carry one request each.
    "cancel": "channel only: it stops a request that is still running",
}

# Verbs the launcher answers itself. They are not requests: they start or hold
# the process rather than being executed by a session.
LAUNCHER_VERBS = [
    ("repl", "Hold one pipe of JSON-line requests and events. The primary mode.",
     [("file", "--file", "F", "Scene the session opens."),
      ("standalone", "--standalone", "", "Run the loop in this process, without a daemon.")]),
    ("session open", "Start the session daemon for this directory.",
     [("file", "--file", "F", "Scene the session opens.")]),
]

# Every projection that the naming rule does not produce, and the reason for it.
IRREGULAR = {
    # Code is the argument of `exec`, and `-c` is what every language runtime
    # calls it. A script is a path, so it reads as the verb's own argument.
    ("exec", "code"): {"flag": "-c", "kind": "Text", "value": "CODE"},
    ("exec", "script"): {"position": 0, "kind": "Path", "value": "SCRIPT.py"},
    # The per-request feedback override is an image policy, and the only part of
    # it worth a flag is whether a picture comes back at all.
    ("exec", "feedback"): {"flag": "--image", "assign": "feedback.mode", "kind": "Word",
                           "value": "delta|full|off", "doc": "Image policy for this request."},
    ("program", "feedback"): {"flag": "--image", "assign": "feedback.mode", "kind": "Word",
                              "value": "delta|full|off", "doc": "Image policy for this request."},
    # An action reads as the verb's second word: `session rollback`, not
    # `session --action rollback`. Its argument follows it.
    ("session", "action"): {"position": 0},
    ("session", "snapshot"): {"position": 1},
    ("session", "feedback"): {"position": 1, "when": "feedback", "kind": "Settings",
                              "value": "KEY=VALUE…"},
    # The scene to save; every other verb's --file selects the one-shot scene.
    ("session", "file"): {"kind": "Path", "value": "F"},
    ("program", "action"): {"position": 0},
    ("program", "version"): {"position": 1},
    ("program", "on"): {"position": 1, "when": "record", "kind": "OnOff"},
    # A program is a file's worth of code, so these read it from one.
    ("program", "text"): {"kind": "Text"},
    ("program", "old"): {"kind": "Text"},
    ("program", "new"): {"kind": "Text"},
    ("target", "action"): {"position": 0},
    ("target", "name"): {"position": 1},
    ("describe", "path"): {"position": 0, "value": "RNA_PATH|channel|schema"},
    # RNA paths contain commas inside subscripts, so they are separate words.
    ("inspect", "select"): {"kind": "Words", "value": "PATH…"},
}


def contract(path):
    """Read DEFS and REQUESTS out of a module without importing it."""
    tables = {}
    for node in ast.parse(open(path, encoding="utf-8").read(), path).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            name = getattr(node.targets[0], "id", None)
            if name in ("DEFS", "REQUESTS"):
                tables[name] = ast.literal_eval(node.value)
    missing = {"DEFS", "REQUESTS"} - set(tables)
    if missing:
        raise SystemExit(f"{path} does not define {', '.join(sorted(missing))}")
    return tables["DEFS"], tables["REQUESTS"]


def projection(op, field, spec, defs):
    """The one CLI projection of one request field."""
    name = field.upper()
    structured = "ref" in spec or (spec.get("type") == "array" and
                                   spec.get("items", {}).get("type") != "string")
    if structured:
        kind, value = "Json", "JSON"
    elif spec.get("type") == "boolean":
        kind, value = ("NoFlag", "") if spec.get("default") is True else ("Flag", "")
    elif spec.get("type") == "array":
        kind, value = "List", name + ",…"
    elif spec.get("type") in ("integer", "number"):
        kind = "Int" if spec["type"] == "integer" else "Num"
        value = "|".join(str(item) for item in spec["enum"]) if "enum" in spec else name
    else:
        kind, value = "Word", "|".join(spec["enum"]) if "enum" in spec else name
    entry = {"field": field, "assign": field, "flag": "", "position": -1, "when": "",
             "kind": kind, "value": value, "doc": spec.get("doc", ""),
             "required": bool(spec.get("required"))}
    override = IRREGULAR.get((op, field), {})
    entry.update(override)
    placeholders = {"OnOff": "on|off", "Settings": "KEY=VALUE", "Json": "JSON",
                    "Flag": "", "NoFlag": ""}
    if "value" not in override and entry["kind"] in placeholders:
        entry["value"] = placeholders[entry["kind"]]
    if entry["position"] < 0 and not entry["flag"]:
        entry["flag"] = ("--no-" if entry["kind"] == "NoFlag" else "--") + field.replace("_", "-")
    return entry


def table(defs, requests):
    """Every verb, with its fields in the order the contract declares them."""
    verbs = []
    for op, request in requests.items():
        if op in NO_CLI:
            continue
        fields = [projection(op, name, spec, defs) for name, spec in request["fields"].items()]
        fields.sort(key=lambda entry: entry["position"] if entry["position"] >= 0 else 99)
        verbs.append({"name": op, "op": op, "doc": request["doc"], "fields": fields})
    return verbs


def quote(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def emit(verbs, source):
    """The generated header: static data, no logic."""
    lines = ["/* SPDX-FileCopyrightText: 2026 blender-cli Authors",
             " *",
             " * SPDX-License-Identifier: GPL-2.0-or-later */",
             "",
             f"/* Generated by agent_cli_gen.py from {source}. Do not edit.",
             " * Every flag here is a field of the request it builds. */",
             "",
             "#pragma once",
             "",
             "namespace blender::agent {",
             "",
             "/* How one argument becomes one JSON value. */",
             "enum class CliKind {",
             "  Word,     /* verbatim string */",
             "  Text,     /* string, or @FILE to read one, or - for stdin */",
             "  Path,     /* string made absolute against the caller's directory */",
             "  Int,      /* integer */",
             "  Num,      /* number */",
             "  List,     /* comma-separated array of strings */",
             "  Words,    /* the following words, until the next flag */",
             "  Json,     /* literal JSON, or @FILE to read it */",
             "  Flag,     /* no argument; true */",
             "  NoFlag,   /* no argument; false */",
             "  OnOff,    /* the word on or off */",
             "  Settings, /* the remaining KEY=VALUE words, merged into the field */",
             "};",
             "",
             "struct CliField {",
             "  const char *field;    /* the request field this fills */",
             "  const char *assign;   /* dotted path assigned, when not the field itself */",
             "  const char *flag;     /* empty when the field is positional */",
             "  int position;         /* positional index, or -1 */",
             "  const char *when;     /* only for this action, when set */",
             "  CliKind kind;",
             "  const char *value;    /* placeholder shown by --help */",
             "  const char *doc;",
             "  bool required;",
             "};",
             "",
             "struct CliVerb {",
             "  const char *name;",
             "  const char *op;       /* empty for a verb the launcher answers itself */",
             "  const char *doc;",
             "  const CliField *fields;",
             "  int field_count;",
             "};",
             ""]
    for verb in verbs:
        lines.append(f"inline const CliField cli_fields_{verb['name']}[] = {{")
        for entry in verb["fields"]:
            lines.append("    {{{}, {}, {}, {}, {}, CliKind::{}, {}, {}, {}}},".format(
                quote(entry["field"]), quote(entry["assign"]), quote(entry["flag"]),
                entry["position"], quote(entry["when"]), entry["kind"],
                quote(entry["value"]), quote(entry["doc"]),
                "true" if entry["required"] else "false"))
        lines.append("};")
        lines.append("")
    for name, doc, fields in LAUNCHER_VERBS:
        symbol = name.replace(" ", "_")
        lines.append(f"inline const CliField cli_fields_{symbol}[] = {{")
        for field, flag, value, field_doc in fields:
            lines.append('    {{{}, {}, {}, -1, "", CliKind::{}, {}, {}, false}},'.format(
                quote(field), quote(field), quote(flag), "Flag" if not value else "Path",
                quote(value), quote(field_doc)))
        lines.append("};")
        lines.append("")
    lines.append("inline const CliVerb CLI_VERBS[] = {")
    for name, doc, fields in LAUNCHER_VERBS:
        symbol = name.replace(" ", "_")
        lines.append('    {{{}, "", {}, cli_fields_{}, {}}},'.format(
            quote(name), quote(doc), symbol, len(fields)))
    for verb in verbs:
        lines.append("    {{{}, {}, {}, cli_fields_{}, {}}},".format(
            quote(verb["name"]), quote(verb["op"]), quote(verb["doc"]),
            verb["name"], len(verb["fields"])))
    lines.append("};")
    lines.append("")
    lines.append(f"inline constexpr int CLI_VERB_COUNT = {len(verbs) + len(LAUNCHER_VERBS)};")
    lines.append("")
    lines.append("}  // namespace blender::agent")
    return "\n".join(lines) + "\n"


def main(argv):
    if len(argv) != 3:
        raise SystemExit("usage: agent_cli_gen.py <contract module> <output header>")
    source, output = argv[1], argv[2]
    defs, requests = contract(source)
    text = emit(table(defs, requests), source.replace("\\", "/").rsplit("/", 1)[-1])
    try:
        if open(output, encoding="utf-8").read() == text:
            return
    except OSError:
        pass
    open(output, "w", encoding="utf-8").write(text)


if __name__ == "__main__":
    main(sys.argv)
