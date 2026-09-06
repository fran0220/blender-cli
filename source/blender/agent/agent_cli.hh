/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

#include <algorithm>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include <json.hpp>

#include "agent_cli_table.hh"

namespace blender::agent {

/* Every CLI verb is one request; its flags are that request's fields. The
 * mapping is not written here: `agent_cli_table.hh` is generated from the
 * request table in the runtime module, so a field added to the contract gets a
 * flag with no edit to this file, and the launcher (which talks to a session)
 * and the in-process one-shot verb build byte-identical requests. Field names,
 * types and enums are validated once, by that same table, in Python.
 *
 * `--file`, `--save` and `--json` are not request fields except where a verb
 * declares them: they select the one-shot scene and the output format. */
struct CommandLine {
  nlohmann::json request = nlohmann::json::object();
  std::string load; /* --file for a one-shot verb: the .blend to open first. */
  std::string save; /* --save for a one-shot verb: the .blend to write after. */
  bool has_save = false;
  bool compact = false; /* --json */
  std::string error;
};

inline void cli_assign(nlohmann::json &request, const std::string &path, nlohmann::json value)
{
  nlohmann::json *node = &request;
  size_t start = 0, dot;
  while ((dot = path.find('.', start)) != std::string::npos) {
    node = &(*node)[path.substr(start, dot - start)];
    start = dot + 1;
  }
  (*node)[path.substr(start)] = std::move(value);
}

inline std::vector<std::string> cli_split(const std::string &text)
{
  std::vector<std::string> items;
  size_t start = 0, comma;
  while ((comma = text.find(',', start)) != std::string::npos) {
    items.push_back(text.substr(start, comma - start));
    start = comma + 1;
  }
  items.push_back(text.substr(start));
  return items;
}

inline std::string cli_absolute(const std::string &path)
{
  return std::filesystem::absolute(std::filesystem::path(path)).lexically_normal().string();
}

/* A statement or a program is too long for one shell word, so a text value may
 * name its source: `@FILE` is that file's contents and `-` is stdin. `@@`
 * begins a literal value, for the rare argument that starts with an at sign. */
inline bool cli_text(const std::string &argument, std::string &text, std::string &error)
{
  if (argument == "-") {
    std::ostringstream buffer;
    buffer << std::cin.rdbuf();
    text = buffer.str();
    return true;
  }
  if (argument.starts_with("@@")) {
    text = argument.substr(1);
    return true;
  }
  if (argument.starts_with("@")) {
    const std::string path = argument.substr(1);
    std::ifstream stream(path, std::ios::binary);
    if (!stream) {
      error = "Could not read " + path;
      return false;
    }
    std::ostringstream buffer;
    buffer << stream.rdbuf();
    text = buffer.str();
    return true;
  }
  text = argument;
  return true;
}

/* KEY=VALUE for `session feedback`; the value is JSON when it parses as JSON. */
inline bool cli_setting(nlohmann::json &request, const std::string &field, const std::string &pair)
{
  const auto equals = pair.find('=');
  if (equals == std::string::npos || equals == 0) {
    return false;
  }
  auto value = nlohmann::json::parse(pair.substr(equals + 1), nullptr, false);
  if (value.is_discarded()) {
    value = pair.substr(equals + 1);
  }
  cli_assign(request, field + "." + pair.substr(0, equals), value);
  return true;
}

inline const CliVerb *cli_verb(const std::string &name)
{
  for (const CliVerb &verb : CLI_VERBS) {
    if (name == verb.name) {
      return &verb;
    }
  }
  return nullptr;
}

inline std::string cli_verb_list()
{
  std::string names;
  for (const CliVerb &verb : CLI_VERBS) {
    if (std::string(verb.name).find(' ') == std::string::npos) {
      names += (names.empty() ? "" : "|") + std::string(verb.name);
    }
  }
  return names;
}

/* The synopsis of one verb, in the order its fields are declared, wrapped so a
 * verb with many fields stays readable. */
inline std::string cli_synopsis(const CliVerb &verb)
{
  const std::string indent(10, ' ');
  std::string line = "  " + std::string(verb.name);
  std::string text;
  for (int i = 0; i < verb.field_count; i++) {
    const CliField &field = verb.fields[i];
    std::string item = field.flag[0] ? std::string(field.flag) : std::string();
    if (field.value[0]) {
      item += (item.empty() ? "" : " ") + std::string(field.value);
    }
    if (field.required) {
      item = field.position >= 0 ? "<" + item + ">" : item;
    }
    else {
      item = "[" + item + "]";
    }
    if (line.size() + item.size() + 1 > 78) {
      text += line + "\n";
      line = indent;
    }
    line += " " + item;
  }
  return text + line + "\n";
}

/* The one usage text, printed by `--help`; every line of it comes from the
 * generated table, so it cannot describe a flag the parser does not have. */
inline void cli_usage()
{
  std::string text =
      "blender-cli — one Blender process serving an agent over one channel.\n"
      "Every verb below is one request; it prints the events that request\n"
      "produced, folded into one document.\n\n";
  for (const CliVerb &verb : CLI_VERBS) {
    text += cli_synopsis(verb) + "      " + verb.doc + "\n";
  }
  text +=
      "\n  Common: --json prints one compact line instead of indented JSON.\n"
      "  Without a session, --file F is the scene the verb loads and --save [F]\n"
      "  writes it afterwards. A value written @FILE is read from that file and\n"
      "  - is read from stdin; @@ starts a literal value.\n"
      "  --version prints the upstream version and the fork tag.\n";
  fputs(text.c_str(), stdout);
}

inline const CliField *cli_flag(const CliVerb &verb, const std::string &flag)
{
  for (int i = 0; i < verb.field_count; i++) {
    if (flag == verb.fields[i].flag) {
      return &verb.fields[i];
    }
  }
  return nullptr;
}

/* The field at one positional index. An action-specific field wins over the
 * general one, so `session feedback k=v` and `session rollback ~1` share a slot. */
inline const CliField *cli_positional(const CliVerb &verb, int index, const std::string &action)
{
  const CliField *general = nullptr;
  for (int i = 0; i < verb.field_count; i++) {
    const CliField &field = verb.fields[i];
    if (field.position != index) {
      continue;
    }
    if (field.when[0]) {
      if (action == field.when) {
        return &field;
      }
    }
    else {
      general = &field;
    }
  }
  return general;
}

inline CommandLine cli_parse(const std::vector<std::string> &args)
{
  CommandLine parsed;
  auto fail = [&](const std::string &message) {
    parsed.error = message;
    return parsed;
  };
  /* The output format is decided before anything can go wrong with the rest,
   * so a rejected command line is reported in the format it asked for. */
  parsed.compact = std::find(args.begin(), args.end(), "--json") != args.end();
  if (args.empty()) {
    return fail("A verb is required: " + cli_verb_list());
  }
  const CliVerb *verb = cli_verb(args[0]);
  if (!verb) {
    return fail("Unknown verb: " + args[0]);
  }
  if (!verb->op[0]) {
    return fail(std::string(verb->name) + " is answered by the launcher, not by a request");
  }
  parsed.request["op"] = verb->op;
  std::vector<std::string> positional;
  for (size_t i = 1; i < args.size(); i++) {
    const std::string &arg = args[i];
    auto value = [&](const std::string &flag) -> std::string {
      if (i + 1 >= args.size()) {
        parsed.error = flag + " requires a value";
        return "";
      }
      return args[++i];
    };
    if (arg == "--json") {
      /* Read before the loop. */
    }
    else if (arg == "--save") {
      parsed.has_save = true;
      if (i + 1 < args.size() && !args[i + 1].starts_with("-")) {
        parsed.save = cli_absolute(args[++i]);
      }
    }
    else if (const CliField *field = cli_flag(*verb, arg)) {
      std::string text;
      switch (field->kind) {
        case CliKind::Flag:
          cli_assign(parsed.request, field->assign, true);
          break;
        case CliKind::NoFlag:
          cli_assign(parsed.request, field->assign, false);
          break;
        case CliKind::Path:
          cli_assign(parsed.request, field->assign, cli_absolute(value(arg)));
          break;
        case CliKind::List:
          cli_assign(parsed.request, field->assign, cli_split(value(arg)));
          break;
        case CliKind::Int:
        case CliKind::Num:
          try {
            text = value(arg);
            if (parsed.error.empty()) {
              cli_assign(parsed.request,
                         field->assign,
                         field->kind == CliKind::Int ? nlohmann::json(std::stoi(text)) :
                                                       nlohmann::json(std::stod(text)));
            }
          }
          catch (const std::exception &) {
            return fail(arg + " requires a number");
          }
          break;
        case CliKind::Text:
        case CliKind::Json: {
          text = value(arg);
          if (!parsed.error.empty()) {
            break;
          }
          std::string body;
          if (!cli_text(text, body, parsed.error)) {
            return parsed;
          }
          if (field->kind == CliKind::Text) {
            cli_assign(parsed.request, field->assign, body);
            break;
          }
          auto document = nlohmann::json::parse(body, nullptr, false);
          if (document.is_discarded()) {
            return fail(arg + " requires a JSON value");
          }
          cli_assign(parsed.request, field->assign, document);
          break;
        }
        case CliKind::Words: {
          auto &items = parsed.request[field->assign];
          if (!items.is_array()) {
            items = nlohmann::json::array();
          }
          while (i + 1 < args.size() && !args[i + 1].starts_with("--")) {
            items.push_back(args[++i]);
          }
          if (items.empty()) {
            return fail(arg + " requires at least one value");
          }
          break;
        }
        default:
          cli_assign(parsed.request, field->assign, value(arg));
          break;
      }
    }
    else if (arg == "--file") {
      parsed.load = cli_absolute(value(arg));
    }
    else if (arg.starts_with("-") && arg != "-") {
      return fail("Unknown option for " + std::string(verb->name) + ": " + arg);
    }
    else {
      positional.push_back(arg);
    }
    if (!parsed.error.empty()) {
      return parsed;
    }
  }
  for (size_t index = 0; index < positional.size();) {
    const std::string action = parsed.request.value("action", std::string());
    const CliField *field = cli_positional(*verb, int(index), action);
    if (!field) {
      return fail(index == 0 ? std::string(verb->name) +
                                   " takes no positional arguments: " + positional[index] :
                               std::string(verb->name) + " takes too many positional arguments");
    }
    if (field->kind == CliKind::Settings) {
      for (; index < positional.size(); index++) {
        if (!cli_setting(parsed.request, field->assign, positional[index])) {
          return fail(std::string(verb->name) + " " + action + " takes " + field->value +
                      " settings, not: " + positional[index]);
        }
      }
      break;
    }
    if (field->kind == CliKind::OnOff) {
      if (positional[index] != "on" && positional[index] != "off") {
        return fail(std::string(verb->name) + " " + action + " requires " + field->value);
      }
      cli_assign(parsed.request, field->assign, positional[index] == "on");
    }
    else if (field->kind == CliKind::Path) {
      cli_assign(parsed.request, field->assign, cli_absolute(positional[index]));
    }
    else {
      cli_assign(parsed.request, field->assign, positional[index]);
    }
    index++;
  }
  /* A field an action needs is missing when its slot was never filled. */
  const std::string action = parsed.request.value("action", std::string());
  for (int i = 0; i < verb->field_count; i++) {
    const CliField &field = verb->fields[i];
    if (field.when[0] && action == field.when && field.kind == CliKind::OnOff &&
        !parsed.request.contains(field.field))
    {
      return fail(std::string(verb->name) + " " + action + " requires " + field.value);
    }
  }
  if (parsed.has_save && parsed.save.empty()) {
    if (parsed.load.empty()) {
      return fail("--save requires a path or --file");
    }
    parsed.save = parsed.load;
  }
  return parsed;
}
}  // namespace blender::agent
