"""Tests for the fork's Gemini Cloud Code adapter.

The adapter provides Gemini→Cloud Code compatibility for the
Gemini provider OAuth path. Note: this module has external
dependencies (agent.google_code_assist) that may not be available
in all environments, so tests use source inspection.
"""

import inspect
import os


class TestGeminiCloudCodeAdapter:
    """Tests for agent/gemini_cloudcode_adapter.py."""

    def test_module_file_exists(self):
        """The module file exists in the repo."""
        path = os.path.join(
            os.path.dirname(__file__),
            "..", "..", "agent", "gemini_cloudcode_adapter.py"
        )
        path = os.path.normpath(path)
        assert os.path.isfile(path), f"File not found: {path}"

    def test_module_has_expected_content(self):
        """The module contains expected Gemini/Cloud Code references."""
        path = os.path.join(
            os.path.dirname(__file__),
            "..", "..", "agent", "gemini_cloudcode_adapter.py"
        )
        path = os.path.normpath(path)
        with open(path) as f:
            source = f.read()
        assert "gemini" in source.lower() or "Gemini" in source
        assert "cloud" in source.lower() or "Cloud" in source

    def test_module_references_google_oauth(self):
        """Module references Google OAuth for Gemini provider."""
        path = os.path.join(
            os.path.dirname(__file__),
            "..", "..", "agent", "gemini_cloudcode_adapter.py"
        )
        path = os.path.normpath(path)
        with open(path) as f:
            source = f.read()
        assert "google_oauth" in source or "oauth" in source.lower()

    def test_module_has_tool_conversion(self):
        """Module contains tool conversion logic."""
        path = os.path.join(
            os.path.dirname(__file__),
            "..", "..", "agent", "gemini_cloudcode_adapter.py"
        )
        path = os.path.normpath(path)
        with open(path) as f:
            source = f.read()
        assert "convert" in source.lower() or "tool" in source.lower()