/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

#include "agent_commands.hh"

namespace blender::agent {
CommandJSON production_execute(bContext *C, const CommandJSON &request);
}
