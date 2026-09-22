"""Fork-only Anthropic server-tool passes — post-consolidation contracts.

The fork's vendored converter (``agent/fork/anthropic_messages.py``) was retired
in favour of upstream's ``agent/anthropic_message_convert.py``, with the fork's
native-web-search / tool_search server-tool passes re-applied on top via
``agent/fork/anthropic_server_tool_passes.py``.

These tests pin the behaviour that consolidation had to preserve exactly, plus
the three regressions the consolidation fixed by adopting upstream's logic.
Behaviour contracts (how blocks relate), never snapshots of converter output.
"""
import pytest

from agent.anthropic_message_convert import convert_messages_to_anthropic

ANTHROPIC = "https://api.anthropic.com"
THIRD_PARTY = "https://gateway.example.com/anthropic"
CLAUDE = "claude-opus-4-20250514"


def _server_tool_use(tu_id, name="web_search"):
    return {"type": "server_tool_use", "id": tu_id, "name": name, "input": {"query": "q"}}


def _web_search_result(tu_id):
    return {"type": "web_search_tool_result", "tool_use_id": tu_id, "content": [
        {"type": "web_search_result", "url": "https://e.com/", "title": "t",
         "page_age": None, "encrypted_content": "enc"}]}


def _tool_search_result(tu_id, variant="regex", suffixed=False):
    btype = f"tool_search_tool_{variant}_tool_result" if suffixed else "tool_search_tool_result"
    return {"type": btype, "tool_use_id": tu_id, "content": [
        {"type": "tool_search_tool_search_result", "tool_references": [
            {"type": "tool_reference", "tool_name": "foo"}]}]}


def _tool_call(tc_id, name="bash", args='{"cmd": "ls"}'):
    return {"id": tc_id, "type": "function", "function": {"name": name, "arguments": args}}


def _assistant_blocks(out):
    return next(m for m in out if m["role"] == "assistant")["content"]


def _types(blocks):
    return [b.get("type") for b in blocks]


def _convert(messages, base_url=ANTHROPIC, model=CLAUDE):
    return convert_messages_to_anthropic(messages, base_url=base_url, model=model)[1]


# ─────────────────────────────────────────────────────────────────────────
# The converter is single-sourced now
# ─────────────────────────────────────────────────────────────────────────

def test_vendored_fork_converter_is_gone():
    """The fork's vendored converter module must no longer exist — upstream's is
    the one true implementation."""
    with pytest.raises(ImportError):
        import agent.fork.anthropic_messages  # noqa: F401


def test_adapter_reexports_upstream_converter():
    """anthropic_adapter's name must resolve to upstream's function object, not a
    fork forwarder (a re-introduced shadowing def would silently bypass the
    consolidated pipeline)."""
    from agent import anthropic_adapter
    from agent import anthropic_message_convert

    assert (anthropic_adapter.convert_messages_to_anthropic
            is anthropic_message_convert.convert_messages_to_anthropic)


# ─────────────────────────────────────────────────────────────────────────
# Pass 1 — recomposition path preserves server_tool_blocks
# ─────────────────────────────────────────────────────────────────────────

def test_server_tool_blocks_precede_text_and_tool_use():
    """server_tool_blocks stashed by the transport must be replayed BEFORE the
    turn's text and tool_use blocks."""
    out = _convert([
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "preamble", "tool_calls": [_tool_call("toolu_a")],
         "server_tool_blocks": [_server_tool_use("s1"), _web_search_result("s1")]},
        {"role": "tool", "tool_call_id": "toolu_a", "content": "ok"},
    ])
    types = _types(_assistant_blocks(out))
    assert types.index("server_tool_use") < types.index("text")
    assert types.index("web_search_tool_result") < types.index("tool_use")


def test_server_tool_blocks_tool_search_normalized_on_recomposition():
    """A variant-suffixed tool_search result in server_tool_blocks is collapsed to
    the bare canonical type Anthropic's input validator accepts."""
    out = _convert([
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "t", "server_tool_blocks": [
            _server_tool_use("ts1", name="tool_search_tool_regex"),
            _tool_search_result("ts1", suffixed=True)]},
        {"role": "user", "content": "u2"},
    ])
    types = _types(_assistant_blocks(out))
    assert "tool_search_tool_result" in types
    assert not any(t != "tool_search_tool_result" and str(t).startswith("tool_search_tool_")
                   and str(t).endswith("_tool_result") for t in types)


# ─────────────────────────────────────────────────────────────────────────
# Pass 2/6 — tool_search result shape + type canonicalization
# ─────────────────────────────────────────────────────────────────────────

def test_tool_search_result_response_only_fields_stripped():
    """Response-only fields on a replayed tool_search result must not reach the
    wire ("Extra inputs are not permitted")."""
    dirty = dict(_tool_search_result("ts2"), text="response-only", citations=["RESPONSE-ONLY"])
    out = _convert([
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "anthropic_content_blocks": [
            _server_tool_use("ts2", name="tool_search_tool_regex"), dirty]},
        {"role": "user", "content": "u2"},
    ])
    block = next(b for b in _assistant_blocks(out) if b["type"] == "tool_search_tool_result")
    assert "text" not in block and "citations" not in block
    assert block["tool_use_id"] == "ts2"


# ─────────────────────────────────────────────────────────────────────────
# Pass 3 — relocation of a tool_search result onto its owning message
# ─────────────────────────────────────────────────────────────────────────

def test_tool_search_result_relocated_to_owning_message():
    """Anthropic delivers the result on a LATER response but requires same-message
    pairing: the result must end up in the message holding its server_tool_use."""
    out = _convert([
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "anthropic_content_blocks": [
            {"type": "text", "text": "a"}, _server_tool_use("ts3", name="tool_search_tool_regex")]},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "", "anthropic_content_blocks": [
            _tool_search_result("ts3"), {"type": "text", "text": "b"}]},
        {"role": "user", "content": "u3"},
    ])
    owning = next(m for m in out
                  if m["role"] == "assistant"
                  and any(b.get("type") == "server_tool_use" for b in m["content"]))
    types = _types(owning["content"])
    use_idx = types.index("server_tool_use")
    assert types[use_idx + 1] == "tool_search_tool_result"
    # And it no longer lingers on the later message.
    others = [m for m in out if m["role"] == "assistant" and m is not owning]
    assert not any(b.get("type") == "tool_search_tool_result"
                   for m in others for b in m["content"])


# ─────────────────────────────────────────────────────────────────────────
# Pass 4 — native web_search same-message orphan rules
# ─────────────────────────────────────────────────────────────────────────

def test_paired_web_search_survives_immediately_adjacent():
    """The exact shape native web_search returns must round-trip with the result
    immediately after its use — otherwise the next call 400s."""
    out = _convert([
        {"role": "user", "content": "search X"},
        {"role": "assistant", "content": "", "anthropic_content_blocks": [
            {"type": "text", "text": "Searching."}, _server_tool_use("s2"),
            _web_search_result("s2"), {"type": "text", "text": "Done."}]},
        {"role": "user", "content": "thanks"},
    ])
    blocks = _assistant_blocks(out)
    types = _types(blocks)
    use_idx = types.index("server_tool_use")
    assert blocks[use_idx + 1]["type"] == "web_search_tool_result"
    assert blocks[use_idx + 1]["tool_use_id"] == "s2"


@pytest.mark.parametrize("blocks,orphan_type", [
    ([{"type": "text", "text": "a"}, _web_search_result("missing")], "web_search_tool_result"),
    ([{"type": "text", "text": "a"}, _server_tool_use("lonely")], "server_tool_use"),
])
def test_web_search_orphan_half_is_stripped(blocks, orphan_type):
    """Either half of a split native web_search pair must be dropped, never sent alone."""
    out = _convert([
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "anthropic_content_blocks": list(blocks)},
        {"role": "user", "content": "u2"},
    ])
    assert orphan_type not in _types(_assistant_blocks(out))


def test_web_search_pair_split_across_messages_does_not_strand_result():
    """Message-merging can split a pair across turns; neither half may survive
    alone once they land in the same merged message."""
    out = _convert([
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "anthropic_content_blocks": [_server_tool_use("s3")]},
        {"role": "assistant", "content": "", "anthropic_content_blocks": [_web_search_result("s3")]},
        {"role": "user", "content": "u2"},
    ])
    types = _types(_assistant_blocks(out))
    # Merged into one message -> the pair is intact and adjacent.
    assert types.count("server_tool_use") == types.count("web_search_tool_result")


# ─────────────────────────────────────────────────────────────────────────
# Pass 5 — server_tool_use whose result never arrived
# ─────────────────────────────────────────────────────────────────────────

def test_server_tool_use_without_any_result_is_dropped():
    """A tool_search server_tool_use whose result never arrived (stream cut) must
    be dropped, or every later call replays the orphan and 400s forever."""
    out = _convert([
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "anthropic_content_blocks": [
            {"type": "text", "text": "a"},
            _server_tool_use("s_dead", name="tool_search_tool_regex")]},
        {"role": "user", "content": "u2"},
    ])
    assert "server_tool_use" not in _types(_assistant_blocks(out))


def test_web_search_paired_use_not_misclassified_as_unpaired():
    """Pass 5's result-id set must include web_search result ids too — otherwise a
    legitimately paired web_search use is dropped and its result stranded."""
    out = _convert([
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "anthropic_content_blocks": [
            _server_tool_use("s4"), _web_search_result("s4")]},
        {"role": "user", "content": "u2"},
    ])
    types = _types(_assistant_blocks(out))
    assert "server_tool_use" in types and "web_search_tool_result" in types


# ─────────────────────────────────────────────────────────────────────────
# Pass 7 — client tool_use ordering
# ─────────────────────────────────────────────────────────────────────────

def test_client_tool_use_moved_after_server_side_blocks():
    """A client tool_use emitted BEFORE server-side blocks must be moved to the
    tail, or the next message's tool_result isn't "immediately after" it."""
    out = _convert([
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("toolu_b")],
         "anthropic_content_blocks": [
             {"type": "tool_use", "id": "toolu_b", "name": "bash", "input": {"cmd": "ls"}},
             _server_tool_use("ts4", name="tool_search_tool_regex"),
             _tool_search_result("ts4")]},
        {"role": "tool", "tool_call_id": "toolu_b", "content": "ok"},
    ])
    types = _types(_assistant_blocks(out))
    assert types.index("tool_use") > types.index("server_tool_use")
    assert types.index("tool_use") > types.index("tool_search_tool_result")


# ─────────────────────────────────────────────────────────────────────────
# Deliberate divergence retained from upstream: replay citations
# ─────────────────────────────────────────────────────────────────────────

def test_citations_stripped_on_verbatim_replay_path():
    """FORK divergence: a replayed text block's citations are stripped, because the
    web_search result they reference can be removed by the orphan pass, leaving a
    dangling encrypted_index. Pinned so a future sync can't silently flip it."""
    out = _convert([
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "anthropic_content_blocks": [
            _server_tool_use("s5"), _web_search_result("s5"),
            {"type": "text", "text": "cited claim", "citations": [
                {"type": "web_search_result_location", "url": "https://e.com/",
                 "title": "t", "cited_text": "ct", "encrypted_index": "ei"}]}]},
        {"role": "user", "content": "u2"},
    ])
    text_blocks = [b for b in _assistant_blocks(out) if b["type"] == "text"]
    assert text_blocks and all("citations" not in b for b in text_blocks)


def test_citations_preserved_on_recomposition_path():
    """The divergence is scoped to verbatim replay only — the recomposition path
    keeps citations, matching both upstream and the retired fork converter."""
    citations = [{"type": "web_search_result_location", "url": "https://e.com/",
                  "title": "t", "cited_text": "ct", "encrypted_index": "ei"}]
    out = _convert([
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "cited", "citations": citations}]},
        {"role": "user", "content": "u2"},
    ])
    block = next(b for b in _assistant_blocks(out) if b["type"] == "text")
    assert block.get("citations") == citations


# ─────────────────────────────────────────────────────────────────────────
# Regressions fixed by adopting upstream's implementation
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("model", [
    "deepseek-r1", "deepseek-v4-pro", "DeepSeek-Flash", "vendor/deepseek-pro",
])
def test_deepseek_thinking_model_behind_generic_relay_keeps_unsigned_thinking(model):
    """REGRESSION (fixed by consolidation): a DeepSeek thinking model reached via a
    generic third-party Anthropic relay needs its unsigned thinking blocks replayed
    for message-history validation. The retired fork converter keyed only on the
    /anthropic URL, so it took the third-party strip-everything path and dropped
    them. Upstream's _model_name_is_deepseek_thinking recognizes the model name."""
    out = _convert([
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "reasoning_content": "unsigned reasoning",
         "tool_calls": [_tool_call("toolu_c")]},
        {"role": "tool", "tool_call_id": "toolu_c", "content": "ok"},
    ], base_url=THIRD_PARTY, model=model)
    blocks = _assistant_blocks(out)
    thinking = [b for b in blocks if b["type"] == "thinking"]
    assert thinking, "unsigned DeepSeek thinking block must survive"
    assert thinking[0]["thinking"] == "unsigned reasoning"
    assert _types(blocks).index("thinking") < _types(blocks).index("tool_use")


@pytest.mark.parametrize("model", ["deepseek-chat", "deepseek-coder"])
def test_non_thinking_deepseek_names_still_take_third_party_strip(model):
    """The fix must stay narrow: an arbitrary DeepSeek chat/distill name is NOT
    evidence of the thinking replay contract, so it keeps the strip behaviour."""
    out = _convert([
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "reasoning_content": "unsigned reasoning",
         "tool_calls": [_tool_call("toolu_d")]},
        {"role": "tool", "tool_call_id": "toolu_d", "content": "ok"},
    ], base_url=THIRD_PARTY, model=model)
    assert "thinking" not in _types(_assistant_blocks(out))


def test_deepseek_signed_blocks_still_stripped_behind_relay():
    """DeepSeek cannot validate Anthropic signatures — signed blocks must still go,
    even for a recognized thinking model."""
    out = _convert([
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("toolu_e")],
         "reasoning_details": [
             {"type": "thinking", "thinking": "signed", "signature": "sigABC"}]},
        {"role": "tool", "tool_call_id": "toolu_e", "content": "ok"},
    ], base_url=THIRD_PARTY, model="deepseek-r1")
    assert "thinking" not in _types(_assistant_blocks(out))


def test_tool_result_backcompat_block_stash_is_honored():
    """REGRESSION (fixed by consolidation): blocks stashed on a tool message under
    the private ``_anthropic_content_blocks`` key must be emitted as tool_result
    inner blocks. The retired fork converter ignored the key and flattened the
    result to its plain text, silently dropping the image."""
    out = _convert([
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("toolu_f")]},
        {"role": "tool", "tool_call_id": "toolu_f", "content": "summary text",
         "_anthropic_content_blocks": [
             {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                          "data": "AAAA"}}]},
        {"role": "user", "content": "u2"},
    ])
    tool_result = next(b for m in out if m["role"] == "user"
                       if isinstance(m["content"], list)
                       for b in m["content"] if b.get("type") == "tool_result")
    inner_types = _types(tool_result["content"])
    assert "image" in inner_types, "stashed image block must survive"
    assert "text" in inner_types, "the text summary must be kept alongside it"
