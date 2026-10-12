"""NVIDIA platform recognition derived from host facts.

Recognition uses OS-reported CPU, GPU or PCI identity without environment-variable input.
"""

from __future__ import annotations

from typing import Literal

from hermes_platform.host import facts

NvidiaPlatform = Literal["rtx_spark", "dgx_spark", "discrete"]

_RTX_SPARK_CPU_MARKER = "N1X"
_DGX_SPARK_MARKERS = ("GB10", "DGX SPARK")


def is_nvidia_n1x_pci_id(pci_id: int) -> bool:
    """Recognize N1X; NVML packs device above the 16-bit vendor ID."""
    return pci_id & 0xFFFF == 0x10DE and 0x2E00 <= pci_id >> 16 <= 0x2E3F


def classify_nvidia_platform(*, os_family: str, native_arch: str, cpu_model: str, gpu_class: str,
                             nvidia_gpu_names: tuple[str, ...]) -> NvidiaPlatform | None:
    """Return the NVIDIA platform the supplied host facts describe, or ``None`` without an NVIDIA GPU.

    - ``rtx_spark``: Windows on arm64 with an NVIDIA GPU or the N1X CPU marker. NVIDIA ships no
      Windows-on-Arm driver for a discrete card, so an NVIDIA adapter there is the N1X; a GPU in
      ``is_nvidia_n1x_pci_id``'s range is therefore always this platform.
    - ``dgx_spark``: Linux on arm64 whose NVIDIA driver names a GB10 GPU, or whose CPU or
      device-tree model names GB10 or DGX Spark. Other arm64 NVIDIA servers are ``discrete``.
    - ``discrete``: any other host with an NVIDIA GPU.
    """
    cpu = cpu_model.replace("_", " ").upper()
    if native_arch == "arm64" and os_family == "win32":
        if gpu_class == "nvidia" or _RTX_SPARK_CPU_MARKER in cpu:
            return "rtx_spark"
    if native_arch == "arm64" and os_family.startswith("linux"):
        names = " ".join(nvidia_gpu_names).upper()
        if any(marker in names or marker in cpu for marker in _DGX_SPARK_MARKERS):
            return "dgx_spark"
    return "discrete" if gpu_class == "nvidia" else None


def nvidia_platform() -> NvidiaPlatform | None:
    """The NVIDIA platform of this host; ``classify_nvidia_platform`` holds the rules.

    Reads only cached host facts (registry, sysfs, procfs, sysctl): no subprocess, no driver
    library, no network. The N1X PCI range is the one the local catalog's measured row uses. Not
    verified on hardware: the N1X CPU marker in the Windows registry, the GB10 model name in
    ``/proc/driver/nvidia/gpus/*/information``, and the DGX Spark device-tree model.
    """
    return classify_nvidia_platform(
        os_family=facts.os_family(), native_arch=facts.native_arch(), cpu_model=facts.cpu_model(),
        gpu_class=facts.gpu_class(), nvidia_gpu_names=facts.nvidia_gpu_names())


def is_nvidia_arm_soc() -> bool:
    """Return whether this host is an RTX Spark or a DGX Spark."""
    return nvidia_platform() in ("rtx_spark", "dgx_spark")
