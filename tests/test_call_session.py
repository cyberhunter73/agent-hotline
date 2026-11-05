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
- `ScriptedAudioSource`/`FakeEars` repeat their last scripted entry for
  exactly one read beyond the end of the script — one "grace" read — and
  then go genuinely silent (a missing chunk / a non-speech transcript) for
  every read after that. The grace read exists only so a test can script a
  single caller utterance and have it double as the answer to a permission
  prompt that utterance itself triggers (the brief's own given
  `test_tool_use_request_routed_through_permission_gate_and_resolved` does
  exactly this with one-item lists), without the fakes clamping forever —
  which would require the production loop to invent an "empty turn ends the
  call" rule just to keep a test from hanging. Tests that need more than one
  real read beyond the initial turn (e.g. a tool-use exchange followed by
  another real caller turn) should size their scripted lists to the exact
  number of reads they need, rather than relying on the grace read.
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
    and pops its own scripted transcript list instead. Repeats the last
    entry for exactly one read beyond the script (see module docstring),
    then reports non-speech (not just an empty string) for every read after
    that, so a test that over-reads a `FakeEars` gets a transcript
    `evaluate_answer`/the silence check genuinely treat as "no real speech"
    rather than a coincidentally-empty one."""

    def __init__(self, transcripts: list[str]):
        self._transcripts = list(transcripts)
        self._calls = 0

    async def listen(self, audio: bytes) -> TranscriptResult:
        call = self._calls
        self._calls += 1
        text = ""
        if self._transcripts:
            if call <= len(self._transcripts) - 1:
                text = self._transcripts[call]
            elif call == len(self._transcripts):
                text = self._transcripts[-1]  # one grace repeat
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
    entry, repeating the last chunk for exactly one grace read beyond the
    script, and then going genuinely silent (`None`) for every read after
    that (see module docstring)."""

    def __init__(self, transcripts: list[str]):
        self._chunks = [f"chunk:{t}".encode() for t in transcripts]
        self._calls = 0

    async def receive_chunk(self) -> bytes | None:
        call = self._calls
        self._calls += 1
        if not self._chunks:
            return None
        if call <= len(self._chunks) - 1:
            return self._chunks[call]
        if call == len(self._chunks):
            return self._chunks[-1]  # one grace repeat
        return None  # genuinely silent from here on


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


class HangingMidTurnAgent:
    """Stands in for `AgentBackend` whose `send_text()` hangs forever
    mid-turn — never yields an event and never returns on its own. Used to
    prove `max_call_length_s` bounds a hang INSIDE a turn (an agent reply
    that never completes), not just the gaps between reads/turns the
    original between-checkpoints-only enforcement relied on."""

    async def start_session(self) -> None:
        pass

    async def send_text(self, text: str):
        await asyncio.sleep(99999)
        if False:  # pragma: no cover - keeps this an async generator
            yield

    async def resolve_tool_use(self, approved: bool) -> None:
        pass

    async def end_session(self) -> None:
        pass


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
    # Three reads are needed here (initial turn, permission answer, next
    # turn) — sized exactly, so this test does not depend on either fake's
    # one-read grace repeat (that mechanism exists only for the brief's own
    # given test, which scripts just one caller utterance for both roles).
    source = ScriptedAudioSource(transcripts=["x", "y", "z"])
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
async def test_unclear_permission_answer_reprompts_then_resolves_normally():
    """Brief requirement: "loop on UNCLEAR (ask again) rather than denying".
    An answer evaluate_answer resolves to UNCLEAR must not deny the tool use
    and must not end the call — it must trigger the re-prompt and keep
    listening until a real yes/no comes back."""
    agent = FakeAgent(events=[ToolUseRequest(description="read a file")])
    mouth = FakeMouth()
    # Four reads: initial turn, an unclear answer, a re-prompt-triggered
    # second answer attempt that actually approves, and (since the loop
    # keeps going afterward) one more read — sized exactly, no grace reads
    # relied on.
    source = ScriptedAudioSource(transcripts=["a", "b", "c", "d"])
    ears = FakeEars(transcripts=["please read the file", "what do you mean by that", "yes", "goodbye"])
    result = await run_call_session(
        agent,
        ears,
        mouth,
        source,
        FakeAudioSink(),
        quit_phrases=["goodbye"],
        silence_timeout_s=30,
        max_call_length_s=1800,
    )
    assert agent.last_tool_decision is True
    assert any("didn't catch" in spoken for spoken in mouth.spoken)
    assert result.status == "quit_phrase"


@pytest.mark.asyncio
async def test_hang_inside_a_turn_is_bounded_by_max_call_length_s():
    """I3: a hang anywhere inside a turn (here: the agent's send_text()
    never completing) must still be bounded by max_call_length_s, not just
    a hang between reads/turns. Wrapped in asyncio.wait_for as a test-level
    safety net: if this regresses, the test fails fast instead of hanging
    the whole suite."""
    agent = HangingMidTurnAgent()
    source = ScriptedAudioSource(transcripts=["hello"])
    result = await asyncio.wait_for(
        run_call_session(
            agent,
            FakeEars(transcripts=["hello"]),
            FakeMouth(),
            source,
            FakeAudioSink(),
            quit_phrases=["goodbye"],
            silence_timeout_s=30,
            max_call_length_s=0.05,
        ),
        timeout=5,
    )
    assert result.status == "max_length"


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
