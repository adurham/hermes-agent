"""FORK regression tests: the /reasoning + /effort interactive effort picker.

The picker's state machine survived a fork/upstream merge, but its UI wiring (the panel
widget, the nav keybindings, the Enter branch, the prompt-blocking filter) was dropped —
bare `/reasoning` opened a modal that painted nothing while the input filter locked the
composer. These tests pin the wiring so a future merge cannot silently re-drop it.

They drive the REAL production methods against a minimal stub CLI; only UI/IO side effects
(_invalidate, save_config_value, _cprint) are stubbed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import cli as cli_mod
from hermes_cli.cli_commands_mixin import CLICommandsMixin
from pathlib import Path

from hermes_cli import cli_tui_mixin
from hermes_cli.cli_tui_mixin import CLITuiMixin

# Source of the mixin that owns the wiring under test (path derived from the module,,
# not hardcoded, so a relocation cannot make these assertions vacuously pass).
TUI_SRC = Path(cli_tui_mixin.__file__).read_text()

HermesCLI = cli_mod.HermesCLI
DSV4 = "deepseek-v4.1-flash"


class _FakeEvent:
    """Minimal prompt_toolkit event: the handlers only touch app.invalidate/current_buffer."""

    def __init__(self):
        self.app = SimpleNamespace(
            invalidate=lambda: None,
            current_buffer=SimpleNamespace(reset=lambda **k: None),
        )


def _make_stub(model=DSV4, provider="ollama-cloud"):
    """Stub CLI with every picker-path attribute and the real picker methods bound."""
    stub = SimpleNamespace()
    stub.model = model
    stub.provider = provider
    stub.reasoning_config = {"enabled": True, "effort": "medium"}
    stub.show_reasoning = False
    stub.reasoning_full = False
    stub.agent = MagicMock()
    stub._reasoning_effort_by_model = {}
    stub._reasoning_picker_state = None
    stub.config = {}
    # UI/IO side effects only — not the logic under test.
    stub._invalidate = lambda min_interval=0.25: None
    stub._paint_now = lambda: None
    stub._capture_modal_input_snapshot = lambda: None
    stub._restore_modal_input_snapshot = lambda: None
    stub._current_reasoning_callback = lambda: None
    # Real production methods.
    stub._open_reasoning_picker = lambda: HermesCLI._open_reasoning_picker(stub)
    stub._close_reasoning_picker = lambda: HermesCLI._close_reasoning_picker(stub)
    stub._handle_reasoning_picker_selection = (
        lambda: HermesCLI._handle_reasoning_picker_selection(stub))
    stub._current_reasoning_level_label = lambda: HermesCLI._current_reasoning_level_label(stub)
    stub._reasoning_levels_for_active_model = (
        lambda: HermesCLI._reasoning_levels_for_active_model(stub))
    stub._apply_reasoning_arg = lambda arg, **kw: HermesCLI._apply_reasoning_arg(stub, arg, **kw)
    stub._handle_reasoning_command = (
        lambda cmd: CLICommandsMixin._handle_reasoning_command(stub, cmd))
    stub._render_scroll_list_panel = (
        lambda *a, **k: CLITuiMixin._render_scroll_list_panel(stub, *a, **k))
    return stub


def _open(stub, command="/reasoning"):
    with patch("cli.save_config_value", return_value=True), patch("cli._cprint"):
        stub._handle_reasoning_command(command)
    return stub._reasoning_picker_state


def _pick(stub, key):
    """Select the row whose key matches and submit through the real Enter path."""
    state = stub._reasoning_picker_state
    state["selected"] = next(i for i, c in enumerate(state["choices"]) if c["key"] == key)
    with patch("cli.save_config_value", return_value=True) as save, patch("cli._cprint"):
        stub._handle_reasoning_picker_selection()
    return [c.args[0] for c in save.call_args_list]


def _dispatch_alias(stub, cmd):
    """Route ``cmd`` the way ``process_command`` does: registry alias -> canonical -> handler."""
    from hermes_cli.commands import resolve_command

    method, _pass_arg = HermesCLI._slash_handler(resolve_command(cmd.split()[0]).name)
    getattr(stub, method)(cmd)


class TestPickerOpens:
    def test_bare_reasoning_opens_the_picker(self):
        stub = _make_stub()
        state = _open(stub)
        assert state, "bare /reasoning must open the picker, not a dead modal"
        assert len(state["choices"]) > 1

    def test_effort_alias_opens_the_same_picker(self):
        stub = _make_stub()
        with patch("cli.save_config_value", return_value=True), patch("cli._cprint"):
            _dispatch_alias(stub, "/effort")
        assert stub._reasoning_picker_state, "/effort must reach the same picker as /reasoning"

    def test_choices_are_filtered_to_the_active_model(self):
        """DSv4 supports none/high/xhigh only — showing the full ladder would mislead."""
        stub = _make_stub()
        state = _open(stub)
        levels = [c["key"] for c in state["choices"] if c["kind"] == "level"]
        assert levels == ["none", "high", "xhigh"]

    def test_wider_ladder_on_a_tiered_model(self):
        stub = _make_stub(model="claude-sonnet-4-6", provider="anthropic")
        state = _open(stub)
        levels = [c["key"] for c in state["choices"] if c["kind"] == "level"]
        assert len(levels) > 3

    def test_display_toggles_and_cancel_are_offered(self):
        stub = _make_stub()
        state = _open(stub)
        kinds = {c["key"]: c["kind"] for c in state["choices"]}
        assert kinds["show"] == "display" and kinds["hide"] == "display"
        assert state["choices"][-1]["kind"] == "cancel"


class TestPanelRenders:
    def test_fragments_render_title_hint_and_every_choice(self):
        stub = _make_stub()
        state = _open(stub)
        frags = CLITuiMixin._get_reasoning_picker_display_fragments(stub)
        text = "".join(t for _s, t in frags)
        assert frags, "a dead panel (empty fragments) is the original bug"
        assert "Reasoning" in text
        assert "Effort: medium" in text
        for choice in state["choices"]:
            assert choice["label"] in text
        assert "❯" in text, "the selected row needs its cursor"
        assert "╭" in text and "╯" in text, "panel chrome missing"

    def test_closed_picker_renders_nothing(self):
        stub = _make_stub()
        stub._reasoning_picker_state = None
        assert CLITuiMixin._get_reasoning_picker_display_fragments(stub) == []


class TestWiringSurvivesMerges:
    """The specific wiring a merge dropped; pinned as source-level invariants.

    These are the only source-reading tests in this file: the failure mode was a merge
    deleting call sites whose helpers stayed behind, and no behavioural test can observe
    "the widget is placed in the layout" without building a real prompt_toolkit app.
    """

    def test_widget_is_built_and_placed_in_the_layout(self):
        assert "reasoning_picker_widget = self._tui_overlay_widget(" in TUI_SRC
        assert "reasoning_picker_widget=reasoning_picker_widget," in TUI_SRC

    def test_layout_builder_accepts_the_widget(self):
        import inspect
        params = inspect.signature(CLITuiMixin._build_tui_layout_children).parameters
        assert "reasoning_picker_widget" in params

    def test_input_is_blocked_while_the_picker_is_open(self):
        """The inverse of the bug: state set + not excluded == stuck composer."""
        assert 'and not getattr(self, "_reasoning_picker_state", None))' in TUI_SRC

    def test_escape_and_nav_keys_are_bound(self):
        assert "kb.add('up', filter=_reasoning_picker)(self._tui_reasoning_picker_up)" in TUI_SRC
        assert "kb.add('down', filter=_reasoning_picker)(self._tui_reasoning_picker_down)" in TUI_SRC
        assert "self._tui_reasoning_picker_escape)" in TUI_SRC

    def test_ctrl_c_can_close_the_picker(self):
        assert '("_reasoning_picker_state", self._close_reasoning_picker)' in TUI_SRC

    def test_widget_filter_tracks_the_state(self):
        stub = _make_stub()
        _open(stub)
        widget = CLITuiMixin._tui_overlay_widget(
            stub, CLITuiMixin._get_reasoning_picker_display_fragments, "_reasoning_picker_state")
        assert bool(widget.filter()) is True
        stub._reasoning_picker_state = None
        assert bool(widget.filter()) is False


class TestNavigation:
    def test_down_and_up_move_the_selection(self):
        stub = _make_stub()
        state = _open(stub)
        start = state["selected"]
        CLITuiMixin._tui_reasoning_picker_down(stub, _FakeEvent())
        assert state["selected"] == start + 1
        CLITuiMixin._tui_reasoning_picker_up(stub, _FakeEvent())
        assert state["selected"] == start

    def test_selection_clamps_at_both_ends(self):
        stub = _make_stub()
        state = _open(stub)
        state["selected"] = 0
        CLITuiMixin._tui_reasoning_picker_up(stub, _FakeEvent())
        assert state["selected"] == 0
        state["selected"] = len(state["choices"]) - 1
        CLITuiMixin._tui_reasoning_picker_down(stub, _FakeEvent())
        assert state["selected"] == len(state["choices"]) - 1

    def test_escape_closes_the_picker(self):
        stub = _make_stub()
        _open(stub)
        CLITuiMixin._tui_reasoning_picker_escape(stub, _FakeEvent())
        assert stub._reasoning_picker_state is None


class TestApply:
    def test_level_choice_reaches_the_agent(self):
        stub = _make_stub()
        _open(stub)
        _pick(stub, "high")
        assert stub.reasoning_config == {"enabled": True, "effort": "high"}
        assert stub.agent is None, "effort change must retire the agent for a rebuild"
        assert stub._reasoning_picker_state is None

    def test_pick_is_session_scoped_by_default(self):
        """A picker pick must not silently rewrite the global default (#86414 parity)."""
        stub = _make_stub()
        _open(stub)
        keys = _pick(stub, "high")
        assert "agent.reasoning_effort" not in keys

    def test_pick_records_the_per_model_default(self):
        """The fork's reasoning_effort_by_model isolation feature is maintained here."""
        stub = _make_stub()
        _open(stub)
        _pick(stub, "high")
        assert stub._reasoning_effort_by_model == {DSV4: "high"}

    def test_persist_global_still_writes_the_global_default(self):
        stub = _make_stub()
        with patch("cli.save_config_value", return_value=True) as save, patch("cli._cprint"):
            stub._apply_reasoning_arg("xhigh", persist_global=True)
        assert "agent.reasoning_effort" in [c.args[0] for c in save.call_args_list]

    def test_display_choice_toggles_show_reasoning(self):
        stub = _make_stub()
        _open(stub)
        _pick(stub, "show")
        assert stub.show_reasoning is True
        assert stub._reasoning_picker_state is None

    def test_cancel_changes_nothing(self):
        stub = _make_stub()
        state = _open(stub)
        state["selected"] = len(state["choices"]) - 1  # Cancel
        with patch("cli.save_config_value", return_value=True), patch("cli._cprint"):
            stub._handle_reasoning_picker_selection()
        assert stub.reasoning_config == {"enabled": True, "effort": "medium"}
        assert stub._reasoning_picker_state is None


class TestTypedFormUnaffected:
    def test_typed_reasoning_level_still_applies(self):
        stub = _make_stub()
        with patch("cli.save_config_value", return_value=True), patch("cli._cprint"):
            stub._handle_reasoning_command("/reasoning high")
        assert stub.reasoning_config == {"enabled": True, "effort": "high"}

    def test_typed_effort_level_still_applies(self):
        stub = _make_stub()
        with patch("cli.save_config_value", return_value=True), patch("cli._cprint"):
            _dispatch_alias(stub, "/effort xhigh")
        assert stub.reasoning_config == {"enabled": True, "effort": "xhigh"}
