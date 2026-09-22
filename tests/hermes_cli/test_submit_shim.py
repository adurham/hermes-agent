"""The `hermes submit` deprecation shim.

``hermes submit`` (fork-only ``hermes_cli/submit.py``) was retired in an
owner-approved consolidation into upstream's ``hermes peer run``, after its
three distinguishing capabilities were ported onto ``hermes_cli/subcommands/
peer.py``: SSE ``--tail`` streaming, the widened flag/env/.env/default
credential chain (reserved target name ``default``), and session-less
operation (``--no-session``).

The command is kept for ONE release as a shim that prints the translated
``hermes peer run`` invocation and exits 2, rather than a hard argparse
"invalid choice" break. Coverage for the capabilities themselves lives in
``tests/hermes_cli/test_peer_cmd.py``; these tests only pin the shim's
contract, so deleting the shim means deleting this file.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli.main import cmd_submit


def _args(**kw):
    defaults = {
        "prompt": [], "file": None, "instructions": None, "gateway_url": None,
        "api_key": None, "tail": False, "tail_run": None, "quiet": False}
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _run(capsys, **kw) -> str:
    with pytest.raises(SystemExit) as exc:
        cmd_submit(_args(**kw))
    # Exit 2 (usage error), never 0: a script that silently kept "working"
    # while doing nothing would be worse than a loud failure.
    assert exc.value.code == 2
    return capsys.readouterr().err


def test_submit_module_is_gone():
    """The implementation is deleted, not merely unreferenced."""
    with pytest.raises(ImportError):
        import hermes_cli.submit  # noqa: F401


def test_shim_names_the_replacement_command(capsys):
    err = _run(capsys, prompt=["do", "the", "thing"])
    assert "retired" in err
    assert 'hermes peer run default "do the thing" --no-session' in err


def test_shim_translates_tail(capsys):
    err = _run(capsys, prompt=["x"], tail=True)
    assert 'hermes peer run default "x" --no-session --tail' in err


def test_shim_translates_tail_run_to_the_tail_action(capsys):
    """``--tail-run <id>`` submitted nothing; its replacement is `peer tail`."""
    err = _run(capsys, tail_run="run_abc123")
    assert "hermes peer tail default run_abc123" in err
    # Not a `peer run` invocation — nothing should be submitted.
    assert "peer run" not in err.split("Run this instead:")[1].split("Notes:")[0]


@pytest.mark.parametrize("kw, expected", [
    ({"file": "task.md"}, "--file task.md"),
    ({"instructions": "be terse"}, "--instructions be terse"),
    ({"gateway_url": "http://gw:8642"}, "--url http://gw:8642"),
    ({"api_key": "k"}, "--api-key k"),
    ({"quiet": True}, "-q"),
], ids=["file", "instructions", "gateway-url->url", "api-key", "quiet"])
def test_shim_translates_each_flag(capsys, kw, expected):
    """Every old flag maps to its new spelling, so the printed command is
    directly runnable rather than something the user must re-derive."""
    assert expected in _run(capsys, prompt=["x"], **kw)


def test_shim_explains_the_session_tradeoff(capsys):
    """--no-session reproduces submit's behavior, but the user should know the
    default (a durable remote transcript) is available by dropping it."""
    err = _run(capsys, prompt=["x"])
    assert "Bot Chat" in err
    assert "transcript" in err


def test_shim_warns_about_positional_ordering(capsys):
    """peer's optional `message` positional cannot follow a flag (pre-existing
    upstream argparse shape), so the shim says so up front."""
    assert "BEFORE any flag" in _run(capsys, prompt=["x"])


def test_submit_still_parses_so_the_shim_is_reachable():
    """The shim is useless if argparse rejects `submit` before dispatch."""
    from hermes_cli.main import _build_cli_parser

    parser, _ = _build_cli_parser()
    args = parser.parse_args(["submit", "--tail", "hello"])
    assert args.func is cmd_submit
    assert args.prompt == ["hello"]
    assert args.tail is True
