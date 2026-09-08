/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

#include <Python.h>
#include <json.hpp>
#include <string>

#include "RNA_types.hh"

namespace blender {
struct bContext;
namespace agent {

using CommandJSON = nlohmann::json;

struct CommandProperty {
  PointerRNA pointer;
  PropertyRNA *property = nullptr;
  int index = -1;
};

CommandJSON command_execute(bContext *C, const CommandJSON &request);
PyObject *command_api(bContext *C);
CommandJSON command_operator(bContext *C,
                             const std::string &name,
                             const CommandJSON &properties = CommandJSON::object());
void command_select(bContext *C,
                    const CommandJSON &objects,
                    const std::string &active = "",
                    const std::string &mode = "OBJECT");
CommandProperty command_resolve(bContext *C, const std::string &path);
CommandJSON command_get(bContext *C, const CommandProperty &target);
void command_set(bContext *C, const CommandProperty &target, const CommandJSON &value);

}  // namespace agent
}  // namespace blender
