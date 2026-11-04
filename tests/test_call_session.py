"""Tests for the shared call session loop.

All fakes here are hand-rolled test doubles (not defined in the task brief's
test snippet, which only names them) designed to mirror the real
collaborators closely enough that these tests exercise real control flow,
not tautologies:

- `FakeAgent` mimics the Claude Agent SDK adapter's "pause on ToolUseRequest,
  resume on the next send_text() call" behaviour (Task 4) with a single
  shared index into a pre-scripted events list, rather than restarting that
  list fresh on every call. That is what makes the cross-task continuation
  contract ("a second send_text() after resolve_tool_use() drains the SAME
  paused turn's follow-up events") testable here at all.
- `ScriptedAudioSource`/`FakeEars` clamp to their last scripted entry once
  exhausted rather than raising or going silent, so a test can supply one
  caller utterance and have it stand in for an answer to a later permission
  prompt too, without needing a distinct scripted entry for every single
  read the loop happens to perform.
- `SilentAudioSource`, `InfiniteAudioSource`, `CancellingAudioSource`, and
  `GoesSilentAfterFirstChunkAudioSource` are single-purpose and do not
  clamp/repeat the same way; each is built to exercise exactly one of the
  loop's timeout/cancellation paths deterministically and fast (no real
  sleeps, ever).
"""

from __future__ import annotations

import asyncio

import pytest

from agent_hotline.agents.base import TextReply, ToolUseRequest
from agent_hotline.call_session import CallResult, run_call_session
from agent_hotline.ears import TranscriptResult

# TranscriptResult.is_likely_speech requires no_speech_prob <= 0.6 and
# avg_logprob >= -1.0 (agent_hotline/ears.py); these are comfortably inside
# both thresholds.
_LIKELY_SPEECH_NO_SPEECH_PROB = 0.05
_LIKELY_SPEECH_AVG_LOGPROB = -0.2


class FakeAgent:
    """Stands in for `AgentBackend`.

    `events` is a single script for the fake's entire lifetime (mirroring
    the real adapter's session-lifetime pump state), consumed one item at a
    time by `send_text()` regardless of what text is passed in. Yielding
    stops immediately after a `ToolUseRequest` (pausing the "turn", exactly
    like the real adapter) and a later `send_text()` call — whether it is a
    reattach after `resolve_tool_use()` or a brand-new caller utterance —
    simply continues popping from wherever the index was left. Once the
    script is exhausted, every further call yields nothing, forever.
    """

    def __init__(self, events: list | None = None):
        self._events = list(events) if events else []
        self._pos = 0
        self.last_tool_decision: bool | None = None
        self.started = False
        self.ended = False

    async def start_session(self) -> None:
        self.started = True

    async def send_text(self, text: str):
        while self._pos < len(self._events):
            event = self._events[self._pos]
            self._pos += 1
            yield event
            if isinstance(event, ToolUseRequest):
                return

    async def resolve_tool_use(self, approved: bool) -> None:
        self.last_tool_decision = approved

    async def end_session(self) -> None:
        self.ended = True


class FakeEars:
    """Stands in for `Ears`. Ignores the audio bytes it is given entirely
    and pops its own scripted transcript list instead, clamping to the last
    entry once exhausted rather than raising."""

    def __init__(self, transcripts: list[str]):
        self._transcripts = list(transcripts)
        self._index = 0

    async def listen(self, audio: bytes) -> TranscriptResult:
        if self._transcripts:
            index = min(self._index, len(self._transcripts) - 1)
            text = self._transcripts[index]
            self._index += 1
        else:
            text = ""
        likely_speech = bool(text)
        return TranscriptResult(
            text=text,
            no_speech_prob=_LIKELY_SPEECH_NO_SPEECH_PROB if likely_speech else 1.0,
            avg_logprob=_LIKELY_SPEECH_AVG_LOGPROB if likely_speech else float("-inf"),
        )


class FakeMouth:
    """Stands in for `Mouth`. Records what it was asked to say."""

    def __init__(self):
        self.spoken: list[str] = []

    async def speak(self, text: str) -> bytes:
        self.spoken.append(text)
        return text.encode()


class FakeAudioSink:
    """Stands in for `AudioSink`. Records every chunk it was handed."""

    def __init__(self):
        self.sent: list[bytes] = []

    async def send_chunk(self, audio: bytes) -> None:
        self.sent.append(audio)


class ScriptedAudioSource:
    """Stands in for `AudioSource`, yielding one dummy chunk per scripted
    entry and then clamping to the last chunk forever once exhausted (see
    module docstring)."""

    def __init__(self, transcripts: list[str]):
        self._chunks = [f"chunk:{t}".encode() for t in transcripts]
        self._index = 0

    async def receive_chunk(self) -> bytes | None:
        if not self._chunks:
            return None
        index = min(self._index, len(self._chunks) - 1)
        self._index += 1
        return self._chunks[index]


class SilentAudioSource:
    """Never has anything to offer."""

    async def receive_chunk(self) -> bytes | None:
        return None


class InfiniteAudioSource:
    """Always has a real chunk ready, forever. Used to prove
    `max_call_length_s` is actually enforced rather than the loop
    coincidentally ending some other way."""

    async def receive_chunk(self) -> bytes | None:
        return b"infinite-chunk"


class CancellingAudioSource:
    """Raises asyncio.CancelledError on every read, simulating the call
    session's own task being cancelled out from under it."""

    async def receive_chunk(self) -> bytes | None:
        raise asyncio.CancelledError()


class GoesSilentAfterFirstChunkAudioSource:
    """Returns exactly one real chunk, then genuine silence (None) forever
    after. Used to prove a permission-answer read that gets nothing back AT
    ALL (not just an unclear answer) resolves to DENY, not APPROVE."""

    def __init__(self):
        self._given_first = False

    async def receive_chunk(self) -> bytes | None:
        if not self._given_first:
            self._given_first = True
            return b"chunk"
        return None


@pytest.mark.asyncio
async def test_quit_phrase_ends_session():
    source = ScriptedAudioSource(transcripts=["goodbye"])
    result = await run_call_session(FakeAgent(), FakeEars(transcripts=["goodbye"]), FakeMouth(), source, FakeAudioSink(), quit_phrases=["goodbye"], silence_timeout_s=30, max_call_length_s=1800)
    assert result.status == "quit_phrase"


@pytest.mark.asyncio
async def test_silence_timeout_ends_session():
    source = SilentAudioSource()
    result = await run_call_session(FakeAgent(), FakeEars(transcripts=[]), FakeMouth(), source, FakeAudioSink(), quit_phrases=["goodbye"], silence_timeout_s=0, max_call_length_s=1800)
    assert result.status == "silence_timeout"


@pytest.mark.asyncio
async def test_max_call_length_ends_session():
    result = await run_call_session(FakeAgent(), FakeEars(transcripts=["hello", "hello", "hello"]), FakeMouth(), InfiniteAudioSource(), FakeAudioSink(), quit_phrases=["goodbye"], silence_timeout_s=30, max_call_length_s=0)
    assert result.status == "max_length"


@pytest.mark.asyncio
async def test_tool_use_request_routed_through_permission_gate_and_resolved():
    agent = FakeAgent(events=[ToolUseRequest(description="read a file")])
    result = await run_call_session(agent, FakeEars(transcripts=["yes"]), FakeMouth(), ScriptedAudioSource(transcripts=["yes"]), FakeAudioSink(), quit_phrases=["goodbye"], silence_timeout_s=30, max_call_length_s=1800)
    assert agent.last_tool_decision is True


@pytest.mark.asyncio
async def test_cancelled_error_reports_cancelled_not_error():
    source = CancellingAudioSource()  # raises asyncio.CancelledError on receive_chunk
    result = await run_call_session(FakeAgent(), FakeEars(transcripts=[]), FakeMouth(), source, FakeAudioSink(), quit_phrases=["goodbye"], silence_timeout_s=30, max_call_length_s=1800)
    assert result.status == "cancelled"


@pytest.mark.asyncio
async def test_tool_use_approval_continues_draining_for_follow_up_text_reply():
    """Regression-shaped test for the exact bug called out in the task
    brief: once a tool use is approved, the agent's follow-up reply for that
    SAME turn must not be silently dropped. `run_call_session` must call
    `agent.send_text("")` again after `resolve_tool_use()` and keep doing so
    until that turn's generator is actually exhausted."""
    agent = FakeAgent(events=[ToolUseRequest(description="read a file"), TextReply(text="done!")])
    mouth = FakeMouth()
    sink = FakeAudioSink()
    source = ScriptedAudioSource(transcripts=["x"])
    result = await run_call_session(
        agent,
        FakeEars(transcripts=["please read the file", "yes", "goodbye"]),
        mouth,
        source,
        sink,
        quit_phrases=["goodbye"],
        silence_timeout_s=30,
        max_call_length_s=1800,
    )
    assert agent.last_tool_decision is True
    assert "done!" in mouth.spoken
    assert result.status == "quit_phrase"


@pytest.mark.asyncio
async def test_permission_prompt_with_no_answer_at_all_denies_not_approves():
    """Integration-level half of the "silence must never approve" guarantee
    (Task 3's `evaluate_answer(None) == DENY` is the unit-level half): a
    genuine timeout waiting for the caller's spoken answer to a tool-use
    prompt (no chunk, no speech, nothing at all — not just an unclear
    answer) must reach `agent.resolve_tool_use(approved=False)`, never
    `True`, and must not be skipped in a way that defaults to approval."""
    agent = FakeAgent(events=[ToolUseRequest(description="delete a file")])
    source = GoesSilentAfterFirstChunkAudioSource()
    result = await run_call_session(
        agent,
        FakeEars(transcripts=["please delete the file"]),
        FakeMouth(),
        source,
        FakeAudioSink(),
        quit_phrases=["goodbye"],
        silence_timeout_s=5,
        max_call_length_s=1800,
    )
    assert agent.last_tool_decision is False
    assert result.status == "silence_timeout"
