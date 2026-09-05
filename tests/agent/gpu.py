# SPDX-FileCopyrightText: 2026 blender-cli Authors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Detect absent native devices, not render failures. CTest reserves exit 77 for skips."""

import ctypes
import sys


def require_device():
    reason = None
    if sys.platform == "darwin":
        metal = ctypes.CDLL("/System/Library/Frameworks/Metal.framework/Metal")
        metal.MTLCreateSystemDefaultDevice.restype = ctypes.c_void_p
        if not metal.MTLCreateSystemDefaultDevice():
            reason = "Metal MTLCreateSystemDefaultDevice returned no device"
    elif sys.platform == "win32":
        try:
            vulkan = ctypes.WinDLL("vulkan-1.dll")
        except OSError as error:
            if error.winerror != 126:
                raise
            reason = "Vulkan loader vulkan-1.dll is not installed"
        else:
            class InstanceInfo(ctypes.Structure):
                _fields_ = [("type", ctypes.c_uint32), ("next", ctypes.c_void_p),
                            ("flags", ctypes.c_uint32), ("app", ctypes.c_void_p),
                            ("layer_count", ctypes.c_uint32), ("layers", ctypes.c_void_p),
                            ("extension_count", ctypes.c_uint32), ("extensions", ctypes.c_void_p)]

            instance = ctypes.c_void_p()
            vulkan.vkCreateInstance.argtypes = [ctypes.POINTER(InstanceInfo), ctypes.c_void_p,
                                              ctypes.POINTER(ctypes.c_void_p)]
            status = vulkan.vkCreateInstance(ctypes.byref(InstanceInfo(type=1)), None,
                                             ctypes.byref(instance))
            if status == -9:  # VK_ERROR_INCOMPATIBLE_DRIVER: loader has no usable ICD.
                reason = "Vulkan loader reports VK_ERROR_INCOMPATIBLE_DRIVER (no usable ICD)"
            else:
                assert status == 0, ("vkCreateInstance", status)
                try:
                    count = ctypes.c_uint32()
                    vulkan.vkEnumeratePhysicalDevices.argtypes = [ctypes.c_void_p,
                                                                 ctypes.POINTER(ctypes.c_uint32),
                                                                 ctypes.c_void_p]
                    status = vulkan.vkEnumeratePhysicalDevices(instance, ctypes.byref(count), None)
                    assert status == 0, ("vkEnumeratePhysicalDevices", status)
                    if not count.value:
                        reason = "Vulkan enumerated zero physical devices"
                finally:
                    vulkan.vkDestroyInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
                    vulkan.vkDestroyInstance(instance, None)
    if reason:
        print("SKIP: " + reason + "; offscreen observation/comparison remains unverified", flush=True)
        raise SystemExit(77)


if __name__ == "__main__":
    require_device()
