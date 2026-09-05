/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

#include <Python.h>

namespace blender {
struct bContext;
namespace agent {
void crashlog_python_context(bool capture);
int session_serve(
    bContext *C, PyObject *arguments, PyObject *snapshot, PyObject *fields, PyObject *module);
}  // namespace agent
}  // namespace blender
