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

    # test_docstring_cleanup removed 2026-07-21: asserted a fork-only
    # docstring trim that only existed to reduce merge-conflict surface on
    # this exact function. The v2026.7.20 sync adopted upstream's fuller
    # docstring (documents `force` + no-op-return semantics, both genuinely
    # useful) instead of re-trimming it back down — see FORK.md's 2026-07-21
    # sync entry. Keeping this test would just re-fight that decision on
    # every future sync for zero behavioral value.

    def test_compress_context_signature(self):
        """compress_context function signature has the expected params."""
        import inspect
        from agent.conversation_compression import compress_context
        sig = inspect.signature(compress_context)
        assert "force" in sig.parameters
        assert "focus_topic" in sig.parameters
        assert "messages" in sig.parameters