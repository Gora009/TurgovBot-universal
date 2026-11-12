"""STT через /audio/transcriptions: нарезка потока PCM на фразы и формат запроса."""
import httpx
import numpy as np
import pytest

from common.llm import settings
from services import stt_service as stt

RATE = 16000


def tone(ms, amp=8000):
    n = int(RATE * ms / 1000)
    t = np.arange(n) / RATE
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.int16).tobytes()


def silence(ms, amp=40):
    n = int(RATE * ms / 1000)
    return (np.random.default_rng(0).normal(0, amp, n)).astype(np.int16).tobytes()


def chunks(pcm, ms=100):
    step = int(RATE * ms / 1000) * 2
    return [pcm[i:i + step] for i in range(0, len(pcm), step)]


async def agen(items):
    for x in items:
        yield x


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_SMART_MODEL", "m")
    monkeypatch.setenv("LLM_STT_MODEL", "whisper-1")
    settings.reset_cache()
    yield
    settings.reset_cache()


async def test_two_phrases_split_by_pause(monkeypatch):
    sent = []

    async def fake_file(data, *, filename, mime, language=None, prompt=None):
        sent.append(len(data))
        return f"фраза {len(sent)}"

    monkeypatch.setattr(stt, "transcribe_file", fake_file)
    pcm = silence(500) + tone(800) + silence(1000) + tone(600) + silence(300)
    events = [e async for e in stt.stream_transcribe(agen(chunks(pcm)), phrases=["УК"])]
    assert events == [{"type": "final", "text": "фраза 1"}, {"type": "final", "text": "фраза 2"}]


async def test_short_click_is_ignored(monkeypatch):
    async def fake_file(*a, **k):
        raise AssertionError("шум не должен уходить на распознавание")

    monkeypatch.setattr(stt, "transcribe_file", fake_file)
    pcm = silence(500) + tone(100) + silence(1500)
    assert [e async for e in stt.stream_transcribe(agen(chunks(pcm)), phrases=["УК"])] == []


async def test_tail_without_pause_is_flushed(monkeypatch):
    async def fake_file(*a, **k):
        return "хвост"

    monkeypatch.setattr(stt, "transcribe_file", fake_file)
    pcm = silence(300) + tone(900)
    events = [e async for e in stt.stream_transcribe(agen(chunks(pcm)), phrases=["УК"])]
    assert events == [{"type": "final", "text": "хвост"}]


async def test_transcription_request_shape(monkeypatch):
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content
        return httpx.Response(200, json={"text": " Привет "})

    real = httpx.AsyncClient
    monkeypatch.setattr(stt.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    text = await stt.transcribe_file(b"OggS...", filename="voice.ogg", mime="audio/ogg", prompt="УК, АК")
    assert text == "Привет"
    assert seen["url"] == "https://api.example.com/v1/audio/transcriptions"
    assert seen["auth"] == "Bearer sk-test"
    body = seen["body"]
    assert b'name="model"' in body and b"whisper-1" in body
    assert b'name="language"' in body and b"\r\nru\r\n" in body
    assert b'filename="voice.ogg"' in body


def test_not_configured(monkeypatch):
    monkeypatch.delenv("LLM_STT_MODEL")
    settings.reset_cache()
    assert stt.stt_configured() is False
