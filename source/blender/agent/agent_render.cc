/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#include "agent_render.hh"

#include <vector>

#include "BKE_context.hh"
#include "BKE_global.hh"
#include "BKE_layer.hh"
#include "BKE_lib_query.hh"
#include "BKE_main.hh"
#include "BLI_listbase.hh"
#include "BLI_utildefines.hh"
#include "DNA_scene_types.h"
#include "IMB_imbuf_types.hh"
#include "RE_pipeline.h"

namespace blender::agent {

struct RecalcState {
  ID *id;
  unsigned int recalc, before_undo, after_undo;
};

PyObject *preserve_recalc(bContext *C)
{
  /* Temporary ID linking/deletion tags *all* scenes, collections and materials.
   * Preserve the user's pending tags, rather than treating observation's bookkeeping
   * as an edit or clearing a real edit made before agent.observe() inside exec. */
  auto *saved = new std::vector<RecalcState>();
  auto save = [&](ID *id) {
    saved->push_back({id, id->recalc, id->recalc_up_to_undo_push, id->recalc_after_undo_push});
  };
  ID *id;
  FOREACH_MAIN_ID_BEGIN (CTX_data_main(C), id) {
    save(id);
    BKE_library_foreach_ID_link(
        CTX_data_main(C),
        id,
        [&](LibraryIDLinkCallbackData *data) {
          if ((data->cb_flag & IDWALK_CB_EMBEDDED) && *data->id_pointer) {
            save(*data->id_pointer);
          }
          return IDWALK_RET_NOP;
        },
        nullptr,
        IDWALK_READONLY);
  }
  FOREACH_MAIN_ID_END;
  PyObject *capsule = PyCapsule_New(saved, "agent.recalc", [](PyObject *capsule) {
    auto *saved = static_cast<std::vector<RecalcState> *>(
        PyCapsule_GetPointer(capsule, "agent.recalc"));
    /* Deletion defers view-layer synchronization; finish it before restoring tags,
     * otherwise the next memfile writer would perform it and see a false edit. */
    BKE_main_view_layers_synced_ensure(static_cast<Main *>(PyCapsule_GetContext(capsule)));
    for (const RecalcState &state : *saved) {
      state.id->recalc = state.recalc;
      state.id->recalc_up_to_undo_push = state.before_undo;
      state.id->recalc_after_undo_push = state.after_undo;
    }
    delete saved;
  });
  PyCapsule_SetContext(capsule, CTX_data_main(C));
  return capsule;
}

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
  for (ViewLayer &layer : scene->view_layers) {
    BKE_view_layer_synced_ensure(*bmain, scene, &layer);
  }
  /* The full engine path owns lazy WM_init_gpu_offscreen and GPU context enable/disable.
   * Unlike the render operator, this does not create a Render Result Image in Main,
   * flush unrelated edit meshes, pause viewports or update the user's scene frame. */
  Render *re = RE_NewSceneRender(scene);
  G.is_break = false;
  Py_BEGIN_ALLOW_THREADS RE_RenderFrame(
      re, bmain, scene, nullptr, nullptr, scene->r.cfra, scene->r.subframe, false);
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
