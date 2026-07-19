"""Regression tests for the stable|volatile system-prompt cache split.

Anthropic prompt caching order is tools -> system -> messages (cumulative
prefix). Hermes used to send the whole system prompt as ONE cached block, so
the volatile tail (memory snapshot, user profile, daily timestamp) sat inside
the cached prefix -- a memory edit or date rollover cold-rewrote the entire
system block, including the byte-stable identity + tool guidance.

The fix inserts an internal SYSTEM_VOLATILE_SENTINEL between the stable+context
head and the volatile tail in build_system_prompt(). On the native Anthropic
cache layout, apply_anthropic_cache_control() splits the system block into
[{stable_head, cache_control}, {volatile_tail}] so the breakpoint lands at the
end of the stable prefix. On every other path the sentinel is stripped so the
model never sees it and sent bytes are unchanged.

These tests assert:
  1. The sentinel never reaches the model (split-consumed or stripped).
  2. Native path -> two blocks, cache_control ONLY on the stable head.
  3. Non-native path -> single block, sentinel stripped, bytes == legacy join.
  4. Total cache breakpoints stay <= 4.
  5. Stripping/splitting is byte-reproducible vs the legacy flat join.
"""
from agent.prompt_caching import (
    apply_anthropic_cache_control,
    split_system_for_cache,
    strip_volatile_sentinel,
    SYSTEM_VOLATILE_SENTINEL,
)

STABLE = "STABLE: identity + tool guidance"
VOLATILE = "VOLATILE: memory + profile + Conversation started: Tuesday"
FLAT_WITH_SENTINEL = STABLE + "\n\n" + SYSTEM_VOLATILE_SENTINEL + "\n\n" + VOLATILE
LEGACY_FLAT = STABLE + "\n\n" + VOLATILE  # what the old "\n\n".join produced


def _count_breakpoints(messages):
    n = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("cache_control"):
                    n += 1
        elif isinstance(m, dict) and m.get("cache_control"):
            n += 1
    return n


def test_strip_reproduces_legacy_bytes():
    assert strip_volatile_sentinel(FLAT_WITH_SENTINEL) == LEGACY_FLAT


def test_split_head_tail_reproduce_legacy_bytes():
    parts = split_system_for_cache(FLAT_WITH_SENTINEL)
    assert parts is not None
    head, tail = parts
    assert head + tail == LEGACY_FLAT
    assert head == STABLE + "\n\n"
    assert tail == VOLATILE


def test_split_returns_none_without_sentinel():
    assert split_system_for_cache(LEGACY_FLAT) is None


def test_native_path_splits_into_two_blocks():
    msgs = [
        {"role": "system", "content": FLAT_WITH_SENTINEL},
        {"role": "user", "content": "hi"},
    ]
    out = apply_anthropic_cache_control(
        msgs, cache_ttl="1h", native_anthropic=True, reserve_tools_breakpoint=True
    )
    sysc = out[0]["content"]
    assert isinstance(sysc, list) and len(sysc) == 2

    stable_block, volatile_block = sysc
    assert stable_block["text"] == STABLE + "\n\n"
    assert "cache_control" in stable_block
    assert volatile_block["text"] == VOLATILE
    assert "cache_control" not in volatile_block

    # Sentinel must never reach the model.
    assert SYSTEM_VOLATILE_SENTINEL not in stable_block["text"]
    assert SYSTEM_VOLATILE_SENTINEL not in volatile_block["text"]


def test_native_path_stays_within_breakpoint_budget():
    # system(stable head) + reserved tools + 2 message breakpoints == 4 max.
    msgs = [
        {"role": "system", "content": FLAT_WITH_SENTINEL},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]
    out = apply_anthropic_cache_control(
        msgs, cache_ttl="5m", native_anthropic=True, reserve_tools_breakpoint=True
    )
    # reserve_tools_breakpoint=True means budget = 4 - 1(system) - 1(tools) = 2
    # message breakpoints, plus the single system breakpoint on the stable
    # head = 3 content-block markers here; tools[] adds the 4th elsewhere.
    assert _count_breakpoints(out) <= 3


def test_non_native_path_strips_sentinel_single_block():
    msgs = [
        {"role": "system", "content": FLAT_WITH_SENTINEL},
        {"role": "user", "content": "hi"},
    ]
    out = apply_anthropic_cache_control(
        msgs, cache_ttl="5m", native_anthropic=False, reserve_tools_breakpoint=False
    )
    sysc = out[0]["content"]
    assert isinstance(sysc, list) and len(sysc) == 1
    assert sysc[0]["text"] == LEGACY_FLAT
    assert SYSTEM_VOLATILE_SENTINEL not in sysc[0]["text"]


def test_no_sentinel_fallback_single_block_native():
    # An older stored prompt (pre-change) or an empty-volatile session has no
    # sentinel; native path must fall back to a single marked block, no crash.
    msgs = [
        {"role": "system", "content": "stable only, no volatile"},
        {"role": "user", "content": "hi"},
    ]
    out = apply_anthropic_cache_control(
        msgs, cache_ttl="5m", native_anthropic=True, reserve_tools_breakpoint=True
    )
    sysc = out[0]["content"]
    assert isinstance(sysc, list) and len(sysc) == 1
    assert "cache_control" in sysc[0]


def test_build_system_prompt_emits_sentinel_only_with_volatile():
    """build_system_prompt joins stable+context+volatile with the sentinel at
    the volatile boundary, and omits it when volatile is empty."""
    from types import SimpleNamespace
    import agent.system_prompt as sp

    captured = {"stable": "S-IDENT", "context": "", "volatile": "V-MEM"}

    def fake_parts(agent, system_message=None):
        return dict(captured)

    orig = sp.build_system_prompt_parts
    sp.build_system_prompt_parts = fake_parts
    try:
        out = sp.build_system_prompt(SimpleNamespace())
        assert SYSTEM_VOLATILE_SENTINEL in out
        assert strip_volatile_sentinel(out) == "S-IDENT\n\nV-MEM"

        # No volatile -> no sentinel.
        captured["volatile"] = ""
        out2 = sp.build_system_prompt(SimpleNamespace())
        assert SYSTEM_VOLATILE_SENTINEL not in out2
        assert out2 == "S-IDENT"
    finally:
        sp.build_system_prompt_parts = orig
