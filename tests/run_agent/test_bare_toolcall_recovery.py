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
