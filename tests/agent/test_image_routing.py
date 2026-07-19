"""Tests for agent/image_routing.py — the per-turn image input mode decision."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import patch


from agent.image_routing import (
    _coerce_capability_bool,
    _coerce_mode,
    _explicit_aux_vision_override,
    _lookup_supports_vision,
    _supports_vision_override,
    build_native_content_parts,
    decide_image_input_mode,
    extract_image_refs,
)


# ─── _coerce_mode ────────────────────────────────────────────────────────────


class TestCoerceMode:
    def test_valid_modes_pass_through(self):
        assert _coerce_mode("auto") == "auto"
        assert _coerce_mode("native") == "native"
        assert _coerce_mode("text") == "text"

    def test_case_insensitive(self):
        assert _coerce_mode("NATIVE") == "native"
        assert _coerce_mode("Auto") == "auto"

    def test_invalid_falls_back_to_auto(self):
        assert _coerce_mode("nonsense") == "auto"
        assert _coerce_mode("") == "auto"
        assert _coerce_mode(None) == "auto"
        assert _coerce_mode(42) == "auto"

    def test_strips_whitespace(self):
        assert _coerce_mode("  native  ") == "native"


# ─── _explicit_aux_vision_override ───────────────────────────────────────────


class TestExplicitAuxVisionOverride:
    def test_none_config(self):
        assert _explicit_aux_vision_override(None) is False

    def test_empty_config(self):
        assert _explicit_aux_vision_override({}) is False

    def test_default_auto_is_not_explicit(self):
        cfg = {"auxiliary": {"vision": {"provider": "auto", "model": "", "base_url": ""}}}
        assert _explicit_aux_vision_override(cfg) is False

    def test_provider_set_is_explicit(self):
        cfg = {"auxiliary": {"vision": {"provider": "openrouter", "model": ""}}}
        assert _explicit_aux_vision_override(cfg) is True

    def test_model_set_is_explicit(self):
        cfg = {"auxiliary": {"vision": {"provider": "auto", "model": "google/gemini-2.5-flash"}}}
        assert _explicit_aux_vision_override(cfg) is True

    def test_base_url_set_is_explicit(self):
        cfg = {"auxiliary": {"vision": {"provider": "auto", "base_url": "http://localhost:11434"}}}
        assert _explicit_aux_vision_override(cfg) is True


# ─── decide_image_input_mode ─────────────────────────────────────────────────


class TestDecideImageInputMode:
    def test_explicit_native_overrides_everything(self):
        cfg = {"agent": {"image_input_mode": "native"}}
        # Non-vision model, aux-vision explicitly configured: native still wins.
        cfg["auxiliary"] = {"vision": {"provider": "openrouter", "model": "foo"}}
        with patch("agent.image_routing._lookup_supports_vision", return_value=False):
            assert decide_image_input_mode("openrouter", "some-non-vision-model", cfg) == "native"

    def test_native_capable_model_attaches_natively_even_with_exo_aux_vision(self):
        """Regression: an explicit (exo) aux vision backend must NOT suppress
        native vision on a model that can already see (e.g. Claude). The old
        code returned 'text' here and shipped every image to the aux model."""
        cfg = {
            "auxiliary": {"vision": {
                "provider": "custom:exo",
                "model": "mlx-community/Qwen3.6-35B-A3B-8bit",
                "base_url": "http://192.168.86.201:52415/v1",
            }},
        }
        with patch("agent.image_routing._lookup_supports_vision", return_value=True):
            assert decide_image_input_mode("anthropic", "claude-opus-4-8", cfg) == "native"

    def test_exo_nonvision_model_delegates_to_aux_vision(self):
        """DSv4 on exo (no native vision) + explicit exo aux vision → text
        (delegate the image to the exo-hosted vision model)."""
        cfg = {
            "auxiliary": {"vision": {
                "provider": "custom:exo",
                "model": "mlx-community/Qwen3.6-35B-A3B-8bit",
                "base_url": "http://192.168.86.201:52415/v1",
            }},
        }
        with patch("agent.image_routing._lookup_supports_vision", return_value=False):
            # work machine: provider string is "exo"
            assert decide_image_input_mode("exo", "mlx-community/DeepSeek-V4-Flash", cfg) == "text"
            # personal machine: provider string is "custom:exo"
            assert decide_image_input_mode("custom:exo", "mlx-community/DeepSeek-V4-Flash", cfg) == "text"

    def test_nonexo_nonvision_model_does_not_route_to_exo_delegate(self):
        """A non-exo, non-vision model must NOT be pulled into the exo vision
        delegate. With native capability unknown/false it native-attaches and
        lets its own provider handle/reject the image — the exo cluster stays
        out of non-exo sessions (Adam's explicit scoping)."""
        cfg = {
            "auxiliary": {"vision": {
                "provider": "custom:exo",
                "model": "mlx-community/Qwen3.6-35B-A3B-8bit",
                "base_url": "http://192.168.86.201:52415/v1",
            }},
        }
        with patch("agent.image_routing._lookup_supports_vision", return_value=False):
            assert decide_image_input_mode("openrouter", "some-text-only-model", cfg) == "native"

    def test_exo_bare_custom_runtime_matched_by_base_url(self):
        """When the runtime collapses the provider to bare 'custom', exo is
        identified by matching the active main base_url against the configured
        exo provider entry."""
        cfg = {
            "model": {"provider": "custom", "base_url": "http://192.168.86.201:52415/v1"},
            "providers": {"exo": {"base_url": "http://192.168.86.201:52415/v1"}},
            "auxiliary": {"vision": {
                "provider": "custom:exo",
                "model": "mlx-community/Qwen3.6-35B-A3B-8bit",
                "base_url": "http://192.168.86.201:52415/v1",
            }},
        }
        with patch("agent.image_routing._lookup_supports_vision", return_value=False):
            assert decide_image_input_mode("custom", "mlx-community/DeepSeek-V4-Flash", cfg) == "text"

    def test_explicit_text_overrides_everything(self):
        cfg = {"agent": {"image_input_mode": "text"}}
        with patch("agent.image_routing._lookup_supports_vision", return_value=True):
            assert decide_image_input_mode("anthropic", "claude-sonnet-4", cfg) == "text"

    def test_auto_with_vision_capable_model(self):
        with patch("agent.image_routing._lookup_supports_vision", return_value=True):
            assert decide_image_input_mode("anthropic", "claude-sonnet-4", {}) == "native"

    def test_auto_with_non_vision_model(self):
        with patch("agent.image_routing._lookup_supports_vision", return_value=False):
            assert decide_image_input_mode("openrouter", "qwen/qwen3-235b", {}) == "text"

    def test_auto_with_unknown_model(self):
        with patch("agent.image_routing._lookup_supports_vision", return_value=None):
            assert decide_image_input_mode("openrouter", "brand-new-slug", {}) == "text"

    def test_nonexo_aux_vision_override_is_ignored_for_routing(self):
        """A non-exo aux vision backend (e.g. OpenRouter Gemini) no longer
        forces text mode. A native-capable main model attaches natively; the
        exo-only delegate scoping means a non-exo aux backend doesn't reroute
        a vision-capable model through the text pipeline."""
        cfg = {"auxiliary": {"vision": {"provider": "openrouter", "model": "google/gemini-2.5-flash"}}}
        with patch("agent.image_routing._lookup_supports_vision", return_value=True):
            assert decide_image_input_mode("anthropic", "claude-sonnet-4", cfg) == "native"

    def test_none_config_is_auto(self):
        with patch("agent.image_routing._lookup_supports_vision", return_value=True):
            assert decide_image_input_mode("anthropic", "claude-sonnet-4", None) == "native"

    def test_invalid_mode_coerces_to_auto(self):
        cfg = {"agent": {"image_input_mode": "weird-value"}}
        with patch("agent.image_routing._lookup_supports_vision", return_value=True):
            assert decide_image_input_mode("anthropic", "claude-sonnet-4", cfg) == "native"

    def test_auto_uses_text_for_text_only_modalities_even_with_attachment_flag(self):
        registry = {
            "xiaomi": {
                "models": {
                    "mimo-v2.5-pro": {
                        "attachment": True,
                        "modalities": {"input": ["text"]},
                        "tool_call": True,
                    },
                },
            },
        }
        with patch("agent.models_dev.fetch_models_dev", return_value=registry):
            assert decide_image_input_mode("xiaomi", "mimo-v2.5-pro", {}) == "text"


# ─── _coerce_capability_bool ─────────────────────────────────────────────────


class TestCoerceCapabilityBool:
    def test_real_bool_passes_through(self):
        assert _coerce_capability_bool(True) is True
        assert _coerce_capability_bool(False) is False

    def test_int_0_and_1(self):
        assert _coerce_capability_bool(1) is True
        assert _coerce_capability_bool(0) is False

    def test_other_ints_return_none(self):
        assert _coerce_capability_bool(2) is None
        assert _coerce_capability_bool(-1) is None

    def test_yaml_true_tokens(self):
        for s in ("true", "TRUE", "True", "yes", "on", "1", "  true  "):
            assert _coerce_capability_bool(s) is True

    def test_yaml_false_tokens(self):
        for s in ("false", "FALSE", "False", "no", "off", "0", "  false  "):
            assert _coerce_capability_bool(s) is False

    def test_quoted_false_does_not_silently_become_true(self):
        # Regression: bool("false") is True in Python. A user writing
        # supports_vision: "false" must NOT enable native vision routing.
        assert _coerce_capability_bool("false") is False

    def test_unrecognised_strings_return_none(self):
        # None == fall through to models.dev, not a silent truthy.
        assert _coerce_capability_bool("maybe") is None
        assert _coerce_capability_bool("") is None
        assert _coerce_capability_bool("definitely") is None

    def test_other_types_return_none(self):
        assert _coerce_capability_bool(None) is None
        assert _coerce_capability_bool([]) is None
        assert _coerce_capability_bool({}) is None
        assert _coerce_capability_bool(1.5) is None


# ─── _supports_vision_override ───────────────────────────────────────────────


class TestSupportsVisionOverride:
    def test_no_cfg_returns_none(self):
        assert _supports_vision_override(None, "custom", "my-llava") is None
        assert _supports_vision_override({}, "custom", "my-llava") is None

    def test_top_level_shortcut_wins(self):
        cfg = {"model": {"supports_vision": True}}
        assert _supports_vision_override(cfg, "custom", "my-llava") is True

    def test_top_level_false_propagates(self):
        cfg = {"model": {"supports_vision": False}}
        assert _supports_vision_override(cfg, "custom", "my-llava") is False

    def test_per_provider_per_model_via_runtime_name(self):
        cfg = {
            "providers": {
                "custom": {"models": {"my-llava": {"supports_vision": True}}},
            },
        }
        assert _supports_vision_override(cfg, "custom", "my-llava") is True

    def test_per_provider_per_model_via_config_name(self):
        # Named custom provider — runtime self.provider == "custom", config
        # holds the original name under model.provider.
        cfg = {
            "model": {"provider": "my-vllm"},
            "providers": {
                "my-vllm": {"models": {"my-llava": {"supports_vision": True}}},
            },
        }
        assert _supports_vision_override(cfg, "custom", "my-llava") is True

    def test_quoted_false_string_in_yaml_does_not_enable(self):
        # Real-world: user writes supports_vision: "false" (quoted).
        cfg = {"model": {"supports_vision": "false"}}
        assert _supports_vision_override(cfg, "custom", "my-llava") is False

    def test_unrecognised_value_falls_through(self):
        cfg = {"model": {"supports_vision": "maybe"}}
        assert _supports_vision_override(cfg, "custom", "my-llava") is None

    def test_no_override_returns_none(self):
        cfg = {"model": {"default": "my-llava"}}
        assert _supports_vision_override(cfg, "custom", "my-llava") is None

    def test_malformed_sections_are_ignored(self):
        # User accidentally wrote a string where a section was expected —
        # don't blow up, just fall through.
        cfg = {"model": "some-string", "providers": ["not-a-dict"]}
        assert _supports_vision_override(cfg, "custom", "my-llava") is None

    def test_custom_colon_name_stripped_suffix_lookup(self):
        # model.provider: custom:my-proxy → should resolve stripped key "my-proxy"
        cfg = {
            "model": {"provider": "custom:my-proxy"},
            "providers": {
                "my-proxy": {"models": {"gpt-5.5": {"supports_vision": True}}},
            },
        }
        assert _supports_vision_override(cfg, "custom", "gpt-5.5") is True

    def test_custom_colon_name_stripped_suffix_false(self):
        # Explicitly disabled vision on the stripped key.
        cfg = {
            "model": {"provider": "custom:my-proxy"},
            "providers": {
                "my-proxy": {"models": {"gpt-5.5": {"supports_vision": False}}},
            },
        }
        assert _supports_vision_override(cfg, "custom", "gpt-5.5") is False

    def test_custom_colon_name_no_stripped_key_falls_through(self):
        # custom:my-proxy but providers only has "custom" — stripped key
        # doesn't match, but "custom" does via runtime provider.
        cfg = {
            "model": {"provider": "custom:my-proxy"},
            "providers": {
                "custom": {"models": {"gpt-5.5": {"supports_vision": True}}},
            },
        }
        assert _supports_vision_override(cfg, "custom", "gpt-5.5") is True


# ─── _lookup_supports_vision (override-aware) ────────────────────────────────


class TestLookupSupportsVisionOverride:
    def test_config_override_short_circuits_models_dev(self):
        # Config says True, models.dev says None — config wins.
        cfg = {"model": {"supports_vision": True}}
        with patch("agent.models_dev.get_model_capabilities", return_value=None):
            assert _lookup_supports_vision("custom", "my-llava", cfg) is True

    def test_config_override_false_beats_vision_capable_models_dev(self):
        # User explicitly disables vision on a models.dev-vision-capable model.
        fake_caps = type("Caps", (), {"supports_vision": True})()
        cfg = {"model": {"supports_vision": False}}
        with patch("agent.models_dev.get_model_capabilities", return_value=fake_caps):
            assert _lookup_supports_vision("anthropic", "claude-sonnet-4", cfg) is False

    def test_no_override_falls_back_to_models_dev(self):
        fake_caps = type("Caps", (), {"supports_vision": True})()
        with patch("agent.models_dev.get_model_capabilities", return_value=fake_caps):
            assert _lookup_supports_vision("anthropic", "claude-sonnet-4", {}) is True

    def test_no_override_no_models_dev_entry_returns_none(self):
        with patch("agent.models_dev.get_model_capabilities", return_value=None), \
             patch("agent.image_routing._should_probe_ollama_vision", return_value=False):
            assert _lookup_supports_vision("custom", "my-llava", {}) is None

    def test_ollama_probe_when_models_dev_missing(self):
        cfg = {"model": {"base_url": "http://localhost:11434/v1"}}
        with patch("agent.models_dev.get_model_capabilities", return_value=None), \
             patch("agent.image_routing._should_probe_ollama_vision", return_value=True), \
             patch("agent.model_metadata.query_ollama_supports_vision", return_value=True):
            assert _lookup_supports_vision("ollama", "gemma4:e2b", cfg) is True

    def test_ollama_probe_false_for_text_only_model(self):
        cfg = {"model": {"base_url": "http://localhost:11434/v1"}}
        with patch("agent.models_dev.get_model_capabilities", return_value=None), \
             patch("agent.image_routing._should_probe_ollama_vision", return_value=True), \
             patch("agent.model_metadata.query_ollama_supports_vision", return_value=False):
            assert _lookup_supports_vision("custom", "gemma4:31b", cfg) is False

    def test_cfg_none_falls_back_to_models_dev(self):
        # Caller didn't pass cfg at all — old call sites must still work.
        with patch("agent.models_dev.get_model_capabilities", return_value=None):
            assert _lookup_supports_vision("openrouter", "x", None) is None


# ─── decide_image_input_mode with auto + override ────────────────────────────


class TestAutoModeRespectsOverride:
    def test_auto_native_for_custom_with_supports_vision_true(self):
        # The motivating bug: Qwen3.6 on local llama.cpp via provider=custom.
        # Without the override, auto falls back to text. With it, auto picks
        # native — no need to also set agent.image_input_mode: native.
        cfg = {"model": {"supports_vision": True}}
        with patch("agent.models_dev.get_model_capabilities", return_value=None):
            assert decide_image_input_mode("custom", "qwen3.6-35b", cfg) == "native"

    def test_auto_text_for_custom_with_supports_vision_false(self):
        cfg = {"model": {"supports_vision": False}}
        with patch("agent.models_dev.get_model_capabilities", return_value=None):
            assert decide_image_input_mode("custom", "some-text-only", cfg) == "text"

    def test_auto_text_for_custom_with_no_override(self):
        # Unchanged baseline: unknown custom model → text.
        with patch("agent.models_dev.get_model_capabilities", return_value=None):
            assert decide_image_input_mode("custom", "unknown", {}) == "text"

    def test_vision_capable_model_attaches_natively_despite_nonexo_aux(self):
        # Native capability is now checked FIRST: a model declared
        # supports_vision: true attaches natively even when a (non-exo) aux
        # vision backend is configured. The aux delegate is reserved for
        # models that can't see, and only when the provider is exo.
        cfg = {
            "model": {"supports_vision": True},
            "auxiliary": {"vision": {"provider": "openrouter", "model": "gemini-2.5-pro"}},
        }
        with patch("agent.models_dev.get_model_capabilities", return_value=None):
            assert decide_image_input_mode("custom", "qwen3.6-35b", cfg) == "native"

    def test_nonvision_custom_model_with_nonexo_aux_does_not_delegate(self):
        # Non-vision model, but the aux backend is NOT exo → don't route to it;
        # native-attach instead (exo-only delegate scoping).
        cfg = {
            "model": {"supports_vision": False},
            "auxiliary": {"vision": {"provider": "openrouter", "model": "gemini-2.5-pro"}},
        }
        with patch("agent.models_dev.get_model_capabilities", return_value=None):
            assert decide_image_input_mode("custom", "some-text-only", cfg) == "native"


# ─── build_native_content_parts ──────────────────────────────────────────────


def _png_bytes() -> bytes:
    """Return a tiny valid 1x1 transparent PNG."""
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABpfZFQAAAAABJRU5ErkJggg=="
    )


class TestBuildNativeContentParts:
    def test_text_then_image(self, tmp_path: Path):
        img = tmp_path / "cat.png"
        img.write_bytes(_png_bytes())
        parts, skipped = build_native_content_parts("hello", [str(img)])
        assert skipped == []
        assert len(parts) == 2
        assert parts[0]["type"] == "text"
        # User caption is preserved and a per-image path hint is appended so
        # the model can use the local path as a string argument for tools
        # that take ``image_url: str`` (issue #18960).
        assert parts[0]["text"] == f"hello\n\n[Image attached at: {img}]"
        assert parts[1]["type"] == "image_url"
        assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_empty_text_inserts_default_prompt(self, tmp_path: Path):
        img = tmp_path / "cat.jpg"
        img.write_bytes(_png_bytes())
        parts, skipped = build_native_content_parts("", [str(img)])
        assert skipped == []
        # Even with empty user text, we insert a neutral prompt so the turn
        # isn't just pixels, and the path hint is appended after.
        assert parts[0]["type"] == "text"
        assert parts[0]["text"] == (
            f"What do you see in this image?\n\n[Image attached at: {img}]"
        )
        assert parts[1]["type"] == "image_url"

    def test_missing_file_is_skipped(self, tmp_path: Path):
        parts, skipped = build_native_content_parts("hi", [str(tmp_path / "missing.png")])
        assert skipped == [str(tmp_path / "missing.png")]
        # Skipped paths are NOT advertised in the path hints — the model
        # would otherwise be told a non-existent file is attached.
        assert parts == [{"type": "text", "text": "hi"}]

    def test_path_hint_appended(self, tmp_path: Path):
        """The local path of each attached image is appended to the user
        text part so MCP/skill tools that take ``image_url: str`` can be
        invoked on the same image (issue #18960). Mirrors text-mode
        behaviour (`Runner._enrich_message_with_vision`).
        """
        img = tmp_path / "scan.png"
        img.write_bytes(_png_bytes())
        parts, _ = build_native_content_parts("attach this", [str(img)])
        text_part = next(p for p in parts if p.get("type") == "text")
        assert "[Image attached at:" in text_part["text"]
        assert str(img) in text_part["text"]
        # User caption is preserved verbatim ahead of the hint.
        assert text_part["text"].startswith("attach this")

    def test_path_hint_one_per_attached_image(self, tmp_path: Path):
        """Each successfully attached image gets its own path hint line;
        skipped images do NOT appear in the hints.
        """
        good = tmp_path / "good.png"
        good.write_bytes(_png_bytes())
        missing = tmp_path / "missing.png"  # never created
        parts, skipped = build_native_content_parts(
            "see attached", [str(good), str(missing)]
        )
        assert skipped == [str(missing)]
        text_part = next(p for p in parts if p.get("type") == "text")
        assert text_part["text"].count("[Image attached at:") == 1
        assert str(good) in text_part["text"]
        assert str(missing) not in text_part["text"]

    def test_multiple_images(self, tmp_path: Path):
        img1 = tmp_path / "a.png"
        img2 = tmp_path / "b.png"
        img1.write_bytes(_png_bytes())
        img2.write_bytes(_png_bytes())
        parts, skipped = build_native_content_parts("compare these", [str(img1), str(img2)])
        assert skipped == []
        image_parts = [p for p in parts if p.get("type") == "image_url"]
        assert len(image_parts) == 2
        # Both paths surface in the text part, one per line.
        text_part = next(p for p in parts if p.get("type") == "text")
        assert text_part["text"].count("[Image attached at:") == 2
        assert str(img1) in text_part["text"]
        assert str(img2) in text_part["text"]

    def test_mime_inference_jpg(self, tmp_path: Path):
        # Real JPEG bytes (SOI marker FF D8 FF): sniffing now wins over suffix.
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 32)
        parts, _ = build_native_content_parts("x", [str(img)])
        url = parts[1]["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")

    def test_mime_inference_webp(self, tmp_path: Path):
        # Real WEBP bytes (RIFF....WEBP): sniffing now wins over suffix.
        img = tmp_path / "pic.webp"
        img.write_bytes(b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32)
        parts, _ = build_native_content_parts("", [str(img)])
        url = parts[1]["image_url"]["url"]
        assert url.startswith("data:image/webp;base64,")

    def test_mime_sniff_overrides_misleading_extension(self, tmp_path: Path):
        """Discord-style bug: file is named .webp but contains PNG bytes.
        Anthropic rejects on MIME mismatch (HTTP 400) so we MUST sniff.
        Regression guard for the user-reported Discord PNG-as-WEBP failure.
        """
        img = tmp_path / "discord_cached.webp"
        img.write_bytes(_png_bytes())  # bytes are PNG, suffix lies
        parts, _ = build_native_content_parts("", [str(img)])
        url = parts[1]["image_url"]["url"]
        assert url.startswith("data:image/png;base64,"), (
            f"Expected MIME sniffing to detect PNG bytes regardless of .webp suffix, got: {url[:60]}"
        )


# ─── Oversize handling ───────────────────────────────────────────────────────


class TestLargeImageHandling:
    """Large images attach at native size; shrink is handled reactively at
    retry time in ``run_agent._try_shrink_image_parts_in_messages`` rather
    than proactively here.
    """

    def test_large_image_passes_through_unchanged(self, tmp_path: Path):
        """A multi-MB image is attached as-is — no resize, no skip."""
        from agent import image_routing as _ir

        img = tmp_path / "medium.png"
        # 200 KB of real bytes; not huge but enough to verify no size gate fires.
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"X" * 200_000)
        url = _ir._file_to_data_url(img)
        assert url is not None
        assert url.startswith("data:image/png;base64,")
        # Base64 expansion means output is ~4/3 of input, plus header.
        assert len(url) > 200_000

    def test_missing_file_returns_none(self, tmp_path: Path):
        from agent import image_routing as _ir
        missing = tmp_path / "does_not_exist.png"
        assert _ir._file_to_data_url(missing) is None

    def test_build_native_parts_no_provider_kwarg(self, tmp_path: Path):
        """build_native_content_parts takes text + paths, no provider kwarg."""
        from agent import image_routing as _ir

        img = tmp_path / "cat.png"
        img.write_bytes(_png_bytes())
        parts, skipped = _ir.build_native_content_parts("hi", [str(img)])
        assert skipped == []
        assert len(parts) == 2
        assert parts[0]["type"] == "text"
        assert parts[1]["type"] == "image_url"


# ─── extract_image_refs ──────────────────────────────────────────────────────


class TestExtractImageRefs:
    """Scan task body / inbound text for image paths and URLs (kanban worker
    enrichment, issue raised May 2026)."""

    def test_empty_or_none_returns_empty(self):
        assert extract_image_refs("") == ([], [])
        assert extract_image_refs(None) == ([], [])  # type: ignore[arg-type]

    def test_finds_absolute_path(self, tmp_path: Path):
        img = tmp_path / "screenshot.png"
        img.write_bytes(_png_bytes())
        body = f"Look at {img} and tell me what's wrong."
        paths, urls = extract_image_refs(body)
        assert paths == [str(img)]
        assert urls == []

    def test_finds_home_relative_path(self, tmp_path: Path, monkeypatch):
        # Simulate ~/foo.png by pointing HOME at tmp_path and creating the file
        monkeypatch.setenv("HOME", str(tmp_path))
        img = tmp_path / "foo.png"
        img.write_bytes(_png_bytes())
        paths, urls = extract_image_refs("see ~/foo.png please")
        assert paths == [str(img)]
        assert urls == []

    def test_skips_nonexistent_paths(self, tmp_path: Path):
        # Path-shaped but no file on disk → skipped.
        body = f"What's at {tmp_path}/never_created.png ?"
        paths, urls = extract_image_refs(body)
        assert paths == []
        assert urls == []

    def test_finds_http_image_url(self):
        body = "Check out https://example.com/photos/cat.png — cute right?"
        paths, urls = extract_image_refs(body)
        assert paths == []
        assert urls == ["https://example.com/photos/cat.png"]

    def test_finds_https_url_with_query_string(self):
        body = "Diagram: https://cdn.example.com/img.jpeg?size=large&v=2 here"
        paths, urls = extract_image_refs(body)
        assert urls == ["https://cdn.example.com/img.jpeg?size=large&v=2"]

    def test_url_trailing_punctuation_stripped(self):
        # Prose punctuation right after the URL must not be part of the URL.
        body = "See https://example.com/a.png."
        paths, urls = extract_image_refs(body)
        assert urls == ["https://example.com/a.png"]

    def test_ignores_non_image_urls(self):
        body = "See https://example.com/page.html and https://x.com/y.pdf"
        paths, urls = extract_image_refs(body)
        assert urls == []

    def test_dedupes_paths_and_urls(self, tmp_path: Path):
        img = tmp_path / "dup.png"
        img.write_bytes(_png_bytes())
        body = (
            f"First {img} then again {img}. "
            "Also https://example.com/x.png and https://example.com/x.png again."
        )
        paths, urls = extract_image_refs(body)
        assert paths == [str(img)]
        assert urls == ["https://example.com/x.png"]

    def test_ignores_paths_in_fenced_code_block(self, tmp_path: Path):
        img = tmp_path / "real.png"
        img.write_bytes(_png_bytes())
        body = (
            "Outside the block, attach this:\n"
            f"{img}\n"
            "But not these examples:\n"
            "```\n"
            f"some_other_image: /tmp/example.png\n"
            f"url: https://example.com/example.png\n"
            "```\n"
        )
        paths, urls = extract_image_refs(body)
        assert paths == [str(img)]
        assert urls == []

    def test_ignores_paths_in_inline_code(self, tmp_path: Path):
        img = tmp_path / "real.jpg"
        img.write_bytes(_png_bytes())
        body = (
            f"Attach {img}, but ignore the example "
            "`https://example.com/skip.png` in backticks."
        )
        paths, urls = extract_image_refs(body)
        assert paths == [str(img)]
        assert urls == []

    def test_does_not_match_paths_inside_urls(self, tmp_path: Path):
        # The lookbehind in the regex prevents matching the path-portion of
        # a URL as a local path. Only the URL should be detected.
        body = "Just the URL: https://example.com/some/dir/image.png"
        paths, urls = extract_image_refs(body)
        assert paths == []
        assert urls == ["https://example.com/some/dir/image.png"]

    def test_mixed_paths_and_urls(self, tmp_path: Path):
        img = tmp_path / "local.png"
        img.write_bytes(_png_bytes())
        body = (
            f"Compare local {img} against the design at "
            "https://example.com/design/v2.png — does it match?"
        )
        paths, urls = extract_image_refs(body)
        assert paths == [str(img)]
        assert urls == ["https://example.com/design/v2.png"]

    def test_case_insensitive_extension(self, tmp_path: Path):
        img = tmp_path / "shouty.PNG"
        img.write_bytes(_png_bytes())
        body = f"see {img}"
        paths, urls = extract_image_refs(body)
        assert paths == [str(img)]


# ─── build_native_content_parts with URLs ────────────────────────────────────


class TestBuildNativeContentPartsURLs:
    """URL pass-through support added so kanban task bodies (and other
    inbound surfaces) can route remote image URLs straight to the model."""

    def test_url_only_no_local_paths(self):
        parts, skipped = build_native_content_parts(
            "what is this?",
            [],
            image_urls=["https://example.com/diagram.png"],
        )
        assert skipped == []
        assert len(parts) == 2
        assert parts[0]["type"] == "text"
        assert "[Image attached: https://example.com/diagram.png]" in parts[0]["text"]
        assert parts[0]["text"].startswith("what is this?")
        assert parts[1] == {
            "type": "image_url",
            "image_url": {"url": "https://example.com/diagram.png"},
        }

    def test_mixed_path_and_url(self, tmp_path: Path):
        img = tmp_path / "local.png"
        img.write_bytes(_png_bytes())
        parts, skipped = build_native_content_parts(
            "compare these",
            [str(img)],
            image_urls=["https://example.com/remote.jpg"],
        )
        assert skipped == []
        # 1 text + 2 image parts (local data URL first, then remote URL).
        image_parts = [p for p in parts if p.get("type") == "image_url"]
        assert len(image_parts) == 2
        assert image_parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
        assert image_parts[1]["image_url"]["url"] == "https://example.com/remote.jpg"
        text = parts[0]["text"]
        assert "[Image attached at:" in text
        assert "[Image attached: https://example.com/remote.jpg]" in text

    def test_empty_url_list_is_no_op(self, tmp_path: Path):
        img = tmp_path / "x.png"
        img.write_bytes(_png_bytes())
        # image_urls=[] should behave the same as not passing it at all.
        parts_no_urls, _ = build_native_content_parts("hi", [str(img)])
        parts_empty_urls, _ = build_native_content_parts("hi", [str(img)], image_urls=[])
        assert parts_no_urls == parts_empty_urls

    def test_blank_url_strings_are_dropped(self):
        parts, _ = build_native_content_parts(
            "x", [], image_urls=["", "  ", "https://example.com/a.png"]
        )
        image_parts = [p for p in parts if p.get("type") == "image_url"]
        assert len(image_parts) == 1
        assert image_parts[0]["image_url"]["url"] == "https://example.com/a.png"

    def test_url_only_inserts_default_prompt_when_text_empty(self):
        parts, _ = build_native_content_parts(
            "", [], image_urls=["https://example.com/a.png"]
        )
        assert parts[0]["type"] == "text"
        assert parts[0]["text"].startswith("What do you see in this image?")


# ─── _file_to_data_url ingestion ceiling ─────────────────────────────────────


import pytest

from agent.image_routing import _NATIVE_IMAGE_CEILING_BYTES, _file_to_data_url


def _has_pillow() -> bool:
    try:
        import PIL  # noqa: F401

        return True
    except Exception:
        return False


def _big_jpeg_bytes(side: int = 4000) -> bytes:
    """Return a JPEG large enough to exceed the ingestion ceiling.

    A noise-filled image resists JPEG compression so the encoded bytes stay
    well above _NATIVE_IMAGE_CEILING_BYTES even at quality 95.
    """
    from PIL import Image
    import io as _io
    import os as _os

    img = Image.frombytes("RGB", (side, side), _os.urandom(side * side * 3))
    buf = _io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


class TestFileToDataUrlIngestionCeiling:
    """The proactive ingestion ceiling — the root-cause fix for the 35 MB
    phone-photo 413 ``request_too_large`` that no conversation compression
    could recover from."""

    def test_small_image_passes_through_at_native_size(self, tmp_path: Path):
        """Images under the ceiling are encoded verbatim — no quality tax,
        no resize round-trip for the common case (screenshots, uploads)."""
        img = tmp_path / "small.png"
        raw = _png_bytes()
        img.write_bytes(raw)
        url = _file_to_data_url(img)
        assert url is not None
        assert url.startswith("data:image/png;base64,")
        # Exact native encoding — same base64 as a direct encode.
        assert url == f"data:image/png;base64,{base64.b64encode(raw).decode('ascii')}"

    def test_missing_file_returns_none(self, tmp_path: Path):
        assert _file_to_data_url(tmp_path / "does_not_exist.png") is None

    @pytest.mark.skipif(not _has_pillow(), reason="Pillow required to downscale")
    def test_oversized_image_is_downscaled_under_ceiling(self, tmp_path: Path):
        """An image whose base64 would breach the ceiling is downscaled at
        ingestion so it never reaches the provider oversized."""
        raw = _big_jpeg_bytes(side=4000)
        # Sanity: the source really is over the ceiling once base64-encoded.
        assert (len(raw) * 4) // 3 > _NATIVE_IMAGE_CEILING_BYTES
        img = tmp_path / "huge.jpg"
        img.write_bytes(raw)

        url = _file_to_data_url(img)
        assert url is not None
        assert url.startswith("data:image/")
        # The whole point: the emitted data URL fits under the ceiling.
        assert len(url) <= _NATIVE_IMAGE_CEILING_BYTES

    def test_oversized_falls_back_to_native_when_pillow_absent(
        self, tmp_path: Path, monkeypatch
    ):
        """If Pillow can't be imported or installed, _file_to_data_url must
        still return the native-size encode (the reactive retry-loop shrink
        is the backstop) rather than dropping the image entirely."""
        # A payload that *looks* oversized by byte count. Content validity
        # doesn't matter — we force the resize path to fail and assert the
        # fallback still yields a data URL.
        raw = b"\xff\xd8\xff" + b"\x00" * (_NATIVE_IMAGE_CEILING_BYTES + 1024)
        img = tmp_path / "oversized.jpg"
        img.write_bytes(raw)

        import tools.vision_tools as vt

        def _boom(*a, **k):
            raise RuntimeError("resize unavailable")

        monkeypatch.setattr(vt, "_resize_image_for_vision", _boom)

        url = _file_to_data_url(img)
        assert url is not None
        assert url.startswith("data:image/")
        # Native-size fallback: full bytes preserved for the retry-loop shrink.
        assert base64.b64encode(raw).decode("ascii") in url


# ─── Format compatibility: transcode non-universal formats to PNG ────────────


class TestFormatCompatibility:
    """Some image formats Discord (and other chat platforms) accept aren't
    accepted by every major vision provider. Anthropic for example returns
    HTTP 400 'Could not process image' for AVIF/HEIC/BMP/TIFF/ICO/SVG.

    We transcode anything outside the universal-safe set (PNG/JPEG/GIF/WEBP)
    to PNG with Pillow before declaring media_type so the provider call
    actually succeeds. Regression coverage for the user-reported Discord
    'Could not process image' HTTP 400 (issue #25935).
    """

    def test_avif_sniffed_correctly(self):
        from agent.image_routing import _sniff_mime_from_bytes
        avif_header = b"\x00\x00\x00\x20ftypavif\x00\x00\x00\x00"
        assert _sniff_mime_from_bytes(avif_header) == "image/avif"

    def test_tiff_sniffed_both_endians(self):
        from agent.image_routing import _sniff_mime_from_bytes
        assert _sniff_mime_from_bytes(b"II*\x00" + b"\x00" * 16) == "image/tiff"
        assert _sniff_mime_from_bytes(b"MM\x00*" + b"\x00" * 16) == "image/tiff"

    def test_ico_sniffed_correctly(self):
        from agent.image_routing import _sniff_mime_from_bytes
        assert _sniff_mime_from_bytes(b"\x00\x00\x01\x00" + b"\x00" * 16) == "image/x-icon"

    def test_heic_still_sniffed(self):
        from agent.image_routing import _sniff_mime_from_bytes
        heic_header = b"\x00\x00\x00\x20ftypheic\x00\x00\x00\x00"
        assert _sniff_mime_from_bytes(heic_header) == "image/heic"

    def test_svg_sniffed_correctly(self):
        from agent.image_routing import _sniff_mime_from_bytes
        assert _sniff_mime_from_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"/>') == "image/svg+xml"
        assert _sniff_mime_from_bytes(b'<?xml version="1.0"?><svg/>') == "image/svg+xml"

    def test_bmp_transcoded_to_png(self, tmp_path: Path):
        """BMP file should land as image/png in the data URL, not image/bmp,
        because not every provider (Anthropic) accepts BMP."""
        import pytest
        Image = pytest.importorskip("PIL.Image", reason="Pillow not installed; transcode is best-effort")
        from agent.image_routing import _file_to_data_url

        img_path = tmp_path / "scan.bmp"
        Image.new("RGB", (4, 4), (255, 0, 0)).save(img_path, format="BMP")
        url = _file_to_data_url(img_path)
        assert url is not None
        assert url.startswith("data:image/png;base64,"), (
            f"BMP must be transcoded to PNG for cross-provider compatibility, got: {url[:60]}"
        )

    def test_tiff_transcoded_to_png(self, tmp_path: Path):
        import pytest
        Image = pytest.importorskip("PIL.Image", reason="Pillow not installed; transcode is best-effort")
        from agent.image_routing import _file_to_data_url

        img_path = tmp_path / "scan.tiff"
        Image.new("RGB", (4, 4), (0, 255, 0)).save(img_path, format="TIFF")
        url = _file_to_data_url(img_path)
        assert url is not None
        assert url.startswith("data:image/png;base64,")

    def test_png_passes_through_no_transcode(self, tmp_path: Path):
        """Universal-safe formats must NOT be re-encoded — preserves bytes."""
        from agent.image_routing import _file_to_data_url

        img_path = tmp_path / "ok.png"
        img_path.write_bytes(_png_bytes())
        url = _file_to_data_url(img_path)
        assert url is not None
        assert url.startswith("data:image/png;base64,")
        b64 = url.split(",", 1)[1]
        assert base64.b64decode(b64) == _png_bytes()

    def test_file_to_data_url_blocks_read_denied_image_path(self, tmp_path: Path):
        """Native image routing must honor the shared credential read guard."""
        from agent.image_routing import _file_to_data_url

        img_path = tmp_path / ".env"
        img_path.write_bytes(_png_bytes())

        assert _file_to_data_url(img_path) is None

    def test_native_content_parts_skip_read_denied_local_image(self, tmp_path: Path):
        from agent.image_routing import build_native_content_parts

        img_path = tmp_path / ".env.local"
        img_path.write_bytes(_png_bytes())

        parts, skipped = build_native_content_parts("inspect this", [str(img_path)])

        assert skipped == [str(img_path)]
        assert all(part.get("type") != "image_url" for part in parts)

    def test_native_content_parts_blocks_image_symlink_to_read_denied_file(self, tmp_path: Path):
        from agent.image_routing import build_native_content_parts
        import os
        import pytest

        secret = tmp_path / ".env"
        secret.write_bytes(_png_bytes())
        img_link = tmp_path / "secret.png"
        try:
            os.symlink(secret, img_link)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlinks unavailable: {exc}")

        parts, skipped = build_native_content_parts("inspect this", [str(img_link)])

        assert skipped == [str(img_link)]
        assert all(part.get("type") != "image_url" for part in parts)

    def test_jpeg_passes_through_no_transcode(self, tmp_path: Path):
        from agent.image_routing import _file_to_data_url

        img_path = tmp_path / "ok.jpg"
        img_path.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9")
        url = _file_to_data_url(img_path)
        assert url is not None
        assert url.startswith("data:image/jpeg;base64,")

    def test_transcode_failure_is_skipped_not_crashed(self, tmp_path: Path):
        """If Pillow can't decode (corrupted bytes labeled as a rare format),
        return None so the caller skips it rather than sending broken data."""
        from agent.image_routing import _file_to_data_url

        img_path = tmp_path / "corrupt.avif"
        img_path.write_bytes(b"\x00\x00\x00\x20ftypavif" + b"\x00" * 32)
        url = _file_to_data_url(img_path)
        assert url is None

    def test_svg_skipped_not_transcoded(self, tmp_path: Path):
        """SVG is vector; Pillow can't rasterize it. It must be skipped
        (None) rather than producing an invalid data URL."""
        from agent.image_routing import _file_to_data_url

        img_path = tmp_path / "icon.svg"
        img_path.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"/>')
        url = _file_to_data_url(img_path)
        assert url is None
