"""FORK regression tests: agent.reasoning_effort_by_model key matching.

The served model id carries a provider prefix and often a build suffix
(``deepseek/deepseek-v4.1-flash-1017``) while the map key is the bare family
name (``deepseek-v4.1-flash``). Exact-string matching silently missed, every
session fell back to the global effort, and with the nous profile relaying
effort verbatim the models ran at the server's default thinking tier
(laziness/empty-turn symptoms). Matching is now exact-first, then normalized
through ``_normalized_model_id``.
"""

from cli import _normalized_model_id, _resolve_reasoning_for_model


class TestNormalizedModelId:
    def test_strips_provider_prefix_and_build_suffix(self):
        assert _normalized_model_id("deepseek/deepseek-v4.1-flash-1017") == "deepseek-v4.1-flash"

    def test_bare_key_normalizes_to_itself(self):
        assert _normalized_model_id("deepseek-v4.1-flash") == "deepseek-v4.1-flash"

    def test_version_segments_survive(self):
        assert _normalized_model_id("z-ai/glm-5.3-flashx") == "glm-5.3-flashx"

    def test_single_digit_tails_survive(self):
        assert _normalized_model_id("claude-sonnet-5-5") == "claude-sonnet-5-5"
        assert _normalized_model_id("gpt-5") == "gpt-5"

    def test_mid_id_build_markers_survive(self):
        # Only TRAILING markers are build noise; an internal one is versioning.
        assert _normalized_model_id("gpt-4-1106-preview") == "gpt-4-1106-preview"

    def test_latest_and_free_suffixes(self):
        assert _normalized_model_id("acme/model-large-latest") == "model-large"
        assert _normalized_model_id("acme/model-large:free") == "model-large"

    def test_date_style_suffix(self):
        assert _normalized_model_id("acme/model-20240229") == "model"


class TestResolveReasoningForModel:
    MAP = {"deepseek-v4.1-flash": "max", "glm-5.3-flashx": "max", "claude-sonnet-5-5": "max"}

    def test_exact_match_still_wins(self):
        cfg = _resolve_reasoning_for_model("glm-5.3-flashx", self.MAP, "low")
        assert cfg == {"enabled": True, "effort": "max"}

    def test_prefixed_and_suffixed_served_id_matches_bare_key(self):
        cfg = _resolve_reasoning_for_model("deepseek/deepseek-v4.1-flash-1017", self.MAP, "low")
        assert cfg == {"enabled": True, "effort": "max"}

    def test_case_insensitive(self):
        cfg = _resolve_reasoning_for_model("DeepSeek/DeepSeek-V4.1-Flash-1017", self.MAP, "low")
        assert cfg == {"enabled": True, "effort": "max"}

    def test_unmatched_model_falls_back_to_global(self):
        cfg = _resolve_reasoning_for_model("totally/other-model", self.MAP, "high")
        assert cfg == {"enabled": True, "effort": "high"}

    def test_empty_map_falls_back_to_global(self):
        cfg = _resolve_reasoning_for_model("deepseek/deepseek-v4.1-flash-1017", {}, "low")
        assert cfg == {"enabled": True, "effort": "low"}

    def test_exact_entry_beats_normalized_entry(self):
        map_ = {"deepseek/deepseek-v4.1-flash-1017": "high", "deepseek-v4.1-flash": "max"}
        cfg = _resolve_reasoning_for_model("deepseek/deepseek-v4.1-flash-1017", map_, "low")
        assert cfg == {"enabled": True, "effort": "high"}
