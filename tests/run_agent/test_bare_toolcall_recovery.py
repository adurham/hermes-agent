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
