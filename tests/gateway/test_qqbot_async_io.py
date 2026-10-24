"""QQ adapter: dispatched handlers are owned tasks, and file I/O keeps the loop's contract."""

import asyncio
import logging

import pytest

from gateway.config import PlatformConfig


def _adapter():
    from plugins.platforms.qqbot.adapter import QQAdapter
    return QQAdapter(PlatformConfig(enabled=True, extra={"app_id": "a", "client_secret": "b"}))


@pytest.mark.asyncio
async def test_dispatched_message_task_is_held_and_its_failure_logged(caplog):
    adapter = _adapter()
    release = asyncio.Event()

    async def failing_handler(*_args):
        await release.wait()
        raise RuntimeError("handler boom")

    adapter._handle_c2c_message = failing_handler
    with caplog.at_level(logging.ERROR, logger="gateway.platforms.qqbot.adapter"):
        adapter._dispatch_payload({"op": 0, "t": "C2C_MESSAGE_CREATE", "s": 1, "d": {"id": "m1"}})
        (task,) = adapter._dispatch_tasks
        release.set()
        with pytest.raises(RuntimeError):
            await task
        await asyncio.sleep(0)  # done-callbacks run on the next loop iteration

    assert not adapter._dispatch_tasks
    failures = [r for r in caplog.records if "Background task failed" in r.getMessage()]
    assert failures and failures[0].exc_info[1].args == ("handler boom",)


@pytest.mark.asyncio
async def test_stt_uploads_the_wav_file_bytes(tmp_path):
    adapter = _adapter()
    wav = tmp_path / "voice.wav"
    wav.write_bytes(b"RIFF-fake-wav")
    sent = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"text": " hello "}

    class _Client:
        async def post(self, url, *, headers, files, data, timeout):
            name, payload, mime = files["file"]
            sent.update(url=url, name=name, payload=payload if isinstance(payload, bytes) else payload.read(),
                        mime=mime, model=data["model"])
            return _Resp()

    adapter._http_client = _Client()
    adapter._resolve_stt_config = lambda: {
        "base_url": "https://stt.example", "api_key": "k", "model": "whisper-1", "timeout": 60}

    assert await adapter._call_stt(str(wav)) == "hello"
    assert sent == {"url": "https://stt.example/audio/transcriptions", "name": "voice.wav",
                    "payload": b"RIFF-fake-wav", "mime": "audio/wav", "model": "whisper-1"}
    assert await adapter._call_stt(str(tmp_path / "missing.wav")) is None


@pytest.mark.asyncio
async def test_upload_local_file_path_errors(tmp_path, monkeypatch):
    adapter = _adapter()
    adapter._http_client = object()
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="placeholder"):
        await adapter._upload_local_file("c2c", "u1", "<path>", 1, None)
    with pytest.raises(FileNotFoundError, match=str(tmp_path / "nope.png")):
        await adapter._upload_local_file("c2c", "u1", "nope.png", 1, None)
