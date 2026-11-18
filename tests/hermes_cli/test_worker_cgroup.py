"""hermes_cli.worker_cgroup: a worker adopts its own capped cgroup.

The mechanism under test is the one that keeps a runaway kanban/gateway worker from
OOM-killing ``hermes-gateway.service``: the worker starts as a child of the gateway,
asks the user manager to start a transient scope owning its pid, and lands in
``hermes-workers.slice`` with its own ``MemoryMax``. Everything here is hermetic -- no
systemd is contacted; the real adoption is exercised by the host verification recorded on
the card.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from hermes_cli import worker_cgroup as wc

GIB = 1024 ** 3
GATEWAY = "/user.slice/user-1000.slice/user@1000.service/app.slice/hermes-gateway.service"
SCOPE = ("/user.slice/user-1000.slice/user@1000.service/hermes.slice/hermes-workers.slice/"
         "hermes-worker-managed-1234-abcdef.scope")


def _settings(**overrides):
    settings = {
        "enabled": True,
        "slice": "hermes-workers.slice",
        "memory_max": 256 * 1024 ** 2,
        "memory_high": None,
        "oom_score_adj": 500,
    }
    settings.update(overrides)
    return settings


def _walk_properties(argv: list[str]) -> list[tuple[str, str, str]]:
    """Parse the ``a(sv)`` payload back out of the busctl argv (type-directed)."""
    start = argv.index("fail") + 1
    count = int(argv[start])
    index = start + 1
    properties: list[tuple[str, str, str]] = []
    for _ in range(count):
        name, kind = argv[index], argv[index + 1]
        index += 2
        if kind in {"au", "as"}:
            length = int(argv[index])
            index += 1
            values = list(argv[index:index + length])
            index += length
            properties.append((name, kind, ",".join(values)))
        else:
            properties.append((name, kind, argv[index]))
            index += 1
    assert index == len(argv) - 1 and argv[index] == "0", "property list must end on the empty aux array"
    return properties


def test_plan_defaults_are_bounded() -> None:
    settings = wc.plan(environ={}, physical=64 * GIB)

    assert settings["enabled"] is True
    assert settings["slice"] == "hermes-workers.slice"
    assert settings["memory_max"] == 6 * GIB
    # memory.high is opt-in: with swap exhausted it turns a runaway into an indefinite
    # reclaim throttle instead of a kill.
    assert settings["memory_high"] is None
    assert settings["oom_score_adj"] == 500


def test_plan_never_exceeds_a_quarter_of_physical_memory() -> None:
    assert wc.plan(environ={}, physical=8 * GIB)["memory_max"] == 2 * GIB


def test_plan_honours_env_overrides_and_can_be_disabled() -> None:
    tightened = wc.plan(environ={"HERMES_WORKER_MEMORY_MAX_MB": "1024",
                                 "HERMES_WORKER_MEMORY_HIGH_MB": "768",
                                 "HERMES_WORKER_SLICE": "cards.slice",
                                 "HERMES_WORKER_OOM_SCORE_ADJ": "900"}, physical=64 * GIB)
    assert (tightened["memory_max"], tightened["memory_high"]) == (1 * GIB, 768 * 1024 ** 2)
    assert tightened["slice"] == "cards.slice"
    assert tightened["oom_score_adj"] == 900

    assert wc.plan(environ={"HERMES_WORKER_CGROUP": "0"}, physical=64 * GIB) == {
        "enabled": False, "reason": "disabled by HERMES_WORKER_CGROUP"}


def test_plan_caps_oom_score_adj_at_the_kernel_maximum() -> None:
    assert wc.plan(environ={"HERMES_WORKER_OOM_SCORE_ADJ": "4000"}, physical=64 * GIB)["oom_score_adj"] == 1000


def test_start_transient_unit_properties_are_well_formed() -> None:
    properties = dict((name, value) for name, _kind, value in _walk_properties(
        wc._start_transient_unit_argv("busctl", unit="hermes-worker-managed-1-aa.scope",
                                      settings=_settings(), pid=4242)))

    assert properties["Slice"] == "hermes-workers.slice"
    assert properties["PIDs"] == "4242"
    assert properties["MemoryMax"] == str(256 * 1024 ** 2)
    assert properties["MemorySwapMax"] == "0"
    assert properties["OOMPolicy"] == "continue"
    assert properties["CollectMode"] == "inactive-or-failed"


def test_start_transient_unit_adds_memory_high_only_when_asked() -> None:
    without = {name for name, _kind, _value in _walk_properties(
        wc._start_transient_unit_argv("busctl", unit="u.scope", settings=_settings(), pid=1))}
    with_high = {name for name, _kind, _value in _walk_properties(
        wc._start_transient_unit_argv("busctl", unit="u.scope",
                                      settings=_settings(memory_high=200 * 1024 ** 2), pid=1))}

    assert "MemoryHigh" not in without
    assert "MemoryHigh" in with_high


def test_adopt_skips_when_not_inside_a_systemd_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wc, "own_cgroup_path", lambda: "/user.slice/user-1000.slice/session-2.scope")
    monkeypatch.setattr(wc.subprocess, "run",
                        lambda *a, **k: pytest.fail("no bus call outside a systemd service"))

    info = wc.adopt_worker_cgroup(_settings())

    assert info["status"] == "skipped"
    assert "not inside a systemd service" in info["reason"]


def test_adopt_is_idempotent_for_an_already_scoped_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wc, "own_cgroup_path", lambda: SCOPE)

    info = wc.adopt_worker_cgroup(_settings())

    assert info["status"] == "skipped"
    assert info["reason"].startswith("already in hermes-worker-managed-")


def test_adopt_skips_without_busctl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wc, "own_cgroup_path", lambda: GATEWAY)
    monkeypatch.setattr(wc.shutil, "which", lambda _name: None)

    assert wc.adopt_worker_cgroup(_settings())["status"] == "skipped"


def test_adopt_reports_a_refused_scope_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wc, "own_cgroup_path", lambda: GATEWAY)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, b"", b"Failed to connect to bus: No such file")

    monkeypatch.setattr(wc.subprocess, "run", fake_run)

    info = wc.adopt_worker_cgroup(_settings(), busctl="/usr/bin/busctl")

    assert info["status"] == "failed"
    assert "Failed to connect to bus" in info["reason"]
    assert info["unit"].startswith("hermes-worker-managed-")
    assert info["path"] == GATEWAY


def test_adopt_reports_a_scope_that_never_owns_the_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wc, "own_cgroup_path", lambda: GATEWAY)
    monkeypatch.setattr(wc.subprocess, "run",
                        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b'o "/job/1"\n', b""))
    monkeypatch.setattr(wc.time, "sleep", lambda _seconds: None)

    info = wc.adopt_worker_cgroup(_settings(), busctl="/usr/bin/busctl", deadline=0.0)

    assert info["status"] == "failed"
    assert "never migrated" in info["reason"]


def test_adopt_moves_the_charge_and_raises_the_oom_score(monkeypatch: pytest.MonkeyPatch) -> None:
    paths = iter([GATEWAY, GATEWAY, SCOPE])
    monkeypatch.setattr(wc, "own_cgroup_path", lambda: next(paths, SCOPE))
    monkeypatch.setattr(wc, "unit_name", lambda pid=None: "hermes-worker-managed-1234-abcdef.scope")
    monkeypatch.setattr(wc.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(wc, "_memory_max_of", lambda path: 256 * 1024 ** 2)
    monkeypatch.setattr(wc, "_set_oom_score_adj", lambda value: value)
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(argv, 0, b'o "/org/freedesktop/systemd1/job/7"\n', b"")

    monkeypatch.setattr(wc.subprocess, "run", fake_run)

    info = wc.adopt_worker_cgroup(_settings(), busctl="/usr/bin/busctl")

    assert info["status"] == "adopted"
    assert info["path"] == SCOPE
    assert info["unit"].startswith("hermes-worker-managed-")
    assert info["memory_max"] == 256 * 1024 ** 2
    assert info["oom_score_adj"] == 500
    assert "StartTransientUnit" in captured["argv"]
    assert captured["argv"][captured["argv"].index("PIDs") + 3] == str(os.getpid())


# --- the loss evidence: what the cgroup itself says about a death --------------------------

def test_scope_memory_reads_the_counters_of_the_scope_the_pid_is_charged_to(tmp_path, monkeypatch):
    """A memcg OOM kill must be readable from the record: ``oom_kill`` and peak vs cap."""
    scope = "hermes.slice/hermes-workers.slice/hermes-worker-managed-1-ab.scope"
    base = tmp_path / scope
    base.mkdir(parents=True)
    (base / "memory.max").write_text("6442450944\n", encoding="utf-8")
    (base / "memory.current").write_text("1200\n", encoding="utf-8")
    (base / "memory.peak").write_text("6442450944\n", encoding="utf-8")
    (base / "memory.events").write_text(
        "low 0\nhigh 0\nmax 17\noom 1\noom_kill 1\noom_group_kill 0\nsock_throttled 0\n",
        encoding="utf-8")
    monkeypatch.setattr(wc, "_cgroup_of", lambda pid: "/" + scope)

    report = wc.scope_memory(4242, root=tmp_path)

    assert report["path"] == "/" + scope
    assert report["max"] == 6442450944 and report["peak"] == 6442450944
    assert report["events"]["oom_kill"] == 1
    assert report["events"]["max"] == 17


def test_scope_memory_is_empty_when_the_cgroup_is_gone(tmp_path, monkeypatch):
    """A collected unit costs the evidence, never the caller."""
    monkeypatch.setattr(wc, "_cgroup_of", lambda pid: "/hermes.slice/gone.scope")

    assert wc.scope_memory(4242, root=tmp_path) == {}


def test_scope_memory_survives_a_pid_that_never_existed():
    """No /proc entry (or a non-numeric pid) is ``{}`` -- this runs on the loss path."""
    assert wc.scope_memory(2 ** 31 - 1) == {}
