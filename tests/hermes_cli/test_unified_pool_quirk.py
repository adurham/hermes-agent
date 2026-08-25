"""The unified-pool vendor quirk must never misclassify a discrete card.

Spark-class devices (RTX Spark N1X, measured): nvidia-smi answers from a
16 GiB WDDM carve-out while the CUDA allocator addresses the whole
45.4 GiB unified pool at full bandwidth — decode stays ~200 GB/s
effective 8 GiB past the carve-out. Budgeting from smi there produces
false "larger than your GPU memory" rows and -ot CPU pinning measured
2.3x SLOWER than letting the allocator place everything.

The other direction is the regression this file guards: a workstation
card must never be budgeted as UMA. The driver's INTEGRATED attribute
decides when readable (both directions); the attribute-less engine
fallback needs two independent numeric gates, each alone unmeetable by
any discrete card."""

from __future__ import annotations

import hermes_cli.local_runtime.hardware as hw

GIB = 1 << 30

# Measured Spark N1X shape.
SPARK_SMI_TOTAL = 16320 << 20
SPARK_POOL = 46464 << 20
SPARK_RAM = 48 * GIB


def _no_cache(monkeypatch):
    monkeypatch.setattr(hw, "_pool_probe_cache", None)


# ── _unified_pool_bytes: the classification gate ─────────────


def test_integrated_attribute_wins_positive(monkeypatch):
    """Driver says integrated=True -> unified, no numeric gates needed."""
    monkeypatch.setattr(hw, "_device_pool_view", lambda: (SPARK_POOL, True))
    assert hw._unified_pool_bytes(SPARK_SMI_TOTAL, SPARK_RAM) == SPARK_POOL


def test_integrated_attribute_wins_negative(monkeypatch):
    """Driver says integrated=False -> discrete, even when the numbers
    would pass both fallback gates (attribute outranks arithmetic)."""
    monkeypatch.setattr(hw, "_device_pool_view", lambda: (SPARK_POOL, False))
    assert hw._unified_pool_bytes(SPARK_SMI_TOTAL, SPARK_RAM) is None


def test_engine_fallback_spark_shape_passes(monkeypatch):
    """Attribute unreadable (engine fallback): the measured Spark shape
    passes both gates — 2.85x disagreement, pool ~= RAM."""
    monkeypatch.setattr(hw, "_device_pool_view", lambda: (SPARK_POOL, None))
    assert hw._unified_pool_bytes(SPARK_SMI_TOTAL, SPARK_RAM) == SPARK_POOL


def test_discrete_card_agreeing_within_rounding_stays_discrete(monkeypatch):
    """A healthy discrete card: allocator and smi agree within rounding.
    Fails the disagreement gate regardless of box RAM."""
    smi = 24 * GIB
    monkeypatch.setattr(hw, "_device_pool_view",
                        lambda: (smi + (200 << 20), None))
    assert hw._unified_pool_bytes(smi, 24 * GIB) is None
    assert hw._unified_pool_bytes(smi, 256 * GIB) is None


def test_ram_matched_workstation_card_stays_discrete(monkeypatch):
    """THE case commit 6f5ccf16d7 feared: a 48 GB card in a 48 GB box.
    smi and the allocator AGREE (both say 48), so the disagreement gate
    fails even though pool == RAM would pass the size gate."""
    monkeypatch.setattr(hw, "_device_pool_view", lambda: (48 * GIB, None))
    assert hw._unified_pool_bytes(48 * GIB, 48 * GIB) is None


def test_pool_smaller_than_ram_fraction_stays_discrete(monkeypatch):
    """Disagreement without the RAM-sized-pool signature stays discrete:
    a hypothetical card whose allocator over-reports 2x in a huge-RAM box
    is a driver bug to distrust, not a unified pool."""
    monkeypatch.setattr(hw, "_device_pool_view", lambda: (32 * GIB, None))
    assert hw._unified_pool_bytes(16 * GIB, 128 * GIB) is None


def test_no_probe_available_stays_discrete(monkeypatch):
    """No driver API, no engine binary -> exactly today's behavior."""
    monkeypatch.setattr(hw, "_device_pool_view", lambda: None)
    assert hw._unified_pool_bytes(SPARK_SMI_TOTAL, SPARK_RAM) is None


# ── probe_budget wiring ──────────────────────────────────────


def _spark_machine(monkeypatch, *, view):
    _no_cache(monkeypatch)
    monkeypatch.setattr(hw, "_nvidia_vram",
                        lambda: (SPARK_SMI_TOTAL, 14848 << 20))
    monkeypatch.setattr(hw, "_ram_bytes",
                        lambda: (SPARK_RAM, 32 * GIB))
    monkeypatch.setattr(hw, "_device_pool_view", lambda: view)


def test_budget_unified_pool_planning(monkeypatch):
    """Planning budget on Spark: the allocator pool itself minus headroom,
    uma=True, ram_available=0 — host memory must not double-count as
    spill room. NO OS-RAM clamp: in 32/32 carve mode the carved half
    vanishes from GlobalMemoryStatusEx, so clamping to OS RAM threw away
    exactly the carved capacity (measured: pool ~46.3 GiB constant across
    carve settings while OS RAM read 46.2 then 30.2)."""
    _spark_machine(monkeypatch, view=(SPARK_POOL, True))
    b = hw.probe_budget(planning=True)
    assert b.uma is True
    assert b.ram_available_bytes == 0
    assert b.total_device_bytes == SPARK_POOL
    assert b.usable_vram_bytes == int(SPARK_POOL * (1 - hw._UMA_HEADROOM_FRACTION))
    # The whole point: the budget must dwarf the carve-out.
    assert b.usable_vram_bytes > 2 * SPARK_SMI_TOTAL


def test_budget_unified_pool_live_counts_dedicated_free_plus_os_available(monkeypatch):
    """Live budget = smi-free + OS-available (each side alone under-counts:
    smi free saturates at the carve-out, OS-available can't see it)."""
    _spark_machine(monkeypatch, view=(SPARK_POOL, True))
    b = hw.probe_budget(planning=False)
    assert b.uma is True
    live = (14848 << 20) + 32 * GIB
    assert b.usable_vram_bytes == int(min(SPARK_POOL, live)
                                      * (1 - hw._UMA_HEADROOM_FRACTION))


def test_budget_unified_no_smi_still_classifies(monkeypatch):
    """nvidia-smi off PATH must not change the verdict: the driver API
    (system loader, PATH-independent) still classifies unified, planning
    still budgets the full pool, and live falls back to OS-available."""
    _no_cache(monkeypatch)
    monkeypatch.setattr(hw, "_nvidia_vram", lambda: None)
    monkeypatch.setattr(hw, "_ram_bytes", lambda: (SPARK_RAM, 32 * GIB))
    monkeypatch.setattr(hw, "_device_pool_view", lambda: (SPARK_POOL, True))
    planning = hw.probe_budget(planning=True)
    assert planning.uma is True
    assert planning.total_device_bytes == SPARK_POOL
    assert planning.usable_vram_bytes == int(
        SPARK_POOL * (1 - hw._UMA_HEADROOM_FRACTION))
    live = hw.probe_budget(planning=False)
    assert live.usable_vram_bytes == int(32 * GIB * (1 - hw._UMA_HEADROOM_FRACTION))


def test_budget_discrete_unchanged_when_probe_says_discrete(monkeypatch):
    """integrated=False keeps the existing discrete path bit-for-bit."""
    _spark_machine(monkeypatch, view=(SPARK_POOL, False))
    b = hw.probe_budget(planning=True)
    assert b.uma is False
    assert b.total_device_bytes == SPARK_SMI_TOTAL
    margin = max(hw._MARGIN_FLOOR, int(SPARK_SMI_TOTAL * hw._MARGIN_FRACTION))
    assert b.usable_vram_bytes == SPARK_SMI_TOTAL - margin
    assert b.ram_available_bytes == SPARK_RAM


def test_budget_discrete_unchanged_when_probe_unavailable(monkeypatch):
    _spark_machine(monkeypatch, view=None)
    b = hw.probe_budget(planning=True)
    assert b.uma is False
    assert b.ram_available_bytes == SPARK_RAM


def test_engine_fallback_without_smi_stays_conservative(monkeypatch):
    """Engine-fallback view (no INTEGRATED verdict) + no smi numbers: the
    disagreement gate has nothing to compare against, so the quirk stays
    off and budgeting falls to the conservative RAM-as-UMA path — an
    attribute-less pool claim alone must never flip the verdict."""
    _no_cache(monkeypatch)
    monkeypatch.setattr(hw, "_nvidia_vram", lambda: None)
    monkeypatch.setattr(hw, "_ram_bytes", lambda: (SPARK_RAM, 32 * GIB))
    monkeypatch.setattr(hw, "_device_pool_view", lambda: (SPARK_POOL, None))
    b = hw.probe_budget(planning=True)
    assert b.uma is True
    assert b.total_device_bytes == SPARK_RAM  # RAM path, not the pool


def test_smi_resolver_caches_and_survives_empty_path(monkeypatch):
    """The resolver consults PATH first, and a resolution (hit or miss) is
    cached for the process."""
    calls = []
    monkeypatch.setattr(hw, "_smi_path_cache", None)
    monkeypatch.setattr(hw.shutil, "which",
                        lambda name: calls.append(name) or "/usr/bin/nvidia-smi")
    assert hw._nvidia_smi_path() == "/usr/bin/nvidia-smi"
    assert hw._nvidia_smi_path() == "/usr/bin/nvidia-smi"
    assert len(calls) == 1


# ── probe cache ──────────────────────────────────────────────


def test_pool_probe_hit_is_cached_for_process(monkeypatch):
    _no_cache(monkeypatch)
    calls = []

    def probe():
        calls.append(1)
        return (SPARK_POOL, True)

    monkeypatch.setattr(hw, "_cuda_driver_pool", probe)
    monkeypatch.setattr(hw, "_engine_device_pool", lambda: None)
    assert hw._device_pool_view() == (SPARK_POOL, True)
    assert hw._device_pool_view() == (SPARK_POOL, True)
    assert len(calls) == 1


def test_pool_probe_miss_retries_after_ttl(monkeypatch):
    """A miss must not be permanent: the engine binary can appear
    mid-session via the pane's runtime install."""
    _no_cache(monkeypatch)
    monkeypatch.setattr(hw, "_cuda_driver_pool", lambda: None)
    answers = [None, (SPARK_POOL, None)]
    monkeypatch.setattr(hw, "_engine_device_pool", lambda: answers.pop(0))

    now = [1000.0]
    monkeypatch.setattr(hw.time, "monotonic", lambda: now[0])
    assert hw._device_pool_view() is None
    now[0] += 1.0          # inside TTL: cached miss, no re-probe
    assert hw._device_pool_view() is None
    now[0] += hw._POOL_NEGATIVE_TTL_S + 1.0
    assert hw._device_pool_view() == (SPARK_POOL, None)
    assert not answers


# ── device-line parsing (engine fallback) ────────────────────


def test_device_line_regex_handles_parenthesized_names():
    """Real Spark line: the device name itself contains parentheses —
    the LAST group must win."""
    line = ("  CUDA0: NVIDIA RTX Spark N1X (5120-core Blackwell RTX GPU) "
            "(46464 MiB, 46284 MiB free)")
    m = hw._DEVICE_LINE_RE.search(line)
    assert m and int(m.group(1)) == 46464
