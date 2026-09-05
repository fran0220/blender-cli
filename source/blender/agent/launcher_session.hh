/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

#include "agent_socket.hh"

#include <algorithm>
#include <chrono>
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

/* Return -1 only when the original one-shot launcher should take over. */
template<typename Spawn> int session_client(const std::vector<std::string> &args, Spawn spawn)
{
  if (args.empty() || args[0].starts_with("--")) {
    return -1;
  }
  const bool compact = std::find(args.begin(), args.end(), "--json") != args.end();
  auto print = [&](const nlohmann::json &result) {
    puts(result.dump(compact ? -1 : 2).c_str());
    return result.is_object() && result.value("ok", true) == false ? 1 : 0;
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
      std::vector<std::string> serving = args;
      serving[1] = "serve";
      pid = spawn(serving, directory / "session.log");
      if (pid <= 0) {
        throw std::runtime_error("Could not start session daemon");
      }
      std::ofstream(pidfile) << pid << '\n';
      for (int i = 0; i < 1000; i++) {
        Socket fd = socket_connect(path.string());
        if (fd != invalid_socket) {
          socket_close(fd);
          return print(with_autosave({{"session", std::to_string(pid)}, {"socket", path.string()}},
                                     "previous_autosave"));
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
      return -1;
    }
    std::vector<std::string> forwarded(args.begin() + 1, args.end());
    const auto id = std::chrono::steady_clock::now().time_since_epoch().count();
    nlohmann::json request = {{"id", id}, {"verb", args[0]}, {"args", {{"argv", forwarded}}}};
    /* IDs are supplied by raw clients. A launcher uses one connection for one request. */
    if (!socket_write(fd, request.dump() + "\n")) {
      socket_close(fd);
      throw std::runtime_error("Could not send session request");
    }
    if (closing) {
      fd_set readers;
      FD_ZERO(&readers);
      FD_SET(fd, &readers);
      timeval timeout{2, 0};
      if (select(int(fd + 1), &readers, nullptr, nullptr, &timeout) <= 0) {
        socket_close(fd);
        SessionLock lock(directory / "session.lock");
        terminate_process(pid);
        std::filesystem::remove(path);
        std::filesystem::remove(pidfile);
        return print({{"ok", true}, {"forced", true}});
      }
    }
    std::string line;
    try {
      line = socket_read_line(fd);
    }
    catch (...) {
      socket_close(fd);
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
    auto response = nlohmann::json::parse(line);
    auto result = response.at("result");
    if (closing && result.is_object() && result.value("ok", false)) {
      for (int i = 0; i < 200 && process_alive(pid); i++) {
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
      }
      if (process_alive(pid)) {
        terminate_process(pid);
        std::filesystem::remove(path);
        std::filesystem::remove(pidfile);
        result["forced"] = true;
      }
    }
    return print(result);
  }
  catch (const std::exception &error) {
    return print(
        {{"ok", false}, {"error", {{"type", "SessionError"}, {"message", error.what()}}}});
  }
}
}  // namespace blender::agent
