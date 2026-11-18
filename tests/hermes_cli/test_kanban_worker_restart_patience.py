"""A Kanban worker WAITS OUT a planned gateway restart instead of failing its attempt.

A `hermes update` / `hermes gateway restart` opens the owner's after-turn wait, which counts the
card's managed turn as active work and defers `stop()` for it — but refuses every client read while
it waits, so the worker cannot see its own receipt until the NEXT owner answers. Failing at
`OWNER_WAIT_S` there is what amputated 13 in-flight attempts on 2026-11-17 (each died 181-188 s
after the cut), and the worker's exit is what ended the very turn the drain was deferring for
(`_watch_submitter`). So a restart the worker can SEE (`draining`/`restart_wait`) is waited out on
the restart's own budget (`RESTART_WAIT_BUDGET_S`), and its refusals stop being final.

Pinned here:
  * a worker cut by a restart wait resumes its receipt against the next owner (attempt intact);
  * a restart that never returns still fails, but at the restart budget and naming the restart;
  * a plain silent owner (no restart announced) still fails at `OWNER_WAIT_S`;
  * an identity/handshake refusal during the announced restart is retried, a profile refusal is not;
  * the lost attempt's own last words name the restart, so the board's host-failure verdict sees it.
"""

import asyncio
from contextlib import asynccontextmanager
import importlib

import pytest

from hermes_cli import gateway_client, kanban_worker_client
from gateway.drain_control import RESTART_WAIT_REASON

GUE = gateway_client.GatewayUnavailableError


def _rebind_modules():
    """Follow a module PURGE another test in this session performed.

    ``test_kanban_cli_dispatch_passthrough`` / ``test_kanban_per_profile_cap`` delete every
    ``hermes_cli*`` entry from ``sys.modules`` to get clean imports. Afterwards this file still
    holds the pre-purge module objects while ``kanban_worker_client.run``'s function-local imports
    bind the FRESH ones — so a patched ``connect_gateway`` is invisible and the client dials the
    real gateway (measured: 10/10 tests here fail in a full ``tests/hermes_cli/test_kanban*`` run
    and pass alone). Rebinding the globals makes this file honest either way.
    """
    global gateway_client, kanban_worker_client, GUE
    gateway_client = importlib.import_module("hermes_cli.gateway_client")
    kanban_worker_client = importlib.import_module("hermes_cli.kanban_worker_client")
    GUE = gateway_client.GatewayUnavailableError


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class _Owner:
    """Scripted owner: ``connects`` one step per dial, ``receipts`` one per ``prompt.receipt``.

    A step is None (succeed), an exception (raised) or a callable (run first, so it can advance the
    clock or raise). ``after`` is the step for every dial past the scripted ones.
    """

    def __init__(self, connects=(), receipts=(), after=None):
        self.connects, self.receipts, self.after = list(connects), list(receipts), after
        self.dials, self.calls = 0, []

    @staticmethod
    def _step(steps, default=None):
        step = steps.pop(0) if steps else default
        if callable(step):
            step = step()
        if isinstance(step, Exception):
            raise step
        return step

    def connect(self):
        owner = self

        @asynccontextmanager
        async def _connect():
            owner.dials += 1
            owner._step(owner.connects, owner.after)

            class _Client:
                events = asyncio.Queue()

                async def rpc(self, method, **params):
                    owner.calls.append(method)
                    if method == "kanban.run":
                        return {"session_id": "s1", "receipt": {"status": "pending", "admission_id": "a1"}}
                    return {"status": owner._step(owner.receipts, "terminal")}

            yield _Client()

        return _connect


def _restart_wait():
    return GUE(f"Gateway draining: {RESTART_WAIT_REASON}", "draining", RESTART_WAIT_REASON)


def _draining():
    """The read a draining owner refuses mid-turn (``runtime_draining``): the poll that ends the
    attached phase and makes the worker redial into the restart wait."""
    return gateway_client.GatewayRPCError("runtime_draining")


def _slow(clock, seconds, step):
    """Run *step* after *seconds* of real waiting (the drain's own time)."""
    def _run():
        clock.now += seconds
        return step() if callable(step) else step
    return _run


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    _rebind_modules()
    monkeypatch.delenv("HERMES_TUI_GATEWAY_URL", raising=False)
    # The restart marker is read from the process home: pin it to the test's tmp dir so a real
    # restart on this box (or a leftover marker) cannot make a "final" case look retryable.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway import session_kanban
    monkeypatch.setattr(session_kanban, "worker_result",
                        lambda *_: {"exit_code": 0, "last_output": "card closed"})
    clock = _Clock()
    monkeypatch.setattr(kanban_worker_client, "time", clock)

    async def _sleep(seconds):
        clock.now += seconds
    monkeypatch.setattr(kanban_worker_client.asyncio, "sleep", _sleep)
    return clock


async def _run(owner, monkeypatch, tmp_path):
    monkeypatch.setattr(gateway_client, "connect_gateway", owner.connect())
    return await kanban_worker_client.run({"task_id": "t1"}, str(tmp_path / "kanban.db"))


@pytest.mark.asyncio
async def test_a_worker_cut_by_a_restart_wait_resumes_against_the_next_owner(monkeypatch, _env, tmp_path):
    # The turn is running; the restart wait opens and refuses the receipt read; the worker redials
    # into the wait for three minutes of drain (a sibling card turn finishing) and then the
    # replacement owner answers the SAME admission.
    owner = _Owner(connects=[None] + [_slow(_env, 30, _restart_wait) for _ in range(6)] + [None],
                   receipts=["pending", _draining()])

    assert await _run(owner, monkeypatch, tmp_path) == 0
    # The claim is submitted once: the attempt and its admission survive the restart.
    assert owner.calls.count("kanban.run") == 1
    assert _env.now > kanban_worker_client.OWNER_WAIT_S   # it really outlived OWNER_WAIT_S


@pytest.mark.asyncio
async def test_a_restart_that_never_returns_fails_at_the_restart_budget(monkeypatch, _env, tmp_path):
    owner = _Owner(connects=[None], receipts=["pending", _draining()],
                   after=_slow(_env, 30, _restart_wait))

    with pytest.raises(gateway_client.GatewayClientError,
                       match="owner silent for .* after a planned gateway restart"):
        await _run(owner, monkeypatch, tmp_path)
    assert _env.now >= kanban_worker_client.RESTART_WAIT_BUDGET_S
    assert owner.calls.count("kanban.run") == 1


@pytest.mark.asyncio
async def test_a_plain_silent_owner_still_fails_at_owner_wait(monkeypatch, _env, tmp_path):
    # No restart was ever announced: a wedge is still the card's to fail on, not the host's.
    owner = _Owner(connects=[None], receipts=["pending", _draining()],
                   after=GUE("Gateway starting: deadline", "starting", "deadline"))

    with pytest.raises(gateway_client.GatewayClientError, match="owner silent for 1[89]\\ds"):
        await _run(owner, monkeypatch, tmp_path)
    assert kanban_worker_client.OWNER_WAIT_S <= _env.now < kanban_worker_client.RESTART_WAIT_BUDGET_S


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["service_identity_unverified", "runtime_exited"],
                         ids=["unit-rewritten", "owner-gone"])
async def test_an_owner_that_is_being_replaced_is_not_final(monkeypatch, _env, tmp_path, reason):
    """Between stop() and the replacement's first answer the unit is rewritten: retry that.

    Measured 2026-11-17: `Gateway inaccessible: service_identity_unverified` killed this card's
    first attempt 40 s into an update's down window.
    """
    # Built here, not in the parametrize list: the list is evaluated at COLLECTION time, and a
    # module purge by an earlier test in the same session would leave a stale exception class that
    # ``run``'s ``except`` (bound to the fresh module) does not catch.
    transient = GUE(f"Gateway inaccessible: {reason}", "inaccessible", reason)
    owner = _Owner(connects=[None, _restart_wait(), transient, None],
                   receipts=["pending", _draining()])

    assert await _run(owner, monkeypatch, tmp_path) == 0
    assert owner.calls.count("kanban.run") == 1


@pytest.mark.asyncio
async def test_a_profile_refusal_is_still_final_during_a_restart(monkeypatch, _env, tmp_path):
    # The replacement owner will give the same answer, so waiting out the budget only delays it.
    owner = _Owner(connects=[None, _restart_wait(),
                             GUE("Gateway inaccessible: profile_mismatch", "inaccessible", "profile_mismatch")],
                   receipts=["pending", _draining()])

    with pytest.raises(GUE, match="profile_mismatch"):
        await _run(owner, monkeypatch, tmp_path)
    assert _env.now < kanban_worker_client.RESTART_WAIT_BUDGET_S


# --- the DOWN window: no owner is in a restart wait, so the MARKER is the evidence ------------
#
# Between the old owner's ``stop()`` and the replacement's first answer, discovery refuses with
# ``service_identity_unverified`` while the unit is rewritten. That refusal is indistinguishable
# from a broken install, and it killed this card's first attempt 40 s into an update's down window
# (t_39446095): the worker read it as a credential problem and exited 1.

def _write_marker(tmp_path, age_s=30.0):
    import json as _json
    import time as _time
    (tmp_path / ".restart_pending.json").write_text(_json.dumps(
        {"requested_at": _time.time() - age_s, "via_service": True, "detached": False}),
        encoding="utf-8")


def _identity_refusal():
    return GUE("Gateway inaccessible: service_identity_unverified",
               "inaccessible", "service_identity_unverified")


@pytest.mark.asyncio
async def test_an_identity_refusal_with_a_fresh_restart_marker_is_waited_out(monkeypatch, _env, tmp_path):
    """The first dial lands in the down window: the marker says the host is restarting, not broken."""
    _write_marker(tmp_path, age_s=40)
    owner = _Owner(connects=[_identity_refusal(), None], receipts=["terminal"])

    assert await _run(owner, monkeypatch, tmp_path) == 0
    assert owner.calls.count("kanban.run") == 1


@pytest.mark.asyncio
async def test_an_identity_refusal_with_no_marker_is_still_final(monkeypatch, _env, tmp_path):
    """No restart was requested: a unit whose identity does not verify is the install's answer."""
    owner = _Owner(connects=[_identity_refusal()], receipts=[])

    with pytest.raises(GUE, match="service_identity_unverified"):
        await _run(owner, monkeypatch, tmp_path)
    # It failed on the first dial, without spending the restart budget on a broken install.
    assert _env.now < kanban_worker_client.OWNER_WAIT_S


@pytest.mark.asyncio
async def test_a_stale_restart_marker_does_not_explain_a_new_refusal(monkeypatch, _env, tmp_path):
    """A marker left over from an old restart must not turn a real breakage into a 40-minute wait."""
    _write_marker(tmp_path, age_s=kanban_worker_client.RESTART_WAIT_BUDGET_S + 60)
    owner = _Owner(connects=[_identity_refusal()], receipts=[])

    with pytest.raises(GUE, match="service_identity_unverified"):
        await _run(owner, monkeypatch, tmp_path)
    assert _env.now < kanban_worker_client.OWNER_WAIT_S


@pytest.mark.asyncio
async def test_a_finished_restart_restores_the_plain_owner_wait(monkeypatch, _env, tmp_path):
    """After the replacement answers, a silence is the CARD's again — measured from that answer.

    Otherwise a restart seen early in an attempt would leave a later wedge with whatever the
    restart did not spend of the restart budget.
    """
    wedge = GUE("Gateway starting: deadline", "starting", "deadline")
    owner = _Owner(connects=[None, _restart_wait(), None, wedge],
                   receipts=["pending", _draining(), "pending", _draining()], after=wedge)

    with pytest.raises(gateway_client.GatewayClientError, match="owner silent for 1[89]\\ds"):
        await _run(owner, monkeypatch, tmp_path)
    assert _env.now < kanban_worker_client.RESTART_WAIT_BUDGET_S


def test_an_attempt_ended_by_the_restart_names_it_in_its_last_words(monkeypatch, tmp_path, capsys):
    """The board reads the worker's own log as the attempt's last words: say the restart there."""
    from gateway import session_kanban
    # No ``worker_result`` event: the owner never settled the turn — the drain outran it.
    monkeypatch.setattr(session_kanban, "worker_result", lambda *_: {})

    assert kanban_worker_client._exit_code({"status": "terminal"}, {}, str(tmp_path),
                                           planned_restart=True) == 1
    printed = capsys.readouterr().out
    assert "planned gateway restart" in printed

    # A turn the owner SETTLED keeps its own result, even a nonzero one: a restart that merely
    # overlapped the card's real failure must not excuse it.
    monkeypatch.setattr(session_kanban, "worker_result",
                        lambda *_: {"exit_code": 130, "last_output": "partial card work"})
    assert kanban_worker_client._exit_code({"status": "terminal"}, {}, str(tmp_path),
                                           planned_restart=True) == 130
    printed = capsys.readouterr().out
    assert "partial card work" in printed
    assert "planned gateway restart" not in printed

    # A turn that finished before stop() keeps its own result too: no restart sentence, no charge.
    monkeypatch.setattr(session_kanban, "worker_result", lambda *_: {"exit_code": 0, "last_output": "done"})
    assert kanban_worker_client._exit_code({"status": "terminal"}, {}, str(tmp_path),
                                           planned_restart=True) == 0
    assert "planned gateway restart" not in capsys.readouterr().out
