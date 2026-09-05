/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

#include <cstring>
#include <stdexcept>
#include <string>

#ifdef _WIN32
#  include <afunix.h>
#  include <winsock2.h>
#else
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
  if (path.size() >= sizeof(address.sun_path)) {
    throw std::runtime_error("Session socket path is too long");
  }
  memcpy(address.sun_path, path.c_str(), path.size() + 1);
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
  char c;
  while (recv(fd, &c, 1, 0) == 1) {
    if (c == '\n') {
      return text;
    }
    text += c;
    if (text.size() > 16 * 1024 * 1024) {
      throw std::runtime_error("Session message exceeds 16 MiB");
    }
  }
  throw std::runtime_error("Session disconnected before answering");
}
}  // namespace blender::agent
