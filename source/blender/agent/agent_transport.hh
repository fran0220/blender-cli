/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

#include "agent_socket.hh"

#include <atomic>
#include <condition_variable>
#include <cstdio>
#include <deque>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include <json.hpp>

namespace blender::agent {

/* No Blender or Python API is reachable from a transport thread: it moves and
 * parses protocol bytes, answers `cancel`, and hands requests to the main
 * thread in arrival order. */
class Channel {
 public:
  struct Peer {
    Socket fd = invalid_socket;
    std::string input, output;
    /* End of input means "no more requests", not "discard my answers": a
     * `repl` bridge closes its write side as soon as its own stdin ends. */
    bool reading = true;
    /* No request of a peer runs before the peer has been told what it joined. */
    bool greeted = false;
  };
  struct Request {
    std::shared_ptr<Peer> peer;
    nlohmann::json message;
  };

  std::atomic<bool> cancelled{false};

  virtual ~Channel() = default;

  /* Main thread: take the next request, waiting at most 10 ms. */
  bool next(Request &request)
  {
    std::unique_lock lock(mutex_);
    ready_.wait_for(lock, std::chrono::milliseconds(10), [this] { return !queue_.empty(); });
    if (queue_.empty() || !queue_.front().peer->greeted) {
      return false;
    }
    request = std::move(queue_.front());
    queue_.pop_front();
    active_id_ = request.message["id"];
    cancelled = false;
    return true;
  }

  /* Main thread: one event, written as it is produced rather than at the end. */
  void send(const Request &request, const std::string &line)
  {
    std::lock_guard lock(mutex_);
    write(*request.peer, line + "\n");
  }

  void finish(const Request &)
  {
    std::lock_guard lock(mutex_);
    active_id_ = nullptr;
    cancelled = false;
  }

  /* True once no further request can arrive (stdin closed). */
  bool ended() const
  {
    return ended_.load();
  }

  /* Peers that have joined but have not been told what they joined. */
  std::vector<std::shared_ptr<Peer>> take_ungreeted()
  {
    std::lock_guard lock(mutex_);
    for (const auto &peer : ungreeted_) {
      /* Marked on the way out, so a greeting that cannot be built does not
       * leave the peer's requests unanswerable. */
      peer->greeted = true;
    }
    return std::exchange(ungreeted_, {});
  }

  void greet(const std::shared_ptr<Peer> &peer, const std::string &line)
  {
    std::lock_guard lock(mutex_);
    write(*peer, line + "\n");
  }

 protected:
  std::mutex mutex_;
  std::condition_variable ready_;
  std::deque<Request> queue_;
  std::vector<std::shared_ptr<Peer>> ungreeted_;
  nlohmann::json active_id_;
  std::atomic<bool> ended_{false};
  bool stopping_ = false;

  /* Called with the mutex held, from either thread. */
  virtual void write(Peer &peer, const std::string &text) = 0;

  /* Transport thread, mutex held. Complete lines only; `cancel` never queues. */
  void consume(const std::shared_ptr<Peer> &peer)
  {
    size_t end;
    while ((end = peer->input.find('\n')) != std::string::npos) {
      auto message = nlohmann::json::parse(peer->input.substr(0, end), nullptr, false);
      peer->input.erase(0, end + 1);
      if (!message.is_object() || !message["id"].is_number_integer()) {
        protocol_error(
            *peer, nullptr, "Every request is a JSON object with an integer id and an op");
      }
      else if (message.value("op", std::string()) == "cancel") {
        for (const auto &field : message.items()) {
          if (field.key() != "id" && field.key() != "op" && field.key() != "target") {
            protocol_error(*peer, message["id"], "cancel accepts only: target");
            return;
          }
        }
        if (!message["target"].is_number_integer()) {
          protocol_error(*peer, message["id"], "cancel requires an integer target");
          continue;
        }
        const bool running = !active_id_.is_null() && message["target"] == active_id_;
        if (running) {
          cancelled = true;
        }
        write(*peer,
              nlohmann::json({{"id", message["id"]},
                              {"event", "done"},
                              {"ok", true},
                              {"target", message["target"]},
                              {"cancelled", running}})
                      .dump() +
                  "\n");
      }
      else {
        queue_.push_back({peer, std::move(message)});
        ready_.notify_one();
      }
    }
  }

  void protocol_error(Peer &peer, const nlohmann::json &id, const char *message)
  {
    write(peer,
          nlohmann::json({{"id", id},
                          {"event", "error"},
                          {"ok", false},
                          {"type", "ProtocolError"},
                          {"message", message}})
                  .dump() +
              "\n");
  }
};

/* The session endpoint: many clients, one request in flight. */
class SocketChannel : public Channel {
  Socket listener_ = invalid_socket;
  std::thread thread_;
  std::vector<std::shared_ptr<Peer>> peers_;

  void write(Peer &peer, const std::string &text) override
  {
    peer.output += text;
  }

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
          if (peer->reading) {
            FD_SET(peer->fd, &readers);
            highest = std::max(highest, peer->fd);
          }
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
          bool full = peers_.size() >= FD_SETSIZE - 1;
#ifndef _WIN32
          full |= fd >= FD_SETSIZE;
#endif
          if (full || !socket_nonblocking(fd)) {
            socket_close(fd);
          }
          else {
            peers_.push_back(std::make_shared<Peer>(Peer{fd, {}, {}, true, false}));
            ungreeted_.push_back(peers_.back());
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
            int n = ::send(peer->fd,
                           peer->output.data(),
                           int(std::min<size_t>(peer->output.size(), 65536)),
                           flags);
            closed = n == 0 || (n < 0 && !socket_would_block());
            if (n > 0) {
              peer->output.erase(0, n);
            }
          }
        }
        if (!closed && peer->reading && FD_ISSET(peer->fd, &readers)) {
          char buffer[8192];
          int n = recv(peer->fd, buffer, sizeof(buffer), 0);
          peer->reading = n > 0 || (n < 0 && socket_would_block());
          closed = n < 0 && !socket_would_block();
          if (n > 0) {
            peer->input.append(buffer, n);
            consume(peer);
            closed |= peer->input.size() > 16 * 1024 * 1024;
          }
        }
        /* A peer that will send nothing more is kept until its own answers
         * have drained and no queued or running request still refers to it. */
        closed |= !peer->reading && peer->output.empty() && peer.use_count() == 1;
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
  explicit SocketChannel(const std::string &path)
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

  ~SocketChannel() override
  {
    /* Give the last events a bounded opportunity to drain. */
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

/* `blender-cli repl` in this process: the same protocol on stdin/stdout. */
class StdioChannel : public Channel {
  FILE *output_;
  std::shared_ptr<Peer> peer_ = std::make_shared<Peer>();
  std::thread thread_;

  void write(Peer &, const std::string &text) override
  {
    fwrite(text.data(), 1, text.size(), output_);
    fflush(output_);
  }

  void read_loop()
  {
    char buffer[8192];
    while (fgets(buffer, sizeof(buffer), stdin)) {
      std::lock_guard lock(mutex_);
      peer_->input.append(buffer);
      consume(peer_);
    }
    ended_ = true;
    ready_.notify_one();
  }

 public:
  explicit StdioChannel(FILE *output) : output_(output)
  {
    /* The conversation exists from the start, so it is greeted before the
     * first request is read. */
    ungreeted_.push_back(peer_);
    thread_ = std::thread([this] { read_loop(); });
  }

  /* The reader blocks in fgets until stdin closes; it is detached because the
   * process exits immediately after the loop returns. */
  ~StdioChannel() override
  {
    thread_.detach();
  }
};
}  // namespace blender::agent
