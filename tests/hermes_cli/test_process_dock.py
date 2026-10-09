"""Background processes share the classic CLI live-work dock with subagents (Processes block)."""
import time
from types import SimpleNamespace

from prompt_toolkit.utils import get_cwidth

from agent.i18n import t


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def test_dock_paints_processes_under_agents_and_retires_finished_rows(monkeypatch):
    from hermes_cli import cli_process_dock
    from hermes_cli.cli_subagent_monitor import SubagentMonitor
    from tools import delegate_tool_registry as registry
    from tools.process_registry import process_registry

    monkeypatch.setattr(registry, '_active_subagents', {})
    owner = SimpleNamespace(session_id='owner')
    registry._register_subagent(dict(subagent_id='a1', owner_agent_session_id='owner',
        goal='Check module', started_at=time.time() - 5, status='running', last_tool='read_file'))
    quick = process_registry.spawn_local(command="echo hello-dock; exit 3", cwd='.', task_id='t', owner_task_id='t', session_key='')
    slow = process_registry.spawn_local(command="sleep 30", cwd='.', task_id='t', owner_task_id='t', session_key='')
    quick_id, slow_id = quick.id, slow.id
    try:
        _wait(lambda: process_registry.get(quick_id).exited)
        process_registry.list_sessions()  # observes the exit → exited_at stamped
        dock = SubagentMonitor(SimpleNamespace(agent=owner))
        assert dock.refresh()
        text = dock.dock_text(columns=100, rows=30)
        lines = text.splitlines()
        assert t('cli.subagents.subagents_heading', count=1) in lines[0]
        agents_at = next(i for i, line in enumerate(lines) if 'Check module' in line)
        summary = ' · '.join((t('cli.subagents.count_running', count=1), t('cli.subagents.count_done', count=1)))
        procs_at = next(i for i, line in enumerate(lines)
                        if t('cli.subagents.processes_heading', summary=summary, controls='') in line)
        assert agents_at < procs_at
        assert any('⚙ sleep 30' in line and t('cli.dock.starting') in line for line in lines)
        assert any(f"✘ echo hello-dock; exit 3 · {t('cli.dock.exit_code', code=3)}" in line for line in lines)
        assert all(get_cwidth(line) <= 100 for line in lines)
        # Every viewport keeps at least one row of each block.
        narrow = dock.dock_text(columns=40, rows=14).splitlines()
        assert any('Check module' in line for line in narrow)
        assert any(t('cli.subagents.title_processes') in line for line in narrow)
        assert all(get_cwidth(line) <= 40 for line in narrow)
        dock.collapsed = True
        assert dock.dock_text(columns=100, rows=30).count('\n') == 0
        assert f"{t('cli.subagents.count_live', count=1)} · {t('cli.subagents.count_procs_one', count=1)}" in dock.dock_text(columns=100, rows=30)
        # Finished rows leave after the retention window; running ones stay.
        later = cli_process_dock.process_rows(time.time() + cli_process_dock.RETAIN_SECONDS + 1)
        assert [r['id'] for r in later] == [slow_id]
    finally:
        process_registry.kill_process(slow_id)


def test_process_activity_rolls_over_to_mins_past_60():
    """A long-running process must not render a bare ``696s``/``300s``.

    Regression for the "seconds displayed >= 60" dock defect: the process row
    rendered raw ``f"{elapsed}s"`` while every other TUI counter had already
    switched to ``MmSSs`` past a minute (the same 7m01s-form the user reported
    from the retired swarm board, now on the process rows too)."""
    from hermes_cli.cli_process_dock import process_activity

    assert process_activity({"status": "running", "elapsed": 59, "detail": ""}) == "59s · starting"
    assert process_activity({"status": "running", "elapsed": 90, "detail": ""}) == "1m30s · starting"
    assert process_activity({"status": "running", "elapsed": 300, "detail": ""}) == "5m00s · starting"
    # 696s was the user's exact "11m36s"-style example.
    assert process_activity({"status": "running", "elapsed": 696, "detail": ""}) == "11m36s · starting"
    # Finished row: the "N s ago" age rolls over too.
    assert process_activity(
        {"status": "done", "exit_code": 0, "elapsed": 0, "since_exit": 300}
    ) == "exit 0 · 5m00s ago"
    # No bare-seconds field >= 60 anywhere in the rendered line.
    for el in (60, 61, 599, 600, 3599, 3600):
        line = process_activity({"status": "running", "elapsed": el, "detail": ""})
        assert "ss" not in line and not any(
            tok.endswith("s") and tok[:-1].isdigit() and int(tok[:-1]) >= 60 for tok in line.split(" · ")
        ), line


def test_process_dock_clips_on_true_display_width_not_get_cwidth():
    """A VS-16 glyph in a dock row must not push the row one cell past budget.

    The dock measured every width with raw ``prompt_toolkit.utils.get_cwidth``,
    which reports 1 cell for an emoji base + U+FE0F (e.g. ``⚙️``/``⚠️``) that
    kitty renders as 2. An undercounted ``_clip``/pad lets the row exceed the
    terminal budget and wrap onto a second line — the "wrapped continuation
    overlaps the row below / duplicated digit" mechanism in FORK.md. Every
    measurement now routes through ``agent.display.display_cwidth``.
    """
    from agent.display import display_cwidth
    from hermes_cli.cli_subagent_monitor import _clip

    vs16 = "\u2699\ufe0f"  # GEAR + VARIATION SELECTOR-16
    text = "x" * 38 + vs16
    clipped = _clip(text, 39)
    assert display_cwidth(clipped) <= 39, "clipped row must fit the display budget"
    # And a plainly-too-long string still fills the budget exactly.
    long = "y" * 60
    assert display_cwidth(_clip(long, 39)) == 39


def test_monitor_controls_stop_processes_and_never_steer_them():
    from hermes_cli.cli_subagent_monitor import SubagentMonitor
    from tools.process_registry import process_registry

    slow = process_registry.spawn_local(command="sleep 30", cwd='.', task_id='t', owner_task_id='t', session_key='')
    slow_id = slow.id
    try:
        dock = SubagentMonitor(SimpleNamespace(agent=None))
        dock.refresh()
        dock.selected_id = slow_id
        assert dock.selected_process is not None
        assert 'error' in dock.control('steer', 'nope')
        assert process_registry.get(slow_id).exited is False
        assert dock.control('stop')['status'] == 'killed'
        _wait(lambda: process_registry.get(slow_id).exited)
        dock.refresh()
        assert any(r['id'] == slow_id and r['status'] == 'killed' for r in dock.processes)
    finally:
        process_registry.kill_process(slow_id)
