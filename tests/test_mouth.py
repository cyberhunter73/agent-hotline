import threading

import pytest

from agent_hotline.config import TtsConfig
from agent_hotline.mouth import Mouth, make_mouth


class FakeTtsEngine:
    def __init__(self, returns: bytes):
        self._returns = returns
        self.received_text: str | None = None
        self.thread_name: str | None = None

    def synthesize(self, text: str) -> bytes:
        self.received_text = text
        self.thread_name = threading.current_thread().name
        return self._returns


@pytest.mark.asyncio
async def test_mouth_speak_delegates_to_engine_off_loop():
    fake_engine = FakeTtsEngine(returns=b"wav-bytes")
    mouth = Mouth(engine=fake_engine)
    audio = await mouth.speak("hello")
    assert audio == b"wav-bytes"
    assert fake_engine.received_text == "hello"
    assert fake_engine.thread_name != threading.current_thread().name


def test_make_mouth_rejects_empty_voice_with_clear_error():
    """M1: an empty tts.voice must raise a clear error, not a bare
    IndexError from config.voice[0]."""
    with pytest.raises(ValueError, match="voice"):
        make_mouth(TtsConfig(engine="kokoro", voice=""))
