"""Tests for agent/cc_aliases.py — CC tool-name aliasing for the OAuth path.

Verifies the HERMES_TO_CC and CC_TO_HERMES mappings and the
argument adaptation functions for each aliased tool.
"""

import pytest


class TestCCAliases:
    """Tests for the CC alias name mappings."""

    def test_hermes_to_cc_maps_terminal(self):
        """terminal → Bash is the canonical mapping."""
        from agent.cc_aliases import HERMES_TO_CC
        assert HERMES_TO_CC["terminal"] == "Bash"

    def test_hermes_to_cc_maps_read_file(self):
        """read_file → Read."""
        from agent.cc_aliases import HERMES_TO_CC
        assert HERMES_TO_CC["read_file"] == "Read"

    def test_hermes_to_cc_maps_patch(self):
        """patch → Edit."""
        from agent.cc_aliases import HERMES_TO_CC
        assert HERMES_TO_CC["patch"] == "Edit"

    def test_hermes_to_cc_maps_write_file(self):
        """write_file → Write."""
        from agent.cc_aliases import HERMES_TO_CC
        assert HERMES_TO_CC["write_file"] == "Write"

    def test_hermes_to_cc_maps_search_files(self):
        """search_files → Grep."""
        from agent.cc_aliases import HERMES_TO_CC
        assert HERMES_TO_CC["search_files"] == "Grep"

    def test_cc_to_hermes_is_inverse(self):
        """CC_TO_HERMES is the inverse of HERMES_TO_CC."""
        from agent.cc_aliases import HERMES_TO_CC, CC_TO_HERMES
        for hermes_name, cc_name in HERMES_TO_CC.items():
            assert CC_TO_HERMES[cc_name] == hermes_name

    def test_cc_to_hermes_includes_agent(self):
        """Agent → delegate_task is an extra mapping."""
        from agent.cc_aliases import CC_TO_HERMES
        assert CC_TO_HERMES["Agent"] == "delegate_task"

    def test_adapt_bash_passes_command(self):
        """_adapt_bash passes 'command' through unchanged."""
        from agent.cc_aliases import _adapt_bash
        result = _adapt_bash({"command": "ls -la", "description": "list files"})
        assert result["command"] == "ls -la"

    def test_adapt_bash_converts_timeout(self):
        """_adapt_bash converts ms timeout to seconds."""
        from agent.cc_aliases import _adapt_bash
        result = _adapt_bash({"command": "sleep 1", "timeout": 30000})
        assert result["timeout"] == 30  # 30000ms → 30s

    def test_adapt_bash_converts_background(self):
        """_adapt_bash converts run_in_background to background."""
        from agent.cc_aliases import _adapt_bash
        result = _adapt_bash({"command": "ping 8.8.8.8", "run_in_background": True})
        assert result["background"] is True

    def test_adapt_read_renames_file_path(self):
        """_adapt_read renames 'file_path' to 'path'."""
        from agent.cc_aliases import _adapt_read
        result = _adapt_read({"file_path": "/etc/hosts"})
        assert result["path"] == "/etc/hosts"

    def test_adapt_read_passes_offset(self):
        """_adapt_read passes offset through as-is."""
        from agent.cc_aliases import _adapt_read
        result = _adapt_read({"file_path": "/etc/hosts", "offset": 0})
        assert result["offset"] == 0

    def test_adapt_edit_renames_file_path(self):
        """_adapt_edit renames 'file_path' to 'path'."""
        from agent.cc_aliases import _adapt_edit
        result = _adapt_edit({"file_path": "/etc/hosts", "old_string": "foo", "new_string": "bar"})
        assert result["path"] == "/etc/hosts"

    def test_adapt_edit_passes_old_string(self):
        """_adapt_edit passes old_string through."""
        from agent.cc_aliases import _adapt_edit
        result = _adapt_edit({"file_path": "/etc/hosts", "old_string": "foo", "new_string": "bar"})
        assert result["old_string"] == "foo"

    def test_adapt_edit_passes_new_string(self):
        """_adapt_edit passes new_string through."""
        from agent.cc_aliases import _adapt_edit
        result = _adapt_edit({"file_path": "/etc/hosts", "old_string": "foo", "new_string": "bar"})
        assert result["new_string"] == "bar"

    def test_adapt_edit_sets_mode(self):
        """_adapt_edit sets mode to 'replace'."""
        from agent.cc_aliases import _adapt_edit
        result = _adapt_edit({"file_path": "/etc/hosts", "old_string": "foo", "new_string": "bar"})
        assert result["mode"] == "replace"

    def test_adapt_write_renames_file_path(self):
        """_adapt_write renames 'file_path' to 'path'."""
        from agent.cc_aliases import _adapt_write
        result = _adapt_write({"file_path": "/tmp/test.txt", "content": "hello"})
        assert result["path"] == "/tmp/test.txt"

    def test_adapt_grep_passes_pattern(self):
        """_adapt_grep passes 'pattern' through."""
        from agent.cc_aliases import _adapt_grep
        result = _adapt_grep({"pattern": "foo.*bar", "path": "/src"})
        assert result["pattern"] == "foo.*bar"

    def test_adapt_grep_passes_path(self):
        """_adapt_grep passes 'path' through."""
        from agent.cc_aliases import _adapt_grep
        result = _adapt_grep({"pattern": "test", "path": "/src"})
        assert result["path"] == "/src"

    def test_adapt_grep_passes_glob(self):
        """_adapt_grep passes 'glob' through."""
        from agent.cc_aliases import _adapt_grep
        result = _adapt_grep({"pattern": "test", "glob": "*.py"})
        assert result["glob"] == "*.py"

    def test_adapt_agent_maps_prompt_to_goal(self):
        """_adapt_agent maps 'prompt' to 'goal'."""
        from agent.cc_aliases import _adapt_agent
        result = _adapt_agent({"prompt": "do something", "description": "info"})
        assert result["goal"] == "do something"

    def test_adapt_agent_maps_description_to_context(self):
        """_adapt_agent maps 'description' to 'context'."""
        from agent.cc_aliases import _adapt_agent
        result = _adapt_agent({"prompt": "do something", "description": "info"})
        assert result["context"] == "info"

    def test_adapt_agent_passes_model(self):
        """_adapt_agent passes 'model' through."""
        from agent.cc_aliases import _adapt_agent
        result = _adapt_agent({"prompt": "do", "model": "claude-sonnet-4-6"})
        assert result["model"] == "claude-sonnet-4-6"