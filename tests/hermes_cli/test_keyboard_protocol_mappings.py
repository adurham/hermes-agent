"""Regression tests for kitty keyboard protocol → prompt_toolkit ANSI mappings.

Under kitty's "disambiguate escape codes" flag (CSI > 1 u, which Hermes
pushes at startup), bare Esc and other modified keys arrive as CSI-u
sequences instead of their legacy bytes.  prompt_toolkit's built-in
ANSI_SEQUENCES table only knows the legacy forms, so without our shim
they leak into the input buffer as literal text (e.g. "[27u" for Esc)
and every kb.add('escape', ...) handler stops working.

These tests pin the mappings so a future refactor can't silently lose
the bare-Esc binding the user actually pressed in #the-screenshot-bug.
"""
from __future__ import annotations


def test_register_prompt_toolkit_keys_maps_bare_escape():
    """`\\x1b[27u` (bare Esc under kitty disambiguate) → Keys.Escape."""
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
    from prompt_toolkit.keys import Keys

    from hermes_cli.keyboard_protocol import register_prompt_toolkit_keys

    register_prompt_toolkit_keys()

    # The headline fix from the screenshot bug: pressing Esc with the
    # protocol active must arrive as Keys.Escape, not literal "[27u".
    assert ANSI_SEQUENCES.get("\x1b[27u") is Keys.Escape


def test_register_prompt_toolkit_keys_maps_escape_modifier_variants():
    """Modifier-stripped Esc variants all collapse to Keys.Escape.

    The kitty spec encodes modifiers as `1 + (shift=1 + alt=2 + ctrl=4)`,
    so `;1u` (no modifier), `;2u` (Shift), and `;5u` (Ctrl) all describe
    forms of Escape we want to treat as plain Esc.
    """
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
    from prompt_toolkit.keys import Keys

    from hermes_cli.keyboard_protocol import register_prompt_toolkit_keys

    register_prompt_toolkit_keys()

    for seq in ("\x1b[27u", "\x1b[27;1u", "\x1b[27;2u", "\x1b[27;5u"):
        assert ANSI_SEQUENCES.get(seq) is Keys.Escape, f"{seq!r} not mapped to Escape"


def test_register_prompt_toolkit_keys_is_idempotent():
    """Re-registering must not raise or corrupt the existing mappings."""
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
    from prompt_toolkit.keys import Keys

    from hermes_cli.keyboard_protocol import register_prompt_toolkit_keys

    register_prompt_toolkit_keys()
    register_prompt_toolkit_keys()
    register_prompt_toolkit_keys()

    assert ANSI_SEQUENCES.get("\x1b[27u") is Keys.Escape
