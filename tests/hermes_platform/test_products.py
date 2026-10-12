from __future__ import annotations

import subprocess

import pytest

from hermes_platform.host import facts, products
from hermes_platform.host.facts import parse_nvidia_gpu_model

_N1X_CPU = f"NVIDIA {products._RTX_SPARK_CPU_MARKER}"
_GB10_INFORMATION = "Model: \t\t NVIDIA GB10\nIRQ:   \t\t 196\nGPU UUID: \t GPU-0\n"


@pytest.mark.parametrize(
    ("os_family", "native_arch", "cpu_model", "gpu_class", "gpu_information", "expected"),
    [
        ("darwin", "arm64", "Apple M3 Max", "apple_silicon", "", None),
        ("win32", "amd64", "AMD Ryzen 9 7950X", "nvidia", "", "discrete"),
        ("win32", "arm64", _N1X_CPU, "nvidia", "", "rtx_spark"),
        ("win32", "arm64", _N1X_CPU, "unknown", "", "rtx_spark"),
        ("win32", "arm64", "Snapdragon X Elite", "none", "", None),
        ("linux", "arm64", "", "nvidia", _GB10_INFORMATION, "dgx_spark"),
        ("linux", "arm64", "NVIDIA DGX Spark", "nvidia", "", "dgx_spark"),
        ("linux", "arm64", "", "nvidia", "Model: \t\t NVIDIA GH200 480GB\n", "discrete"),
        ("linux", "amd64", "Intel Xeon", "nvidia", "Model: \t\t NVIDIA GeForce RTX 4090\n", "discrete"),
        ("linux", "amd64", "Intel Xeon", "none", "", None),
    ],
)
def test_nvidia_platform_from_host_facts(os_family, native_arch, cpu_model, gpu_class, gpu_information, expected):
    names = tuple(filter(None, [parse_nvidia_gpu_model(gpu_information)]))
    assert products.classify_nvidia_platform(
        os_family=os_family, native_arch=native_arch, cpu_model=cpu_model, gpu_class=gpu_class,
        nvidia_gpu_names=names) == expected


def test_nvidia_platform_spawns_no_process(monkeypatch):
    facts.clear_caches()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("nvidia_platform spawned a process"))
    assert products.nvidia_platform() in ("rtx_spark", "dgx_spark", "discrete", None)
    facts.clear_caches()
