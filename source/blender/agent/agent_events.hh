/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

#include <string>
#include <vector>

#include <json.hpp>

namespace blender::agent {

/* Where an event goes the moment Python produces it. */
struct EventSink {
  virtual ~EventSink() = default;
  virtual void event(const std::string &line) = 0;
};

/* One-shot verbs answer with the folded envelope, so they keep the events. */
struct CollectingSink : public EventSink {
  std::vector<nlohmann::json> events;
  void event(const std::string &line) override
  {
    events.push_back(nlohmann::json::parse(line, nullptr, false));
  }
};

/* The single derivation of the folded envelope from an event stream. Both the
 * in-process one-shot verb and the launcher's session client use it, so the
 * envelope has no fields of its own. */
inline nlohmann::json fold(const std::vector<nlohmann::json> &events)
{
  nlohmann::json envelope = nlohmann::json::object();
  nlohmann::json images = nlohmann::json::array();
  std::string out, err;
  bool terminated = false;
  for (const auto &event : events) {
    if (!event.is_object()) {
      continue;
    }
    const std::string kind = event.value("event", std::string());
    nlohmann::json body = event;
    body.erase("id");
    body.erase("event");
    if (kind == "log") {
      (event.value("stream", std::string("stdout")) == "stderr" ? err : out) += event.value(
          "text", std::string());
    }
    else if (kind == "value") {
      envelope["value"] = event["value"];
    }
    else if (kind == "diff" || kind == "perception" || kind == "objective") {
      envelope[kind] = body;
    }
    else if (kind == "image") {
      images.push_back(body);
    }
    else if (kind == "done") {
      envelope.update(body);
      terminated = true;
    }
    else if (kind == "error") {
      body.erase("ok");
      envelope["ok"] = false;
      if (body.contains("autosave")) {
        envelope["autosave"] = body["autosave"];
        body.erase("autosave");
      }
      envelope["error"] = body;
      terminated = true;
    }
    /* `progress` is transient by definition: the folded envelope carries the
     * result it converged to, not the search's intermediate reports. */
  }
  if (!images.empty()) {
    envelope["images"] = images;
  }
  if (!out.empty()) {
    envelope["stdout"] = out;
  }
  if (!err.empty()) {
    envelope["stderr"] = err;
  }
  if (!terminated) {
    envelope["ok"] = false;
    envelope["error"] = {{"type", "ProtocolError"},
                         {"message", "The event stream ended without done or error"}};
  }
  return envelope;
}

/* Exit status of a verb that printed this envelope. */
inline int envelope_status(const nlohmann::json &envelope)
{
  return envelope.value("ok", true) ? 0 : 1;
}
}  // namespace blender::agent
