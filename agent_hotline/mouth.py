"""Text-to-speech ("mouth") for agent-hotline.

The engine's synthesize call is blocking, so it runs in an executor to keep the
asyncio event loop (and the timers that race it) responsive.
"""

from __future__ import annotations

import asyncio
import io
import wave
from typing import Protocol

from agent_hotline.config import TtsConfig

KOKORO_SAMPLE_RATE = 24000


class TtsEngine(Protocol):
    def synthesize(self, text: str) -> bytes: ...


class Mouth:
    def __init__(self, engine: TtsEngine):
        self._engine = engine

    async def speak(self, text: str) -> bytes:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._engine.synthesize, text)


class _KokoroEngine:
    """Adapter from Kokoro's float audio chunks to WAV bytes."""

    def __init__(self, pipeline: object, voice: str):
        self._pipeline = pipeline
        self._voice = voice

    def synthesize(self, text: str) -> bytes:
        import numpy as np

        chunks = [
            audio for _graphemes, _phonemes, audio in self._pipeline(text, voice=self._voice)  # type: ignore[operator]
        ]
        samples = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(KOKORO_SAMPLE_RATE)
            wav.writeframes(pcm.tobytes())
        return buffer.getvalue()


def make_mouth(config: TtsConfig) -> Mouth:
    # M1: guard against an empty tts.voice with a clear error instead of a
    # bare IndexError from config.voice[0] below.
    if not config.voice:
        raise ValueError("TtsConfig.voice must be a non-empty string; set tts.voice in your config")

    # Imported here so importing this module does not require kokoro.
    from kokoro import KPipeline

    # Kokoro voice names are prefixed with their language code (e.g. "af_heart" -> "a").
    pipeline = KPipeline(lang_code=config.voice[0])
    return Mouth(engine=_KokoroEngine(pipeline, voice=config.voice))
