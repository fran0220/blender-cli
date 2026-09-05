/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#include <cctype>
#include <cstdio>
#include <cstring>
#include <unordered_map>

#include <Python.h>

#ifdef _WIN32
#  include <io.h>
#  define dup _dup
#  define dup2 _dup2
#  define fdopen _fdopen
#  define fileno _fileno
#else
#  include <unistd.h>
#endif

#include "BLI_utildefines.hh"

#include "BKE_blender_cli_command.hh"
#include "BKE_blender_version.h"
#include "BKE_context.hh"
#include "BKE_idtype.hh"
#include "BKE_main.hh"

#include "BPY_extern.hh"
#include "DNA_ID.h"

#include "AGENT_command.hh"
#include "agent_cli.hh"
#include "agent_context.hh"
#include "agent_events.hh"
#include "agent_session.hh"

namespace blender::agent {

struct RequestState {
  bContext *context;
  std::unordered_map<unsigned int, unsigned int> initial_recalc;
};

/* Snapshot real Main IDs, not Python references which can become invalid after deletion/load. */
static PyObject *id_state(PyObject *self, PyObject *args)
{
  int reset;
  if (!PyArg_ParseTuple(args, "p", &reset)) {
    return nullptr;
  }
  auto &state = *static_cast<RequestState *>(PyCapsule_GetPointer(self, "agent.request"));
  if (reset) {
    state.initial_recalc.clear();
  }
  PyObject *result = PyDict_New();
  ID *id;
  FOREACH_MAIN_ID_BEGIN (CTX_data_main(state.context), id) {
    if (reset) {
      state.initial_recalc[id->session_uid] = id->recalc;
      id->recalc_after_undo_push = 0;
    }
    std::string type = BKE_idtype_idcode_to_name(GS(id->name));
    for (char &c : type) {
      c = char(std::toupper(static_cast<unsigned char>(c)));
    }
    const unsigned int flags = id->recalc_after_undo_push |
                               (id->recalc & ~state.initial_recalc[id->session_uid]);
    PyObject *key = PyLong_FromUnsignedLong(id->session_uid);
    PyObject *value = Py_BuildValue("(ssI)", type.c_str(), id->name + 2, flags);
    PyDict_SetItem(result, key, value);
    Py_DECREF(key);
    Py_DECREF(value);
  }
  FOREACH_MAIN_ID_END;
  return result;
}

static PyMethodDef id_state_method = {"id_state", id_state, METH_VARARGS, nullptr};

static PyObject *recalc_fields()
{
  PyObject *result = PyDict_New();
#define FIELD(flag, name) \
  { \
    PyObject *value = PyLong_FromUnsignedLong(flag); \
    PyDict_SetItemString(result, name, value); \
    Py_DECREF(value); \
  }
  FIELD(ID_RECALC_TRANSFORM, "transform");
  FIELD(ID_RECALC_GEOMETRY, "geometry");
  FIELD(ID_RECALC_ANIMATION, "animation");
  FIELD(ID_RECALC_PSYS_ALL, "particles");
  FIELD(ID_RECALC_SHADING, "shading");
  FIELD(ID_RECALC_SELECT, "selection");
  FIELD(ID_RECALC_BASE_FLAGS, "base_flags");
  FIELD(ID_RECALC_POINT_CACHE, "point_cache");
  FIELD(ID_RECALC_EDITORS, "editors");
  FIELD(ID_RECALC_SYNC_TO_EVAL, "copy_on_eval");
  FIELD(ID_RECALC_SEQUENCER_STRIPS, "sequencer");
  FIELD(ID_RECALC_FRAME_CHANGE, "frame_change");
  FIELD(ID_RECALC_AUDIO_FPS | ID_RECALC_AUDIO_VOLUME | ID_RECALC_AUDIO_MUTE |
            ID_RECALC_AUDIO_LISTENER | ID_RECALC_AUDIO,
        "audio");
  FIELD(ID_RECALC_PARAMETERS, "parameters");
  FIELD(ID_RECALC_SOURCE, "source");
  FIELD(ID_RECALC_TAG_FOR_UNDO, "undo");
  FIELD(ID_RECALC_NTREE_OUTPUT, "node_output");
  FIELD(ID_RECALC_HIERARCHY, "hierarchy");
  FIELD(ID_RECALC_COMPOSITOR, "compositor");
#undef FIELD
  return result;
}

/* `--file F` after a verb that owns the loop: the scene the session opens. */
static std::string loaded_file(int argc, const char **argv)
{
  for (int i = 0; i + 1 < argc; i++) {
    if (STREQ(argv[i], "--file")) {
      return cli_absolute(argv[i + 1]);
    }
  }
  return "";
}

class AgentCommand : public CommandHandler {
 public:
  AgentCommand() : CommandHandler("agent") {}

  int exec(bContext *C, int argc, const char **argv) override
  {
    if (argc == 0 || STREQ(argv[0], "--help")) {
      puts(
          "Usage: blender-cli <repl|exec|inspect|observe|describe|session|target|program|fit>\n"
          "  repl [--file F] [--standalone]   one pipe of JSON-line requests and events\n"
          "  exec -c CODE | FILE.py [--no-record] [--timeout S] [--image delta|full|off]\n"
          "  inspect [--object NAME] [--full] [--select PATH ...]\n"
          "  observe [--views front,persp] [--passes color,wire,silhouette,normal,depth]\n"
          "          [--size 512|768|1024] [--frame OBJECT] [--ref IMG] [--overlay]\n"
          "          [--layout sheet|separate] [--out PATH | --inline]\n"
          "  describe RNA_PATH | channel | --schema\n"
          "  session open|status|feedback|save|close|snapshot|rollback|history\n"
          "          [--label L] [--file F] [--json-file F | KEY=VALUE ...]\n"
          "  target set NAME --ref IMG [--view V] [--mask auto|none] [--fit bbox|none]\n"
          "         [--metrics iou,chamfer,ssim,hist] | target list | target clear [NAME]\n"
          "  program get|set|patch|run|history|rollback|record [--text T] [--old O] [--new N]\n"
          "          [--label L] [--version V]\n"
          "  fit --params JSON [--objective JSON] [--budget JSON] [--method M]\n"
          "  Common: --file F --save [F] --json\n"
          "  --version: upstream version and fork tag");
      return 0;
    }
    if (STREQ(argv[0], "--version")) {
      const char *cycle = STRINGIFY(BLENDER_VERSION_CYCLE);
      const bool release = STREQ(cycle, "release");
      printf("Blender %s\nblender-cli %d.%d.%d%s%s-agent.1\n",
             BKE_blender_version_string(),
             BLENDER_VERSION / 100,
             BLENDER_VERSION % 100,
             BLENDER_VERSION_PATCH,
             release ? "" : "-",
             release ? "" : cycle);
      return 0;
    }
    /* Keep Blender's native reports and shutdown output off the protocol stream. */
    fflush(stdout);
    FILE *output = fdopen(dup(fileno(stdout)), "w");
    if (!output || dup2(fileno(stderr), fileno(stdout)) < 0) {
      if (output) {
        fclose(output);
      }
      return 1;
    }
    const PyGILState_STATE gil = PyGILState_Ensure();
    BPY_context_set(C);
    RequestState state{C, {}};
    PyObject *capsule = PyCapsule_New(&state, "agent.request", nullptr);
    PyObject *native = PyDict_New();
    PyObject *snapshot = PyCFunction_New(&id_state_method, capsule);
    PyObject *fields = recalc_fields();
    PyDict_SetItemString(native, "id_state", snapshot);
    PyDict_SetItemString(native, "fields", fields);
    Py_DECREF(snapshot);
    Py_DECREF(fields);
    Py_DECREF(capsule);
    PyObject *module = PyImport_ImportModule("agent_runtime");
    PyObject *helper = PyImport_ImportModule("agent");
    if (helper) {
      PyObject *api = native_api(C);
      PyObject_SetAttrString(helper, "_native", api);
      Py_DECREF(api);
      Py_DECREF(helper);
    }
    int status = 1;
    if (module) {
      const bool serving = argc >= 2 && STREQ(argv[0], "session") && STREQ(argv[1], "serve");
      const bool repl = STREQ(argv[0], "repl");
      if (serving || repl) {
        status = session_serve(C, module, native, loaded_file(argc, argv), repl, output);
      }
      else {
        status = one_shot(module, native, argc, argv, output);
      }
    }
    if (PyErr_Occurred()) {
      PyErr_Print();
      fputs(
          "{\"ok\":false,\"error\":{\"type\":\"InternalError\","
          "\"message\":\"Agent runtime failed; see stderr\"}}\n",
          output);
      status = 1;
    }
    Py_XDECREF(module);
    Py_DECREF(native);
    PyGILState_Release(gil);
    fclose(output);
    return status;
  }

 private:
  /* One request, then the folded envelope: the same events, printed as one
   * document instead of streamed. */
  static int one_shot(PyObject *module, PyObject *native, int argc, const char **argv, FILE *out)
  {
    CommandLine parsed = cli_parse(std::vector<std::string>(argv, argv + argc));
    if (!parsed.error.empty()) {
      nlohmann::json envelope = {{"ok", false},
                                 {"error", {{"type", "ValueError"}, {"message", parsed.error}}}};
      fprintf(out, "%s\n", envelope.dump(parsed.compact ? -1 : 2).c_str());
      return 1;
    }
    parsed.request["id"] = 1;
    nlohmann::json config = {{"request", parsed.request}, {"file", parsed.load}};
    if (parsed.has_save) {
      config["save"] = parsed.save;
    }
    CollectingSink sink;
    PyObject *emitter = event_emitter(sink);
    PyDict_SetItemString(native, "emit", emitter);
    Py_DECREF(emitter);
    PyObject *answer = PyObject_CallMethod(
        module, "one_shot", "Os", native, config.dump().c_str());
    if (!answer) {
      return 1;
    }
    Py_DECREF(answer);
    const auto envelope = fold(sink.events);
    fprintf(out, "%s\n", envelope.dump(parsed.compact ? -1 : 2).c_str());
    return envelope_status(envelope);
  }
};

void command_register()
{
  BKE_blender_cli_command_register(std::make_unique<AgentCommand>());
}

}  // namespace blender::agent
