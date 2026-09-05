/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

#include <cstring>
#include <filesystem>
#include <stdexcept>
#include <string>

#ifdef _WIN32
#  ifndef NOMINMAX
#    define NOMINMAX
#  endif
#  include <winsock2.h>

#  include <afunix.h>
#else
#  include <cerrno>
#  include <fcntl.h>
#  include <sys/socket.h>
#  include <sys/un.h>
#  include <unistd.h>
#endif

namespace blender::agent {
#ifdef _WIN32
using Socket = SOCKET;
constexpr Socket invalid_socket = INVALID_SOCKET;
inline void socket_init()
{
  WSADATA data;
  if (WSAStartup(MAKEWORD(2, 2), &data)) {
    throw std::runtime_error("WSAStartup failed");
  }
}
inline void socket_close(Socket fd)
{
  closesocket(fd);
}
#else
using Socket = int;
constexpr Socket invalid_socket = -1;
inline void socket_init() {}
inline void socket_close(Socket fd)
{
  close(fd);
}
#endif

inline sockaddr_un socket_address(const std::string &path)
{
  sockaddr_un address{};
  address.sun_family = AF_UNIX;
  /* Both peers start in the session directory. Relative addressing avoids
   * sun_path's limit even when the absolute working directory is very long.
   * Callers retain the absolute path for reporting and cleanup after Python
   * changes its working directory; no process-wide chdir is needed here. */
  std::string socket_path = path;
  if (socket_path.size() >= sizeof(address.sun_path)) {
    socket_path =
        std::filesystem::path(path).lexically_relative(std::filesystem::current_path()).string();
  }
  if (socket_path.empty() || socket_path.size() >= sizeof(address.sun_path)) {
    throw std::runtime_error("Session socket path is too long");
  }
  memcpy(address.sun_path, socket_path.c_str(), socket_path.size() + 1);
  return address;
}

inline Socket socket_connect(const std::string &path)
{
  const auto address = socket_address(path);
  Socket fd = socket(AF_UNIX, SOCK_STREAM, 0);
  if (fd != invalid_socket &&
      connect(fd, reinterpret_cast<const sockaddr *>(&address), sizeof(address)) == 0)
  {
    return fd;
  }
  if (fd != invalid_socket) {
    socket_close(fd);
  }
  return invalid_socket;
}

inline bool socket_write(Socket fd, const std::string &text)
{
  size_t offset = 0;
  while (offset < text.size()) {
#ifdef MSG_NOSIGNAL
    constexpr int flags = MSG_NOSIGNAL;
#else
    constexpr int flags = 0;
#endif
    int count = send(fd, text.data() + offset, int(text.size() - offset), flags);
    if (count <= 0) {
      return false;
    }
    offset += count;
  }
  return true;
}

inline std::string socket_read_line(Socket fd)
{
  std::string text;
  char buffer[8192];
  int count;
  while ((count = recv(fd, buffer, sizeof(buffer), 0)) > 0) {
    text.append(buffer, count);
    const auto end = text.find('\n');
    if (end != std::string::npos) {
      return text.substr(0, end);
    }
  }
  throw std::runtime_error("Session disconnected before answering");
}

inline bool socket_nonblocking(Socket fd)
{
#ifdef _WIN32
  u_long enabled = 1;
  return ioctlsocket(fd, FIONBIO, &enabled) == 0;
#else
  return fcntl(fd, F_SETFL, O_NONBLOCK) == 0;
#endif
}

inline bool socket_would_block()
{
#ifdef _WIN32
  return WSAGetLastError() == WSAEWOULDBLOCK;
#else
#  if EAGAIN != EWOULDBLOCK
  if (errno == EWOULDBLOCK) {
    return true;
  }
#  endif
  return errno == EAGAIN || errno == EINTR;
#endif
}
}  // namespace blender::agent
