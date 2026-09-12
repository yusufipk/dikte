"""Hardware facts shown beside local models."""

import ctypes
import unittest
from unittest import mock

from dikte import hardware


class CpuThreads(unittest.TestCase):
    def test_affinity_takes_precedence_over_total_logical_cpus(self):
        with mock.patch.object(hardware.os, "sched_getaffinity",
                               return_value={2, 4, 6}, create=True), \
                mock.patch.object(hardware.os, "cpu_count", return_value=32):
            self.assertEqual(hardware.cpu_threads(), 3)

    def test_missing_or_unusable_affinity_falls_back_to_logical_cpus(self):
        for error in (AttributeError, OSError, NotImplementedError):
            with self.subTest(error=error), \
                    mock.patch.object(hardware.os, "sched_getaffinity",
                                      side_effect=error, create=True), \
                    mock.patch.object(hardware.os, "cpu_count", return_value=8):
                self.assertEqual(hardware.cpu_threads(), 8)

    def test_unknown_cpu_count_still_allows_one_worker(self):
        with mock.patch.object(hardware.os, "sched_getaffinity",
                               side_effect=AttributeError, create=True), \
                mock.patch.object(hardware.os, "cpu_count", return_value=None):
            self.assertEqual(hardware.cpu_threads(), 1)


class VulkanCall:
    """A Python callable that accepts ctypes function metadata."""

    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class VulkanMemoryType(ctypes.Structure):
    _fields_ = [("propertyFlags", ctypes.c_uint32),
                ("heapIndex", ctypes.c_uint32)]


class VulkanMemoryHeap(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint64), ("flags", ctypes.c_uint32)]


class VulkanMemoryProperties(ctypes.Structure):
    _fields_ = [
        ("memoryTypeCount", ctypes.c_uint32),
        ("memoryTypes", VulkanMemoryType * 32),
        ("memoryHeapCount", ctypes.c_uint32),
        ("memoryHeaps", VulkanMemoryHeap * 16),
    ]


class VulkanIDProperties(ctypes.Structure):
    _fields_ = [
        ("sType", ctypes.c_uint32),
        ("pNext", ctypes.c_void_p),
        ("deviceUUID", ctypes.c_uint8 * 16),
        ("driverUUID", ctypes.c_uint8 * 16),
        ("deviceLUID", ctypes.c_uint8 * 8),
        ("deviceNodeMask", ctypes.c_uint32),
        ("deviceLUIDValid", ctypes.c_uint32),
    ]


class VulkanProperties2(ctypes.Structure):
    _fields_ = [
        ("sType", ctypes.c_uint32),
        ("pNext", ctypes.c_void_p),
        ("properties", ctypes.c_uint8 * 4096),
    ]


class FakeVulkan:
    """The Vulkan 1.0 calls used by the probe, with one discrete card."""

    def __init__(self):
        self.destroyed = False
        self.vkCreateInstance = VulkanCall(self.create_instance)
        self.vkDestroyInstance = VulkanCall(self.destroy_instance)
        self.vkEnumeratePhysicalDevices = VulkanCall(self.enumerate_devices)
        self.vkGetPhysicalDeviceProperties = VulkanCall(self.device_properties)
        self.vkGetPhysicalDeviceMemoryProperties = VulkanCall(self.memory_properties)

    @staticmethod
    def create_instance(_create, _allocator, output):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(11)
        return 0

    def destroy_instance(self, _instance, _allocator):
        self.destroyed = True

    @staticmethod
    def enumerate_devices(_instance, count, devices):
        ctypes.cast(count, ctypes.POINTER(ctypes.c_uint32))[0] = 1
        if devices is not None:
            devices[0] = ctypes.c_void_p(22)
        return 0

    @staticmethod
    def device_properties(_device, output):
        raw = bytearray(4096)
        raw[16:20] = hardware.VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU.to_bytes(
            4, byteorder="little"
        )
        name = b"NVIDIA GeForce RTX 5070"
        raw[20:20 + len(name)] = name
        ctypes.memmove(output, bytes(raw), len(raw))

    @staticmethod
    def memory_properties(_device, output):
        memory = VulkanMemoryProperties()
        memory.memoryHeapCount = 2
        memory.memoryHeaps[0].size = 32 << 30
        memory.memoryHeaps[0].flags = 0
        memory.memoryHeaps[1].size = 12 << 30
        memory.memoryHeaps[1].flags = hardware.VK_MEMORY_HEAP_DEVICE_LOCAL_BIT
        ctypes.memmove(output, ctypes.byref(memory), ctypes.sizeof(memory))


class FakeVulkan11(FakeVulkan):
    UUID = bytes.fromhex("00112233445566778899aabbccddeeff")

    def __init__(self):
        super().__init__()
        self.vkEnumerateInstanceVersion = VulkanCall(self.instance_version)
        self.vkGetPhysicalDeviceProperties2 = VulkanCall(self.device_properties2)

    @staticmethod
    def instance_version(output):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_uint32))[0] = 0x00401000
        return 0

    @classmethod
    def device_properties2(cls, _device, output):
        properties = ctypes.cast(
            output, ctypes.POINTER(VulkanProperties2)
        ).contents
        identifier = ctypes.cast(
            properties.pNext, ctypes.POINTER(VulkanIDProperties)
        ).contents
        for index, value in enumerate(cls.UUID):
            identifier.deviceUUID[index] = value


class VulkanDevices(unittest.TestCase):
    def test_a_discrete_card_reports_device_local_memory_not_the_system_heap(self):
        device = hardware._vulkan_device(
            "AMD Radeon RX 6750 XT",
            hardware.VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU,
            [(16 << 30, 0), (12 << 30, hardware.VK_MEMORY_HEAP_DEVICE_LOCAL_BIT)],
        )
        self.assertEqual(
            device,
            hardware.GraphicsDevice(
                "AMD Radeon RX 6750 XT", 12 << 30, shared=False,
            ),
        )

    def test_an_integrated_gpu_names_its_device_local_heap_as_shared(self):
        device = hardware._vulkan_device(
            "Intel UHD Graphics",
            hardware.VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU,
            [(8 << 30, hardware.VK_MEMORY_HEAP_DEVICE_LOCAL_BIT)],
        )
        self.assertEqual(
            device,
            hardware.GraphicsDevice("Intel UHD Graphics", 8 << 30, shared=True),
        )

    def test_an_unknown_device_type_does_not_guess_its_memory_kind(self):
        device = hardware._vulkan_device(
            "Virtual graphics adapter",
            hardware.VK_PHYSICAL_DEVICE_TYPE_VIRTUAL_GPU,
            [(8 << 30, hardware.VK_MEMORY_HEAP_DEVICE_LOCAL_BIT)],
        )
        self.assertIsNotNone(device)
        self.assertIsNone(device.shared)

    def test_a_software_vulkan_processor_is_not_presented_as_a_gpu(self):
        device = hardware._vulkan_device(
            "llvmpipe",
            hardware.VK_PHYSICAL_DEVICE_TYPE_CPU,
            [(16 << 30, hardware.VK_MEMORY_HEAP_DEVICE_LOCAL_BIT)],
        )
        self.assertIsNone(device)

    def test_backend_reported_order_is_mapped_to_stable_vulkan_devices(self):
        devices = (
            hardware.GraphicsDevice("Intel UHD Graphics", 8 << 30, True,
                                    "vulkan:intel"),
            hardware.GraphicsDevice("NVIDIA GeForce RTX 5070", 12 << 30, False,
                                    "vulkan:nvidia"),
        )
        mapped = hardware.match_backend_devices(devices, (
            "NVIDIA GeForce RTX 5070 (NVIDIA proprietary)",
            "Intel UHD Graphics (Mesa Intel)",
        ))
        self.assertEqual(
            [(device.identifier, device.backend_index) for device in mapped],
            [("vulkan:nvidia", 0), ("vulkan:intel", 1)],
        )

    def test_identically_named_cards_are_not_mapped_to_the_wrong_uuid(self):
        devices = (
            hardware.GraphicsDevice("NVIDIA RTX 5070", 12 << 30, False,
                                    "vulkan:first"),
            hardware.GraphicsDevice("NVIDIA RTX 5070", 12 << 30, False,
                                    "vulkan:second"),
        )
        self.assertEqual(
            hardware.match_backend_devices(
                devices, ("NVIDIA RTX 5070 (NVIDIA proprietary)",)
            ),
            (),
        )

    def test_one_uuid_cannot_be_mapped_to_two_backend_ordinals(self):
        device = hardware.GraphicsDevice(
            "NVIDIA RTX 5070", 12 << 30, False, "vulkan:only"
        )
        self.assertEqual(
            hardware.match_backend_devices(
                (device,),
                (
                    "NVIDIA RTX 5070 (NVIDIA proprietary)",
                    "NVIDIA RTX 5070 (NVIDIA proprietary)",
                ),
            ),
            (),
        )

    def test_an_unidentified_same_name_peer_keeps_mapping_ambiguous(self):
        devices = (
            hardware.GraphicsDevice(
                "NVIDIA RTX 5070", 12 << 30, False, "vulkan:known"
            ),
            hardware.GraphicsDevice(
                "NVIDIA RTX 5070", 12 << 30, False, ""
            ),
        )
        self.assertEqual(
            hardware.match_backend_devices(
                devices, ("NVIDIA RTX 5070 (NVIDIA proprietary)",)
            ),
            (),
        )

    def test_vulkan_inventory_does_not_guess_whisper_backend_indices(self):
        raw = [
            ("llvmpipe", hardware.VK_PHYSICAL_DEVICE_TYPE_CPU,
             [(16 << 30, hardware.VK_MEMORY_HEAP_DEVICE_LOCAL_BIT)],
             "vulkan:software"),
            ("Intel UHD Graphics", hardware.VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU,
             [(8 << 30, hardware.VK_MEMORY_HEAP_DEVICE_LOCAL_BIT)],
             "vulkan:intel"),
            ("NVIDIA GeForce RTX 5070", hardware.VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU,
             [(12 << 30, hardware.VK_MEMORY_HEAP_DEVICE_LOCAL_BIT)],
             "vulkan:nvidia"),
        ]
        with mock.patch.object(hardware, "_vulkan_library", return_value=object()), \
                mock.patch.object(hardware, "_enumerate_vulkan", create=True,
                                  return_value=raw):
            devices = hardware._vulkan_devices()
        self.assertEqual(
            [(device.name, device.backend_index) for device in devices],
            [("Intel UHD Graphics", None), ("NVIDIA GeForce RTX 5070", None)],
        )

    def test_a_uuid_exposed_by_two_icds_is_not_selectable(self):
        duplicate = "vulkan:00112233445566778899aabbccddeeff"
        raw = [
            ("AMD Radeon RX 6750 XT", hardware.VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU,
             [(12 << 30, hardware.VK_MEMORY_HEAP_DEVICE_LOCAL_BIT)], duplicate),
            ("AMD Radeon RX 6750 XT", hardware.VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU,
             [(12 << 30, hardware.VK_MEMORY_HEAP_DEVICE_LOCAL_BIT)], duplicate),
        ]
        with mock.patch.object(hardware, "_vulkan_library", return_value=object()), \
                mock.patch.object(hardware, "_enumerate_vulkan", return_value=raw):
            devices = hardware._vulkan_devices()
        self.assertEqual([device.identifier for device in devices], ["", ""])

    def test_other_platforms_do_not_enter_the_linux_vulkan_probe(self):
        with mock.patch.object(hardware.sys, "platform", "darwin"), \
                mock.patch.object(hardware, "_vulkan_devices", create=True) as probe:
            self.assertEqual(hardware.graphics_devices(), ())
        probe.assert_not_called()

    def test_a_missing_vulkan_loader_is_no_graphics_information(self):
        with mock.patch.object(hardware.ctypes.util, "find_library", return_value=None):
            self.assertIsNone(hardware._vulkan_library())

    def test_a_broken_vulkan_driver_is_no_graphics_information(self):
        with mock.patch.object(hardware, "_vulkan_library", return_value=object()), \
                mock.patch.object(hardware, "_enumerate_vulkan",
                                  side_effect=RuntimeError("broken ICD")):
            self.assertEqual(hardware._vulkan_devices(), ())

    def test_vulkan_calls_return_name_type_and_device_local_heaps(self):
        library = FakeVulkan()
        devices = hardware._enumerate_vulkan(library)
        self.assertEqual(
            devices,
            [("NVIDIA GeForce RTX 5070",
              hardware.VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU,
              [(32 << 30, 0),
               (12 << 30, hardware.VK_MEMORY_HEAP_DEVICE_LOCAL_BIT)],
              "")],
        )
        self.assertTrue(library.destroyed)

    def test_vulkan_1_1_device_uuid_becomes_the_stable_identity(self):
        devices = hardware._enumerate_vulkan(FakeVulkan11())
        self.assertEqual(
            devices[0][3],
            "vulkan:00112233445566778899aabbccddeeff",
        )


if __name__ == "__main__":
    unittest.main()
