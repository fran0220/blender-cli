/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

#include <cstdio>

namespace blender::agent {
void command_register();
/* Registered by a live session. Null filepath/output arguments query the other half. */
extern void (*crashlog_callback)(const char **filepath, FILE *output);
}  // namespace blender::agent
