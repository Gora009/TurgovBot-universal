"""
STT для голосового оверлея через OpenAI-совместимый /audio/transcriptions.

Работает с OpenAI (whisper-1, gpt-4o-mini-transcribe), Groq (whisper-large-v3-turbo),
локальными faster-whisper / whisper.cpp серверами и любым API с тем же эндпоинтом.
Поставщик — роль `stt` (LLM_STT_BASE_URL / LLM_STT_API_KEY / LLM_STT_MODEL, по
умолчанию общие LLM_BASE_URL / LLM_API_KEY).

Такие API не потоковые: принимают готовый аудиофайл. Поэтому поток PCM (LINEAR16 16 кГц
mono) режется на фразы по паузам: адаптивный порог громкости отделяет речь от фона,
после паузы STT_SILENCE_MS (или через STT_MAX_UTTERANCE_SEC) фраза уходит на распознавание
и возвращается событием {"type": "final", "text": ...}. Промежуточные partial-события
по умолчанию выключены (каждое — отдельный платный запрос), см. STT_PARTIAL_INTERVAL_SEC.

Контракт stream_transcribe прежний (его использует services/voice_session.py).
"""

from __future__ import annotations

import io
import logging
import os
import wave
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
import numpy as np

import config
from common.llm import settings as llm_settings

logger = logging.getLogger(__name__)

_BYTES_PER_SAMPLE = 2  # LINEAR16


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


SILENCE_MS = _env_float("STT_SILENCE_MS", 700)
MAX_UTTERANCE_SEC = _env_float("STT_MAX_UTTERANCE_SEC", 20)
MIN_SPEECH_MS = _env_float("STT_MIN_SPEECH_MS", 250)
PREROLL_MS = _env_float("STT_PREROLL_MS", 300)
ABS_RMS_THRESHOLD = _env_float("STT_VAD_RMS", 300)       # нижняя граница «речи» (шкала int16)
NOISE_FACTOR = _env_float("STT_VAD_NOISE_FACTOR", 3.0)  # речь = громче фона во столько раз
PARTIAL_INTERVAL_SEC = _env_float("STT_PARTIAL_INTERVAL_SEC", 0)  # 0 — без partial-событий


def _rms(pcm: bytes) -> float:
    usable = len(pcm) - len(pcm) % _BYTES_PER_SAMPLE
    if usable <= 0:
        return 0.0
    samples = np.frombuffer(pcm[:usable], dtype=np.int16).astype(np.float64)
    return float(np.sqrt(np.mean(samples ** 2)))


def _wav(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(_BYTES_PER_SAMPLE)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _prompt_from_phrases(phrases: Optional[List[str]]) -> str:
    """Whisper принимает подсказку-контекст (~224 токена): кладём доменные термины."""
    if not phrases:
        return ""
    out, size = [], 0
    for p in phrases:
        if size + len(p) + 2 > 600:
            break
        out.append(p)
        size += len(p) + 2
    return ", ".join(out)


def stt_configured() -> bool:
    return llm_settings.endpoint("stt").configured


async def transcribe_pcm(pcm: bytes, *, language: str, prompt: str = "",
                         sample_rate: Optional[int] = None) -> str:
    """Распознаёт один фрагмент PCM. Возвращает текст (может быть пустым)."""
    wav = _wav(pcm, sample_rate or config.STT_SAMPLE_RATE)
    return await transcribe_file(wav, filename="speech.wav", mime="audio/wav", language=language, prompt=prompt)


async def transcribe_file(data_bytes: bytes, *, filename: str, mime: str,
                          language: Optional[str] = None, prompt: Optional[str] = None) -> str:
    """Распознаёт готовый аудиофайл (wav, ogg/opus из Telegram, mp3, m4a…)."""
    ep = llm_settings.endpoint("stt")
    if not ep.configured:
        raise RuntimeError("STT не настроен: задайте LLM_STT_MODEL (и LLM_STT_BASE_URL / _API_KEY при необходимости)")
    language = language or config.STT_LANGUAGE
    if prompt is None:
        from services.stt_phrases import default_phrases
        prompt = _prompt_from_phrases(default_phrases())
    data = {"model": ep.model, "language": (language or "ru").split("-")[0], "response_format": "json"}
    if prompt:
        data["prompt"] = prompt
    files = {"file": (filename, data_bytes, mime)}
    headers = dict(llm_settings.extra_headers())
    if ep.api_key:
        headers["Authorization"] = f"Bearer {ep.api_key}"
    async with httpx.AsyncClient(timeout=llm_settings.request_timeout()) as client:
        resp = await client.post(f"{ep.base_url}/audio/transcriptions", data=data, files=files, headers=headers)
    if resp.status_code >= 400:
        raise RuntimeError(f"STT HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        return (resp.json().get("text") or "").strip()
    except ValueError:
        return resp.text.strip()


class _Segmenter:
    """Режет поток PCM на фразы по паузам (адаптивный порог громкости)."""

    def __init__(self, sample_rate: int):
        self.rate = sample_rate
        self.noise: Optional[float] = None  # оценка уровня фона
        self.preroll = bytearray()
        self.speech = bytearray()
        self.in_speech = False
        self.silence_ms = 0.0
        self.speech_ms = 0.0
        self.since_partial_ms = 0.0

    def _ms(self, nbytes: int) -> float:
        return nbytes / _BYTES_PER_SAMPLE / self.rate * 1000

    def feed(self, chunk: bytes) -> Optional[str]:
        """Возвращает 'final' (фраза готова), 'partial' (пора промежуточный) или None."""
        ms = self._ms(len(chunk))
        level = _rms(chunk)
        if self.noise is None:
            self.noise = min(level, ABS_RMS_THRESHOLD)
        threshold = max(ABS_RMS_THRESHOLD, self.noise * NOISE_FACTOR)
        loud = level >= threshold
        if not loud:
            self.noise = 0.95 * self.noise + 0.05 * level  # фон учим только по тихим кускам

        if not self.in_speech:
            self.preroll.extend(chunk)
            max_pre = int(PREROLL_MS / 1000 * self.rate) * _BYTES_PER_SAMPLE
            if len(self.preroll) > max_pre:
                del self.preroll[: len(self.preroll) - max_pre]
            if loud:
                self.in_speech = True
                self.speech = bytearray(self.preroll)
                self.preroll.clear()
                self.silence_ms = 0.0
                self.speech_ms = ms
                self.since_partial_ms = 0.0
            return None

        self.speech.extend(chunk)
        if loud:
            self.silence_ms = 0.0
            self.speech_ms += ms
        else:
            self.silence_ms += ms
        self.since_partial_ms += ms
        if self.silence_ms >= SILENCE_MS or self._ms(len(self.speech)) >= MAX_UTTERANCE_SEC * 1000:
            return "final"
        if PARTIAL_INTERVAL_SEC > 0 and self.since_partial_ms >= PARTIAL_INTERVAL_SEC * 1000:
            self.since_partial_ms = 0.0
            return "partial"
        return None

    def take(self) -> Optional[bytes]:
        """Забрать накопленную фразу (None — это был короткий шум, а не речь)."""
        pcm, enough = bytes(self.speech), self.speech_ms >= MIN_SPEECH_MS
        self.speech = bytearray()
        self.in_speech = False
        self.silence_ms = self.speech_ms = 0.0
        return pcm if (pcm and enough) else None


async def stream_transcribe(
    audio_chunks: AsyncIterator[bytes],
    *,
    language: Optional[str] = None,
    phrases: Optional[List[str]] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """Распознаёт поток PCM LINEAR16 16 кГц mono.

    Отдаёт события {"type": "final"|"partial", "text": str} — по одному final на фразу.
    """
    language = language or config.STT_LANGUAGE
    if not phrases:
        from services.stt_phrases import default_phrases
        phrases = default_phrases()
    prompt = _prompt_from_phrases(phrases)
    seg = _Segmenter(config.STT_SAMPLE_RATE)

    async def _final(pcm: bytes) -> Optional[Dict[str, Any]]:
        text = await transcribe_pcm(pcm, language=language, prompt=prompt)
        return {"type": "final", "text": text} if text else None

    try:
        async for chunk in audio_chunks:
            if not chunk:
                continue
            state = seg.feed(chunk)
            if state == "final":
                pcm = seg.take()
                if pcm:
                    ev = await _final(pcm)
                    if ev:
                        yield ev
            elif state == "partial":
                text = await transcribe_pcm(bytes(seg.speech), language=language, prompt=prompt)
                if text:
                    yield {"type": "partial", "text": text}
        if seg.in_speech:  # поток закончился посреди фразы — дораспознаём хвост
            pcm = seg.take()
            if pcm:
                ev = await _final(pcm)
                if ev:
                    yield ev
    except Exception:
        logger.exception("[STT] ошибка распознавания")
        raise
