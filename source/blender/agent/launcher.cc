/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#include <cstdio>
#include <filesystem>
#include <string>
#include <vector>

#include "launcher_session.hh"

#ifdef _WIN32
#  include <windows.h>
#else
#  include <unistd.h>
#  ifdef __APPLE__
#    include <mach-o/dyld.h>
#  endif
#endif

#ifdef _WIN32
/* Quote according to the Windows CRT command-line parsing rules. */
static std::wstring quote(const std::wstring &arg)
{
  std::wstring result = L"\"";
  size_t slashes = 0;
  for (wchar_t c : arg) {
    if (c == L'\\') {
      slashes++;
      continue;
    }
    result.append(slashes * (c == L'"' ? 2 : 1), L'\\');
    slashes = 0;
    if (c == L'"') {
      result += L'\\';
    }
    result += c;
  }
  result.append(slashes * 2, L'\\');
  return result + L'"';
}

static std::wstring wide(const std::string &text)
{
  int n = MultiByteToWideChar(
      CP_UTF8, MB_ERR_INVALID_CHARS, text.data(), int(text.size()), nullptr, 0);
  std::wstring result(n, L'\0');
  MultiByteToWideChar(
      CP_UTF8, MB_ERR_INVALID_CHARS, text.data(), int(text.size()), result.data(), n);
  return result;
}

int wmain(int argc, wchar_t **argv)
{
  std::vector<wchar_t> path(32768);
  DWORD length = GetModuleFileNameW(nullptr, path.data(), DWORD(path.size()));
  if (!length || length == path.size()) {
    return 1;
  }
  auto binary = std::filesystem::path(path.data()).parent_path() / L"blender.exe";
  std::vector<std::string> arguments;
  for (int i = 1; i < argc; i++) {
    int n = WideCharToMultiByte(CP_UTF8, 0, argv[i], -1, nullptr, 0, nullptr, nullptr);
    std::string text(n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, argv[i], -1, text.data(), n, nullptr, nullptr);
    text.pop_back();
    arguments.push_back(std::move(text));
  }
  int client_status = blender::agent::session_client(
      arguments, [&](const auto &serving, const auto &log) {
        std::wstring command = quote(binary.wstring()) +
                               L" --factory-startup --disable-autoexec --command agent";
        for (const auto &arg : serving) {
          command += L" " + quote(wide(arg));
        }
        SECURITY_ATTRIBUTES security{sizeof(SECURITY_ATTRIBUTES), nullptr, TRUE};
        HANDLE output = CreateFileW(log.c_str(),
                                    FILE_APPEND_DATA,
                                    FILE_SHARE_READ | FILE_SHARE_WRITE,
                                    &security,
                                    OPEN_ALWAYS,
                                    FILE_ATTRIBUTE_NORMAL,
                                    nullptr);
        HANDLE input = CreateFileW(L"NUL",
                                   GENERIC_READ,
                                   FILE_SHARE_READ | FILE_SHARE_WRITE,
                                   &security,
                                   OPEN_EXISTING,
                                   0,
                                   nullptr);
        STARTUPINFOEXW startup{};
        startup.StartupInfo.cb = sizeof(startup);
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
        startup.StartupInfo.hStdInput = input;
        startup.StartupInfo.hStdOutput = startup.StartupInfo.hStdError = output;
        /* Do not inherit the launcher's captured stdout/stderr pipes. Even an
         * unused inherited copy would prevent the caller from receiving EOF
         * until the detached daemon exits. Only its own log and NUL are shared. */
        SIZE_T attribute_size = 0;
        InitializeProcThreadAttributeList(nullptr, 1, 0, &attribute_size);
        std::vector<char> attributes(attribute_size);
        startup.lpAttributeList = reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(
            attributes.data());
        PROCESS_INFORMATION process{};
        BOOL success = InitializeProcThreadAttributeList(
            startup.lpAttributeList, 1, 0, &attribute_size);
        if (success) {
          HANDLE handles[] = {input, output};
          success = UpdateProcThreadAttribute(startup.lpAttributeList,
                                              0,
                                              PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                                              handles,
                                              sizeof(handles),
                                              nullptr,
                                              nullptr);
          if (success) {
            success = CreateProcessW(binary.c_str(),
                                     command.data(),
                                     nullptr,
                                     nullptr,
                                     TRUE,
                                     DETACHED_PROCESS | EXTENDED_STARTUPINFO_PRESENT,
                                     nullptr,
                                     nullptr,
                                     &startup.StartupInfo,
                                     &process);
          }
          DeleteProcThreadAttributeList(startup.lpAttributeList);
        }
        CloseHandle(input);
        CloseHandle(output);
        if (!success) {
          return -1;
        }
        CloseHandle(process.hThread);
        CloseHandle(process.hProcess);
        return int(process.dwProcessId);
      });
  if (client_status >= 0) {
    return client_status;
  }
  std::wstring command = quote(binary.wstring()) +
                         L" --factory-startup --disable-autoexec --command agent";
  for (int i = 1; i < argc; i++) {
    command += L" " + quote(argv[i]);
  }
  STARTUPINFOW startup = {};
  startup.cb = sizeof(startup);
  PROCESS_INFORMATION process = {};
  if (!CreateProcessW(binary.c_str(),
                      command.data(),
                      nullptr,
                      nullptr,
                      TRUE,
                      0,
                      nullptr,
                      nullptr,
                      &startup,
                      &process))
  {
    fprintf(stderr, "blender-cli: CreateProcessW failed (%lu)\n", GetLastError());
    return 1;
  }
  WaitForSingleObject(process.hProcess, INFINITE);
  DWORD status = 1;
  GetExitCodeProcess(process.hProcess, &status);
  CloseHandle(process.hThread);
  CloseHandle(process.hProcess);
  return int(status);
}
#else
int main(int argc, char **argv)
{
  std::filesystem::path self;
#  ifdef __APPLE__
  uint32_t size = 0;
  _NSGetExecutablePath(nullptr, &size);
  std::vector<char> path(size);
  if (_NSGetExecutablePath(path.data(), &size) == 0) {
    self = std::filesystem::canonical(path.data());
  }
#  else
  std::error_code error;
  self = std::filesystem::read_symlink("/proc/self/exe", error);
#  endif
  if (self.empty()) {
    self = std::filesystem::absolute(argv[0]);
  }
#  ifdef __APPLE__
  std::string binary = (self.parent_path() / "Blender").string();
#  else
  std::string binary = (self.parent_path() / "blender").string();
#  endif
  signal(SIGPIPE, SIG_IGN);
  std::vector<std::string> client_arguments(argv + 1, argv + argc);
  int client_status = blender::agent::session_client(
      client_arguments, [&](const auto &serving, const auto &log) {
        pid_t pid = fork();
        if (pid != 0) {
          return int(pid);
        }
        if (setsid() < 0) {
          _exit(1);
        }
        int input = open("/dev/null", O_RDONLY);
        int output = open(log.c_str(), O_WRONLY | O_CREAT | O_APPEND, 0600);
        if (input < 0 || output < 0 || dup2(input, STDIN_FILENO) < 0 ||
            dup2(output, STDOUT_FILENO) < 0 || dup2(output, STDERR_FILENO) < 0)
        {
          _exit(1);
        }
        close(input);
        close(output);
        std::vector<std::string> command = {
            binary, "--factory-startup", "--disable-autoexec", "--command", "agent"};
        command.insert(command.end(), serving.begin(), serving.end());
        std::vector<char *> pointers;
        for (auto &argument : command) {
          pointers.push_back(argument.data());
        }
        pointers.push_back(nullptr);
        execv(binary.c_str(), pointers.data());
        _exit(1);
      });
  if (client_status >= 0) {
    return client_status;
  }
  std::vector<std::string> arguments = {
      binary, "--factory-startup", "--disable-autoexec", "--command", "agent"};
  for (int i = 1; i < argc; i++) {
    arguments.emplace_back(argv[i]);
  }
  std::vector<char *> pointers;
  for (std::string &argument : arguments) {
    pointers.push_back(argument.data());
  }
  pointers.push_back(nullptr);
  execv(binary.c_str(), pointers.data());
  perror("blender-cli: execv");
  return 1;
}
#endif
