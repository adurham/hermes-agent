"""Per-role FALLBACK for ``delegation.model_by_role`` in the delegate_task
dispatch loop.

A dict-form ``model_by_role`` entry that pins a provider may additionally
carry a ``fallback`` sub-key — a second full ``{model, provider, ...}``
bundle, same shape as the primary entry. It exists so a role pinned to a
quota-limited/rate-limited backend (e.g. ``ollama-cloud`` with a weekly
usage cap) can degrade to a backup identity instead of refusing the
dispatch outright.

Two situations, both exercised here:

  1. CONSTRUCTION-TIME: the primary's credential resolution itself fails
     (bad provider config, missing key, ...) — the child is built directly
     on the fallback's own bundle instead of refusing the spawn. Only ONE
     hop: if the fallback ALSO fails to resolve, the spawn is refused
     loudly exactly like today's no-fallback-configured case.
  2. RUNTIME: the primary resolves fine, but the dispatched child's model
     call itself fails at runtime with a retryable-class error. This is
     proven in ``tests/run_agent/test_role_fallback_runtime_activation.py``
     via the REAL ``AIAgent`` retry loop; here we only assert that
     delegate_task attaches the correct one-entry raw fallback chain to
     the child in this situation (never touching/resolving it eagerly —
     that's `_build_child_agent`'s existing ``fallback_model=`` contract).

The real ``_resolve_delegation_credentials`` / ``_resolve_role_credentials``
run here — only ``resolve_runtime_provider`` (the process/network boundary)
is faked — so the resolution chain itself is exercised, not mocked past.
"""

import json
import threading
from unittest.mock import MagicMock

import pytest

from tools.delegate_tool import delegate_task


BATCH_PROVIDER = "anthropic"
ROLE_PROVIDER = "ollama-cloud"
FALLBACK_PROVIDER = "anthropic-fallback"

_RUNTIME_BUNDLES = {
    BATCH_PROVIDER: {
        "provider": BATCH_PROVIDER,
        "base_url": "https://api.anthropic.com",
        "api_key": "ant-batch-key",
        "api_mode": "anthropic_messages",
        "request_overrides": {"batch_marker": True},
        "max_output_tokens": 8192,
        "command": None,
        "args": [],
        "model": "claude-sonnet-4-6",
    },
    ROLE_PROVIDER: {
        "provider": ROLE_PROVIDER,
        "base_url": "https://ollama.com/v1",
        "api_key": "ollama-role-key",
        "api_mode": "chat_completions",
        "request_overrides": {"role_marker": True},
        "max_output_tokens": 4096,
        "command": None,
        "args": [],
        "model": "glm-5.3",
    },
    FALLBACK_PROVIDER: {
        "provider": FALLBACK_PROVIDER,
        "base_url": "https://api.anthropic.com",
        "api_key": "ant-fallback-key",
        "api_mode": "anthropic_messages",
        "request_overrides": {"fallback_marker": True},
        "max_output_tokens": 8192,
        "command": None,
        "args": [],
        "model": "claude-fable-5",
    },
}


def _make_parent(depth=0):
    """Mock parent agent carrying the fields delegate_task reads."""
    parent = MagicMock()
    parent.base_url = "https://api.anthropic.com"
    parent.api_key = "parent-key"
    parent.provider = BATCH_PROVIDER
    parent.api_mode = "anthropic_messages"
    parent.model = "claude-sonnet-4-6"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = depth
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    parent._fallback_chain = []
    emitted = []
    parent._emit_status = lambda msg: emitted.append(msg)
    parent._emitted_status = emitted
    return parent


def _fake_runtime_provider(known=None, raise_for=None):
    """Stand in for hermes_cli.runtime_provider.resolve_runtime_provider.

    ``raise_for`` optionally names a provider key that must raise instead
    of resolving, so a test can force a construction-time credential
    failure for a specific bundle without touching the others.
    """
    bundles = _RUNTIME_BUNDLES if known is None else known

    def _resolve(requested=None, target_model=None, **_kw):
        key = (requested or "").strip().lower()
        if raise_for and key == raise_for:
            raise RuntimeError(f"provider {requested!r} is quota-exhausted")
        if key not in bundles:
            raise RuntimeError(f"Unknown provider {requested!r}")
        return dict(bundles[key])

    return _resolve


@pytest.fixture
def dispatch(monkeypatch):
    """Run delegate_task with credential resolution wired to fake runtimes.

    Returns a callable(tasks, entry_map, **kw) -> (parsed_result, captured,
    parent) where ``captured`` is the list of kwargs passed to
    ``_build_child_preserving_parent_tools`` for every child that was
    actually constructed, and ``parent`` is the mock parent agent (so a
    test can inspect ``parent._emitted_status``).
    """
    import hermes_cli.ruflo_agents as ruflo
    import hermes_cli.runtime_provider as runtime_provider
    import tools.delegate_tool as dt

    def _run(
        tasks,
        entry_map,
        model_map=None,
        known=None,
        raise_for=None,
        batch_cfg=None,
        resolve_spy=None,
    ):
        if model_map is None:
            model_map = {
                role: entry["model"] for role, entry in entry_map.items()
            }

        monkeypatch.setattr(
            ruflo, "get_role_model_map", lambda: dict(model_map), raising=False
        )
        monkeypatch.setattr(
            ruflo, "get_role_entry_map", lambda: dict(entry_map), raising=False
        )
        _resolve_fn = _fake_runtime_provider(known, raise_for=raise_for)
        if resolve_spy is not None:
            _inner = _resolve_fn

            def _resolve_fn(requested=None, target_model=None, **kw):
                resolve_spy((requested or "").strip().lower())
                return _inner(requested=requested, target_model=target_model, **kw)

        monkeypatch.setattr(
            runtime_provider,
            "resolve_runtime_provider",
            _resolve_fn,
        )
        monkeypatch.setattr(
            dt,
            "_load_config",
            lambda: dict(
                batch_cfg
                if batch_cfg is not None
                else {"model": "claude-sonnet-4-6", "provider": BATCH_PROVIDER}
            ),
        )
        import tools.delegation_router as dr

        monkeypatch.setattr(dr, "route_task_models", lambda *a, **k: {})

        captured = []

        def _capture(**kwargs):
            captured.append(kwargs)
            child = MagicMock()
            child._auto_route_info = None
            return child

        monkeypatch.setattr(dt, "_build_child_preserving_parent_tools", _capture)
        monkeypatch.setattr(
            dt,
            "_run_single_child",
            lambda *a, **k: {
                "task_index": 0,
                "status": "completed",
                "summary": "ok",
                "api_calls": 1,
                "duration_seconds": 0.1,
            },
        )

        parent = _make_parent()
        result = json.loads(delegate_task(tasks=tasks, parent_agent=parent))
        return result, captured, parent

    return _run


def _by_agent_type(captured, agent_type):
    for kwargs in captured:
        if kwargs.get("agent_type") == agent_type:
            return kwargs
    raise AssertionError(f"no child constructed for agent_type={agent_type!r}")


TASK_FABLE = {
    "goal": "Coordinate the campaign PM workflow for the day",
    "agent_type": "fable",
}
TASK_BARE = {
    "goal": "Summarize the delegation section of AGENTS.md for the team",
    "agent_type": "researcher",
}

ENTRY_MAP_WITH_FALLBACK = {
    "fable": {
        "model": "glm-5.3",
        "provider": ROLE_PROVIDER,
        "fallback": {"model": "claude-fable-5", "provider": FALLBACK_PROVIDER},
    },
    "researcher": {"model": "claude-haiku-4-5"},
}
ENTRY_MAP_NO_FALLBACK = {
    "fable": {"model": "glm-5.3", "provider": ROLE_PROVIDER},
    "researcher": {"model": "claude-haiku-4-5"},
}


class TestPrimarySuccessNeverTouchesFallback:
    """(a) Primary resolves fine -> the fallback bundle is never resolved,
    only attached (lazily, as a raw dict) for the runtime retry path."""

    def test_primary_success_dispatches_on_primary_bundle(self, dispatch):
        _result, captured, _parent = dispatch(
            [TASK_FABLE, TASK_BARE], ENTRY_MAP_WITH_FALLBACK
        )

        fable = _by_agent_type(captured, "fable")
        assert fable["override_provider"] == ROLE_PROVIDER
        assert fable["model"] == "glm-5.3"
        assert fable["override_api_key"] == "ollama-role-key"

    def test_primary_success_does_not_call_resolve_for_fallback_provider(
        self, dispatch
    ):
        """The fallback's own credentials are never resolved on the
        success path -- only attached as a raw, unresolved dict, exactly
        like the top-level ``fallback_providers`` config entries."""
        resolved_providers = []
        dispatch(
            [TASK_FABLE, TASK_BARE],
            ENTRY_MAP_WITH_FALLBACK,
            resolve_spy=resolved_providers.append,
        )

        assert FALLBACK_PROVIDER not in resolved_providers, (
            f"fallback provider was resolved on the success path: "
            f"{resolved_providers}"
        )
        assert ROLE_PROVIDER in resolved_providers

    def test_primary_success_no_fallback_engaged_notice(self, dispatch):
        """No visible fallback notice is emitted when the primary just works."""
        _result, _captured, parent = dispatch(
            [TASK_FABLE, TASK_BARE], ENTRY_MAP_WITH_FALLBACK
        )
        assert not any("Fallback engaged" in m for m in parent._emitted_status)

    def test_primary_success_attaches_the_raw_fallback_entry_to_the_child(
        self, dispatch
    ):
        """The child receives a one-entry runtime chain for the API-call
        retry path, carrying the fallback's own (unresolved) identity."""
        _result, captured, _parent = dispatch(
            [TASK_FABLE, TASK_BARE], ENTRY_MAP_WITH_FALLBACK
        )

        fable = _by_agent_type(captured, "fable")
        chain = fable["override_fallback_chain"]
        assert chain == [{"model": "claude-fable-5", "provider": FALLBACK_PROVIDER}]

    def test_no_fallback_declared_leaves_chain_none(self, dispatch):
        """Backward compatibility: a role with no ``fallback`` sub-key gets
        ``override_fallback_chain=None`` exactly like before this feature."""
        _result, captured, _parent = dispatch(
            [TASK_FABLE, TASK_BARE], ENTRY_MAP_NO_FALLBACK
        )

        fable = _by_agent_type(captured, "fable")
        assert fable["override_fallback_chain"] is None
        bare = _by_agent_type(captured, "researcher")
        assert bare["override_fallback_chain"] is None


class TestPrimaryCredentialFailureEngagesFallback:
    """(b) Primary credential resolution fails -> fallback engages and its
    OWN credentials are resolved, never mixed with the primary's."""

    def test_fallback_engages_and_dispatches_on_its_own_bundle(self, dispatch):
        _result, captured, _parent = dispatch(
            [TASK_FABLE, TASK_BARE],
            ENTRY_MAP_WITH_FALLBACK,
            raise_for=ROLE_PROVIDER,
        )

        fable = _by_agent_type(captured, "fable")
        assert fable["override_provider"] == FALLBACK_PROVIDER
        assert fable["override_provider"] != ROLE_PROVIDER
        assert fable["model"] == "claude-fable-5"
        assert fable["override_api_key"] == "ant-fallback-key"
        assert fable["override_base_url"] == "https://api.anthropic.com"

    def test_fallback_bundle_never_mixes_with_primary_fields(self, dispatch):
        _result, captured, _parent = dispatch(
            [TASK_FABLE, TASK_BARE],
            ENTRY_MAP_WITH_FALLBACK,
            raise_for=ROLE_PROVIDER,
        )

        fable = _by_agent_type(captured, "fable")
        role_bundle = _RUNTIME_BUNDLES[ROLE_PROVIDER]
        for key, role_value in (
            ("override_base_url", role_bundle["base_url"]),
            ("override_api_key", role_bundle["api_key"]),
            ("override_api_mode", role_bundle["api_mode"]),
            ("override_request_overrides", role_bundle["request_overrides"]),
        ):
            assert fable[key] != role_value, f"{key} leaked from the primary bundle"

    def test_fallback_engaged_child_has_no_further_runtime_fallback(
        self, dispatch
    ):
        """One hop only: a child dispatched directly on the fallback bundle
        gets no runtime chain -- the hop was already spent."""
        _result, captured, _parent = dispatch(
            [TASK_FABLE, TASK_BARE],
            ENTRY_MAP_WITH_FALLBACK,
            raise_for=ROLE_PROVIDER,
        )

        fable = _by_agent_type(captured, "fable")
        assert fable["override_fallback_chain"] is None

    def test_fallback_engaged_emits_a_visible_notice(self, dispatch):
        _result, _captured, parent = dispatch(
            [TASK_FABLE, TASK_BARE],
            ENTRY_MAP_WITH_FALLBACK,
            raise_for=ROLE_PROVIDER,
        )

        assert any(
            "Fallback engaged" in m and "fable" in m for m in parent._emitted_status
        ), parent._emitted_status

    def test_sibling_in_same_batch_is_unaffected(self, dispatch):
        """Per-child isolation: the bare-string sibling is untouched by the
        pinned role's primary failure."""
        _result, captured, _parent = dispatch(
            [TASK_FABLE, TASK_BARE],
            ENTRY_MAP_WITH_FALLBACK,
            raise_for=ROLE_PROVIDER,
        )

        bare = _by_agent_type(captured, "researcher")
        assert bare["override_provider"] == BATCH_PROVIDER
        assert bare["model"] == "claude-haiku-4-5"


class TestBothPrimaryAndFallbackFail:
    """(e) Both fail -> loud raise naming both, no silent third attempt."""

    def test_both_unresolvable_returns_tool_error_naming_both(self, dispatch):
        entry_map = {
            "fable": {
                "model": "glm-5.3",
                "provider": ROLE_PROVIDER,
                "fallback": {
                    "model": "claude-fable-5",
                    "provider": "no-such-fallback-provider",
                },
            },
            "researcher": {"model": "claude-haiku-4-5"},
        }
        result, captured, _parent = dispatch(
            [TASK_FABLE, TASK_BARE], entry_map, raise_for=ROLE_PROVIDER
        )

        assert "error" in result, result
        assert "fable" in result["error"]
        assert ROLE_PROVIDER in result["error"]
        assert "no-such-fallback-provider" in result["error"]
        # No child at all -- the whole batch refuses (matches today's
        # single-unresolvable-pin behavior; delegate_task fails the batch
        # eagerly on the first ValueError from a child's preflight).
        assert not any(
            kwargs.get("agent_type") == "fable" for kwargs in captured
        )

    def test_no_fallback_declared_at_all_keeps_todays_exact_behavior(
        self, dispatch
    ):
        """Regression guard: a role with NO fallback sub-key still refuses
        the spawn loudly on primary failure, unchanged from before this
        feature (test_delegate_role_provider.py already pins this; this is
        the same contract restated against the new code path)."""
        result, captured, _parent = dispatch(
            [TASK_FABLE, TASK_BARE],
            ENTRY_MAP_NO_FALLBACK,
            raise_for=ROLE_PROVIDER,
        )

        assert "error" in result, result
        assert "fable" in result["error"]
        assert not any(
            kwargs.get("agent_type") == "fable" for kwargs in captured
        )


class TestFallbackRequiresPrimaryProvider:
    """A ``fallback`` sub-key on a role with no ``provider`` of its own is
    dropped at normalization time (hermes_cli/personas.py) -- there is
    nothing for it to be a fallback FOR. This is asserted at the personas
    layer in test_personas.py; here we just confirm dispatch behaves
    exactly as if no fallback existed for such an entry."""

    def test_bare_string_role_ignores_a_stray_fallback_key(self, dispatch):
        # get_role_entry_map() would never actually produce this shape for
        # a bare-string role (only real dict entries reach here), but the
        # dispatch loop's own guard (``if _role_provider and isinstance(...)``)
        # must independently refuse to treat a fallback as meaningful
        # without a primary provider pin, so simulate the entry map
        # directly rather than going through YAML parsing.
        entry_map = {
            "fable": {"model": "claude-haiku-4-5"},  # no provider
            "researcher": {"model": "claude-haiku-4-5"},
        }
        _result, captured, _parent = dispatch(
            [TASK_FABLE, TASK_BARE], entry_map
        )

        fable = _by_agent_type(captured, "fable")
        assert fable["override_provider"] == BATCH_PROVIDER
        assert fable["override_fallback_chain"] is None


class TestEndToEndWithRealEntryMapAndTempHermesHome:
    """E2E: a REAL ``~/.hermes/config.yaml`` (temp HERMES_HOME) drives the
    ``fallback`` sub-key all the way from YAML through
    ``hermes_cli.personas.get_role_entry_map()`` normalization into the
    delegate_task dispatch loop. Only ``resolve_runtime_provider`` (the
    process/network boundary) is faked -- everything else, including
    config parsing, is real (AGENTS.md: E2E with real imports against a
    temp HERMES_HOME, not mocks of the seam under test)."""

    def test_yaml_fallback_subkey_reaches_dispatch_on_primary_failure(
        self, tmp_path, monkeypatch
    ):
        import hermes_cli.config as hc
        import hermes_cli.runtime_provider as runtime_provider
        import tools.delegate_tool as dt
        import tools.delegation_router as dr

        home = tmp_path / "hermes"
        home.mkdir()
        (home / "config.yaml").write_text(
            "model:\n  default: claude-sonnet-4-6\n"
            "delegation:\n"
            "  model_by_role:\n"
            "    fable:\n"
            "      model: glm-5.3\n"
            "      provider: ollama-cloud\n"
            "      fallback:\n"
            "        model: claude-fable-5\n"
            "        provider: anthropic-fallback\n"
            "    researcher: claude-haiku-4-5\n"
        )
        monkeypatch.setenv("HERMES_HOME", str(home))
        hc._LOAD_CONFIG_CACHE.clear()

        monkeypatch.setattr(
            runtime_provider,
            "resolve_runtime_provider",
            _fake_runtime_provider(raise_for=ROLE_PROVIDER),
        )
        monkeypatch.setattr(
            dt,
            "_load_config",
            lambda: {"model": "claude-sonnet-4-6", "provider": BATCH_PROVIDER},
        )
        monkeypatch.setattr(dr, "route_task_models", lambda *a, **k: {})

        captured = []

        def _capture(**kwargs):
            captured.append(kwargs)
            child = MagicMock()
            child._auto_route_info = None
            return child

        monkeypatch.setattr(dt, "_build_child_preserving_parent_tools", _capture)
        monkeypatch.setattr(
            dt,
            "_run_single_child",
            lambda *a, **k: {
                "task_index": 0,
                "status": "completed",
                "summary": "ok",
                "api_calls": 1,
                "duration_seconds": 0.1,
            },
        )

        try:
            parent = _make_parent()
            result = json.loads(
                delegate_task(
                    tasks=[TASK_FABLE, TASK_BARE], parent_agent=parent
                )
            )
        finally:
            hc._LOAD_CONFIG_CACHE.clear()

        assert "results" in result, result
        fable = _by_agent_type(captured, "fable")
        assert fable["override_provider"] == FALLBACK_PROVIDER
        assert fable["model"] == "claude-fable-5"
        bare = _by_agent_type(captured, "researcher")
        assert bare["override_provider"] == BATCH_PROVIDER

    def test_yaml_no_fallback_subkey_is_backward_compatible(
        self, tmp_path, monkeypatch
    ):
        """A config.yaml with no ``fallback`` sub-key at all behaves
        byte-for-byte like before this feature shipped."""
        import hermes_cli.config as hc
        import hermes_cli.runtime_provider as runtime_provider
        import tools.delegate_tool as dt
        import tools.delegation_router as dr

        home = tmp_path / "hermes"
        home.mkdir()
        (home / "config.yaml").write_text(
            "model:\n  default: claude-sonnet-4-6\n"
            "delegation:\n"
            "  model_by_role:\n"
            "    fable:\n"
            "      model: glm-5.3\n"
            "      provider: ollama-cloud\n"
            "    researcher: claude-haiku-4-5\n"
        )
        monkeypatch.setenv("HERMES_HOME", str(home))
        hc._LOAD_CONFIG_CACHE.clear()

        monkeypatch.setattr(
            runtime_provider, "resolve_runtime_provider", _fake_runtime_provider()
        )
        monkeypatch.setattr(
            dt,
            "_load_config",
            lambda: {"model": "claude-sonnet-4-6", "provider": BATCH_PROVIDER},
        )
        monkeypatch.setattr(dr, "route_task_models", lambda *a, **k: {})

        captured = []

        def _capture(**kwargs):
            captured.append(kwargs)
            child = MagicMock()
            child._auto_route_info = None
            return child

        monkeypatch.setattr(dt, "_build_child_preserving_parent_tools", _capture)
        monkeypatch.setattr(
            dt,
            "_run_single_child",
            lambda *a, **k: {
                "task_index": 0,
                "status": "completed",
                "summary": "ok",
                "api_calls": 1,
                "duration_seconds": 0.1,
            },
        )

        try:
            parent = _make_parent()
            result = json.loads(
                delegate_task(
                    tasks=[TASK_FABLE, TASK_BARE], parent_agent=parent
                )
            )
        finally:
            hc._LOAD_CONFIG_CACHE.clear()

        assert "results" in result, result
        fable = _by_agent_type(captured, "fable")
        assert fable["override_provider"] == ROLE_PROVIDER
        assert fable["override_fallback_chain"] is None
