/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#include <cstdio>
#include <filesystem>
#include <string>
#include <vector>

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

int wmain(int argc, wchar_t **argv)
{
  std::vector<wchar_t> path(32768);
  DWORD length = GetModuleFileNameW(nullptr, path.data(), DWORD(path.size()));
  if (!length || length == path.size()) {
    return 1;
  }
  auto binary = std::filesystem::path(path.data()).parent_path() / L"blender.exe";
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
