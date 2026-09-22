"""Classic CLI subagent dock and scoped controls; no agent-loop state is changed."""
from __future__ import annotations

import json
import time

from prompt_toolkit.utils import get_cwidth

# Per-status glyph. A child sitting inside a blocking nested ``delegate_task``
# is NOT "running" in the same sense as one doing its own work — without the
# distinction a nested orchestrator's row looked exactly as busy as the workers
# it was merely waiting on, which is misleading when supervising a multi-level
# swarm. The status strings are the ones the progress relay mirrors onto the
# registry record (``tools/delegate_tool_progress._ChildProgressRelay``).
_STATUS_GLYPH = {
    'queued': '⏸',
    'starting': '⏳',
    'running': '🔀',
    'waiting_on_children': '👥',
    'summarizing': '📝',
    'completed': '✅',
    'ok': '✅',
    'failed': '❌',
    'error': '❌',
    'timeout': '⏱',
    'interrupted': '⛔',
}

# Statuses meaning "this row is done" — used by the overflow summary to report
# how many HIDDEN rows are still doing work.
_TERMINAL_STATUSES = frozenset({'completed', 'ok', 'failed', 'error', 'timeout', 'interrupted'})

# Spaces of indent per nesting level. 2 reads as a hierarchy without eating the
# (already tight) horizontal budget a row shares with model, status and tool.
_INDENT_WIDTH = 2

# Hard ceiling on rendered indentation. ``delegation.max_spawn_depth`` bounds
# real nesting well below this, but a display path must not be the thing that
# breaks when a config raises it — past this level rows stack at the same indent
# instead of marching off the right edge.
_MAX_RENDER_DEPTH = 4


def _clip(value, width):
    text = ' '.join(str(value or '').split())
    text = ''.join(c for c in text if c.isprintable())
    if get_cwidth(text) <= width:
        return text
    result = ''
    for char in text:
        if get_cwidth(result + char) > max(0, width - 1):
            break
        result += char
    return result + ('…' if width else '')


def format_elapsed(seconds):
    """Elapsed time, switching to ``MmSSs`` past 60s.

    Mirrors ``cli.py::_render_spinner_text``'s rollover format (minutes NOT
    zero-padded, seconds zero-padded — ``1m05s``, ``12m09s``) so every live
    counter in the TUI reads the same way once it crosses a minute, instead of
    the dock being the one place still showing a bare growing ``421s``.
    """
    try:
        seconds = max(0.0, float(seconds or 0))
    except (TypeError, ValueError):
        return '0s'
    if seconds < 60:
        return f'{seconds:.0f}s'
    minutes, secs = divmod(int(seconds), 60)
    return f'{minutes}m{secs:02d}s'


def shorten_model(model):
    """Strip a ``provider/model`` prefix down to the model slug.

    Applied to BOTH halves of a fallback label — shortening only the effective
    model renders ``⚠ ollama-cloud/glm-5.3→claude-opus-5``, blowing the row's
    width budget with the one part the user least needs.
    """
    text = str(model or '').strip()
    if not text:
        return ''
    return text.split('/', 1)[1] if '/' in text else text


def model_label(row):
    """The row's model identity, marked when the child silently failed over.

    Compact form (``⚠ primary→effective``) because this shares one line with
    status, tool and elapsed. Reads the fields ``_list_payload`` already
    resolves LIVE off the child agent, so a child that failed over mid-run
    shows what it is actually running on, not its dispatch-time snapshot.
    """
    from agent.failover_state import format_model_label

    return format_model_label(
        shorten_model(row.get('model')) or '?',
        fallback_active=row.get('fallback_active'),
        primary_model=shorten_model(row.get('primary_model')),
        compact=True,
    )


def order_rows_for_display(rows):
    """Group rows into parent → child order with EFFECTIVE depths.

    Returns ``(row, depth)`` pairs. The registry hands back a flat list scoped
    to the caller's spawn tree, which can span several concurrent dispatches and
    several nesting levels; plain input order can interleave a grandchild with
    an unrelated top-level child — right depth, wrong neighbours.

    Effective depth is computed from parent links actually PRESENT in this set,
    not from the record's declared ``depth``: a child whose parent already
    finished and left the registry renders as a root rather than floating at an
    indent under nothing. Every input row appears exactly once; duplicate ids
    and parent cycles are handled defensively.
    """
    if not rows:
        return []

    by_id = {}
    for row in rows:
        by_id.setdefault(row.get('subagent_id'), row)

    children, roots = {}, []
    for row in rows:
        parent = row.get('parent_id')
        if parent and parent in by_id and by_id[parent] is not row:
            children.setdefault(parent, []).append(row)
        else:
            roots.append(row)

    ordered, seen = [], set()

    def emit(row, depth):
        # id()-keyed, not subagent_id-keyed: duplicate-id rows are distinct
        # objects that should each render once.
        if id(row) in seen:
            return
        seen.add(id(row))
        ordered.append((row, depth))
        # Past the indent ceiling keep descending but stop deepening.
        nxt = depth if depth >= _MAX_RENDER_DEPTH else depth + 1
        for child in children.get(row.get('subagent_id'), ()):
            emit(child, nxt)

    for root in roots:
        emit(root, 0)
    # Safety net: anything unreachable from a root (a cycle among non-root rows)
    # still renders, as a root, so no row is ever dropped.
    for row in rows:
        if id(row) not in seen:
            emit(row, 0)
    return ordered


def row_activity(row, width=None):
    """The work-state half of a row: status glyph, model, tool tally, last tool.

    Everything here comes from the registry record the progress relay mirrors
    into (``mirror_subagent_activity``), which is what lets the dock show the
    per-child detail that previously existed only on the retired swarm board.

    ``width`` degrades the row gracefully instead of letting the outer clip
    truncate it from the right, which would drop the most operationally useful
    field first. On a narrow terminal the segments are shed in reverse priority
    order — tool tally, then model, then status — so "what is this child doing
    right now" (the last tool) survives longest. Upstream's dock showed the tool
    at 32 columns and a regression test pins that; the added signals must not
    cost it.
    """
    status = row.get('status') or 'starting'
    glyph = _STATUS_GLYPH.get(status, '🔀')
    count = row.get('tool_count')
    tool = str(row.get('last_tool') or '').strip()
    if tool.startswith('mcp_'):
        tool = tool[4:]

    # (segment, droppable) in render order; dropped right-to-left by priority.
    tally = f"{count} tool{'' if count == 1 else 's'}" if isinstance(count, int) else None
    last = f'last: {tool}' if tool else None
    candidates = [f'{glyph} {model_label(row)}', status, tally, last]
    # Priority: keep the last tool, then status, then model, then the tally.
    drop_order = [2, 0, 1]

    parts = [p for p in candidates if p]
    if width is None:
        return ' · '.join(parts)
    for index in drop_order:
        rendered = ' · '.join(p for p in candidates if p)
        if get_cwidth(rendered) <= width:
            return rendered
        candidates[index] = None
    return ' · '.join(p for p in candidates if p) or status


def row_prefix(depth):
    """Indent + elbow marking a row as a child of the row above it.

    Top-level rows keep the exact unindented format; nesting is legible without
    relying on colour.
    """
    depth = max(0, min(int(depth or 0), _MAX_RENDER_DEPTH))
    return (' ' * (_INDENT_WIDTH * depth)) + '└─ ' if depth else ''


class SubagentMonitor:
    def __init__(self, cli):
        self.cli = cli
        self.entries = []
        self.selected_id = None
        self._signature = None
        self._last_poll = 0
        self.app = None
        self.opening = False
        self.collapsed = False

    @property
    def selected(self):
        return next((r for r in self.entries if r['subagent_id'] == self.selected_id), None)

    def refresh(self, now=None):
        from tools.delegate_tool_registry import _list_payload, list_active_subagents
        now = time.time() if now is None else now
        parent = getattr(self.cli, 'agent', None)
        entries = _list_payload(parent)['subagents'] if parent is not None else []
        # The scoped control-plane snapshot supplies authority and transcript paths;
        # its matching public lifecycle record supplies the latest observed activity.
        activity = {r['subagent_id']: r for r in list_active_subagents()} if entries else {}
        for row in entries:
            live = activity.get(row['subagent_id'], {})
            row['elapsed'] = max(0, int(now - live.get('started_at', now)))
            row['last_tool'] = live.get('last_tool') or row.get('last_tool') or ''
            row.pop('running_seconds', None)
        # Parent → child ordering with effective depths, so the dock renders the
        # spawn tree rather than a flat list. Done once per refresh (not per
        # paint) because both the dock and the full-screen roster render from it
        # and ``entries`` is also what the signature/selection logic walks.
        self.entries = [
            dict(row, display_depth=depth) for row, depth in order_rows_for_display(entries)
        ]
        signature = json.dumps(self.entries, sort_keys=True, default=str)
        changed = signature != self._signature
        self._signature = signature
        if self.selected is None:
            self.selected_id = self.entries[0]['subagent_id'] if self.entries else None
        return changed

    def invalidate(self):
        from hermes_cli.cli_terminal_mixin import _run_on_app_loop

        app = self.app
        if app is not None:
            # Teardown clears app.loop; don't let it interleave with a worker's
            # invalidate call, which reads the loop more than once.
            _run_on_app_loop(app, app.invalidate)

    def tick(self):
        now = time.monotonic()
        if now - self._last_poll < 1:
            return
        self._last_poll = now
        if self.refresh():
            if self.app is not None:
                self.invalidate()
            else:
                self.cli._invalidate()

    def select(self, delta):
        if self.entries:
            index = next((i for i, r in enumerate(self.entries) if r['subagent_id'] == self.selected_id), 0)
            self.selected_id = self.entries[(index + delta) % len(self.entries)]['subagent_id']

    def control(self, action, message=None, *, target=None):
        from tools.delegate_tool_registry import _handle_control_action
        return json.loads(_handle_control_action(action, target or self.selected_id, message, getattr(self.cli, 'agent', None)))

    def dock_text(self, *, columns, rows):
        if not self.entries:
            return ''
        if self.collapsed:
            count = f'{len(self.entries)} live'
            # Keep both controls before spending scarce cells on activity.
            headings = (
                f'Subagents · {count} · Ctrl+T expand · F7 restore',
                f'{count} · Ctrl+T expand · F7 restore',
                f'{count} · Ctrl+T · F7',
                count,
            )
            width = max(0, columns - 1)
            heading = next((text for text in headings if get_cwidth(text) <= width), count)
            row = self.entries[0]
            activity = f"last: {row['last_tool']}" if row.get('last_tool') else row.get('status') or 'starting'
            if get_cwidth(heading + ' · ' + activity) <= width:
                heading += ' · ' + activity
            return _clip(' ' + heading, max(0, columns))
        columns = max(0, columns - 2)
        count = min(len(self.entries), max(1, min(4, (rows - 10) // 3)))
        hidden = len(self.entries) - count
        heading = f' Subagents · {len(self.entries)} live · Ctrl+T expand · F7 collapse'
        lines = [_clip(heading, columns)]
        for row in self.entries[:count]:
            # Status glyph + live model identity (fallback-marked) + tool tally
            # + last tool, then the goal in whatever space remains. Elapsed
            # rolls over to MmSSs past a minute, matching every other TUI
            # counter. The activity is width-budgeted so a narrow terminal sheds
            # the least useful field rather than clipping the last tool away.
            prefix = row_prefix(row.get('display_depth'))
            elapsed = format_elapsed(row.get('elapsed'))
            budget = max(0, columns - get_cwidth(prefix) - get_cwidth(elapsed) - 12)
            activity = f"{elapsed} · {row_activity(row, width=budget)}"
            # Reserve activity even on narrow terminals; task names use the remainder.
            goal_width = max(3, columns - get_cwidth(activity) - get_cwidth(prefix) - 5)
            lines.append(_clip(f" {prefix}{_clip(row.get('goal'), goal_width)} · {activity}", columns))
        if hidden:
            # Break out how many hidden rows are still working: "8 hidden, all
            # finished" and "8 hidden, all running" are very different situations
            # for someone watching a live dock.
            live = sum(1 for r in self.entries[count:] if r.get('status') not in _TERMINAL_STATUSES)
            suffix = f' ({live} running)' if live else ''
            lines.append(_clip(f' +{hidden} more{suffix} · Ctrl+T all subagents', columns))
        return '\n'.join(' ' + line for line in lines)


def read_tail(path):
    if not path:
        return 'Live transcript not available yet.'
    try:
        with open(path, 'rb') as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 32768))
            text = stream.read(32768).decode('utf-8', errors='replace')
        return ''.join(c for c in text if c.isprintable() or c in '\n\t')
    except OSError:
        return 'Live transcript not available yet.'


def modal_prompt_active(cli):
    return any(getattr(cli, name, None) for name in (
        '_clarify_state', '_approval_state', '_slash_confirm_state', '_sudo_state',
        '_secret_state', '_model_picker_state', '_command_palette_state'))


def build_monitor_application(monitor, **kwargs):
    from prompt_toolkit.application import Application
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.widgets import TextArea

    state = {'detail': False, 'steering': False, 'confirm': False, 'notice': ''}
    steer = TextArea(height=1, prompt='Steer: ', multiline=False)
    tail = TextArea(read_only=True, scrollbar=True, wrap_lines=True)

    def roster_text():
        size = app.output.get_size()
        rows = []
        for row in monitor.entries:
            selected = row['subagent_id'] == monitor.selected_id
            # Same signal set as the dock (glyph, live model + fallback marker,
            # status, tool tally, last tool, MmSSs elapsed) plus the lineage
            # indent, so switching between the two surfaces reads identically.
            prefix = f"{row_prefix(row.get('display_depth'))}{format_elapsed(row.get('elapsed'))} · {row_activity(row)} · "
            note = str(row.get('last_note') or '').strip()
            suffix = f' · {note}' if note else ''
            goal_width = max(0, size.columns - 2 - get_cwidth(prefix + suffix))
            goal = _clip(row.get('goal') or row['subagent_id'], goal_width)
            text = f"{'❯' if selected else ' '} " + _clip(prefix + goal + suffix, max(0, size.columns - 2))
            # Pad selection in terminal cells, not codepoints (task names may be wide).
            text += ' ' * max(0, size.columns - get_cwidth(text))
            rows.append(('class:subagent-dock.selected' if selected else '', text + '\n'))
        return rows or [('', 'No live subagents. Results arrive in the conversation.')]

    def cursor():
        index = next((i for i, row in enumerate(monitor.entries) if row['subagent_id'] == monitor.selected_id), 0)
        return Point(x=0, y=index)

    roster = Window(FormattedTextControl(roster_text, focusable=True, get_cursor_position=cursor))

    def update_tail():
        row = monitor.selected
        text = read_tail(row.get('live_transcript')) if row else 'This subagent is no longer live.'
        if text != tail.text:
            following = tail.buffer.cursor_position == len(tail.text)
            position = tail.buffer.cursor_position
            tail.text = text
            tail.buffer.cursor_position = len(text) if following else min(position, len(text))

    def header():
        row = monitor.selected
        title = f"Subagents · {len(monitor.entries)} live"
        if state['detail'] and row:
            title += f" · {row['subagent_id']} · {row.get('goal') or ''}"
        return [('class:subagent-dock.heading', _clip(title, app.output.get_size().columns))]

    def footer():
        narrow = app.output.get_size().columns < 60
        if state['confirm']:
            return 'Stop? y yes · Esc cancel' if narrow else 'Stop selected subagent? y confirm · Esc cancel'
        if state['steering']:
            return 'Enter send · Esc cancel' if narrow else 'Enter queues guidance · Esc cancels (does not interrupt)'
        if narrow:
            return 'PgUp/Dn · s steer x stop · Esc' if state['detail'] else '↑↓ · Enter tail · Ctrl+T close'
        return ('Esc roster · PgUp/PgDn tail · s steer · x stop' if state['detail'] else
                '↑/↓ select · Enter tail · s steer · x stop · q/Ctrl+T close')

    kb = KeyBindings()
    normal = Condition(lambda: not state['steering'] and not state['confirm'])
    listing = normal & Condition(lambda: not state['detail'])

    @kb.add('up', filter=listing)
    def up(event):
        monitor.select(-1)

    @kb.add('down', filter=listing)
    def down(event):
        monitor.select(1)

    @kb.add('enter', filter=listing)
    def detail(event):
        if monitor.selected:
            state['detail'] = True
            update_tail()
            app.layout.focus(tail)

    @kb.add('s', filter=normal)
    def start_steer(event):
        if monitor.selected:
            state['steering'] = True
            state['target'] = monitor.selected_id
            app.layout.focus(steer)

    @kb.add('enter', filter=Condition(lambda: state['steering']))
    def send_steer(event):
        if not steer.text.strip():
            return
        result = monitor.control('steer', steer.text, target=state['target'])
        state['notice'] = result.get('error') or result.get('note') or str(result)
        steer.text = ''
        state['steering'] = False
        app.layout.focus(tail if state['detail'] else roster)

    @kb.add('x', filter=normal)
    def stop(event):
        if monitor.selected:
            state['confirm'] = True
            state['target'] = monitor.selected_id

    @kb.add('y', filter=Condition(lambda: state['confirm']))
    def confirm(event):
        result = monitor.control('stop', target=state['target'])
        state['notice'] = result.get('error') or result.get('note') or str(result)
        state['confirm'] = False

    @kb.add('escape', eager=True)
    def back(event):
        if state['steering'] or state['confirm']:
            state['steering'] = state['confirm'] = False
            app.layout.focus(tail if state['detail'] else roster)
        elif state['detail']:
            state['detail'] = False
            app.layout.focus(roster)
        else:
            app.exit()

    @kb.add('q', filter=normal)
    @kb.add('f6', filter=normal)
    @kb.add('c-t', filter=normal)
    @kb.add('c-c')
    def close(event):
        app.exit()

    layout = Layout(HSplit([
        Window(FormattedTextControl(header), height=1),
        ConditionalContainer(roster, filter=Condition(lambda: not state['detail'])),
        ConditionalContainer(tail, filter=Condition(lambda: state['detail'])),
        ConditionalContainer(steer, filter=Condition(lambda: state['steering'])),
        Window(FormattedTextControl(lambda: _clip(state['notice'], app.output.get_size().columns)), height=1),
        Window(FormattedTextControl(footer), height=1),
    ], style='class:subagent-dock'), focused_element=roster)
    def before_render(app):
        # Prompts arrive on worker threads; exit on the UI loop, including the
        # first frame if a prompt won the race with in_terminal() acquisition.
        if modal_prompt_active(monitor.cli) and not app.is_done:
            app.exit()
        elif state['detail']:
            update_tail()

    from prompt_toolkit.styles import Style
    from hermes_cli.skin_engine import get_prompt_toolkit_style_overrides
    kwargs.setdefault('style', Style.from_dict(get_prompt_toolkit_style_overrides()))
    app = Application(layout=layout, key_bindings=kb, full_screen=True, mouse_support=False,
                      before_render=before_render, **kwargs)
    return app


def open_monitor(cli):
    import asyncio
    from prompt_toolkit.application import in_terminal
    monitor = getattr(cli, '_subagent_monitor', None)
    if monitor is None or monitor.opening:
        return
    monitor.opening = True

    async def run():
        try:
            async with in_terminal():
                monitor.refresh()
                monitor.app = build_monitor_application(monitor)
                await monitor.app.run_async()
        finally:
            monitor.app = None
            monitor.opening = False
            cli._invalidate()

    asyncio.get_running_loop().create_task(run())


def toggle_dock(cli):
    monitor = getattr(cli, '_subagent_monitor', None)
    if monitor is not None:
        monitor.collapsed = not monitor.collapsed
        cli._invalidate()


def install_dock(cli):
    from prompt_toolkit.application import get_app
    from prompt_toolkit.layout import ConditionalContainer, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.filters import Condition
    monitor = SubagentMonitor(cli)
    cli._subagent_monitor = monitor
    monitor.refresh()

    def text():
        size = get_app().output.get_size()
        lines = monitor.dock_text(columns=size.columns, rows=size.rows).splitlines()
        return [('class:subagent-dock.heading' if i == 0 else '',
                 line + ('\n' if i < len(lines) - 1 else ''))
                for i, line in enumerate(lines)]

    cli._subagent_dock_widget = ConditionalContainer(
        Window(FormattedTextControl(text), wrap_lines=False, dont_extend_height=True,
               style='class:subagent-dock'),
        filter=Condition(lambda: bool(monitor.entries) and not modal_prompt_active(cli)),
    )
