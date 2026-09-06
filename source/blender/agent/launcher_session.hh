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
#include <deque>
#include <filesystem>
#include <fstream>
#include <json.hpp>
#include <mutex>
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

/* Everything `repl` writes to stdout is one line of the same protocol. */
inline void bridge_write(const nlohmann::json &event)
{
  puts(event.dump().c_str());
  fflush(stdout);
}

/* The pipe outlives the process behind it. The bridge relays lines verbatim,
 * remembers which requests are outstanding, and when the session dies it
 * answers each of them, reopens the session and keeps serving the same pipe.
 * It never decides what the reopened session rebuilds from: it reports the
 * verdict the session states in its greeting. */
class Bridge {
  std::mutex mutex_;
  Socket fd_;
  std::deque<nlohmann::json> outstanding_;
  bool ended_ = false;
  std::thread writer_;

  /* Transport side: forward whole lines and record what is owed an answer. */
  void write_loop()
  {
    std::string pending;
    char buffer[8192];
    while (fgets(buffer, sizeof(buffer), stdin)) {
      pending += buffer;
      if (pending.empty() || pending.back() != '\n') {
        continue;
      }
      auto message = nlohmann::json::parse(pending, nullptr, false);
      std::lock_guard lock(mutex_);
      if (message.is_object() && message.contains("id")) {
        /* Recorded before the write, so a request lost to a dying session is
         * still answered rather than silently dropped. */
        outstanding_.push_back(message["id"]);
      }
      socket_write(fd_, pending);
      pending.clear();
    }
    std::lock_guard lock(mutex_);
    ended_ = true;
    socket_shutdown_write(fd_);
  }

 public:
  explicit Bridge(Socket fd) : fd_(fd)
  {
    writer_ = std::thread([this] { write_loop(); });
  }

  ~Bridge()
  {
    /* The writer is blocked in the C library until stdin closes; the process
     * exits immediately after the bridge returns. */
    writer_.detach();
  }

  bool ended()
  {
    std::lock_guard lock(mutex_);
    return ended_;
  }

  std::deque<nlohmann::json> take_outstanding()
  {
    std::lock_guard lock(mutex_);
    return std::exchange(outstanding_, {});
  }

  /* Requests that have been read but not yet answered. */
  bool owes()
  {
    std::lock_guard lock(mutex_);
    return !outstanding_.empty();
  }

  void answered(const nlohmann::json &id)
  {
    std::lock_guard lock(mutex_);
    const auto found = std::find(outstanding_.begin(), outstanding_.end(), id);
    if (found != outstanding_.end()) {
      outstanding_.erase(found);
    }
  }

  /* Closing the dead socket under the same lock the writer uses keeps a
   * forwarded line from ever reaching a reused descriptor. */
  void reconnect(Socket fd)
  {
    std::lock_guard lock(mutex_);
    socket_close(fd_);
    fd_ = fd;
  }

  Socket socket()
  {
    std::lock_guard lock(mutex_);
    return fd_;
  }
};

/* Return -1 only when the one-shot launcher should take over. */
template<typename Spawn> int session_client(const std::vector<std::string> &args, Spawn spawn)
{
  if (args.empty() || args[0].starts_with("--")) {
    return -1;
  }
  const bool repl = args[0] == "repl";
  /* A repl writes protocol, never a document: its own failures are one line. */
  const bool compact = repl || std::find(args.begin(), args.end(), "--json") != args.end();
  if (repl && std::find(args.begin(), args.end(), "--standalone") != args.end()) {
    /* A standalone repl is the loop itself, in one process, with no daemon. */
    return -1;
  }
  auto print = [&](const nlohmann::json &result) {
    puts(result.dump(compact ? -1 : 2).c_str());
    return result.is_object() && result.value("ok", true) == false ? 1 : 0;
  };
  auto failure = [&](const std::string &type, const std::string &message) {
    if (repl) {
      /* On the channel, a failure is an event with no request behind it. */
      bridge_write({{"id", nullptr},
                    {"event", "error"},
                    {"ok", false},
                    {"type", type},
                    {"message", message}});
      return 1;
    }
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
    /* A repl recovers a dead session itself; every other verb reports it. */
    if (!opening && !closing && !repl && std::filesystem::exists(pidfile) && !process_alive(pid)) {
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
      if (std::filesystem::exists(pidfile) && process_alive(pid)) {
        throw std::runtime_error("Session process is alive but endpoint is unavailable");
      }
      if (!repl) {
        if (std::filesystem::exists(pidfile)) {
          return dead_session();
        }
        return -1;
      }
    }
    if (repl) {
      /* Reopening is one operation whether the session died before this repl
       * started or during it. What the reopened session rebuilds from is its
       * decision, stated in its greeting; the bridge only relays it. */
      const auto file_arg = std::find(args.begin(), args.end(), "--file");
      auto reopen = [&]() {
        std::vector<std::string> serving = {"session", "serve"};
        if (file_arg != args.end() && file_arg + 1 != args.end()) {
          serving.insert(serving.end(), {"--file", *(file_arg + 1)});
        }
        std::filesystem::remove(path);
        std::filesystem::remove(pidfile);
        start_daemon(serving);
        Socket opened = socket_connect(path.string());
        if (opened == invalid_socket) {
          throw std::runtime_error("Session started but its endpoint is unavailable");
        }
        return opened;
      };
      if (fd == invalid_socket) {
        fd = reopen();
      }
      Bridge bridge(fd);
      LineReader reader(fd);
      while (true) {
        std::string line;
        try {
          line = reader.next();
        }
        catch (const std::exception &) {
          if (bridge.ended() && !bridge.owes()) {
            return 0;
          }
          /* The session is gone. Answer what it owed, reopen, and say what
           * the new one is, on the same pipe. */
          const auto lost = pid;
          const auto lost_autosave = directory / ("autosave-" + std::to_string(lost) + ".blend");
          for (int i = 0; i < 200 && process_alive(lost); i++) {
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
          }
          nlohmann::json greeting;
          try {
            fd = reopen();
            bridge.reconnect(fd);
            reader = LineReader(fd);
            greeting = nlohmann::json::parse(reader.next(), nullptr, false);
          }
          catch (const std::exception &error) {
            return failure("SessionError",
                           std::string("Session ") + std::to_string(lost) +
                               " exited and could not be reopened: " + error.what());
          }
          for (const auto &id : bridge.take_outstanding()) {
            nlohmann::json crashed = nlohmann::json::object();
            crashed["id"] = id;
            crashed["event"] = "error";
            crashed["ok"] = false;
            crashed["type"] = "Crashed";
            crashed["message"] = "Session " + std::to_string(lost) +
                                 " exited during this request; see .blender-cli/session.log. "
                                 "The session was reopened and this pipe still serves it";
            crashed["recovered_from"] = greeting.value("recovered_from", nlohmann::json());
            crashed["step"] = greeting.value("step", nlohmann::json());
            crashed["snapshot"] = greeting.value("snapshot", nlohmann::json());
            if (std::filesystem::is_regular_file(lost_autosave)) {
              crashed["autosave"] = lost_autosave.string();
            }
            bridge_write(crashed);
          }
          bridge_write(greeting);
          if (bridge.ended()) {
            /* Nothing more can arrive, and everything read has been answered. */
            return 0;
          }
          continue;
        }
        puts(line.c_str());
        fflush(stdout);
        auto event = nlohmann::json::parse(line, nullptr, false);
        if (event.is_object()) {
          const auto kind = event.value("event", std::string());
          if (kind == "done" || kind == "error") {
            bridge.answered(event["id"]);
          }
        }
      }
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
