"""Per-role provider overrides in the delegate_task dispatch loop.

``delegation.model_by_role`` entries may be a bare model string (legacy) or a
dict declaring their own ``provider`` (plus optional base_url/api_key/
api_mode), mirroring ``delegation.by_provider``. A role that pins a provider
must dispatch its children on THAT provider's full credential bundle — before
this, every child in a batch was built with the single batch-level bundle, so
a role pinned to another provider sent its model slug to the wrong endpoint
and 404'd.

These are behavior contracts on what actually reaches child construction:
which credential bundle each child gets, that siblings stay isolated, that the
whole bundle travels together, and that an unresolvable pin refuses the spawn
instead of silently falling back.

The real ``_resolve_delegation_credentials`` / ``_resolve_role_credentials``
run here — only ``resolve_runtime_provider`` (the process/network boundary) is
faked — so the resolution chain itself is exercised, not mocked past.
"""

import json
import threading
from unittest.mock import MagicMock, patch

import pytest

from tools.delegate_tool import delegate_task


BATCH_PROVIDER = "anthropic"
ROLE_PROVIDER = "ollama-cloud"

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
        "model": "gpt-oss:120b-cloud",
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
    return parent


def _fake_runtime_provider(known=None):
    """Stand in for hermes_cli.runtime_provider.resolve_runtime_provider."""
    bundles = _RUNTIME_BUNDLES if known is None else known

    def _resolve(requested=None, target_model=None, **_kw):
        key = (requested or "").strip().lower()
        if key not in bundles:
            raise RuntimeError(f"Unknown provider {requested!r}")
        return dict(bundles[key])

    return _resolve


@pytest.fixture
def dispatch(monkeypatch):
    """Run delegate_task with credential resolution wired to fake runtimes.

    Returns a callable(tasks, entry_map, model_map=None, known=None) →
    (parsed_result, captured_child_kwargs).
    """
    import hermes_cli.ruflo_agents as ruflo
    import hermes_cli.runtime_provider as runtime_provider
    import tools.delegate_tool as dt

    def _run(tasks, entry_map, model_map=None, known=None, batch_cfg=None,
             entry_map_raises=False):
        if model_map is None:
            model_map = {
                role: entry["model"] for role, entry in entry_map.items()
            }

        def _entry_map_fn():
            if entry_map_raises:
                raise RuntimeError("entry map unavailable")
            return dict(entry_map)

        monkeypatch.setattr(
            ruflo, "get_role_model_map", lambda: dict(model_map), raising=False
        )
        monkeypatch.setattr(
            ruflo, "get_role_entry_map", _entry_map_fn, raising=False
        )
        monkeypatch.setattr(
            runtime_provider,
            "resolve_runtime_provider",
            _fake_runtime_provider(known),
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
        # Auto-route must not fire: every task here states an agent_type, but
        # pin it off anyway so no auxiliary LLM call can be attempted.
        # delegate_task imports this symbol from the router module at call
        # time, so patch it at its source.
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

        result = json.loads(delegate_task(tasks=tasks, parent_agent=_make_parent()))
        return result, captured

    return _run


def _by_agent_type(captured, agent_type):
    for kwargs in captured:
        if kwargs.get("agent_type") == agent_type:
            return kwargs
    raise AssertionError(f"no child constructed for agent_type={agent_type!r}")


TASK_ROLE_PINNED = {
    "goal": "Port the retry helper in tools/http.py to the new backoff API",
    "agent_type": "jr-coder",
}
TASK_BARE = {
    "goal": "Summarize the delegation section of AGENTS.md for the team",
    "agent_type": "researcher",
}

ENTRY_MAP_MIXED = {
    "jr-coder": {"model": "qwen3-coder:480b-cloud", "provider": ROLE_PROVIDER},
    "researcher": {"model": "claude-haiku-4-5"},
}
ENTRY_MAP_BARE_ONLY = {
    "jr-coder": {"model": "claude-haiku-4-5"},
    "researcher": {"model": "claude-haiku-4-5"},
}


class TestRoleProviderReachesChild:
    def test_role_pinned_child_gets_its_own_provider(self, dispatch):
        """A role entry declaring a provider dispatches on THAT provider."""
        _result, captured = dispatch(
            [TASK_ROLE_PINNED, TASK_BARE], ENTRY_MAP_MIXED
        )

        pinned = _by_agent_type(captured, "jr-coder")
        assert pinned["override_provider"] == ROLE_PROVIDER
        assert pinned["override_provider"] != BATCH_PROVIDER
        # The role's model still comes from the role map, and it is now paired
        # with the provider that can actually serve it.
        assert pinned["model"] == "qwen3-coder:480b-cloud"

    def test_sibling_in_same_batch_keeps_batch_credentials(self, dispatch):
        """Per-child isolation: a bare-string sibling is untouched."""
        _result, captured = dispatch(
            [TASK_ROLE_PINNED, TASK_BARE], ENTRY_MAP_MIXED
        )

        bare = _by_agent_type(captured, "researcher")
        batch = _RUNTIME_BUNDLES[BATCH_PROVIDER]
        assert bare["override_provider"] == BATCH_PROVIDER
        assert bare["override_base_url"] == batch["base_url"]
        assert bare["override_api_key"] == batch["api_key"]
        assert bare["override_api_mode"] == batch["api_mode"]

    def test_whole_bundle_travels_together(self, dispatch):
        """No mixing: the pinned child gets none of the batch bundle's fields."""
        _result, captured = dispatch(
            [TASK_ROLE_PINNED, TASK_BARE], ENTRY_MAP_MIXED
        )

        pinned = _by_agent_type(captured, "jr-coder")
        role = _RUNTIME_BUNDLES[ROLE_PROVIDER]
        batch = _RUNTIME_BUNDLES[BATCH_PROVIDER]

        assert pinned["override_base_url"] == role["base_url"]
        assert pinned["override_api_key"] == role["api_key"]
        assert pinned["override_api_mode"] == role["api_mode"]
        assert pinned["override_request_overrides"] == role["request_overrides"]
        assert pinned["override_max_tokens"] == role["max_output_tokens"]
        assert pinned["override_acp_command"] == role["command"]
        assert pinned["override_acp_args"] == role["args"]

        for key, batch_value in (
            ("override_base_url", batch["base_url"]),
            ("override_api_key", batch["api_key"]),
            ("override_api_mode", batch["api_mode"]),
            ("override_request_overrides", batch["request_overrides"]),
            ("override_max_tokens", batch["max_output_tokens"]),
        ):
            assert pinned[key] != batch_value, f"{key} leaked from batch bundle"

    def test_role_child_never_inherits_other_providers_default_model(
        self, dispatch
    ):
        """The config-level model fallback follows the bundle in use.

        A role entry always carries a model, so this asserts the pinned child's
        model is the role's — never the batch provider's default.
        """
        _result, captured = dispatch(
            [TASK_ROLE_PINNED, TASK_BARE], ENTRY_MAP_MIXED
        )

        pinned = _by_agent_type(captured, "jr-coder")
        assert pinned["model"] != _RUNTIME_BUNDLES[BATCH_PROVIDER]["model"]
        assert pinned["model"] != "claude-sonnet-4-6"


class TestBackwardCompatibility:
    def test_all_bare_string_roles_get_the_batch_bundle(self, dispatch):
        """Legacy configs are byte-for-byte unchanged: one bundle for all."""
        _result, captured = dispatch(
            [TASK_ROLE_PINNED, TASK_BARE], ENTRY_MAP_BARE_ONLY
        )

        assert len(captured) == 2
        batch = _RUNTIME_BUNDLES[BATCH_PROVIDER]
        for kwargs in captured:
            assert kwargs["override_provider"] == BATCH_PROVIDER
            assert kwargs["override_base_url"] == batch["base_url"]
            assert kwargs["override_api_key"] == batch["api_key"]
            assert kwargs["override_api_mode"] == batch["api_mode"]
            assert kwargs["override_request_overrides"] == batch["request_overrides"]
            assert kwargs["override_max_tokens"] == batch["max_output_tokens"]
            assert kwargs["override_acp_command"] == batch["command"]
            assert kwargs["override_acp_args"] == batch["args"]
            # Role map still supplies the model, exactly as before.
            assert kwargs["model"] == "claude-haiku-4-5"

    def test_entry_map_unavailable_degrades_to_batch_bundle(self, dispatch):
        """A broken/missing entry-map API must not break dispatch.

        The parallel-worker API may be absent on older configs; the guard must
        degrade to {} — i.e. every child falls back to the batch bundle —
        rather than taking dispatch down.
        """
        result, captured = dispatch(
            [TASK_ROLE_PINNED, TASK_BARE],
            ENTRY_MAP_MIXED,
            entry_map_raises=True,
        )

        assert "results" in result, result
        assert len(captured) == 2
        for kwargs in captured:
            assert kwargs["override_provider"] == BATCH_PROVIDER


class TestFailureIsLoud:
    def test_unresolvable_role_provider_returns_tool_error(self, dispatch):
        """An unresolvable pin refuses the spawn and names role + provider."""
        entry_map = {
            "jr-coder": {"model": "qwen3-coder:480b-cloud", "provider": "no-such-provider"},
            "researcher": {"model": "claude-haiku-4-5"},
        }
        result, captured = dispatch([TASK_ROLE_PINNED, TASK_BARE], entry_map)

        assert "error" in result, result
        assert "jr-coder" in result["error"]
        assert "no-such-provider" in result["error"]

    def test_unresolvable_role_provider_does_not_dispatch_on_batch_provider(
        self, dispatch
    ):
        """The whole point: never silently fall back to the batch provider."""
        entry_map = {
            "jr-coder": {"model": "qwen3-coder:480b-cloud", "provider": "no-such-provider"},
            "researcher": {"model": "claude-haiku-4-5"},
        }
        _result, captured = dispatch([TASK_ROLE_PINNED, TASK_BARE], entry_map)

        for kwargs in captured:
            if kwargs.get("agent_type") == "jr-coder":
                raise AssertionError(
                    "pinned child was dispatched despite unresolvable provider: "
                    f"override_provider={kwargs.get('override_provider')!r}"
                )


class TestRoleCredentialMemoization:
    def test_same_role_resolves_provider_once_per_batch(self, monkeypatch):
        """N children on one pinned role must not re-resolve N times."""
        import tools.delegate_tool as dt

        calls = []
        real = dt._resolve_delegation_credentials

        def _counting(cfg, parent_agent):
            calls.append(dict(cfg))
            return real(cfg, parent_agent)

        monkeypatch.setattr(dt, "_resolve_delegation_credentials", _counting)

        cache = {}
        entry = {"model": "qwen3-coder:480b-cloud", "provider": ROLE_PROVIDER}
        import hermes_cli.runtime_provider as runtime_provider

        monkeypatch.setattr(
            runtime_provider, "resolve_runtime_provider", _fake_runtime_provider()
        )

        first = dt._resolve_role_credentials(entry, _make_parent(), cache)
        second = dt._resolve_role_credentials(dict(entry), _make_parent(), cache)

        assert first is second
        assert len(calls) == 1

    def test_role_resolution_reuses_the_shared_credential_resolver(self):
        """Reuse contract: role bundles come from _resolve_delegation_credentials."""
        import tools.delegate_tool as dt

        seen = {}

        def _fake(cfg, parent_agent):
            seen.update(cfg)
            return {
                "model": cfg.get("model"),
                "provider": cfg.get("provider"),
                "base_url": None,
                "api_key": None,
                "api_mode": None,
            }

        with patch.object(dt, "_resolve_delegation_credentials", _fake):
            out = dt._resolve_role_credentials(
                {
                    "model": "m",
                    "provider": "p",
                    "base_url": "http://x/v1",
                    "api_mode": "chat_completions",
                },
                _make_parent(),
                {},
            )

        # Synthetic cfg mirrors delegation config shape and carries no
        # by_provider key (that branch must stay skipped for a role entry).
        assert seen["model"] == "m"
        assert seen["provider"] == "p"
        assert seen["base_url"] == "http://x/v1"
        assert seen["api_mode"] == "chat_completions"
        assert "by_provider" not in seen
        assert out["provider"] == "p"

    def test_role_resolution_propagates_valueerror(self, monkeypatch):
        """Resolution failure must surface, never be swallowed into a bundle."""
        import tools.delegate_tool as dt

        def _boom(cfg, parent_agent):
            raise ValueError("nope")

        monkeypatch.setattr(dt, "_resolve_delegation_credentials", _boom)
        with pytest.raises(ValueError):
            dt._resolve_role_credentials({"model": "m", "provider": "p"}, None, {})


class TestEndToEndWithRealEntryMap:
    """E2E: the REAL get_role_entry_map (no fake) drives dispatch.

    Everything from a raw ``delegation.model_by_role`` config block through
    personas normalization, the delegate_task loop, and credential
    resolution runs for real; only ``resolve_runtime_provider`` (the
    process/network boundary) is faked. This is the path a user's config.yaml
    actually takes — a mocked entry map could pass while the real
    normalization shape disagrees.
    """

    def test_raw_config_dict_entry_routes_child_to_its_provider(
        self, monkeypatch
    ):
        import hermes_cli.runtime_provider as runtime_provider
        import tools.delegate_tool as dt
        import tools.delegation_router as dr

        # Raw config as a user would write it: one dict entry pinning a
        # provider, one legacy bare string.
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {
                "delegation": {
                    "model_by_role": {
                        "jr-coder": {
                            "model": "qwen3-coder:480b-cloud",
                            "provider": ROLE_PROVIDER,
                        },
                        "researcher": "claude-haiku-4-5",
                    }
                }
            },
        )
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
            return MagicMock()

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

        result = json.loads(
            delegate_task(
                tasks=[TASK_ROLE_PINNED, TASK_BARE], parent_agent=_make_parent()
            )
        )
        assert "results" in result, result

        pinned = _by_agent_type(captured, "jr-coder")
        bare = _by_agent_type(captured, "researcher")

        # The dict entry's provider wins for its child...
        assert pinned["override_provider"] == ROLE_PROVIDER
        assert pinned["override_base_url"] == _RUNTIME_BUNDLES[ROLE_PROVIDER]["base_url"]
        assert pinned["model"] == "qwen3-coder:480b-cloud"
        # ...and the bare-string sibling still rides the batch bundle.
        assert bare["override_provider"] == BATCH_PROVIDER
        assert bare["model"] == "claude-haiku-4-5"


class TestRouterToleratesDictEntries:
    """route_task_models is handed the FLATTENED string map by delegate_task,
    but plugins/tests call it directly — a dict-form value must still yield a
    string route["model"], never a dict."""

    def _route(self, role_model_map, monkeypatch):
        from tools import delegation_router as dr

        monkeypatch.setattr(
            dr, "_classify", lambda pending, **kw: {0: ("standard", "why", "")}
        )
        return dr.route_task_models(
            [{"goal": "Refactor the retry helper to use the new backoff API"}],
            role_model_map,
            {"auto_route": {"enabled": True}},
            "anthropic",
        )

    def test_dict_entry_yields_string_model(self, monkeypatch):
        routes = self._route(
            {"coder": {"model": "claude-sonnet-4-6", "provider": "anthropic"}},
            monkeypatch,
        )
        assert 0 in routes
        assert routes[0]["model"] == "claude-sonnet-4-6"
        assert isinstance(routes[0]["model"], str)

    def test_bare_string_entry_still_routes(self, monkeypatch):
        routes = self._route({"coder": "claude-sonnet-4-6"}, monkeypatch)
        assert routes[0]["model"] == "claude-sonnet-4-6"

    def test_modelless_dict_entry_fails_open(self, monkeypatch):
        """An entry with no usable model routes nothing (never a dict model)."""
        routes = self._route({"coder": {"provider": "anthropic"}}, monkeypatch)
        assert routes == {}

    def test_entry_model_helper_contract(self):
        from tools.delegation_router import _entry_model

        assert _entry_model("m") == "m"
        assert _entry_model({"model": "m", "provider": "p"}) == "m"
        assert _entry_model({"provider": "p"}) == ""
        assert _entry_model(None) == ""
        assert _entry_model(123) == ""


class TestRoleAliasDispatch:
    """``agent_type="sr-coder"`` dispatches exactly like ``agent_type="coder"``.

    ``sr-coder`` is a pure synonym (hermes_cli.personas.ROLE_ALIASES) carrying
    no config of its own, so these assert what actually reaches child
    construction — the dispatch loop resolves the alias to ``coder``'s config
    key for BOTH role-keyed lookups (model and credential entry).

    Stated as a relation against a ``coder`` sibling in the same batch rather
    than as a frozen model literal: the contract is "the alias resolves like
    its target", which must survive ``coder`` being retargeted.
    """

    ENTRY_MAP_CODER_OPUS = {
        "coder": {"model": "claude-opus-5"},
        "researcher": {"model": "claude-haiku-4-5"},
    }

    TASK_SR_CODER = {
        "goal": "Harden the retry helper in tools/http.py against partial reads",
        "agent_type": "sr-coder",
    }
    TASK_CODER = {
        "goal": "Port the retry helper in tools/http.py to the new backoff API",
        "agent_type": "coder",
    }

    def test_sr_coder_resolves_to_the_same_model_as_coder(self, dispatch):
        """The headline contract, asserted as a relation."""
        _result, captured = dispatch(
            [self.TASK_SR_CODER, self.TASK_CODER], self.ENTRY_MAP_CODER_OPUS
        )

        alias_child = _by_agent_type(captured, "sr-coder")
        canonical_child = _by_agent_type(captured, "coder")
        assert alias_child["model"] == canonical_child["model"]
        assert alias_child["override_provider"] == canonical_child["override_provider"]

    def test_sr_coder_dispatches_on_opus_5(self, dispatch):
        """Value-level proof against a fixture mirroring the live config."""
        _result, captured = dispatch(
            [self.TASK_SR_CODER, TASK_BARE], self.ENTRY_MAP_CODER_OPUS
        )

        alias_child = _by_agent_type(captured, "sr-coder")
        assert alias_child["model"] == "claude-opus-5"
        # Bare-string entry: no provider override, child keeps the batch bundle.
        assert alias_child["override_provider"] == BATCH_PROVIDER

    def test_alias_child_keeps_its_own_agent_type_for_the_persona_prompt(
        self, dispatch
    ):
        """Config resolution aliases; the dispatched identity does not.

        The child is still built with ``agent_type="sr-coder"`` — only the
        CONFIG KEY used to look up its model/credentials is aliased.
        """
        _result, captured = dispatch(
            [self.TASK_SR_CODER, TASK_BARE], self.ENTRY_MAP_CODER_OPUS
        )

        assert _by_agent_type(captured, "sr-coder")["agent_type"] == "sr-coder"

    def test_alias_inherits_a_provider_pin_from_its_target(self, dispatch):
        """When ``coder`` pins its own provider, the alias gets that bundle."""
        entry_map = {
            "coder": {"model": "qwen3-coder:480b-cloud", "provider": ROLE_PROVIDER},
            "researcher": {"model": "claude-haiku-4-5"},
        }
        _result, captured = dispatch([self.TASK_SR_CODER, TASK_BARE], entry_map)

        alias_child = _by_agent_type(captured, "sr-coder")
        assert alias_child["override_provider"] == ROLE_PROVIDER
        assert alias_child["model"] == "qwen3-coder:480b-cloud"
        # The whole bundle travels together, not a mix of the two providers.
        assert alias_child["override_api_key"] == "ollama-role-key"
        assert alias_child["override_base_url"] == "https://ollama.com/v1"

    def test_explicit_sr_coder_entry_wins_over_the_alias(self, dispatch):
        """An alias is a fallback: a configured entry always takes priority."""
        entry_map = {
            "coder": {"model": "claude-opus-5"},
            "sr-coder": {"model": "claude-sonnet-4-6"},
        }
        _result, captured = dispatch(
            [self.TASK_SR_CODER, self.TASK_CODER], entry_map
        )

        assert _by_agent_type(captured, "sr-coder")["model"] == "claude-sonnet-4-6"
        assert _by_agent_type(captured, "coder")["model"] == "claude-opus-5"

    def test_unconfigured_target_falls_through_to_the_batch_model(self, dispatch):
        """No ``coder`` entry: the alias invents nothing, batch default applies."""
        _result, captured = dispatch(
            [self.TASK_SR_CODER, TASK_BARE],
            {"researcher": {"model": "claude-haiku-4-5"}},
        )

        alias_child = _by_agent_type(captured, "sr-coder")
        assert alias_child["model"] == "claude-sonnet-4-6"  # batch cfg model
        assert alias_child["override_provider"] == BATCH_PROVIDER


class TestCoderDispatchUnaffected:
    """Regression guard: adding the alias changed nothing for ``coder``."""

    ENTRY_MAP = {
        "coder": {"model": "claude-opus-5"},
        "jr-coder": {"model": "qwen3-coder:480b-cloud", "provider": ROLE_PROVIDER},
        "researcher": {"model": "claude-haiku-4-5"},
    }

    def test_coder_dispatches_on_its_configured_model_and_batch_provider(
        self, dispatch
    ):
        _result, captured = dispatch(
            [{"goal": "Refactor the parser", "agent_type": "coder"}, TASK_BARE],
            self.ENTRY_MAP,
        )

        child = _by_agent_type(captured, "coder")
        assert child["model"] == "claude-opus-5"
        assert child["override_provider"] == BATCH_PROVIDER
        assert child["override_api_key"] == "ant-batch-key"

    def test_sibling_roles_still_resolve_independently(self, dispatch):
        """The pre-existing provider-pin behavior is untouched."""
        _result, captured = dispatch(
            [
                {"goal": "Refactor the parser", "agent_type": "coder"},
                {"goal": "Port the retry helper", "agent_type": "jr-coder"},
                {"goal": "Summarize AGENTS.md", "agent_type": "researcher"},
            ],
            self.ENTRY_MAP,
        )

        assert _by_agent_type(captured, "coder")["override_provider"] == BATCH_PROVIDER
        assert _by_agent_type(captured, "jr-coder")["override_provider"] == ROLE_PROVIDER
        assert (
            _by_agent_type(captured, "researcher")["override_provider"]
            == BATCH_PROVIDER
        )
        assert _by_agent_type(captured, "jr-coder")["model"] == "qwen3-coder:480b-cloud"

    def test_unknown_agent_type_still_falls_through(self, dispatch):
        """A non-alias, unconfigured role behaves exactly as before."""
        _result, captured = dispatch(
            [{"goal": "Do the thing", "agent_type": "no-such-role"}, TASK_BARE],
            self.ENTRY_MAP,
        )

        child = _by_agent_type(captured, "no-such-role")
        assert child["model"] == "claude-sonnet-4-6"  # batch cfg model
        assert child["override_provider"] == BATCH_PROVIDER
