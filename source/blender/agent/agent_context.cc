/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#include "agent_context.hh"
#include "agent_render.hh"

#include "BKE_context.hh"
#include "BKE_main.hh"
#include "BKE_screen.hh"
#include "BKE_workspace.hh"
#include "BLI_listbase.hh"
#include "DNA_scene_types.h"
#include "DNA_screen_types.h"
#include "DNA_windowmanager_types.h"
#include "ED_screen.hh"
#include "ED_util.hh"
#include "WM_api.hh"
#include "WM_types.hh"
#include "wm_window.hh"

namespace blender::agent {

void context_ensure(bContext *C)
{
  Main *bmain = CTX_data_main(C);
  wmWindowManager *wm = CTX_wm_manager(C);
  wmWindow *win = wm->windows.first();
  if (!win) {
    /* Unlike WM_window_open, this only allocates data, never a GHOST window. */
    win = wm_window_new(bmain, wm, nullptr, false);
    win->scene = CTX_data_scene(C);
    win->sizex = win->sizey = 1024;
  }
  WorkSpace *workspace = BKE_workspace_active_get(win->workspace_hook);
  if (!workspace) {
    workspace = BKE_workspace_add(bmain, "Agent");
    BKE_workspace_active_set(win->workspace_hook, workspace);
  }
  bScreen *screen = BKE_workspace_active_layout_get(win->workspace_hook) ?
                        WM_window_get_active_screen(win) :
                        nullptr;
  ScrArea *area = nullptr;
  if (screen) {
    for (ScrArea &candidate : screen->areabase) {
      if (candidate.spacetype == SPACE_VIEW3D) {
        area = &candidate;
        break;
      }
    }
  }
  if (!area) {
    WorkSpaceLayout *layout = ED_workspace_layout_add(bmain, workspace, win, "Agent");
    WM_window_set_active_layout(win, workspace, layout);
    screen = BKE_workspace_layout_screen_get(layout);
    area = screen->areabase.first();
    SpaceType *type = BKE_spacetype_from_id(SPACE_VIEW3D);
    SpaceLink *space = type->create(area, win->scene);
    area->spacetype = SPACE_VIEW3D;
    area->type = type;
    BLI_addhead(&area->spacedata, space);
    area->regionbase = space->regionbase;
    space->regionbase.clear_no_delete();
  }
  /* Never cache these pointers across a file load or memfile decode. */
  CTX_wm_window_set(C, win);
  if (!screen->context) {
    /* Background refresh only installs screen context callbacks, without drawing.
     * Without it, selected_objects is empty even after object.select_all succeeds. */
    ED_screen_refresh(C, wm, win);
  }
  ED_area_and_region_types_init(area);
  CTX_wm_area_set(C, area);
  CTX_wm_region_set(C, BKE_area_find_region_type(area, RGN_TYPE_WINDOW));
}

static bContext *context(PyObject *self)
{
  return static_cast<bContext *>(PyCapsule_GetPointer(self, "agent.context"));
}

static PyObject *ensure(PyObject *self, PyObject *)
{
  context_ensure(context(self));
  Py_RETURN_NONE;
}

static PyObject *flush(PyObject *self, PyObject *)
{
  ED_editors_flush_edits(CTX_data_main(context(self)));
  Py_RETURN_NONE;
}

static PyObject *render_scene(PyObject *self, PyObject *name)
{
  return render(context(self), name);
}

static PyObject *recalc_guard(PyObject *self, PyObject *)
{
  return preserve_recalc(context(self));
}

static bool viewport_required(bContext *C)
{
  CTX_wm_operator_poll_msg_set(
      C,
      "GPU viewport selection is unavailable in the agent's undrawn context; "
      "select mesh elements with bpy or bmesh instead");
  return false;
}

PyObject *native_api(bContext *C)
{
  /* These screen-coordinate selection operators assume a drawn GPU selection buffer.
   * Change their poll, not bpy's interface or the operator execution implementation. */
  for (const char *name : {"VIEW3D_OT_select",
                           "VIEW3D_OT_select_box",
                           "VIEW3D_OT_select_circle",
                           "VIEW3D_OT_select_lasso"})
  {
    WM_operatortype_find(name, false)->poll = viewport_required;
  }
  static PyMethodDef methods[] = {
      {"context", ensure, METH_NOARGS, nullptr},
      {"flush", flush, METH_NOARGS, nullptr},
      {"render", render_scene, METH_O, nullptr},
      {"preserve_recalc", recalc_guard, METH_NOARGS, nullptr},
  };
  PyObject *capsule = PyCapsule_New(C, "agent.context", nullptr);
  PyObject *result = PyDict_New();
  for (auto &method : methods) {
    PyObject *function = PyCFunction_New(&method, capsule);
    PyDict_SetItemString(result, method.ml_name, function);
    Py_DECREF(function);
  }
  Py_DECREF(capsule);
  return result;
}
}  // namespace blender::agent
