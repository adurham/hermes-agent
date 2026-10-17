"""Behavior lock for ``agent.anthropic_adapter.build_anthropic_kwargs`` and its phases in
``agent/anthropic_adapter_kwargs.py`` (OAuth identity, tools / tool_choice, thinking, fast mode).

A seeded sample of the models × endpoints × OAuth × tools × tool_choice × reasoning × output-cap
× fast-mode × cache axes is built and every result is checked against the wire contract: which
top-level fields exist, the resolved model and output cap, the OAuth system prefix and tool-name
wire form, the ``tool_choice`` mapping, the thinking / effort / context-management shape, the
fast-mode betas, the tool cache breakpoint and the stripping of tool_use blocks for tools that
are not on the wire. Expected values are derived from the adapter's own per-model / per-endpoint
predicates, so the lock follows catalog updates and only fails when the builder's COMPOSITION of
them changes. A few concrete requests are pinned in full on top.

Promotes the throwaway old-vs-new differential used when the builder was split into phases
(code-health ratchet slice 1) into a committed regression net. Hermetic: no network, no config
beyond the isolated ``HERMES_HOME``.
"""

from __future__ import annotations

import copy
import itertools
import json
import random
from typing import Any

import pytest

from agent import anthropic_adapter as aa
from agent.anthropic_adapter import build_anthropic_kwargs
from agent.prompt_caching import _build_marker


SYSTEM_TEXT = "You are Hermes Agent by Nous Research."
TOOL_NAMES = ("read_file", "mcp_linear_get_issue", "memory")


def _messages() -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_TEXT},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "read_file", "arguments": "{\"path\": \"a\"}"}},
            {"id": "c2", "type": "function", "function": {"name": "ghost_tool", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "tool", "tool_call_id": "c2", "content": "gone"},
        {"role": "user", "content": "next"},
    ]


def _tools() -> list[dict]:
    return [{"type": "function", "function": {
        "name": name, "description": f"Hermes Agent tool {name}",
        "parameters": {"type": "object", "properties": {}},
    }} for name in TOOL_NAMES]


MODELS = (
    "claude-opus-4-6", "claude-opus-4.7", "claude-opus-4-8", "claude-opus-5-5", "claude-sonnet-4-6",
    "claude-sonnet-4-5", "claude-sonnet-5-5", "claude-haiku-4-5", "claude-haiku-5-5",
    "claude-3-7-sonnet-20250219", "claude-fable-5-1", "anthropic/claude-opus-4.8", "kimi-k2.5",
    "MiniMax-M2", "qwen3.5-plus",
)
BASE_URLS = (
    None, "https://api.anthropic.com", "https://api.minimax.io/anthropic",
    "https://proxy.example.com/anthropic", "https://inference-api.nousresearch.com/v1",
    "https://foo.services.ai.azure.com/anthropic", "https://api.kimi.com/coding",
)
REASONING = (
    None, {"enabled": False}, {"enabled": True}, {"enabled": True, "effort": "low"},
    {"enabled": True, "effort": "high"}, {"enabled": True, "effort": "xhigh"},
    {"enabled": True, "effort": "max"}, {"enabled": True, "effort": "minimal"},
)
TOOL_CHOICES = (None, "auto", "required", "none", "read_file")
CAPS = ((None, None), (4096, None), (None, 8000), (200_000, 1000), (1000, 64_000))
CACHE = ((False, "5m"), (True, "5m"), (True, "1h"))

AXES = (MODELS, BASE_URLS, (False, True), (False, True), TOOL_CHOICES, REASONING, CAPS,
        (False, True), CACHE, (False, True), (False, True))
SAMPLE_SIZE = 700
_SEED = 20261017

ALLOWED_KEYS = {"model", "messages", "max_tokens", "system", "tools", "tool_choice", "thinking",
                "output_config", "context_management", "temperature", "speed", "betas"}


def _grid() -> list[tuple]:
    """Seeded sample of the full product, plus every model × endpoint × OAuth corner once."""
    rng = random.Random(_SEED)
    cases = [tuple(rng.choice(axis) for axis in AXES) for _ in range(SAMPLE_SIZE)]
    for model, base_url, oauth in itertools.product(MODELS, BASE_URLS, (False, True)):
        cases.append((model, base_url, oauth, True, "read_file", None, (None, None), True,
                      (True, "1h"), False, False))
    return cases


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    for var in ("HERMES_THINKING_DISPLAY", "HERMES_CONTEXT_1M_THRESHOLD_TOKENS",
                "NOUS_INFERENCE_BASE_URL"):
        monkeypatch.delenv(var, raising=False)


def _build(case: tuple) -> tuple[dict, dict]:
    (model, base_url, oauth, with_tools, tool_choice, reasoning, (max_tokens, context_length),
     fast_mode, (cache_tools, cache_ttl), preserve_dots, drop_1m) = case
    args = dict(
        model=model, messages=_messages(), tools=_tools() if with_tools else None,
        max_tokens=max_tokens, reasoning_config=copy.deepcopy(reasoning), tool_choice=tool_choice,
        is_oauth=oauth, preserve_dots=preserve_dots, context_length=context_length,
        base_url=base_url, fast_mode=fast_mode, drop_context_1m_beta=drop_1m,
        cache_tools=cache_tools, cache_ttl=cache_ttl,
    )
    pristine = copy.deepcopy(args)
    kwargs = build_anthropic_kwargs(**args)
    assert args == pristine, "builder mutated its inputs"
    return args, kwargs


def _expected_model(a: dict) -> str:
    if aa._is_nous_portal_endpoint(a["base_url"]):
        return a["model"]
    return aa.normalize_model_name(a["model"], preserve_dots=a["preserve_dots"])


def _expected_thinking(a: dict, model: str, cap: int) -> dict:
    """The thinking-related fields (and output cap) the builder must produce."""
    rc = a["reasoning_config"]
    if rc is None and aa._supports_adaptive_thinking(model):
        rc = {"enabled": True, "effort": "medium"}
    out: dict[str, Any] = {"max_tokens": cap}
    if not rc:
        return out
    if rc.get("enabled") is False:
        if aa._model_matches(model, aa._BETWEEN_TOOLS_OFF_CLAUDE_SUBSTRINGS):
            out["thinking"] = {"type": "between_tools"}
        elif aa._accepts_thinking_disable(model):
            out["thinking"] = {"type": "disabled"}
        return out
    if aa._is_pre_thinking_haiku(model):
        return out
    effort = str(rc.get("effort", "medium")).lower()
    if aa._supports_adaptive_thinking(model):
        level = aa.ADAPTIVE_EFFORT_MAP.get(effort, "medium")
        if level == "xhigh" and not aa._supports_xhigh_effort(model):
            level = "high"
        out["thinking"] = {"type": "adaptive"}
        out["output_config"] = {"effort": level}
        if not aa._is_third_party_anthropic_endpoint(a["base_url"]):
            out["context_management"] = {
                "edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}
        return out
    budget = aa.THINKING_BUDGET.get(effort, 8000)
    out["thinking"] = {"type": "enabled", "budget_tokens": budget}
    if not aa._forbids_sampling_params(model):
        out["temperature"] = 1
    out["max_tokens"] = max(cap, budget + 4096)
    return out


def _expected_cap(a: dict, model: str) -> int:
    cap = aa._resolve_anthropic_messages_max_tokens(
        a["max_tokens"], model, context_length=a["context_length"])
    if a["context_length"] and cap > a["context_length"]:
        cap = max(a["context_length"] - 1, 1)
    return cap


def _check_system(a: dict, kwargs: dict) -> None:
    if not a["is_oauth"]:
        assert kwargs["system"] == SYSTEM_TEXT
        return
    system = kwargs["system"]
    assert system[0] == {"type": "text", "text": aa._CLAUDE_CODE_SYSTEM_PREFIX}
    joined = " ".join(b["text"] for b in system)
    assert "Hermes Agent" not in joined and "Nous Research" not in joined
    assert len(system) == 2


def _wire_names(a: dict, kwargs: dict) -> list[str]:
    """The tool names the request carries (empty when no tools or tool_choice='none')."""
    names = [t["name"] for t in kwargs.get("tools", [])]
    if a["is_oauth"]:
        assert all(n.startswith("mcp__") for n in names), names
        assert names[:2] == ["mcp__read_file", "mcp__linear_get_issue"] or not names
    else:
        assert names == list(TOOL_NAMES) or not names
    return names


def _check_tools(a: dict, kwargs: dict) -> None:
    present = bool(a["tools"]) and a["tool_choice"] != "none"
    assert ("tools" in kwargs) is present
    names = _wire_names(a, kwargs)
    if present:
        markers = [t.get("cache_control") for t in kwargs["tools"]]
        tail = _build_marker(a["cache_ttl"]) if a["cache_tools"] else None
        assert markers == [None, None, tail]
    choice = kwargs.get("tool_choice")
    if not present or a["tool_choice"] in (None, "auto"):
        assert choice is None
    elif a["tool_choice"] == "required":
        assert choice == {"type": "any"}
    else:
        assert choice == {"type": "tool", "name": names[0]}


def _check_messages(a: dict, kwargs: dict) -> None:
    tool_uses = [b for m in kwargs["messages"] if isinstance(m["content"], list)
                 for b in m["content"] if b.get("type") == "tool_use"]
    if not a["tools"]:
        assert tool_uses == []
        return
    wire = "mcp__read_file" if a["is_oauth"] else "read_file"
    assert [(b["id"], b["name"], b["input"]) for b in tool_uses] == [("c1", wire, {"path": "a"})]
    assert "ghost_tool" in json.dumps(kwargs["messages"])  # kept as a text breadcrumb


def _check_fast_mode(a: dict, kwargs: dict, model: str) -> None:
    fast = (a["fast_mode"] and not aa._is_third_party_anthropic_endpoint(a["base_url"])
            and aa._supports_fast_mode(model))
    if not fast:
        assert "speed" not in kwargs and "betas" not in kwargs
        return
    expected = aa._common_betas_for_base_url(
        a["base_url"], drop_context_1m_beta=a["drop_context_1m_beta"], model=model)
    if a["is_oauth"]:
        expected = expected + aa._OAUTH_ONLY_BETAS
    assert kwargs["speed"] == "fast"
    assert kwargs["betas"] == expected + [aa._FAST_MODE_BETA]


def _check_case(a: dict, kwargs: dict) -> None:
    assert set(kwargs) <= ALLOWED_KEYS
    model = _expected_model(a)
    assert kwargs["model"] == model
    thinking = _expected_thinking(a, model, _expected_cap(a, model))
    for key in ("max_tokens", "thinking", "output_config", "context_management", "temperature"):
        assert kwargs.get(key) == thinking.get(key), key
    _check_system(a, kwargs)
    _check_tools(a, kwargs)
    _check_messages(a, kwargs)
    _check_fast_mode(a, kwargs, model)


def test_seeded_grid_matches_the_wire_contract():
    cases = _grid()
    seen = set()
    for case in cases:
        args, kwargs = _build(case)
        try:
            _check_case(args, kwargs)
        except AssertionError as exc:
            raise AssertionError(f"case {case!r}: {exc}") from exc
        seen.add(tuple(sorted(k for k in kwargs if k not in ("model", "messages", "max_tokens"))))
    # The sample really exercises the optional fields (guards against a grid that silently
    # stops reaching a phase, e.g. because every model lost fast mode).
    reached = set().union(*map(set, seen))
    assert reached == ALLOWED_KEYS - {"model", "messages", "max_tokens"}


def test_build_is_deterministic():
    for case in _grid()[:50]:
        assert _build(case)[1] == _build(case)[1]


def test_context_1m_threshold_strips_the_beta_for_small_prompts(monkeypatch):
    """With the opt-in gate on, a small prompt goes out with per-request betas minus context-1m."""
    monkeypatch.setenv("HERMES_CONTEXT_1M_THRESHOLD_TOKENS", "1000000")
    monkeypatch.setattr(aa, "_base_url_needs_context_1m_beta", lambda base_url: True)
    kwargs = build_anthropic_kwargs(
        "claude-opus-4-6", _messages(), _tools(), None, None, base_url=None, is_oauth=True)
    full = aa._common_betas_for_base_url(None, model="claude-opus-4-6") + aa._OAUTH_ONLY_BETAS
    assert aa._CONTEXT_1M_BETA in full
    assert kwargs["betas"] == [b for b in full if b != aa._CONTEXT_1M_BETA]

    # A prompt above the threshold keeps the client-level betas (no per-request override).
    monkeypatch.setenv("HERMES_CONTEXT_1M_THRESHOLD_TOKENS", "1")
    kwargs = build_anthropic_kwargs(
        "claude-opus-4-6", _messages(), _tools(), None, None, base_url=None, is_oauth=True)
    assert "betas" not in kwargs


# ── pinned requests ──────────────────────────────────────────────────────────────

def _wire_messages(read_file_name: str) -> list[dict]:
    return [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "text",
             "text": "[Previous tool call: ghost_tool({}) — tool no longer available in this turn.]"},
            {"type": "tool_use", "id": "c1", "name": read_file_name, "input": {"path": "a"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "ok"},
            {"type": "text", "text": "[Previous tool result for ghost_tool: gone]"},
            {"type": "text", "text": "next"},
        ]},
    ]


def _wire_tool(name: str, source: str, **extra: Any) -> dict:
    return {"name": name, "description": f"Hermes Agent tool {source}",
            "input_schema": {"type": "object", "properties": {}}, **extra}


def test_pinned_oauth_forced_tool_on_native_anthropic():
    kwargs = build_anthropic_kwargs(
        "claude-opus-4-6", _messages(), _tools(), None, None, tool_choice="read_file",
        is_oauth=True, cache_tools=True, cache_ttl="1h")
    assert kwargs == {
        "model": "claude-opus-4-6",
        "messages": _wire_messages("mcp__read_file"),
        "max_tokens": kwargs["max_tokens"],
        "system": [{"type": "text", "text": aa._CLAUDE_CODE_SYSTEM_PREFIX},
                   {"type": "text", "text": "You are Claude Code by Anthropic."}],
        "tools": [
            _wire_tool("mcp__read_file", "read_file"),
            _wire_tool("mcp__linear_get_issue", "mcp_linear_get_issue"),
            _wire_tool(kwargs["tools"][2]["name"], "memory",
                       cache_control={"type": "ephemeral", "ttl": "1h"}),
        ],
        "tool_choice": {"type": "tool", "name": "mcp__read_file"},
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium"},
        "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
    }
    assert kwargs["max_tokens"] == aa._get_anthropic_max_output("claude-opus-4-6")
    assert kwargs["tools"][2]["name"].startswith("mcp__")


def test_pinned_manual_thinking_on_minimax():
    kwargs = build_anthropic_kwargs(
        "claude-sonnet-4-5", _messages(), _tools(), 4096, {"enabled": True, "effort": "high"},
        tool_choice="required", base_url="https://api.minimax.io/anthropic")
    assert kwargs == {
        "model": "claude-sonnet-4-5",
        "messages": _wire_messages("read_file"),
        "max_tokens": 16000 + 4096,
        "system": SYSTEM_TEXT,
        "tools": [_wire_tool(n, n) for n in TOOL_NAMES],
        "tool_choice": {"type": "any"},
        "thinking": {"type": "enabled", "budget_tokens": 16000},
        "temperature": 1,
    }


def test_pinned_portal_keeps_catalog_id_and_skips_native_only_fields():
    kwargs = build_anthropic_kwargs(
        "anthropic/claude-opus-4.8", _messages(), None, 2048, {"enabled": True, "effort": "xhigh"},
        base_url="https://inference-api.nousresearch.com/v1", fast_mode=True, context_length=1024)
    assert kwargs == {
        "model": "anthropic/claude-opus-4.8",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": kwargs["messages"][1]["content"]},
            {"role": "user", "content": kwargs["messages"][2]["content"]},
        ],
        "max_tokens": 1023,
        "system": SYSTEM_TEXT,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "xhigh"},
    }
    assert "tool_use" not in json.dumps(kwargs["messages"])
