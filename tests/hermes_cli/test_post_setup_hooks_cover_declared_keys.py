"""Every provider-declared ``post_setup`` key must resolve to a registered hook.

A provider row (bundled or plugin) advertises ``post_setup: <key>`` so that picking
it in ``hermes tools`` pip-installs its optional dependency. ``valid_post_setup_keys()``
derives its allowlist from exactly those declarations, so a declared key with no
entry in ``_POST_SETUP_HOOKS`` passes validation and then silently no-ops:
``_run_post_setup`` resolves through ``.get(key, lambda: None)``.

That is how the fork's ``trafilatura`` extract backend (``plugins/web/trafilatura``)
lost its installer: the key survived the upstream refactor that replaced the if/elif
``_run_post_setup`` ladder with the ``_POST_SETUP_HOOKS`` table, the installation
branch did not. A user picking Trafilatura got "Saved" and a web_extract backend
whose package was never installed.

This is a contract between two pieces of data, not a snapshot: it holds for any
provider set, so new no-key providers get the same protection.
"""

from __future__ import annotations


def test_every_declared_post_setup_key_has_a_hook():
    from hermes_cli.tools_config_post_setup import _POST_SETUP_HOOKS, valid_post_setup_keys

    missing = sorted(valid_post_setup_keys() - set(_POST_SETUP_HOOKS))
    assert not missing, (
        f"provider(s) declare post_setup key(s) with no registered hook: {missing} — "
        "picking that provider would silently skip its dependency install. "
        "Add a _PIP_POST_SETUP_HOOKS entry (or a bespoke _POST_SETUP_HOOKS hook) for each key."
    )


def test_trafilatura_post_setup_installs_the_package(monkeypatch):
    """The extract backend's key installs `trafilatura` through the pip path, and the
    pip hook's own state check is what makes the call idempotent."""
    from hermes_cli import tools_config_post_setup as tps

    assert tps._POST_SETUP_HOOKS["trafilatura"]  # resolves to a real callable
    spec = tps._PIP_POST_SETUP_HOOKS["trafilatura"]
    assert spec["module"] == "trafilatura"
    assert "trafilatura" in spec["args"]

    calls = []
    monkeypatch.setattr(tps, "_pip_install", lambda args, **kw: calls.append(args) or _ok())
    monkeypatch.setattr(tps, "_importable", lambda module: False)  # package absent -> install

    tps._run_post_setup("trafilatura")

    assert calls == [spec["args"]], f"trafilatura install was not attempted: {calls!r}"


class _ok:
    returncode = 0
    stderr = ""
    stdout = ""
