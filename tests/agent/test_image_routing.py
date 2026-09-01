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
    _should_probe_ollama_vision,
    _supports_vision_override,
    build_native_content_parts,
    decide_image_input_mode,
    extract_image_refs,
)


# ─── _coerce_mode ────────────────────────────────────────────────────────────


class TestCoerceMode:

    def test_case_insensitive(self):
        assert _coerce_mode("NATIVE") == "native"
        assert _coerce_mode("Auto") == "auto"

    def test_invalid_falls_back_to_auto(self):
        assert _coerce_mode("nonsense") == "auto"
        assert _coerce_mode("") == "auto"
        assert _coerce_mode(None) == "auto"
        assert _coerce_mode(42) == "auto"



# ─── _explicit_aux_vision_override ───────────────────────────────────────────


class TestExplicitAuxVisionOverride:
    def test_none_config(self):
        assert _explicit_aux_vision_override(None) is False

    def test_empty_config(self):
        assert _explicit_aux_vision_override({}) is False






# ─── decide_image_input_mode ─────────────────────────────────────────────────


class TestDecideImageInputMode:

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

    def test_auto_unset_aux_backend_native_remains_default(self):
        """No configured aux backend -> native for vision-capable mains
        (the unconfigured-install default is unchanged)."""
        for cfg in ({}, {"auxiliary": {}}, {"auxiliary": {"vision": {"provider": "auto"}}}):
            with patch("agent.image_routing._lookup_supports_vision", return_value=True):
                assert decide_image_input_mode("anthropic", "claude-sonnet-4", cfg) == "native"

    def test_image_input_mode_native_overrides_aux_backend(self):
        """agent.image_input_mode: native stays the absolute escape hatch —
        forces native attach even with an explicit aux backend."""
        cfg = {"agent": {"image_input_mode": "native"},
               "auxiliary": {"vision": {"provider": "openrouter", "model": "google/gemini-2.5-flash"}}}
        with patch("agent.image_routing._lookup_supports_vision", return_value=True):
            assert decide_image_input_mode("anthropic", "claude-sonnet-4", cfg) == "native"


    def test_none_config_is_auto(self):
        with patch("agent.image_routing._lookup_supports_vision", return_value=True):
            assert decide_image_input_mode("anthropic", "claude-sonnet-4", None) == "native"




# ─── _coerce_capability_bool ─────────────────────────────────────────────────


class TestCoerceCapabilityBool:
    def test_real_bool_passes_through(self):
        assert _coerce_capability_bool(True) is True
        assert _coerce_capability_bool(False) is False

    def test_int_0_and_1(self):
        assert _coerce_capability_bool(1) is True
        assert _coerce_capability_bool(0) is False






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












# ─── _lookup_supports_vision (override-aware) ────────────────────────────────


class TestLookupSupportsVisionOverride:


    def test_no_override_falls_back_to_models_dev(self):
        fake_caps = type("Caps", (), {"supports_vision": True})()
        with patch("agent.models_dev.get_model_capabilities", return_value=fake_caps):
            assert _lookup_supports_vision("anthropic", "claude-sonnet-4", {}) is True


    def test_ollama_probe_when_models_dev_missing(self):
        cfg = {"model": {"base_url": "http://localhost:11434/v1"}}
        with patch("agent.models_dev.get_model_capabilities", return_value=None), \
             patch("agent.image_routing._should_probe_ollama_vision", return_value=True), \
             patch("agent.model_metadata.query_ollama_supports_vision", return_value=True):
            assert _lookup_supports_vision("ollama", "gemma4:e2b", cfg) is True


    def test_cfg_none_falls_back_to_models_dev(self):
        # Caller didn't pass cfg at all — old call sites must still work.
        with patch("agent.models_dev.get_model_capabilities", return_value=None):
            assert _lookup_supports_vision("openrouter", "x", None) is None


# ─── _should_probe_ollama_vision ──────────────────────────────────────────────


class TestShouldProbeOllamaVision:
    """Regression tests for issue #89863: remote OpenAI-compatible endpoints
    must not be fingerprint-probed (with or without an api_key).
    """

    def test_ollama_provider_always_probes(self):
        # provider="ollama" → probe regardless of base_url
        assert _should_probe_ollama_vision("ollama", "") is True

    def test_empty_base_url_returns_false(self):
        assert _should_probe_ollama_vision("custom", "") is False

    def test_remote_endpoint_not_probed(self):
        # A remote sglang/vLLM endpoint must NEVER be fingerprinted — that's
        # the 401-spray bug from #89863.
        assert _should_probe_ollama_vision(
            "custom", "https://my-remote-host/v1"
        ) is False

    def test_remote_endpoint_not_probed_without_key(self):
        # Same as above but explicit: no api_key is not a reason to probe.
        assert _should_probe_ollama_vision(
            "custom", "https://inference.example.com/v1", api_key=""
        ) is False

    def test_remote_endpoint_not_probed_with_key(self):
        # Even WITH an api_key, remote endpoints are not local and must not be
        # fingerprinted — the key just prevents 401s, it doesn't make the
        # endpoint local.
        assert _should_probe_ollama_vision(
            "custom", "https://inference.example.com/v1", api_key="sk-xxxx"
        ) is False

    def test_local_endpoint_with_key_passes_key(self):
        # A local endpoint is still probed; the api_key must be forwarded so
        # keyed local servers don't 401.
        with patch(
            "agent.model_metadata.detect_local_server_type",
            return_value="ollama",
        ) as mock_detect:
            result = _should_probe_ollama_vision(
                "custom", "http://localhost:11434/v1", api_key="sk-local"
            )
        assert result is True
        mock_detect.assert_called_once_with(
            "http://localhost:11434/v1", api_key="sk-local"
        )

    def test_local_endpoint_without_key(self):
        # Legacy call: no api_key → forwarded as "" (existing behaviour).
        with patch(
            "agent.model_metadata.detect_local_server_type",
            return_value="ollama",
        ) as mock_detect:
            result = _should_probe_ollama_vision(
                "custom", "http://127.0.0.1:11434/v1"
            )
        assert result is True
        mock_detect.assert_called_once_with("http://127.0.0.1:11434/v1", api_key="")


# ─── decide_image_input_mode with auto + override ────────────────────────────


class TestAutoModeRespectsOverride:


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


    def test_finds_http_image_url(self):
        body = "Check out https://example.com/photos/cat.png — cute right?"
        paths, urls = extract_image_refs(body)
        assert paths == []
        assert urls == ["https://example.com/photos/cat.png"]











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





# ─── vision alias for custom providers ──────────────────────────────────────


class TestCustomProviderVisionAlias:
    """`vision: true` should work as an alias for `supports_vision: true`.

    Covers both config shapes that host named custom providers:
      * the ``providers.<name>.models`` dict, and
      * the legacy list-style ``custom_providers`` entries.

    Regression for the review of PR #31912: named custom providers resolve
    to the runtime value ``provider="custom"`` while the config keeps the
    user-declared name under ``model.provider``. The existing candidate-name
    resolver must be *extended* to accept the ``vision`` alias, not replaced.
    """


    def test_providers_dict_vision_alias_false(self):
        cfg = {
            "providers": {
                "my-vllm": {"models": {"llama-3": {"vision": False}}}
            }
        }
        assert _supports_vision_override(cfg, "my-vllm", "llama-3") is False


    def test_named_custom_provider_bare_custom_runtime_vision_alias(self):
        """Teknium's requested regression case.

        A named custom provider (``model.provider: my-vllm``) is rewritten to
        the runtime value ``provider="custom"`` by
        ``hermes_cli/runtime_provider.py``. The resolver must still match the
        ``my-vllm`` entry via the ``model.provider`` candidate and honour the
        ``vision`` alias.
        """
        cfg = {
            "model": {"provider": "my-vllm"},
            "providers": {
                "my-vllm": {"models": {"llava-v1.6": {"vision": True}}}
            },
        }
        # Runtime provider is the bare normalized value "custom".
        assert _supports_vision_override(cfg, "custom", "llava-v1.6") is True
        assert decide_image_input_mode("custom", "llava-v1.6", cfg) == "native"



    def test_vision_alias_none_when_model_absent(self):
        cfg = {
            "custom_providers": [
                {"name": "my-vllm", "models": {"llava": {"vision": True}}}
            ]
        }
        assert _supports_vision_override(cfg, "custom:my-vllm", "other") is None


def _fake_key(tag: str) -> str:
    """Build an obviously-fake placeholder key from parts — never a literal
    that could be mistaken for (or collide with) a real credential."""
    return "fake-" + tag + "-not-a-secret"


class TestProbeApiKeyForwarding:
    """The local server-type probe must carry the provider's API key (#89863).

    A remote API-keyed endpoint answers the probe waterfall with 401s
    without the Authorization header, and an unauthorized probe can never
    produce a positive verdict — so every image-bearing turn re-ran the
    5-request waterfall against the user's own server.
    """

    def test_resolve_inference_api_key_model_block(self):
        from agent.image_routing import _resolve_inference_api_key

        key = _fake_key("model")
        cfg = {"model": {"api_key": key}}
        assert _resolve_inference_api_key(cfg, "custom") == key

    def test_resolve_inference_api_key_providers_block(self):
        from agent.image_routing import _resolve_inference_api_key

        key = _fake_key("prov")
        cfg = {
            "model": {"provider": "custom:remote"},
            "providers": {
                "custom:remote": {"base_url": "https://x/v1", "api_key": key}
            },
        }
        assert _resolve_inference_api_key(cfg, "custom:remote") == key

    def test_resolve_inference_api_key_custom_providers_list(self):
        from agent.image_routing import _resolve_inference_api_key

        key = _fake_key("list")
        cfg = {
            "model": {"provider": "remote"},
            "custom_providers": [
                {"name": "remote", "base_url": "https://x/v1", "api_key": key}
            ],
        }
        assert _resolve_inference_api_key(cfg, "remote") == key

    def test_resolve_inference_api_key_absent(self):
        from agent.image_routing import _resolve_inference_api_key

        assert _resolve_inference_api_key({"model": {}}, "custom") == ""
        assert _resolve_inference_api_key(None, "custom") == ""

    def test_should_probe_forwards_api_key(self):
        from agent.image_routing import _should_probe_ollama_vision

        key = _fake_key("probe")
        with patch(
            "agent.model_metadata.detect_local_server_type",
            return_value=None,
        ) as detect:
            _should_probe_ollama_vision("custom", "https://remote/v1", api_key=key)
        detect.assert_called_once_with("https://remote/v1", api_key=key)

    def test_lookup_passes_resolved_key_to_probe(self):
        """The full lookup path resolves the key from cfg and hands it to the
        probe — the exact chain that sprayed 401s in #89863."""
        key = _fake_key("lookup")
        import agent.models_dev  # noqa: F401 — make the patch target importable
        with patch(
            "agent.models_dev.get_model_capabilities", return_value=None
        ), patch(
            "agent.image_routing._resolve_inference_base_url",
            return_value="https://remote/v1",
        ), patch(
            "agent.model_metadata.detect_local_server_type", return_value=None
        ) as detect:
            _lookup_supports_vision("custom", "llava", {"model": {"api_key": key}})
        assert detect.call_args.kwargs.get("api_key") == key
