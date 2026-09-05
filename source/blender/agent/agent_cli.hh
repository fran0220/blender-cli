/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

#include <json.hpp>

namespace blender::agent {

/* Every CLI verb is one request; its flags are that request's fields. This is
 * the only place that mapping exists, so the launcher (talking to a session)
 * and the in-process one-shot verb build byte-identical requests. Field names
 * and types are validated once, by the request table in `agent_runtime.py`.
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

/* The one usage text, printed by `--help`. */
inline void cli_usage()
{
  puts(
      "Usage: blender-cli <repl|exec|inspect|observe|describe|session|target|program|fit>\n"
      "  repl [--file F] [--standalone]   one pipe of JSON-line requests and events\n"
      "  exec -c CODE | FILE.py [--no-record] [--timeout S] [--image delta|full|off]\n"
      "  inspect [--object NAME] [--full] [--select PATH ...]\n"
      "  observe [--views front,persp] [--passes color,wire,silhouette,normal,depth]\n"
      "          [--size 512|768|1024] [--frame OBJECT] [--ref IMG] [--overlay]\n"
      "          [--layout sheet|separate] [--out PATH | --inline]\n"
      "  describe RNA_PATH | channel | --schema\n"
      "  session open|status|feedback|save|close|snapshot|rollback|history\n"
      "          [--label L] [--file F] [--json-file F | KEY=VALUE ...]\n"
      "  target set NAME --ref IMG [--view V] [--mask auto|none] [--fit bbox|none]\n"
      "         [--metrics iou,chamfer,ssim,hist] | target list | target clear [NAME]\n"
      "  program get|set|patch|run|history|rollback|record [--text T] [--old O] [--new N]\n"
      "          [--label L] [--version V] [--from-step N]\n"
      "  fit --params JSON [--objective JSON] [--budget JSON] [--method M]\n"
      "  Common: --file F --save [F] --json\n"
      "  --version: upstream version and fork tag");
}

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

/* KEY=VALUE for `session feedback`; the value is JSON when it parses as JSON. */
inline bool cli_setting(nlohmann::json &request, const std::string &pair)
{
  const auto equals = pair.find('=');
  if (equals == std::string::npos || equals == 0) {
    return false;
  }
  auto value = nlohmann::json::parse(pair.substr(equals + 1), nullptr, false);
  if (value.is_discarded()) {
    value = pair.substr(equals + 1);
  }
  cli_assign(request, "feedback." + pair.substr(0, equals), value);
  return true;
}

inline CommandLine cli_parse(const std::vector<std::string> &args)
{
  CommandLine parsed;
  auto fail = [&](const std::string &message) {
    parsed.error = message;
    return parsed;
  };
  if (args.empty()) {
    return fail(
        "A verb is required: exec|inspect|observe|describe|session|target|program|fit|repl");
  }
  const std::string op = args[0];
  static const std::vector<std::string> ops = {
      "exec", "inspect", "observe", "describe", "session", "target", "program", "fit"};
  if (std::find(ops.begin(), ops.end(), op) == ops.end()) {
    return fail("Unknown verb: " + op);
  }
  parsed.request["op"] = op;
  std::vector<std::string> positional;
  for (size_t i = 1; i < args.size(); i++) {
    const std::string &arg = args[i];
    auto value = [&](const char *flag) -> std::string {
      if (i + 1 >= args.size()) {
        parsed.error = std::string(flag) + " requires a value";
        return "";
      }
      return args[++i];
    };
    if (arg == "--json") {
      parsed.compact = true;
    }
    else if (arg == "--save") {
      parsed.has_save = true;
      if (i + 1 < args.size() && !args[i + 1].starts_with("-")) {
        parsed.save = cli_absolute(args[++i]);
      }
    }
    else if (arg == "--file" && op != "session") {
      parsed.load = cli_absolute(value("--file"));
    }
    else if (arg == "--file") {
      parsed.request["file"] = cli_absolute(value("--file"));
    }
    else if (op == "exec" && arg == "-c") {
      parsed.request["code"] = value("-c");
    }
    else if (op == "exec" && arg == "--timeout") {
      const std::string text = value("--timeout");
      try {
        parsed.request["timeout"] = std::stod(text);
      }
      catch (const std::exception &) {
        return fail("--timeout requires a number");
      }
    }
    else if (op == "exec" && arg == "--no-record") {
      parsed.request["record"] = false;
    }
    else if (op == "exec" && arg == "--image") {
      /* A per-request `feedback` is an image policy, so the key is its `mode`. */
      cli_assign(parsed.request, "feedback.mode", value("--image"));
    }
    else if (op == "inspect" && arg == "--object") {
      parsed.request["object"] = value("--object");
    }
    else if (op == "inspect" && arg == "--full") {
      parsed.request["full"] = true;
    }
    else if (op == "inspect" && arg == "--select") {
      auto &select = parsed.request["select"];
      if (!select.is_array()) {
        select = nlohmann::json::array();
      }
      while (i + 1 < args.size() && !args[i + 1].starts_with("--")) {
        select.push_back(args[++i]);
      }
      if (select.empty()) {
        return fail("--select requires at least one RNA path");
      }
    }
    else if (op == "observe" && (arg == "--views" || arg == "--passes")) {
      parsed.request[arg.substr(2)] = cli_split(value(arg.c_str()));
    }
    else if (op == "observe" && arg == "--size") {
      try {
        parsed.request["size"] = std::stoi(value("--size"));
      }
      catch (const std::exception &) {
        return fail("--size requires an integer");
      }
    }
    else if (op == "observe" &&
             (arg == "--ref" || arg == "--layout" || arg == "--frame" || arg == "--out"))
    {
      parsed.request[arg.substr(2)] = value(arg.c_str());
    }
    else if (op == "observe" && (arg == "--overlay" || arg == "--inline")) {
      parsed.request[arg.substr(2)] = true;
    }
    else if (op == "describe" && arg == "--schema") {
      parsed.request["path"] = "schema";
    }
    else if (op == "session" && arg == "--label") {
      parsed.request["label"] = value("--label");
    }
    else if (op == "session" && arg == "--json-file") {
      std::ifstream stream(value("--json-file"));
      auto policy = nlohmann::json::parse(stream, nullptr, false);
      if (!policy.is_object()) {
        return fail("--json-file must contain a JSON object");
      }
      parsed.request["feedback"] = policy;
    }
    else if (op == "target" &&
             (arg == "--ref" || arg == "--view" || arg == "--mask" || arg == "--fit"))
    {
      parsed.request[arg.substr(2)] = value(arg.c_str());
    }
    else if (op == "target" && arg == "--metrics") {
      parsed.request["metrics"] = cli_split(value("--metrics"));
    }
    else if (op == "program" && (arg == "--text" || arg == "--old" || arg == "--new" ||
                                 arg == "--label" || arg == "--version"))
    {
      parsed.request[arg.substr(2)] = value(arg.c_str());
    }
    else if (op == "program" && arg == "--from-step") {
      try {
        parsed.request["from_step"] = std::stoi(value("--from-step"));
      }
      catch (const std::exception &) {
        return fail("--from-step requires an integer");
      }
    }
    else if (op == "fit" && arg == "--method") {
      parsed.request["method"] = value("--method");
    }
    else if (op == "fit" && (arg == "--params" || arg == "--objective" || arg == "--budget")) {
      auto body = nlohmann::json::parse(value(arg.c_str()), nullptr, false);
      if (body.is_discarded()) {
        return fail(arg + " requires a JSON value");
      }
      parsed.request[arg.substr(2)] = body;
    }
    else if (arg.starts_with("-") && arg != "-") {
      return fail("Unknown option for " + op + ": " + arg);
    }
    else {
      positional.push_back(arg);
    }
    if (!parsed.error.empty()) {
      return parsed;
    }
  }
  auto take = [&](size_t index, const char *field) {
    if (positional.size() > index) {
      parsed.request[field] = positional[index];
    }
  };
  if (op == "exec") {
    if (!positional.empty()) {
      parsed.request["script"] = cli_absolute(positional[0]);
    }
  }
  else if (op == "describe") {
    take(0, "path");
  }
  else if (op == "session") {
    take(0, "action");
    if (parsed.request.value("action", std::string()) == "feedback") {
      for (size_t i = 1; i < positional.size(); i++) {
        if (!cli_setting(parsed.request, positional[i])) {
          return fail("session feedback takes KEY=VALUE settings, not: " + positional[i]);
        }
      }
    }
    else {
      take(1, "snapshot");
    }
  }
  else if (op == "target") {
    take(0, "action");
    take(1, "name");
  }
  else if (op == "program") {
    take(0, "action");
    if (parsed.request.value("action", std::string()) == "record") {
      if (positional.size() < 2 || (positional[1] != "on" && positional[1] != "off")) {
        return fail("program record requires on or off");
      }
      parsed.request["on"] = positional[1] == "on";
    }
    else {
      take(1, "version");
    }
  }
  else if (!positional.empty()) {
    return fail(op + " takes no positional arguments: " + positional[0]);
  }
  /* `session feedback` consumes every remaining word as a KEY=VALUE setting. */
  const size_t allowed = op == "exec" || op == "describe" ? 1 : 2;
  if (parsed.request.value("action", std::string()) != "feedback" && positional.size() > allowed) {
    return fail(op + " takes too many positional arguments: " + positional[allowed]);
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
