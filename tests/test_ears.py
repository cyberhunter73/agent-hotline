import threading

import pytest

from agent_hotline.config import SttConfig
from agent_hotline.ears import Ears, TranscriptResult, make_ears


class FakeSttEngine:
    def __init__(self, returns: TranscriptResult):
        self._returns = returns
        self.received_audio: bytes | None = None
        self.thread_name: str | None = None

    def transcribe(self, audio: bytes) -> TranscriptResult:
        self.received_audio = audio
        self.thread_name = threading.current_thread().name
        return self._returns


def test_transcript_not_likely_speech_on_high_no_speech_prob():
    result = TranscriptResult(text="Thanks for watching!", no_speech_prob=0.95, avg_logprob=-0.2)
    assert result.is_likely_speech is False


def test_transcript_not_likely_speech_on_low_avg_logprob():
    result = TranscriptResult(text="You", no_speech_prob=0.1, avg_logprob=-1.5)
    assert result.is_likely_speech is False


def test_transcript_likely_speech_when_both_within_bounds():
    result = TranscriptResult(text="yes", no_speech_prob=0.05, avg_logprob=-0.3)
    assert result.is_likely_speech is True


def test_transcript_thresholds_are_exclusive_at_the_boundary():
    # Exactly 0.6 / -1.0 is still within bounds; only strictly beyond them is rejected.
    at_bounds = TranscriptResult(text="yes", no_speech_prob=0.6, avg_logprob=-1.0)
    assert at_bounds.is_likely_speech is True


@pytest.mark.asyncio
async def test_ears_listen_delegates_to_engine_off_loop():
    fake_engine = FakeSttEngine(returns=TranscriptResult("yes", 0.05, -0.3))
    ears = Ears(engine=fake_engine)
    result = await ears.listen(b"audio-bytes")
    assert result.text == "yes"
    assert fake_engine.received_audio == b"audio-bytes"
    assert fake_engine.thread_name != threading.current_thread().name


def test_make_ears_raises_when_device_is_none():
    with pytest.raises(ValueError, match="must be set explicitly"):
        make_ears(SttConfig(engine="faster-whisper", device=None))


def test_make_ears_raises_when_device_is_auto():
    with pytest.raises(ValueError, match="auto"):
        make_ears(SttConfig(engine="faster-whisper", device="auto"))
