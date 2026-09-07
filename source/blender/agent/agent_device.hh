/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

#include <string>

#ifdef _WIN32
#  ifndef NOMINMAX
#    define NOMINMAX
#  endif
#  include <windows.h>
#else
#  include <dlfcn.h>
#endif

#ifndef __APPLE__
#  include <vulkan/vulkan_core.h>
#endif

namespace blender::agent {

/* Whether this machine can render at all, answered once, before anything asks
 * the GPU for a context.
 *
 * Blender's offscreen initialisation falls back to a platform GL context when
 * its backend is unavailable, and on a Windows host with no Vulkan ICD that
 * fallback dereferences an extension string it never obtained and takes the
 * process down. A crashed process cannot answer, so the agent loses the
 * session, the request and the reason together. Probing through the loader
 * instead costs one instance create at startup and turns "no device" into an
 * ordinary error that `exec`, `program` and `describe` do not even notice. */
struct Device {
  std::string name;   /* "vulkan", "metal", or empty when there is none. */
  std::string reason; /* Why there is none; empty when there is one. */

  explicit operator bool() const
  {
    return !name.empty();
  }
};

#ifndef __APPLE__
inline Device device_probe_vulkan()
{
#  ifdef _WIN32
  HMODULE loader = LoadLibraryA("vulkan-1.dll");
  auto symbol = [&](const char *name) {
    return loader ? reinterpret_cast<void *>(GetProcAddress(loader, name)) : nullptr;
  };
#  else
  void *loader = dlopen("libvulkan.so.1", RTLD_NOW | RTLD_LOCAL);
  auto symbol = [&](const char *name) { return loader ? dlsym(loader, name) : nullptr; };
#  endif
  if (!loader) {
    return {"", "the Vulkan loader is not installed"};
  }
  auto create = reinterpret_cast<PFN_vkCreateInstance>(symbol("vkCreateInstance"));
  auto enumerate = reinterpret_cast<PFN_vkEnumeratePhysicalDevices>(
      symbol("vkEnumeratePhysicalDevices"));
  auto destroy = reinterpret_cast<PFN_vkDestroyInstance>(symbol("vkDestroyInstance"));
  if (!create || !enumerate || !destroy) {
    return {"", "the Vulkan loader is present but incomplete"};
  }
  VkApplicationInfo application{};
  application.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO;
  application.pApplicationName = "blender-cli";
  application.apiVersion = VK_API_VERSION_1_0;
  VkInstanceCreateInfo info{};
  info.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO;
  info.pApplicationInfo = &application;
  VkInstance instance = VK_NULL_HANDLE;
  if (create(&info, nullptr, &instance) != VK_SUCCESS) {
    return {"", "no Vulkan driver could create an instance"};
  }
  uint32_t count = 0;
  const VkResult enumerated = enumerate(instance, &count, nullptr);
  destroy(instance, nullptr);
  if (enumerated != VK_SUCCESS && enumerated != VK_INCOMPLETE) {
    return {"", "the Vulkan instance reported no usable driver"};
  }
  if (count == 0) {
    return {"", "the Vulkan loader found no physical device"};
  }
  return {"vulkan", ""};
}
#endif

/* Computed on first use and kept: the answer cannot change within a process. */
inline const Device &device()
{
  static const Device probed = [] {
#ifdef __APPLE__
    /* Metal is part of the OS on every machine this build targets. */
    return Device{"metal", ""};
#else
    return device_probe_vulkan();
#endif
  }();
  return probed;
}
}  // namespace blender::agent
