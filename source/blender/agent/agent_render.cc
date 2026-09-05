/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#include "agent_render.hh"

#include "BKE_context.hh"
#include "BKE_global.hh"
#include "BKE_main.hh"
#include "BLI_string.h"
#include "DNA_scene_types.h"
#include "IMB_imbuf_types.hh"
#include "RE_pipeline.h"

namespace blender::agent {

PyObject *render(bContext *C, PyObject *scene_name)
{
  const char *name = PyUnicode_AsUTF8(scene_name);
  if (!name) {
    return nullptr;
  }
  Main *bmain = CTX_data_main(C);
  Scene *scene = nullptr;
  for (Scene &candidate : bmain->scenes) {
    if (STREQ(candidate.id.name + 2, name)) {
      scene = &candidate;
      break;
    }
  }
  if (!scene) {
    return PyErr_Format(PyExc_KeyError, "No observation scene: %s", name);
  }
  /* The full engine path owns lazy WM_init_gpu_offscreen and GPU context enable/disable.
   * Unlike the render operator, this does not create a Render Result Image in Main,
   * flush unrelated edit meshes, pause viewports or update the user's scene frame. */
  Render *re = RE_NewSceneRender(scene);
  G.is_break = false;
  Py_BEGIN_ALLOW_THREADS RE_RenderFrame(
      re, bmain, scene, nullptr, nullptr, scene->r.cfra, 0.0f, false);
  Py_END_ALLOW_THREADS RenderResult *rr = RE_AcquireResultRead(re);
  PyObject *result = nullptr;
  if (rr && !rr->layers.is_empty() && !G.is_break && !rr->error) {
    result = PyDict_New();
    for (RenderPass &pass : rr->layers.first()->passes) {
      if (pass.ibuf && pass.ibuf->float_buffer.data) {
        const Py_ssize_t length = Py_ssize_t(pass.rectx) * pass.recty * pass.channels *
                                  sizeof(float);
        PyObject *buffer = PyBytes_FromStringAndSize(
            reinterpret_cast<const char *>(pass.ibuf->float_buffer.data), length);
        PyDict_SetItemString(result, pass.name, buffer);
        Py_DECREF(buffer);
      }
    }
  }
  else {
    PyErr_Format(PyExc_RuntimeError,
                 "Offscreen EEVEE render failed: %s",
                 rr && rr->error ? rr->error : "no render result or render cancelled");
  }
  RE_ReleaseResult(re);
  RE_FreeRender(re);
  return result;
}
}  // namespace blender::agent
