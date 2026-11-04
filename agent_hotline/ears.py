"""Speech-to-text ("ears") for agent-hotline.

The engine's transcribe call is blocking, so it runs in an executor to keep the
asyncio event loop (and the timers that race it) responsive.
"""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from typing import Protocol

from agent_hotline.config import SttConfig

NO_SPEECH_PROB_THRESHOLD = 0.6
AVG_LOGPROB_THRESHOLD = -1.0


@dataclass
class TranscriptResult:
    text: str
    no_speech_prob: float
    avg_logprob: float

    @property
    def is_likely_speech(self) -> bool:
        if self.no_speech_prob > NO_SPEECH_PROB_THRESHOLD:
            return False
        if self.avg_logprob < AVG_LOGPROB_THRESHOLD:
            return False
        return True


class SttEngine(Protocol):
    def transcribe(self, audio: bytes) -> TranscriptResult: ...


class Ears:
    def __init__(self, engine: SttEngine):
        self._engine = engine

    async def listen(self, audio: bytes) -> TranscriptResult:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._engine.transcribe, audio)


class _FasterWhisperEngine:
    """Adapter from faster-whisper segments to a single TranscriptResult."""

    def __init__(self, model: object):
        self._model = model

    def transcribe(self, audio: bytes) -> TranscriptResult:
        # faster-whisper decodes container formats (e.g. WAV) from a file-like object.
        segments, _info = self._model.transcribe(io.BytesIO(audio))  # type: ignore[attr-defined]
        segments = list(segments)
        if not segments:
            return TranscriptResult(text="", no_speech_prob=1.0, avg_logprob=float("-inf"))
        # Conservative aggregation: the transcript is only as trustworthy as its
        # least-confident segment.
        return TranscriptResult(
            text="".join(segment.text for segment in segments).strip(),
            no_speech_prob=max(segment.no_speech_prob for segment in segments),
            avg_logprob=min(segment.avg_logprob for segment in segments),
        )


def make_ears(config: SttConfig) -> Ears:
    if config.device is None:
        raise ValueError(
            "SttConfig.device must be set explicitly (got None); set stt.device in your config"
        )
    if config.device.strip().lower() == "auto":
        raise ValueError("SttConfig.device may not be 'auto'; set it explicitly (e.g. 'cpu')")

    # Imported here so importing this module does not require faster-whisper.
    from faster_whisper import WhisperModel

    model = WhisperModel("small", device=config.device)
    return Ears(engine=_FasterWhisperEngine(model))
