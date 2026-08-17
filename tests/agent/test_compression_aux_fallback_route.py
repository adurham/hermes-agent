"""The aux-summary fallback must actually reach the main model (#123362).

``_fallback_to_main_for_compression`` clears ``summary_model``, but an omitted route makes
``call_llm(task="compression")`` re-resolve ``auxiliary.compression`` from config — the model
that just failed — so the retry re-fails and later attempts abort without a second fallback.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.context_compressor import ContextCompressor


def _compressor():
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        return ContextCompressor(
            model="main-model", provider="main-provider", quiet_mode=True,
            summary_model_override="quota-exhausted-aux",
        )


def _access_error():
    error = Exception("Error code: 403 - model is not available in the current token plan")
    error.status_code = 403
    return error


def test_access_failed_aux_falls_back_to_named_main_route():
    ok = MagicMock()
    ok.choices = [MagicMock()]
    ok.choices[0].message.content = "summary via main model"
    compressor = _compressor()

    with patch("agent.context_compressor.call_llm", side_effect=[_access_error(), ok]) as mock_call:
        summary = compressor._generate_summary([
            {"role": "user", "content": "do something"}, {"role": "assistant", "content": "ok"},
        ])

    assert summary is not None and "summary via main model" in summary
    first, second = mock_call.call_args_list
    assert first.kwargs.get("model") == "quota-exhausted-aux"
    assert (second.kwargs.get("model"), second.kwargs.get("provider")) == ("main-model", "main-provider")


def test_micro_summarize_also_names_main_route_after_fallback():
    compressor = _compressor()
    compressor._fallback_to_main_for_compression(_access_error(), "failed")
    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="merged"))])

    with patch("agent.auxiliary_client.call_llm", side_effect=_capture), patch.object(
        compressor, "_build_micro_summary_prompt", return_value=[{"role": "user", "content": "x"}]
    ):
        compressor._micro_summarize_one("an exchange")

    assert (captured.get("model"), captured.get("provider")) == ("main-model", "main-provider")
