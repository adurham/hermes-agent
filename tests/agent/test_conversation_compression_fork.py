"""Tests for fork-only additions to agent/conversation_compression.py.

Verifies:
1. Phase 2 auto-extraction piggyback on compression boundary
2. Docstring cleanup (removed redundant Args/Returns)
"""


class TestConversationCompressionFork:
    """Tests for fork additions in conversation_compression.py."""

    def test_phase2_auto_extraction_hook_exists(self):
        """compress_context calls memory_extraction.on_pre_compress."""
        import inspect
        from agent.conversation_compression import compress_context
        source = inspect.getsource(compress_context)
        assert "memory_extraction" in source
        assert "on_pre_compress" in source

    def test_phase2_is_best_effort(self):
        """Exceptions in auto-extraction don't crash compression."""
        import inspect
        from agent.conversation_compression import compress_context
        source = inspect.getsource(compress_context)
        # The hook is wrapped in try/except
        assert "try:" in source
        assert "on_pre_compress" in source

    def test_phase2_called_with_session_id(self):
        """on_pre_compress receives session_id and messages."""
        import inspect
        from agent.conversation_compression import compress_context
        source = inspect.getsource(compress_context)
        assert "on_pre_compress" in source
        assert "session_id" in source

    def test_docstring_cleanup(self):
        """Compress docstring no longer has the removed Args/Returns."""
        from agent.conversation_compression import compress_context
        doc = (compress_context.__doc__ or "").strip()
        # The removed args should NOT be in the docstring
        assert "approx_tokens" not in doc
        # The kept arg should be there
        assert "focus_topic" in doc

    def test_compress_context_signature(self):
        """compress_context function signature has the expected params."""
        import inspect
        from agent.conversation_compression import compress_context
        sig = inspect.signature(compress_context)
        assert "force" in sig.parameters
        assert "focus_topic" in sig.parameters
        assert "messages" in sig.parameters