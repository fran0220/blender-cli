/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

#include <Python.h>

namespace blender {
struct bContext;
namespace agent {
PyObject *render(bContext *C, PyObject *scene_name);
PyObject *preserve_recalc(bContext *C);
}  // namespace agent
}  // namespace blender
