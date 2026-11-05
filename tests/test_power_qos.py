"""Tests for the battery-aware task policy (hermes_cli/power_qos.py)."""

from __future__ import annotations

from hermes_cli import power_qos


def test_battery_qos_is_opt_in():
    assert power_qos.battery_qos_enabled(None) is False
    assert power_qos.battery_qos_enabled({}) is False
    assert power_qos.battery_qos_enabled({"power": {}}) is False
    assert power_qos.battery_qos_enabled({"power": {"battery_qos": False}}) is False
    assert power_qos.battery_qos_enabled({"power": {"battery_qos": True}}) is True


def test_parse_on_battery_is_tri_state():
    assert power_qos.parse_on_battery("Now drawing from 'Battery Power'\n 64%; discharging;") is True
    assert power_qos.parse_on_battery("Now drawing from 'AC Power'\n 90%; charging;") is False
    # A payload we do not understand must not be read as "on AC".
    assert power_qos.parse_on_battery("") is None
    assert power_qos.parse_on_battery("Now drawing from 'UPS Power'") is None


def test_taskpolicy_argv_picks_the_right_flag():
    assert power_qos.taskpolicy_argv(True, pid=1234) == ["/usr/bin/taskpolicy", "-b", "-p", "1234"]
    assert power_qos.taskpolicy_argv(False, pid=1234) == ["/usr/bin/taskpolicy", "-B", "-p", "1234"]


def test_apply_reports_success_and_failure():
    calls = []

    def ok(argv):
        calls.append(argv)
        return 0

    def bad(argv):
        return 1

    def boom(argv):
        raise OSError("no taskpolicy")

    assert power_qos.apply(True, runner=ok, pid=7) is True
    assert calls == [["/usr/bin/taskpolicy", "-b", "-p", "7"]]
    assert power_qos.apply(True, runner=bad, pid=7) is False
    assert power_qos.apply(True, runner=boom, pid=7) is False


def test_watch_needs_two_agreeing_probes_before_it_flips():
    applied = []

    def runner(argv):
        applied.append(argv)
        return 0

    watch = power_qos._PowerWatch(runner=runner, probe=lambda: True)
    watch.tick()
    assert applied == []  # first sighting only arms the flip
    watch.tick()
    assert len(applied) == 1 and "-b" in applied[0]
    watch.tick()
    assert len(applied) == 1  # steady state does nothing


def test_watch_ignores_an_unreadable_probe():
    applied = []

    def runner(argv):
        applied.append(argv)
        return 0

    watch = power_qos._PowerWatch(runner=runner, probe=lambda: None)
    for _ in range(5):
        watch.tick()
    assert applied == []


def test_watch_does_not_flip_on_a_single_blip():
    applied = []
    source = {"on_battery": True}

    def runner(argv):
        applied.append(argv)
        return 0

    watch = power_qos._PowerWatch(runner=runner, probe=lambda: source["on_battery"])
    watch.tick()
    watch.tick()  # now on battery policy
    assert len(applied) == 1

    source["on_battery"] = False
    watch.tick()  # one tick of AC: armed, not applied
    assert len(applied) == 1

    source["on_battery"] = True  # charger came back
    watch.tick()
    watch.tick()
    assert len(applied) == 1  # still the battery policy, no churn


def test_watch_retries_after_a_failed_apply():
    applied = []
    rc = {"v": 1}

    def runner(argv):
        applied.append(argv)
        return rc["v"]

    watch = power_qos._PowerWatch(runner=runner, probe=lambda: True)
    watch.tick()
    watch.tick()
    assert len(applied) == 1  # failed

    rc["v"] = 0
    watch.tick()
    assert len(applied) == 2  # retried, still trying to converge


def test_maybe_enable_is_a_no_op_off_macos(monkeypatch):
    monkeypatch.setattr(power_qos.platform, "system", lambda: "Linux")
    assert power_qos.maybe_enable(config={"power": {"battery_qos": True}}, runner=lambda argv: 0) is False


def test_maybe_enable_does_nothing_unless_opted_in(monkeypatch):
    monkeypatch.setattr(power_qos.platform, "system", lambda: "Darwin")
    called = []

    def runner(argv):
        called.append(argv)
        return 0

    assert power_qos.maybe_enable(config={}, runner=runner, probe=lambda: True) is False
    assert called == []


def test_maybe_enable_applies_when_opted_in(monkeypatch):
    monkeypatch.setattr(power_qos.platform, "system", lambda: "Darwin")
    called = []

    def runner(argv):
        called.append(argv)
        return 0

    assert (
        power_qos.maybe_enable(
            config={"power": {"battery_qos": True}},
            runner=runner,
            probe=lambda: True,
        )
        is True
    )
    assert called and "-b" in called[0]
