/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

#include "agent_cli.hh"
#include "agent_events.hh"
#include "agent_socket.hh"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <json.hpp>
#include <thread>
#include <vector>

#ifdef _WIN32
#  include <windows.h>
#else
#  include <fcntl.h>
#  include <signal.h>
#  include <sys/file.h>
#  include <sys/wait.h>
#endif

namespace blender::agent {

inline bool process_alive(int pid)
{
  if (pid <= 0) {
    return false;
  }
#ifdef _WIN32
  HANDLE process = OpenProcess(SYNCHRONIZE, FALSE, DWORD(pid));
  if (!process) {
    return GetLastError() == ERROR_ACCESS_DENIED;
  }
  bool alive = WaitForSingleObject(process, 0) == WAIT_TIMEOUT;
  CloseHandle(process);
  return alive;
#else
  int status;
  if (waitpid(pid, &status, WNOHANG) == pid) {
    return false;
  }
#  ifdef __linux__
  std::ifstream stat("/proc/" + std::to_string(pid) + "/stat");
  std::string line;
  std::getline(stat, line);
  const auto end = line.rfind(')');
  if (end != std::string::npos && end + 2 < line.size() && line[end + 2] == 'Z') {
    return false;
  }
#  endif
  return kill(pid, 0) == 0 || errno == EPERM;
#endif
}

inline void terminate_process(int pid)
{
  if (pid <= 0) {
    return;
  }
#ifdef _WIN32
  HANDLE process = OpenProcess(PROCESS_TERMINATE, FALSE, DWORD(pid));
  if (process) {
    TerminateProcess(process, 1);
    CloseHandle(process);
  }
#else
  kill(pid, SIGTERM);
  for (int i = 0; i < 100 && process_alive(pid); i++) {
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
  }
  if (process_alive(pid)) {
    kill(pid, SIGKILL);
  }
#endif
}

class SessionLock {
#ifdef _WIN32
  HANDLE file_;
#else
  int file_;
#endif
 public:
  explicit SessionLock(const std::filesystem::path &path)
  {
#ifdef _WIN32
    file_ = CreateFileW(path.c_str(),
                        GENERIC_READ | GENERIC_WRITE,
                        0,
                        nullptr,
                        OPEN_ALWAYS,
                        FILE_ATTRIBUTE_NORMAL,
                        nullptr);
    if (file_ == INVALID_HANDLE_VALUE) {
#else
    file_ = open(path.c_str(), O_CREAT | O_RDWR | O_CLOEXEC, 0600);
    if (file_ < 0 || flock(file_, LOCK_EX | LOCK_NB) != 0) {
      if (file_ >= 0) {
        close(file_);
      }
#endif
      throw std::runtime_error("Another session open/close is in progress");
    }
  }
  ~SessionLock()
  {
#ifdef _WIN32
    CloseHandle(file_);
#else
    close(file_);
#endif
  }
};

/* Read one request's events, in order, until its terminal event. */
inline std::vector<nlohmann::json> read_events(LineReader &reader)
{
  std::vector<nlohmann::json> events;
  while (true) {
    auto event = nlohmann::json::parse(reader.next(), nullptr, false);
    if (!event.is_object()) {
      continue;
    }
    const auto kind = event.value("event", std::string());
    events.push_back(std::move(event));
    if (kind == "done" || kind == "error") {
      return events;
    }
  }
}

/* `repl`: the launcher owns no protocol, it only moves the same bytes. */
inline int session_bridge(Socket fd)
{
  std::thread writer([fd] {
    char buffer[8192];
    while (fgets(buffer, sizeof(buffer), stdin)) {
      if (!socket_write(fd, buffer)) {
        return;
      }
    }
    socket_shutdown_write(fd);
  });
  char chunk[8192];
  int count;
  while ((count = recv(fd, chunk, sizeof(chunk), 0)) > 0) {
    fwrite(chunk, 1, size_t(count), stdout);
    fflush(stdout);
  }
  /* The reader is blocked in the C library until stdin closes; the process
   * exits immediately after this returns. */
  writer.detach();
  return 0;
}

/* Return -1 only when the one-shot launcher should take over. */
template<typename Spawn> int session_client(const std::vector<std::string> &args, Spawn spawn)
{
  if (args.empty() || args[0].starts_with("--")) {
    return -1;
  }
  const bool compact = std::find(args.begin(), args.end(), "--json") != args.end();
  const bool repl = args[0] == "repl";
  if (repl && std::find(args.begin(), args.end(), "--standalone") != args.end()) {
    /* A standalone repl is the loop itself, in one process, with no daemon. */
    return -1;
  }
  auto print = [&](const nlohmann::json &result) {
    puts(result.dump(compact ? -1 : 2).c_str());
    return result.is_object() && result.value("ok", true) == false ? 1 : 0;
  };
  auto failure = [&](const std::string &type, const std::string &message) {
    return print({{"ok", false}, {"error", {{"type", type}, {"message", message}}}});
  };
  try {
    socket_init();
    const auto directory = std::filesystem::current_path() / ".blender-cli";
    const auto path = directory / "session.sock";
    const auto pidfile = directory / "session.pid";
    int pid = 0;
    std::ifstream(pidfile) >> pid;
    auto autosave = directory / ("autosave-" + std::to_string(pid) + ".blend");
    auto with_autosave = [&](nlohmann::json result, const char *key = "autosave") {
      if (std::filesystem::is_regular_file(autosave)) {
        result[key] = autosave.string();
      }
      return result;
    };
    const bool opening = args.size() >= 2 && args[0] == "session" && args[1] == "open";
    const bool closing = args.size() >= 2 && args[0] == "session" && args[1] == "close";
    auto dead_session = [&]() {
      return print(with_autosave(
          {{"ok", false},
           {"error",
            {{"type", "SessionError"},
             {"message",
              "Session " + std::to_string(pid) +
                  " exited unexpectedly; see .blender-cli/session.log. Recover with "
                  "`session open --file <autosave>` or discard with `session close`"}}}}));
    };
    if (!opening && !closing && std::filesystem::exists(pidfile) && !process_alive(pid)) {
      return dead_session();
    }
    /* Starting the daemon is the same operation for `session open` and for a
     * `repl` that finds no endpoint. */
    auto start_daemon = [&](const std::vector<std::string> &serving) {
      std::filesystem::create_directories(directory);
#ifndef _WIN32
      std::filesystem::permissions(directory, std::filesystem::perms::owner_all);
#endif
      SessionLock lock(directory / "session.lock");
      std::ifstream(pidfile) >> pid;
      Socket existing = socket_connect(path.string());
      if (existing != invalid_socket) {
        socket_close(existing);
        throw std::runtime_error("A session is already listening in this directory");
      }
      if (process_alive(pid)) {
        throw std::runtime_error(
            "A session process is alive but not responding; use session close");
      }
      std::filesystem::remove(path);
      std::filesystem::remove(pidfile);
      pid = spawn(serving, directory / "session.log");
      if (pid <= 0) {
        throw std::runtime_error("Could not start session daemon");
      }
      std::ofstream(pidfile) << pid << '\n';
      for (int i = 0; i < 1000; i++) {
        Socket fd = socket_connect(path.string());
        if (fd != invalid_socket) {
          socket_close(fd);
          return;
        }
        if (!process_alive(pid)) {
          break;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
      }
      terminate_process(pid);
      std::filesystem::remove(path);
      std::filesystem::remove(pidfile);
      throw std::runtime_error(
          "Session startup failed within 10 seconds; see .blender-cli/session.log");
    };
    /* One request, its events, and the envelope they fold into. */
    auto converse = [&](Socket fd, const nlohmann::json &request, int timeout_seconds) {
      if (!socket_write(fd, request.dump() + "\n")) {
        socket_close(fd);
        throw std::runtime_error("Could not send session request");
      }
      if (timeout_seconds > 0) {
        fd_set readers;
        FD_ZERO(&readers);
        FD_SET(fd, &readers);
        timeval timeout{timeout_seconds, 0};
        if (select(int(fd + 1), &readers, nullptr, nullptr, &timeout) <= 0) {
          throw std::runtime_error("Session did not answer in time");
        }
      }
      LineReader reader(fd);
      return fold(read_events(reader));
    };
    if (opening) {
      const auto file_arg = std::find(args.begin(), args.end(), "--file");
      if (file_arg != args.end() && file_arg + 1 != args.end()) {
        const auto source = std::filesystem::absolute(*(file_arg + 1));
        if (source.filename().string().starts_with("autosave-") &&
            std::filesystem::is_regular_file(source.parent_path() /
                                             (source.stem().string() + ".json")))
        {
          autosave = source;
        }
      }
      std::vector<std::string> serving = args;
      serving[1] = "serve";
      start_daemon(serving);
      nlohmann::json result = {{"session", std::to_string(pid)}, {"socket", path.string()}};
      Socket fd = socket_connect(path.string());
      if (fd != invalid_socket) {
        /* The daemon knows what it rebuilt its scene from; the launcher does not. */
        const auto status = converse(fd, {{"id", 1}, {"op", "session"}, {"action", "status"}}, 0);
        socket_close(fd);
        if (status.contains("recovered_from") && !status["recovered_from"].is_null()) {
          result["recovered_from"] = status["recovered_from"];
        }
      }
      return print(with_autosave(result, "previous_autosave"));
    }
    Socket fd = socket_connect(path.string());
    if (fd == invalid_socket) {
      if (closing) {
        const bool stale = std::filesystem::exists(pidfile) && !process_alive(pid);
        if (std::filesystem::exists(directory)) {
          SessionLock lock(directory / "session.lock");
          terminate_process(pid);
          std::filesystem::remove(path);
          std::filesystem::remove(pidfile);
        }
        std::filesystem::remove(directory / "session.lock");
        if (stale) {
          return print(with_autosave({{"ok", true}, {"stale", true}}));
        }
        return print({{"ok", true}});
      }
      if (std::filesystem::exists(pidfile)) {
        if (!process_alive(pid)) {
          return dead_session();
        }
        throw std::runtime_error("Session process is alive but endpoint is unavailable");
      }
      if (!repl) {
        return -1;
      }
      std::vector<std::string> serving = {"session", "serve"};
      const auto file_arg = std::find(args.begin(), args.end(), "--file");
      if (file_arg != args.end() && file_arg + 1 != args.end()) {
        serving.insert(serving.end(), {"--file", *(file_arg + 1)});
      }
      start_daemon(serving);
      fd = socket_connect(path.string());
      if (fd == invalid_socket) {
        throw std::runtime_error("Session started but its endpoint is unavailable");
      }
    }
    if (repl) {
      return session_bridge(fd);
    }
    CommandLine parsed = cli_parse(args);
    if (!parsed.error.empty()) {
      socket_close(fd);
      return failure("ValueError", parsed.error);
    }
    if (!parsed.load.empty() || parsed.has_save) {
      socket_close(fd);
      return failure("ValueError",
                     "--file loads only at session open and --save is a one-shot option; "
                     "use `session save --file F` in a session");
    }
    /* IDs are supplied by raw clients. A launcher uses one connection for one request. */
    parsed.request["id"] = std::chrono::steady_clock::now().time_since_epoch().count();
    nlohmann::json envelope;
    try {
      envelope = converse(fd, parsed.request, closing ? 2 : 0);
    }
    catch (...) {
      socket_close(fd);
      if (closing) {
        SessionLock lock(directory / "session.lock");
        terminate_process(pid);
        std::filesystem::remove(path);
        std::filesystem::remove(pidfile);
        return print({{"ok", true}, {"forced", true}});
      }
      /* EOF can precede OS process teardown, particularly on Windows. Report
       * recovery on the request that died, not only on its successor. */
      for (int i = 0; i < 200 && process_alive(pid); i++) {
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
      }
      if (!process_alive(pid)) {
        return dead_session();
      }
      throw;
    }
    socket_close(fd);
    if (closing && envelope.value("ok", false)) {
      for (int i = 0; i < 200 && process_alive(pid); i++) {
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
      }
      if (process_alive(pid)) {
        terminate_process(pid);
        std::filesystem::remove(path);
        std::filesystem::remove(pidfile);
        envelope["forced"] = true;
      }
    }
    return print(envelope);
  }
  catch (const std::exception &error) {
    return failure("SessionError", error.what());
  }
}
}  // namespace blender::agent
