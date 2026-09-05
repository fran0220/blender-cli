/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

#include <Python.h>

#include <cstdio>
#include <string>

#include "agent_events.hh"

namespace blender {
struct bContext;
namespace agent {
void crashlog_python_context(bool capture);

/* The Python callable that writes one event the moment it is produced. */
PyObject *event_emitter(EventSink &sink);

/* Serve requests until `session close` or, on stdio, end of input. `native`
 * carries the request-boundary methods; the session adds its own. */
int session_serve(bContext *C,
                  PyObject *module,
                  PyObject *native,
                  const std::string &file,
                  bool stdio,
                  FILE *output);
}  // namespace agent
}  // namespace blender
