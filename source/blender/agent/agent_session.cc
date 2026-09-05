/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#include "agent_session.hh"
#include "agent_transport.hh"

#include <filesystem>
#include <unordered_map>

#include "BKE_blender_undo.hh"
#include "BKE_context.hh"
#include "BKE_global.hh"
#include "BKE_main.hh"
#include "BKE_undo_system.hh"
#include "BKE_wm_runtime.hh"
#include "BLI_timer.hh"
#include "BLO_undofile.hh"
#include "BPY_extern.hh"
#include "DNA_windowmanager_types.h"
#include "ED_util.hh"

namespace blender::agent {

struct Session {
  bContext *context;
  Transport *transport = nullptr;
  std::unordered_map<std::string, MemFileUndoData *> snapshots;
  std::deque<std::string> order;
  size_t bytes = 0;
  static constexpr size_t budget = 256 * 1024 * 1024;

  void init_undo(bool reset)
  {
    auto *wm = CTX_wm_manager(context);
    auto *&stack = wm->runtime->undo_stack;
    if (!stack) {
      stack = BKE_undosys_stack_create();
    }
    else if (reset) {
      BKE_undosys_stack_clear(stack);
    }
    if (!stack->step_active) {
      BKE_undosys_stack_init_from_main(stack, CTX_data_main(context));
      BKE_undosys_stack_init_from_context(stack, context);
    }
  }

  ~Session()
  {
    for (auto &[id, data] : snapshots) {
      BKE_memfile_undo_free(data);
    }
  }
};

static Session &session(PyObject *self)
{
  return *static_cast<Session *>(PyCapsule_GetPointer(self, "agent.session"));
}

static PyObject *snapshot_create(PyObject *self, PyObject *)
{
  Session &state = session(self);
  state.init_undo(false);
  ED_editors_flush_edits(CTX_data_main(state.context));
  /* Independent memfiles retain every branch without sharing chunk ownership with the UI stack. */
  auto *data = BKE_memfile_undo_encode(CTX_data_main(state.context), nullptr);
  PyObject *hashlib = PyImport_ImportModule("hashlib");
  PyObject *hash = hashlib ? PyObject_CallMethod(hashlib, "sha256", nullptr) : nullptr;
  Py_XDECREF(hashlib);
  if (!hash) {
    BKE_memfile_undo_free(data);
    return nullptr;
  }
  for (const MemFileChunk &chunk : data->memfile.chunks) {
    PyObject *buffer = PyMemoryView_FromMemory(
        const_cast<char *>(chunk.buf), Py_ssize_t(chunk.size), PyBUF_READ);
    PyObject *updated = buffer ? PyObject_CallMethod(hash, "update", "O", buffer) : nullptr;
    Py_XDECREF(buffer);
    if (!updated) {
      Py_DECREF(hash);
      BKE_memfile_undo_free(data);
      return nullptr;
    }
    Py_DECREF(updated);
  }
  PyObject *digest = PyObject_CallMethod(hash, "hexdigest", nullptr);
  Py_DECREF(hash);
  if (!digest) {
    BKE_memfile_undo_free(data);
    return nullptr;
  }
  std::string id = "sha256:" + std::string(PyUnicode_AsUTF8(digest));
  Py_DECREF(digest);
  if (data->undo_size > Session::budget) {
    BKE_memfile_undo_free(data);
    return PyErr_Format(PyExc_MemoryError, "Snapshot exceeds the 256 MiB undo-accounting budget");
  }
  if (state.snapshots.contains(id)) {
    BKE_memfile_undo_free(data);
  }
  else {
    while (state.bytes + data->undo_size > Session::budget) {
      auto oldest = state.order.front();
      state.order.pop_front();
      auto *old = state.snapshots.at(oldest);
      state.bytes -= old->undo_size;
      BKE_memfile_undo_free(old);
      state.snapshots.erase(oldest);
    }
    state.bytes += data->undo_size;
    state.snapshots.emplace(id, data);
    state.order.push_back(id);
  }
  auto *stack = CTX_wm_manager(state.context)->runtime->undo_stack;
  BKE_undosys_step_push_with_type(
      stack, state.context, "Agent snapshot", UndoEncodeHints::None, BKE_UNDOSYS_TYPE_MEMFILE);
  BKE_undosys_stack_limit_steps_and_memory(stack, 2, 32 * 1024 * 1024);
  return PyUnicode_FromString(id.c_str());
}

static PyObject *snapshot_restore(PyObject *self, PyObject *arg)
{
  Session &state = session(self);
  const char *id = PyUnicode_AsUTF8(arg);
  if (!id) {
    return nullptr;
  }
  auto found = state.snapshots.find(id);
  if (found == state.snapshots.end()) {
    return PyErr_Format(PyExc_KeyError, "Unknown or evicted snapshot: %s", id);
  }
  ED_editors_exit(CTX_data_main(state.context), false);
  if (!BKE_memfile_undo_decode(found->second, STEP_UNDO, false, state.context)) {
    return PyErr_Format(PyExc_RuntimeError, "Memfile rollback failed");
  }
  ED_editors_init_for_undo(CTX_data_main(state.context));
  BPY_context_set(state.context);
  state.init_undo(true);
  Py_RETURN_NONE;
}

static PyObject *cancelled(PyObject *self, PyObject *)
{
  auto *transport = session(self).transport;
  /* G.is_break is a plain bool, not atomic. Only the main thread may write it. */
  G.is_break = transport && transport->cancelled.load();
  return PyBool_FromLong(G.is_break);
}

static PyMethodDef methods[] = {
    {"snapshot", snapshot_create, METH_NOARGS, nullptr},
    {"rollback", snapshot_restore, METH_O, nullptr},
    {"cancelled", cancelled, METH_NOARGS, nullptr},
};

int session_serve(
    bContext *C, PyObject *arguments, PyObject *snapshot, PyObject *fields, PyObject *module)
{
  Session state{C};
  PyObject *capsule = PyCapsule_New(&state, "agent.session", nullptr);
  PyObject *native = PyDict_New();
  for (auto &method : methods) {
    PyObject *function = PyCFunction_New(&method, capsule);
    PyDict_SetItemString(native, method.ml_name, function);
    Py_DECREF(function);
  }
  PyObject *runtime = PyObject_CallMethod(
      module, "Session", "OOOO", arguments, snapshot, fields, native);
  Py_DECREF(native);
  Py_DECREF(capsule);
  if (!runtime) {
    return 1;
  }
  const auto directory = std::filesystem::current_path() / ".blender-cli";
  const auto path = directory / "session.sock";
  int status = 0;
  try {
    Transport transport(path.string());
    state.transport = &transport;
    bool closing = false;
    while (!closing) {
      Transport::Request request;
      if (transport.next(request)) {
        G.is_break = false;
        const std::string message = request.message.dump();
        PyObject *answer = PyObject_CallMethod(runtime, "dispatch", "s", message.c_str());
        nlohmann::json result;
        if (answer) {
          result = nlohmann::json::parse(PyUnicode_AsUTF8(answer));
          Py_DECREF(answer);
        }
        else {
          PyErr_Print();
          result = {{"ok", false}, {"error", {{"type", "InternalError"}}}};
        }
        PyObject *closed = PyObject_GetAttrString(runtime, "closing");
        closing = closed && PyObject_IsTrue(closed);
        Py_XDECREF(closed);
        BLI_timer_execute();
        transport.answer(request, result);
      }
      else {
        BLI_timer_execute();
      }
    }
    state.transport = nullptr;
  }
  catch (const std::exception &error) {
    fprintf(stderr, "Session: %s\n", error.what());
    status = 1;
  }
  PyObject *agent = PyImport_ImportModule("agent");
  if (agent) {
    PyObject_SetAttrString(agent, "_session", Py_None);
    Py_DECREF(agent);
  }
  Py_DECREF(runtime);
  std::filesystem::remove(path);
  std::filesystem::remove(directory / "session.pid");
  return status;
}
}  // namespace blender::agent
