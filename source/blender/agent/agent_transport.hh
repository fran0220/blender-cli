/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

#include "agent_socket.hh"

#include <atomic>
#include <condition_variable>
#include <deque>
#include <memory>
#include <mutex>
#include <thread>
#include <vector>

#include <json.hpp>

namespace blender::agent {

/* No Blender or Python API is reachable from the transport thread. */
class Transport {
 public:
  struct Peer {
    Socket fd;
    std::string input, output;
  };
  struct Request {
    std::shared_ptr<Peer> peer;
    nlohmann::json message;
  };

  std::atomic<bool> cancelled{false};

 private:
  Socket listener_ = invalid_socket;
  std::thread thread_;
  std::mutex mutex_;
  std::condition_variable ready_;
  std::deque<Request> queue_;
  std::vector<std::shared_ptr<Peer>> peers_;
  nlohmann::json active_id_;
  bool stopping_ = false;

  void read_loop()
  {
    while (true) {
      fd_set readers;
      FD_ZERO(&readers);
      FD_SET(listener_, &readers);
      Socket highest = listener_;
      {
        std::lock_guard lock(mutex_);
        if (stopping_) {
          break;
        }
        for (const auto &peer : peers_) {
          FD_SET(peer->fd, &readers);
          highest = std::max(highest, peer->fd);
        }
      }
      timeval interval{0, 1000};
      if (select(int(highest + 1), &readers, nullptr, nullptr, &interval) < 0) {
        continue;
      }
      std::lock_guard lock(mutex_);
      if (FD_ISSET(listener_, &readers)) {
        Socket fd = accept(listener_, nullptr, nullptr);
        if (fd != invalid_socket) {
          if (peers_.size() >= 64 || fd >= FD_SETSIZE) {
            socket_close(fd);
          }
          else {
            peers_.push_back(std::make_shared<Peer>(Peer{fd, {}, {}}));
          }
        }
      }
      for (auto it = peers_.begin(); it != peers_.end();) {
        auto &peer = *it;
        bool closed = false;
        if (!peer->output.empty()) {
          /* Only send when writable; a slow client must not stall the main thread. */
          fd_set writers;
          FD_ZERO(&writers);
          FD_SET(peer->fd, &writers);
          timeval now{0, 0};
          if (select(int(peer->fd + 1), nullptr, &writers, nullptr, &now) > 0) {
#ifdef MSG_NOSIGNAL
            constexpr int flags = MSG_NOSIGNAL;
#else
            constexpr int flags = 0;
#endif
            int n = send(peer->fd,
                         peer->output.data(),
                         int(std::min<size_t>(peer->output.size(), 4096)),
                         flags);
            closed = n <= 0;
            if (n > 0) {
              peer->output.erase(0, n);
            }
          }
        }
        if (!closed && FD_ISSET(peer->fd, &readers)) {
          char buffer[8192];
          int n = recv(peer->fd, buffer, sizeof(buffer), 0);
          closed = n <= 0;
          if (n > 0) {
            peer->input.append(buffer, n);
            size_t end;
            while ((end = peer->input.find('\n')) != std::string::npos) {
              auto message = nlohmann::json::parse(peer->input.substr(0, end), nullptr, false);
              peer->input.erase(0, end + 1);
              if (!message.is_object() || !message.contains("id")) {
                peer->output +=
                    "{\"id\":null,\"result\":{\"ok\":false,\"error\":{\"type\":\"ProtocolError\"}}"
                    "}\n";
              }
              else if (message.value("cancel", nlohmann::json(false)) == true) {
                if (!active_id_.is_null() && message["id"] == active_id_) {
                  cancelled = true;
                }
              }
              else {
                queue_.push_back({peer, std::move(message)});
                ready_.notify_one();
              }
            }
            closed |= peer->input.size() > 16 * 1024 * 1024;
          }
        }
        if (closed) {
          socket_close(peer->fd);
          peer->fd = invalid_socket;
          it = peers_.erase(it);
        }
        else {
          ++it;
        }
      }
    }
  }

 public:
  explicit Transport(const std::string &path)
  {
    socket_init();
    const auto address = socket_address(path);
    listener_ = socket(AF_UNIX, SOCK_STREAM, 0);
    if (listener_ == invalid_socket ||
        bind(listener_, reinterpret_cast<const sockaddr *>(&address), sizeof(address)) != 0 ||
        listen(listener_, 64) != 0)
    {
      if (listener_ != invalid_socket) {
        socket_close(listener_);
      }
      throw std::runtime_error("Cannot bind session socket");
    }
    thread_ = std::thread([this] { read_loop(); });
  }

  bool next(Request &request)
  {
    std::unique_lock lock(mutex_);
    ready_.wait_for(lock, std::chrono::milliseconds(10), [this] { return !queue_.empty(); });
    if (queue_.empty()) {
      return false;
    }
    request = std::move(queue_.front());
    queue_.pop_front();
    active_id_ = request.message["id"];
    cancelled = false;
    return true;
  }

  void answer(const Request &request, const nlohmann::json &result)
  {
    std::lock_guard lock(mutex_);
    request.peer->output +=
        nlohmann::json({{"id", request.message["id"]}, {"result", result}}).dump() + "\n";
    active_id_ = nullptr;
  }

  ~Transport()
  {
    /* Give the close response a bounded opportunity to drain. */
    for (int i = 0; i < 100; i++) {
      {
        std::lock_guard lock(mutex_);
        bool pending = false;
        for (auto &peer : peers_) {
          pending |= !peer->output.empty();
        }
        if (!pending) {
          break;
        }
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    {
      std::lock_guard lock(mutex_);
      stopping_ = true;
    }
    thread_.join();
    for (auto &peer : peers_) {
      socket_close(peer->fd);
    }
    socket_close(listener_);
  }
};
}  // namespace blender::agent
