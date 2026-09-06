/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#include "agent_session.hh"
#include "AGENT_command.hh"
#include "agent_context.hh"
#include "agent_transport.hh"

#include <filesystem>
#include <frameobject.h>
#include <fstream>
#include <unordered_map>

#include "BKE_blender_undo.hh"
#include "BKE_context.hh"
#include "BKE_global.hh"
#include "BKE_lib_id.hh"
#include "BKE_main.hh"
#include "BKE_undo_system.hh"
#include "BKE_wm_runtime.hh"
#include "BLI_fileops.hh"
#include "BLI_listbase.hh"
#include "BLI_string.hh"
#include "BLI_string_utf8.hh"
#include "BLI_timer.hh"
#include "BLO_readfile.hh"
#include "BLO_undofile.hh"
#include "BLO_writefile.hh"
#include "BPY_extern.hh"
#include "DNA_windowmanager_types.h"
#include "ED_util.hh"

#ifdef _WIN32
#  include <process.h>
#else
#  include <unistd.h>
#endif

namespace blender::agent {

void (*crashlog_callback)(const char **filepath, FILE *output) = nullptr;
static std::string crashlog_path, crashlog_request, crashlog_python;

static void session_crashlog(const char **filepath, FILE *output)
{
  if (filepath) {
    *filepath = crashlog_path.c_str();
  }
  if (output) {
    fprintf(output, "\n# Agent request\n%s\n", crashlog_request.c_str());
    if (!crashlog_python.empty()) {
      fprintf(output,
              "\n# Python backtrace (captured before releasing the GIL for rendering)\n%s",
              crashlog_python.c_str());
    }
  }
}

void crashlog_python_context(bool capture)
{
  crashlog_python.clear();
  if (!capture || !crashlog_callback) {
    return;
  }
  /* BPY_python_backtrace cannot find a detached PyThreadState during RE_RenderFrame.
   * Prepare text under the GIL, so the crash callback never needs to call Python. */
  PyFrameObject *frame = PyEval_GetFrame();
  Py_XINCREF(frame);
  while (frame) {
    PyCodeObject *code = PyFrame_GetCode(frame);
    crashlog_python += "  File \"" + std::string(PyUnicode_AsUTF8(code->co_filename)) +
                       "\", line " + std::to_string(PyFrame_GetLineNumber(frame)) + " in " +
                       PyUnicode_AsUTF8(code->co_name) + "\n";
    Py_DECREF(code);
    PyFrameObject *next = PyFrame_GetBack(frame);
    Py_DECREF(frame);
    frame = next;
  }
}

struct Session {
  bContext *context;
  Channel *channel = nullptr;
  std::unordered_map<std::string, MemFileUndoData *> snapshots;
  std::unordered_map<std::string, bool> snapshot_dirty;
  std::deque<std::string> order;
  size_t bytes = 0;
  static constexpr size_t budget = 256 * 1024 * 1024;
  std::string current;
  std::filesystem::path autosave;
  bool dirty = false;
  using Clock = std::chrono::steady_clock;
  Clock::time_point last_write = Clock::now(), last_request = Clock::now();

  bool snapshot_write(const std::filesystem::path &path)
  {
    auto *snapshot = snapshots.at(current);
    /* Memfiles now retain shared arrays outside the chunk stream. Decode into an
     * isolated Main before normal serialization; never borrow IDs from live Main. */
    Main *empty = BKE_main_new();
    BlendFileReadParams read_params{};
    read_params.skip_flags = BLO_READ_SKIP_UNDO_OLD_MAIN;
    read_params.undo_direction = STEP_UNDO;
    BlendFileData *data = BLO_read_from_memfile(
        empty, snapshot->filepath, &snapshot->memfile, &read_params, nullptr);
    BKE_main_free(empty);
    bool success = false;
    if (data && !data->main->is_read_invalid) {
      BKE_main_id_refcount_recompute(data->main, false);
      if (data->curscene) {
        BLI_remlink(&data->main->scenes, data->curscene);
        BLI_addhead(&data->main->scenes, data->curscene);
      }
      BlendFileWriteParams write_params{};
      /* Recovery is also opened as an ordinary --file, not just WM's recovery operator. */
      write_params.remap_mode = BLO_WRITE_PATH_REMAP_ABSOLUTE;
      success = BLO_write_file(data->main,
                               path.string().c_str(),
                               G.fileflags | G_FILE_RECOVER_WRITE | G_FILE_COMPRESS,
                               &write_params,
                               nullptr);
    }
    if (data) {
      BLO_blendfiledata_free(data);
    }
    if (success) {
      const auto metadata = path.parent_path() / (path.stem().string() + ".json");
      const auto temporary = metadata.string() + "@";
      std::ofstream stream(temporary);
      stream << nlohmann::json(
                    {{"filepath", snapshot->filepath}, {"dirty", snapshot_dirty.at(current)}})
                    .dump();
      stream.close();
      if (stream) {
        success = BLI_rename_overwrite(temporary.c_str(), metadata.string().c_str()) == 0;
      }
      else {
        success = false;
      }
    }
    return success;
  }

  void autosave_write()
  {
    const auto start = Clock::now();
    const bool success = snapshot_write(autosave);
    last_write = Clock::now();
    dirty = !success;
    fprintf(stderr,
            "Agent autosave: %s %.3f ms\n",
            success ? "written" : "failed",
            std::chrono::duration<double, std::milli>(last_write - start).count());
    fflush(stderr);
  }

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
  const bool dirty = !CTX_wm_manager(state.context)->file_saved;
  state.init_undo(false);
  ED_editors_flush_edits(CTX_data_main(state.context));
  /* Independent memfiles retain every branch without sharing chunk ownership with the UI stack. */
  auto *data = BKE_memfile_undo_encode(CTX_data_main(state.context), nullptr);
  /* Upstream only fills this field for disk undo. Agent memfiles retain their source base path. */
  STRNCPY(data->filepath, BKE_main_blendfile_path(CTX_data_main(state.context)));
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
      state.snapshot_dirty.erase(oldest);
    }
    state.bytes += data->undo_size;
    state.snapshots.emplace(id, data);
    state.order.push_back(id);
  }
  state.snapshot_dirty[id] = dirty;
  auto *stack = CTX_wm_manager(state.context)->runtime->undo_stack;
  BKE_undosys_step_push_with_type(
      stack, state.context, "Agent snapshot", UndoEncodeHints::None, BKE_UNDOSYS_TYPE_MEMFILE);
  BKE_undosys_stack_limit_steps_and_memory(stack, 2, 32 * 1024 * 1024);
  state.current = id;
  state.dirty = true;
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
  context_ensure(state.context);
  BPY_context_set(state.context);
  state.init_undo(true);
  STRNCPY(CTX_data_main(state.context)->filepath, found->second->filepath);
  CTX_wm_manager(state.context)->file_saved = !state.snapshot_dirty.at(id);
  state.current = id;
  state.dirty = true;
  Py_RETURN_NONE;
}

static PyObject *cancelled(PyObject *self, PyObject *)
{
  auto *channel = session(self).channel;
  /* G.is_break is a plain bool, not atomic. Only the main thread may write it. */
  G.is_break = channel && channel->cancelled.load();
  return PyBool_FromLong(G.is_break);
}

static PyObject *restore_metadata(PyObject *self, PyObject *args)
{
  const char *filepath;
  int dirty;
  if (!PyArg_ParseTuple(args, "sp", &filepath, &dirty)) {
    return nullptr;
  }
  Session &state = session(self);
  STRNCPY(CTX_data_main(state.context)->filepath, filepath);
  CTX_wm_manager(state.context)->file_saved = !dirty;
  context_ensure(state.context);
  BPY_context_set(state.context);
  state.init_undo(true);
  Py_RETURN_NONE;
}

static PyObject *snapshot_persist(PyObject *self, PyObject *arg)
{
  const char *path = PyUnicode_AsUTF8(arg);
  if (!path) {
    return nullptr;
  }
  if (!session(self).snapshot_write(std::filesystem::path(path))) {
    return PyErr_Format(PyExc_OSError, "Could not persist snapshot: %s", path);
  }
  Py_RETURN_NONE;
}

static PyObject *request_source(PyObject *, PyObject *arg)
{
  const char *source = PyUnicode_AsUTF8(arg);
  if (!source) {
    return nullptr;
  }
  crashlog_request += "\n" + std::string(source);
  fprintf(stderr, "Agent source: %s\n", source);
  fflush(stderr);
  Py_RETURN_NONE;
}

static PyMethodDef methods[] = {
    {"snapshot", snapshot_create, METH_NOARGS, nullptr},
    {"rollback", snapshot_restore, METH_O, nullptr},
    {"cancelled", cancelled, METH_NOARGS, nullptr},
    {"restore_metadata", restore_metadata, METH_VARARGS, nullptr},
    {"persist", snapshot_persist, METH_O, nullptr},
    {"request_source", request_source, METH_O, nullptr},
};

/* One event line, written the moment Python produces it. */
struct ChannelSink : public EventSink {
  Channel &channel;
  const Channel::Request *request = nullptr;

  explicit ChannelSink(Channel &channel) : channel(channel) {}

  void event(const std::string &line) override
  {
    if (request) {
      channel.send(*request, line);
    }
  }
};

static PyObject *emit_event(PyObject *self, PyObject *arg)
{
  const char *line = PyUnicode_AsUTF8(arg);
  if (!line) {
    return nullptr;
  }
  static_cast<EventSink *>(PyCapsule_GetPointer(self, "agent.sink"))->event(line);
  Py_RETURN_NONE;
}

static PyMethodDef emit_method = {"emit", emit_event, METH_O, nullptr};

PyObject *event_emitter(EventSink &sink)
{
  PyObject *capsule = PyCapsule_New(&sink, "agent.sink", nullptr);
  PyObject *function = PyCFunction_New(&emit_method, capsule);
  Py_DECREF(capsule);
  return function;
}

/* The request log keeps one readable line per request; a long statement is
 * truncated to its first line so a crash dump still identifies it. */
static std::string request_summary(const nlohmann::json &message)
{
  auto summary = message;
  for (const char *key : {"code", "text", "old", "new"}) {
    if (summary.contains(key) && summary[key].is_string()) {
      const auto text = summary[key].get<std::string>();
      char first_line[513];
      BLI_strncpy_utf8(first_line, text.substr(0, text.find('\n')).c_str(), sizeof(first_line));
      summary[key] = first_line;
    }
  }
  return summary.dump();
}

int session_serve(bContext *C,
                  PyObject *module,
                  PyObject *native,
                  const std::string &file,
                  bool stdio,
                  FILE *output)
{
  Session state{C};
  const auto directory = std::filesystem::current_path() / ".blender-cli";
  const auto path = directory / "session.sock";
  std::filesystem::create_directories(directory);
  state.autosave = directory / ("autosave-" + std::to_string(getpid()) + ".blend");
  /* Where a dump would go if one were ever written. Upstream announces the
   * path when it writes one; a session that never crashes says nothing. */
  crashlog_path = (directory / ("session-" + std::to_string(getpid()) + ".crash.txt")).string();
  crashlog_callback = session_crashlog;

  Channel *channel = nullptr;
  int status = 0;
  try {
    /* The stdio reader blocks in the C library until end of input, so the
     * channel outlives this function and is reclaimed by process exit. */
    channel = stdio ? static_cast<Channel *>(new StdioChannel(output)) :
                      static_cast<Channel *>(new SocketChannel(path.string()));
  }
  catch (const std::exception &error) {
    fprintf(stderr, "Session: %s\n", error.what());
    crashlog_callback = nullptr;
    return 1;
  }
  state.channel = channel;
  ChannelSink sink(*channel);

  PyObject *capsule = PyCapsule_New(&state, "agent.session", nullptr);
  for (auto &method : methods) {
    PyObject *function = PyCFunction_New(&method, capsule);
    PyDict_SetItemString(native, method.ml_name, function);
    Py_DECREF(function);
  }
  Py_DECREF(capsule);
  PyObject *emitter = event_emitter(sink);
  PyDict_SetItemString(native, "emit", emitter);
  Py_DECREF(emitter);
  nlohmann::json config = {{"file", file}};
  PyObject *runtime = PyObject_CallMethod(module, "Session", "Os", native, config.dump().c_str());
  if (!runtime) {
    PyErr_Print();
    crashlog_callback = nullptr;
    return 1;
  }

  bool closing = false;
  while (!closing) {
    /* A peer is told what it joined before it is read from. */
    for (const auto &peer : channel->take_ungreeted()) {
      PyObject *greeting = PyObject_CallMethod(runtime, "greeting", nullptr);
      if (greeting) {
        channel->greet(peer, PyUnicode_AsUTF8(greeting));
        Py_DECREF(greeting);
      }
      else {
        PyErr_Print();
      }
    }
    Channel::Request request;
    bool received;
    Py_BEGIN_ALLOW_THREADS received = channel->next(request);
    Py_END_ALLOW_THREADS if (received)
    {
      G.is_break = false;
      crashlog_request = request_summary(request.message);
      fprintf(stderr, "Agent request: %s\n", crashlog_request.c_str());
      fflush(stderr);
      sink.request = &request;
      PyObject *answer = PyObject_CallMethod(
          runtime, "serve", "s", request.message.dump().c_str());
      if (answer) {
        Py_DECREF(answer);
      }
      else {
        PyErr_Print();
        sink.event(nlohmann::json({{"id", request.message["id"]},
                                   {"event", "error"},
                                   {"ok", false},
                                   {"type", "InternalError"},
                                   {"message", "Agent runtime failed; see the session log"}})
                       .dump());
      }
      PyObject *closed = PyObject_GetAttrString(runtime, "closing");
      closing = closed && PyObject_IsTrue(closed);
      Py_XDECREF(closed);
      sink.request = nullptr;
      BLI_timer_execute();
      channel->finish(request);
      crashlog_request.clear();
      state.last_request = Session::Clock::now();
      if (!closing && state.dirty &&
          state.last_request - state.last_write >= std::chrono::seconds(5))
      {
        state.autosave_write();
      }
    }
    else {
      BLI_timer_execute();
      const auto now = Session::Clock::now();
      if (state.dirty && now - state.last_request >= std::chrono::seconds(1) &&
          now - state.last_write >= std::chrono::seconds(1))
      {
        state.autosave_write();
      }
      if (channel->ended()) {
        break;
      }
    }
  }
  state.channel = nullptr;
  PyObject *agent = PyImport_ImportModule("agent");
  if (agent) {
    PyObject_SetAttrString(agent, "_session", Py_None);
    Py_DECREF(agent);
  }
  Py_DECREF(runtime);
  crashlog_callback = nullptr;
  if (!stdio) {
    delete channel;
  }
  if (status == 0) {
    if (!stdio) {
      std::filesystem::remove(path);
      std::filesystem::remove(directory / "session.pid");
      std::filesystem::remove(directory / "session.lock");
    }
    std::filesystem::remove(state.autosave);
    std::filesystem::remove(state.autosave.string() + "@");
    const auto metadata = state.autosave.parent_path() /
                          (state.autosave.stem().string() + ".json");
    std::filesystem::remove(metadata);
    std::filesystem::remove(metadata.string() + "@");
  }
  return status;
}
}  // namespace blender::agent
