"""Tests for bare-XML tool-call recovery in the conversation loop.

DSv4-Flash (and similar open backends) sometimes leak a tool call as bare
<invoke>/<parameter> XML in the assistant *content* with no structured
tool_calls. ``_recover_bare_tool_calls_from_content`` recovers it so the tool
actually runs instead of the XML painting as a final answer. See
agent/conversation_loop.py.
"""
import json

from agent.conversation_loop import _recover_bare_tool_calls_from_content


class TestRecoverBareToolCalls:
    def test_recovers_typed_param_dialect(self):
        """The exact leak from msg 95278 (2026-06-29): bare tags, string= attrs."""
        content = (
            "Config on disk looks good. Let me check the auxiliary section:\n"
            "<tool_call>\n"
            '<invoke name="read_file">\n'
            '<parameter name="limit" string="false">15</parameter>\n'
            '<parameter name="path" string="true">~/.hermes/config.yaml</parameter>\n'
            "</invoke>"
        )
        calls = _recover_bare_tool_calls_from_content(content)
        assert len(calls) == 1
        assert calls[0].function.name == "read_file"
        args = json.loads(calls[0].function.arguments)
        assert args["limit"] == 15
        assert args["path"] == "~/.hermes/config.yaml"
        # OpenAI shape required by the loop's downstream consumers.
        assert calls[0].type == "function"
        assert calls[0].id

    def test_recovers_plain_claude_dialect(self):
        content = (
            '<invoke name="terminal">\n'
            '<parameter name="command">ls -la</parameter>\n'
            "</invoke>"
        )
        calls = _recover_bare_tool_calls_from_content(content)
        assert len(calls) == 1
        assert calls[0].function.name == "terminal"
        assert json.loads(calls[0].function.arguments) == {"command": "ls -la"}

    def test_no_recovery_on_plain_prose(self):
        assert _recover_bare_tool_calls_from_content("just a normal answer") == []

    def test_no_recovery_without_parameter_tag(self):
        # An <invoke> with no <parameter> is too weak a signal (prose mentioning
        # the tag); must not fire.
        content = 'To call a tool, write <invoke name="foo"> then the body.'
        assert _recover_bare_tool_calls_from_content(content) == []

    def test_no_recovery_on_dsml_sentinel_form(self):
        """Sentinel-bearing tags are the backend parser's job, not ours."""
        bar = "\uff5c"
        content = (
            f'<{bar}DSML{bar}invoke name="read_file">'
            f'<{bar}DSML{bar}parameter name="path" string="true">/x</{bar}DSML{bar}parameter>'
            f"</{bar}DSML{bar}invoke>"
        )
        assert _recover_bare_tool_calls_from_content(content) == []

    def test_recovers_multiple_calls(self):
        content = (
            '<invoke name="a"><parameter name="x">1</parameter></invoke>'
            '<invoke name="b"><parameter name="y">2</parameter></invoke>'
        )
        calls = _recover_bare_tool_calls_from_content(content)
        assert [c.function.name for c in calls] == ["a", "b"]

    def test_empty_and_none_content(self):
        assert _recover_bare_tool_calls_from_content("") == []
        assert _recover_bare_tool_calls_from_content(None) == []


class TestStripOrphanToolCallTail:
    """Orphan tool-call TAIL stripping (``_strip_orphan_toolcall_tail``).

    The 2026-07-26 hard_eval leak shape: exo's DSv4 backend lost a tool call's
    OPENING tags upstream, so the final content was the parameter body (a whole
    Python file) ending in bare ``</parameter>\\n</invoke>`` closers — a shape
    ``_recover_bare_tool_calls_from_content`` cannot recover (no ``<invoke``
    opener, tool name gone). The tail must be stripped from the final content
    so raw tags never paint into the visible answer.
    """

    def test_strips_simple_tail(self):
        """The code_lru_cache t1 shape: code + bare closers at the end."""
        from agent.conversation_loop import _strip_orphan_toolcall_tail

        code = (
            "            node = self._Node(key, value)\n"
            "            self.cache[key] = node\n"
            "            self._add_to_front(node)"
        )
        content = code + "\n</parameter>\n</invoke>\n"
        assert _strip_orphan_toolcall_tail(content) == code

    def test_strips_tail_with_trailing_path_parameter_block(self):
        """The code_lru_cache t3 shape: body closer, complete typed path
        parameter block, then </invoke>."""
        from agent.conversation_loop import _strip_orphan_toolcall_tail

        code = "        del self._cache[lru.key]"
        content = (
            code
            + "\n</parameter>\n"
            + '<parameter name="path" string="true">/tmp/x/lru_cache.py</parameter>\n'
            + "</invoke>\n"
        )
        assert _strip_orphan_toolcall_tail(content) == code

    def test_strips_tail_with_trailing_tool_calls_wrapper_closer(self):
        """The residual leak variant caught live 2026-07-27 (exo debug
        capture, hard_eval code_dijkstra): a sentinel-less ``</tool_calls>``
        WRAPPER closer trails the final ``</invoke>``. This net runs BEFORE
        strip_think_blocks removes stray wrapper closers, so the regex must
        match the PRE-strip string — the old ``</invoke>\\s*$`` anchor never
        did, and the tail painted into the visible answer."""
        from agent.conversation_loop import _strip_orphan_toolcall_tail

        code = "    return -1"
        content = code + "\n</parameter>\n</invoke>\n</tool_calls>"
        assert _strip_orphan_toolcall_tail(content) == code

    def test_strips_tail_with_wrapper_closer_dialect_variants(self):
        """Wrapper closers vary with the model's degenerate dialect just like
        openers do: singular/``_called``/V3.2 ``function_calls`` forms."""
        from agent.conversation_loop import _strip_orphan_toolcall_tail

        code = "x = 1"
        for closer in ("</tool_call>", "</tool_called>", "</function_calls>"):
            content = code + "\n</parameter>\n</invoke>\n" + closer + "\n"
            assert _strip_orphan_toolcall_tail(content) == code, closer

    def test_strips_tail_followed_by_models_own_closing_fence(self):
        """The residual leak variant caught live 2026-07-28 (hard_eval
        code_segment_tree t2): the model slips into closers at the end of its
        inline answer and THEN closes its code fence. The fence pushed
        end-of-string past the old anchor so the tail dodged the strip; now
        the tail is stripped and the captured fence re-appended so the code
        block stays balanced."""
        from agent.conversation_loop import _strip_orphan_toolcall_tail

        content = (
            "```python\nclass SegTree:\n    def query(self):\n        return res\n"
            "</parameter>\n</invoke>\n\n```\n"
        )
        stripped = _strip_orphan_toolcall_tail(content)
        assert stripped is not None
        assert "</invoke>" not in stripped
        assert "</parameter>" not in stripped
        assert stripped.count("```") == 2
        assert stripped.rstrip().endswith("```")
        assert "return res" in stripped

    def test_untouched_on_lone_wrapper_closer(self):
        """A bare ``</tool_calls>`` WITHOUT the preceding ``</parameter>``…
        ``</invoke>`` sequence is not the tail signature — that lone closer is
        strip_think_blocks' job, not this net's."""
        from agent.conversation_loop import _strip_orphan_toolcall_tail

        content = "Here is the answer: 42.\n</tool_calls>"
        assert _strip_orphan_toolcall_tail(content) == content

    def test_untouched_when_invoke_opener_present(self):
        """A full <invoke> block is the recovery function's job — never strip."""
        from agent.conversation_loop import _strip_orphan_toolcall_tail

        content = (
            '<invoke name="write_file">'
            '<parameter name="content">x = 1</parameter>'
            "</invoke>"
        )
        assert _strip_orphan_toolcall_tail(content) == content

    def test_untouched_on_mid_text_closers(self):
        """Closers not at the very end are prose — never strip."""
        from agent.conversation_loop import _strip_orphan_toolcall_tail

        content = "The sequence is </parameter> then </invoke> and then EOS."
        assert _strip_orphan_toolcall_tail(content) == content

    def test_untouched_on_dsml_sentinel(self):
        """Sentinel-bearing content is the backend parser's job."""
        from agent.conversation_loop import _strip_orphan_toolcall_tail

        bar = "｜"
        content = f"x = 1\n</{bar}DSML{bar}parameter>\n</parameter>\n</invoke>\n"
        # contains the sentinel → leave alone entirely
        assert _strip_orphan_toolcall_tail(content) == content

    def test_untouched_on_plain_answers_and_empty(self):
        from agent.conversation_loop import _strip_orphan_toolcall_tail

        assert _strip_orphan_toolcall_tail("normal answer") == "normal answer"
        assert _strip_orphan_toolcall_tail("") == ""
        assert _strip_orphan_toolcall_tail(None) is None


# ---------------------------------------------------------------------------
# Wiring regression: the recovery must run at the INTAKE stage so a recovered
# call flips the loop's tool/text dispatch.
#
# The v2026.9.14 merge (f6edb27b86) dropped the fork's post-response call site
# inside ``run_conversation``; both helpers lived on caller-less. The restored
# site is ``agent/turn_response_intake.py::normalize_model_response`` — it runs
# immediately before ``conversation_loop``'s
# ``run_tool_round if assistant_message.tool_calls else finish_text_response``
# dispatch, which is the only place where recovering a call can still turn the
# turn into a tool turn.
# ---------------------------------------------------------------------------

import time as _time
from types import SimpleNamespace as _NS

from agent.conversation_loop import _LoopState, _run_phase
from agent.turn_final_response import finish_text_response
from agent.turn_response_intake import normalize_model_response
from agent.turn_tool_round import run_tool_round


class _Transport:
    def __init__(self, message):
        self._message = message

    def normalize_response(self, response, strip_tool_prefix=False):
        return self._message


def _run_intake(message):
    """Drive the REAL phase machinery: the call at conversation_loop.py:1829."""
    agent = _NS(
        api_mode="chat_completions", quiet_mode=True, verbose_logging=False, log_prefix="",
        _vprint=lambda *a, **k: None, tool_progress_callback=None,
        _incomplete_scratchpad_retries=0, _buffer_vprint=lambda *a, **k: None,
        _codex_incomplete_retries=0, _codex_reasoning_only_streak=0,
        _get_transport=lambda: _Transport(message),
    )
    state = _LoopState(
        user_message="hi", system_message=None, moa_config=None, original_user_message="hi",
        conversation_history=[], effective_task_id=None, turn_id="probe",
        _should_review_memory=False, _plugin_user_context=None, _ext_prefetch_cache=None,
        messages=[], active_system_prompt=None, current_turn_user_idx=0,
        _preflight_compression_blocked=False, max_compression_attempts=3,
        api_call_count=1, api_duration=0.1, api_start_time=_time.time(),
        api_request_id="probe:dispatch", response=None,
    )
    verdict = _run_phase(normalize_model_response, agent, state)
    # The loop's dispatch expression (conversation_loop.py:1835).
    dispatch = run_tool_round if state.assistant_message.tool_calls else finish_text_response
    return verdict, state.assistant_message, dispatch


class TestIntakeStageRecoveryWiring:
    def test_leaked_call_flips_dispatch_to_tool_round(self):
        """A recovered bare-XML call must make THIS response a tool turn."""
        leaked = (
            "Config on disk looks good. Let me check the auxiliary section:\n"
            "<tool_call>\n"
            '<invoke name="read_file">\n'
            '<parameter name="limit" string="false">15</parameter>\n'
            '<parameter name="path" string="true">~/.hermes/config.yaml</parameter>\n'
            "</invoke>"
        )
        verdict, message, dispatch = _run_intake(
            _NS(content=leaked, tool_calls=None, finish_reason="stop",
                reasoning_content=None, reasoning=None)
        )
        assert verdict.action == "fallthrough"
        assert [tc.function.name for tc in message.tool_calls] == ["read_file"]
        # Leaked XML stripped from stored content so it never re-primes the model.
        assert message.content is None
        assert dispatch is run_tool_round

    def test_orphan_tail_is_stripped_and_stays_a_text_turn(self):
        """The closer-only shape has no recoverable name: strip, keep text turn."""
        orphan = "    return -1\n</parameter>\n</invoke>\n</tool_calls>"
        verdict, message, dispatch = _run_intake(
            _NS(content=orphan, tool_calls=None, finish_reason="stop",
                reasoning_content=None, reasoning=None)
        )
        assert verdict.action == "fallthrough"
        assert not message.tool_calls
        assert message.content == "    return -1"
        assert "</invoke>" not in message.content
        assert dispatch is finish_text_response

    def test_plain_prose_is_untouched(self):
        verdict, message, dispatch = _run_intake(
            _NS(content="just a normal answer", tool_calls=None, finish_reason="stop",
                reasoning_content=None, reasoning=None)
        )
        assert verdict.action == "fallthrough"
        assert not message.tool_calls
        assert message.content == "just a normal answer"
        assert dispatch is finish_text_response

