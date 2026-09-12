"""Graphics devices this machine can offer to local model servers."""

import collections
import ctypes
import ctypes.util
import os
import sys


VK_SUCCESS = 0
VK_STRUCTURE_TYPE_APPLICATION_INFO = 0
VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO = 1
VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2 = 1000059001
VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ID_PROPERTIES = 1000071004
VK_API_VERSION_1_0 = 1 << 22
VK_API_VERSION_1_1 = VK_API_VERSION_1_0 | (1 << 12)
VK_MEMORY_HEAP_DEVICE_LOCAL_BIT = 0x00000001
VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU = 1
VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU = 2
VK_PHYSICAL_DEVICE_TYPE_VIRTUAL_GPU = 3
VK_PHYSICAL_DEVICE_TYPE_CPU = 4
VK_MAX_MEMORY_TYPES = 32
VK_MAX_MEMORY_HEAPS = 16


class VkApplicationInfo(ctypes.Structure):
    _fields_ = [
        ("sType", ctypes.c_uint32),
        ("pNext", ctypes.c_void_p),
        ("pApplicationName", ctypes.c_char_p),
        ("applicationVersion", ctypes.c_uint32),
        ("pEngineName", ctypes.c_char_p),
        ("engineVersion", ctypes.c_uint32),
        ("apiVersion", ctypes.c_uint32),
    ]


class VkInstanceCreateInfo(ctypes.Structure):
    _fields_ = [
        ("sType", ctypes.c_uint32),
        ("pNext", ctypes.c_void_p),
        ("flags", ctypes.c_uint32),
        ("pApplicationInfo", ctypes.POINTER(VkApplicationInfo)),
        ("enabledLayerCount", ctypes.c_uint32),
        ("ppEnabledLayerNames", ctypes.c_void_p),
        ("enabledExtensionCount", ctypes.c_uint32),
        ("ppEnabledExtensionNames", ctypes.c_void_p),
    ]


class VkMemoryType(ctypes.Structure):
    _fields_ = [("propertyFlags", ctypes.c_uint32),
                ("heapIndex", ctypes.c_uint32)]


class VkMemoryHeap(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint64), ("flags", ctypes.c_uint32)]


class VkPhysicalDeviceMemoryProperties(ctypes.Structure):
    _fields_ = [
        ("memoryTypeCount", ctypes.c_uint32),
        ("memoryTypes", VkMemoryType * VK_MAX_MEMORY_TYPES),
        ("memoryHeapCount", ctypes.c_uint32),
        ("memoryHeaps", VkMemoryHeap * VK_MAX_MEMORY_HEAPS),
    ]


class VkPhysicalDeviceIDProperties(ctypes.Structure):
    _fields_ = [
        ("sType", ctypes.c_uint32),
        ("pNext", ctypes.c_void_p),
        ("deviceUUID", ctypes.c_uint8 * 16),
        ("driverUUID", ctypes.c_uint8 * 16),
        ("deviceLUID", ctypes.c_uint8 * 8),
        ("deviceNodeMask", ctypes.c_uint32),
        ("deviceLUIDValid", ctypes.c_uint32),
    ]


class VkPhysicalDeviceProperties2(ctypes.Structure):
    # The prefix is fixed and all that is read here. The generously sized byte
    # array gives Vulkan room for the complete VkPhysicalDeviceProperties body
    # without copying its large limits table into this module.
    _fields_ = [
        ("sType", ctypes.c_uint32),
        ("pNext", ctypes.c_void_p),
        ("properties", ctypes.c_uint8 * 4096),
    ]

GraphicsDevice = collections.namedtuple(
    "GraphicsDevice", "name memory shared identifier backend_index",
    defaults=("", None),
)


def cpu_threads():
    """Logical CPUs available to this process, with a portable fallback."""
    try:
        available = len(os.sched_getaffinity(0))
    except (AttributeError, OSError, NotImplementedError):
        available = os.cpu_count() or 1
    return max(1, available)


def graphics_devices():
    """Vulkan graphics devices on Linux; no guess on other systems."""
    if not sys.platform.startswith("linux"):
        return ()
    return _vulkan_devices()


def match_backend_devices(devices, backend_names):
    """Attach GGML's reported ordinals to unambiguous Vulkan identities."""
    matched = []
    for backend_index, backend_name in enumerate(backend_names):
        candidates = [
            device for device in devices
            if (backend_name == device.name
                or backend_name.startswith(device.name + " ("))
        ]
        if len(candidates) == 1 and candidates[0].identifier:
            matched.append(candidates[0]._replace(backend_index=backend_index))
    identifiers = collections.Counter(device.identifier for device in matched)
    return tuple(
        device for device in matched if identifiers[device.identifier] == 1
    )



def _vulkan_library():
    path = ctypes.util.find_library("vulkan")
    if not path:
        return None
    try:
        return ctypes.CDLL(path)
    except OSError:
        return None


def _vulkan_devices():
    library = _vulkan_library()
    if library is None:
        return ()
    try:
        raw_devices = _enumerate_vulkan(library)
    except Exception:
        return ()
    identifiers = collections.Counter(
        row[3] for row in raw_devices if row[3]
    )
    devices = []
    for name, device_type, heaps, identifier in raw_devices:
        device = _vulkan_device(
            name, device_type, heaps,
            identifier=identifier if identifiers[identifier] == 1 else "",
        )
        if device is not None:
            devices.append(device)
    return tuple(devices)


def _vulkan_api_version(library):
    query = getattr(library, "vkEnumerateInstanceVersion", None)
    if query is None:
        return VK_API_VERSION_1_0
    query.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
    query.restype = ctypes.c_int32
    version = ctypes.c_uint32(VK_API_VERSION_1_0)
    if query(ctypes.byref(version)) != VK_SUCCESS:
        return VK_API_VERSION_1_0
    return VK_API_VERSION_1_1 if version.value >= VK_API_VERSION_1_1 \
        else VK_API_VERSION_1_0


def _vulkan_identifier(library, handle, api_version):
    if api_version < VK_API_VERSION_1_1:
        return ""
    query = getattr(library, "vkGetPhysicalDeviceProperties2", None)
    if query is None:
        return ""
    query.argtypes = [ctypes.c_void_p, ctypes.POINTER(VkPhysicalDeviceProperties2)]
    query.restype = None
    identifier = VkPhysicalDeviceIDProperties(
        sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ID_PROPERTIES,
    )
    properties = VkPhysicalDeviceProperties2(
        sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2,
        pNext=ctypes.cast(ctypes.pointer(identifier), ctypes.c_void_p),
    )
    query(handle, ctypes.byref(properties))
    value = bytes(identifier.deviceUUID)
    return f"vulkan:{value.hex()}" if any(value) else ""


def _enumerate_vulkan(library):
    """Raw Vulkan device properties in the loader's enumeration order."""
    library.vkCreateInstance.argtypes = [
        ctypes.POINTER(VkInstanceCreateInfo), ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    library.vkCreateInstance.restype = ctypes.c_int32
    library.vkDestroyInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    library.vkDestroyInstance.restype = None
    library.vkEnumeratePhysicalDevices.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    library.vkEnumeratePhysicalDevices.restype = ctypes.c_int32
    library.vkGetPhysicalDeviceProperties.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    library.vkGetPhysicalDeviceProperties.restype = None
    library.vkGetPhysicalDeviceMemoryProperties.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(VkPhysicalDeviceMemoryProperties),
    ]
    library.vkGetPhysicalDeviceMemoryProperties.restype = None

    api_version = _vulkan_api_version(library)
    app = VkApplicationInfo(
        sType=VK_STRUCTURE_TYPE_APPLICATION_INFO,
        pApplicationName=b"Dikte",
        apiVersion=api_version,
    )
    create = VkInstanceCreateInfo(
        sType=VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,
        pApplicationInfo=ctypes.pointer(app),
    )
    instance = ctypes.c_void_p()
    if library.vkCreateInstance(
            ctypes.byref(create), None, ctypes.byref(instance)) != VK_SUCCESS:
        return []

    try:
        count = ctypes.c_uint32()
        if library.vkEnumeratePhysicalDevices(
                instance, ctypes.byref(count), None) != VK_SUCCESS or not count.value:
            return []
        handles = (ctypes.c_void_p * count.value)()
        if library.vkEnumeratePhysicalDevices(
                instance, ctypes.byref(count), handles) != VK_SUCCESS:
            return []

        found = []
        for handle in handles[:count.value]:
            properties = ctypes.create_string_buffer(4096)
            library.vkGetPhysicalDeviceProperties(handle, properties)
            device_type = ctypes.c_uint32.from_buffer(properties, 16).value
            name = properties.raw[20:276].split(b"\0", 1)[0].decode(
                "utf-8", "replace"
            )
            memory = VkPhysicalDeviceMemoryProperties()
            library.vkGetPhysicalDeviceMemoryProperties(handle, ctypes.byref(memory))
            heaps = [
                (int(memory.memoryHeaps[index].size),
                 int(memory.memoryHeaps[index].flags))
                for index in range(min(memory.memoryHeapCount, VK_MAX_MEMORY_HEAPS))
            ]
            found.append((
                name, device_type, heaps,
                _vulkan_identifier(library, handle, api_version),
            ))
        return found
    finally:
        library.vkDestroyInstance(instance, None)


def _vulkan_device(name, device_type, heaps, identifier=""):
    """One displayable Vulkan device, without host-only memory heaps."""
    if device_type == VK_PHYSICAL_DEVICE_TYPE_CPU:
        return None
    memory = sum(size for size, flags in heaps
                 if flags & VK_MEMORY_HEAP_DEVICE_LOCAL_BIT)
    shared = {
        VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU: True,
        VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU: False,
    }.get(device_type)
    return GraphicsDevice(
        name=name,
        memory=memory,
        shared=shared,
        identifier=identifier,
    )
