"""Audio routes keep blocking config and file I/O off the ASGI event loop.

The dashboard backend serves every desktop/dashboard request from one loop, so a
synchronous config read, temp-audio write or file probe inside these ``async def``
routes stalls every other request while it runs.
"""

import base64
import json
import os
import tempfile
import threading

import pytest
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))


def _recording(threads: list, fn):
    def _wrapped(*args, **kwargs):
        threads.append(threading.current_thread())
        return fn(*args, **kwargs)

    return _wrapped


@pytest.mark.asyncio
async def test_pet_dialogue_reads_config_off_loop(monkeypatch):
    from hermes_cli.web_models import PetDialogueRequest
    from hermes_cli.web_routers.audio import pet_dialogue

    threads: list = []
    monkeypatch.setattr("hermes_cli.config.load_config_readonly",
                        _recording(threads, lambda: {"auxiliary": {"pet_dialogue": {"enabled": False}}}))

    with pytest.raises(HTTPException) as exc:
        await pet_dialogue(PetDialogueRequest(beat="completed", context="", pet_slug="miku"))

    assert exc.value.status_code == 404
    assert threads and threads[0] is not threading.current_thread()


@pytest.mark.asyncio
async def test_elevenlabs_voices_reads_env_off_loop(monkeypatch):
    from hermes_cli.web_routers.audio import get_elevenlabs_voices

    threads: list = []
    monkeypatch.setattr("hermes_cli.config.load_env", _recording(threads, dict))
    monkeypatch.setattr("agent.secret_scope.get_secret", lambda name: "")

    assert await get_elevenlabs_voices() == {"available": False, "voices": []}
    assert threads and threads[0] is not threading.current_thread()


@pytest.mark.asyncio
async def test_transcribe_writes_and_removes_temp_audio_off_loop(monkeypatch):
    from hermes_cli.web_models import AudioTranscriptionRequest
    from hermes_cli.web_routers.audio import transcribe_audio_upload

    threads: list = []
    seen_paths: list = []
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", _recording(threads, tempfile.NamedTemporaryFile))
    real_unlink = os.unlink

    def _unlink(path, *args, **kwargs):
        if os.path.basename(str(path)).startswith("hermes-desktop-voice-"):
            threads.append(threading.current_thread())
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr("os.unlink", _unlink)

    def _transcribe(path):
        seen_paths.append(path)
        with open(path, "rb") as fh:
            assert fh.read() == b"pcm-bytes"
        return {"success": True, "transcript": " hi ", "provider": "fake"}

    monkeypatch.setattr("tools.voice_mode.transcribe_recording", _transcribe)
    payload = AudioTranscriptionRequest(
        data_url="data:audio/webm;base64," + base64.b64encode(b"pcm-bytes").decode(),
        mime_type="audio/webm",
    )

    assert await transcribe_audio_upload(payload) == {"ok": True, "transcript": "hi", "provider": "fake"}
    assert len(threads) == 2  # the temp-file create and its unlink
    assert all(t is not threading.current_thread() for t in threads)
    assert seen_paths and not os.path.exists(seen_paths[0])


@pytest.mark.asyncio
async def test_speak_checks_synthesized_file_off_loop(monkeypatch):
    from hermes_cli.web_models import TTSSpeakRequest
    from hermes_cli.web_routers.audio import speak_text

    threads: list = []
    monkeypatch.setattr("tools.tts_tool.text_to_speech_tool",
                        lambda text, **_: json.dumps({"success": True, "file_path": "/nonexistent/out.mp3"}))
    real_isfile = os.path.isfile

    def _isfile(path):
        if path == "/nonexistent/out.mp3":
            threads.append(threading.current_thread())
        return real_isfile(path)

    monkeypatch.setattr("os.path.isfile", _isfile)

    with pytest.raises(HTTPException) as exc:
        await speak_text(TTSSpeakRequest(text="hello"))

    assert exc.value.status_code == 500 and exc.value.detail == "Audio file missing"
    assert threads and threads[0] is not threading.current_thread()
