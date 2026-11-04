"""Shared call session loop for agent-hotline.

`run_call_session` is the single event loop both inbound and outbound calls
drive: read audio from the caller, transcribe it, check for a quit phrase,
hand the text to the agent, speak back whatever it says, and run a spoken
permission gate on every tool use the agent asks for — with a wall-clock cap
on the whole call and a timeout on each read.

Timeout-structure judgment call (the brief leaves this underspecified, and
flags it for review): there is no separate "permission-answer timeout"
parameter in the interface. `silence_timeout_s` is reused as the per-read
window for BOTH the caller's normal turns and a tool-use permission answer,
via `_receive_with_timeout`. The two cases are deliberately NOT handled the
same way on a miss, though:

- During a normal turn, a missed read (no chunk at all, or a transcript
  `ears` does not consider real speech) ends the whole call with
  `"silence_timeout"` — there is nothing to talk about.
- While waiting specifically for an answer to a permission prompt, a missed
  read is passed straight into `permission_gate.evaluate_answer` as `None`
  (that function already treats a non-speech transcript the same way it
  treats `None`), which resolves to `PermissionAnswer.DENY`. The call is NOT
  ended; `agent.resolve_tool_use(approved=False)` is called and the session
  continues. This is the integration-level half of the "silence must never
  approve" guarantee — Task 3's `evaluate_answer(None) == DENY` is the
  unit-level half this relies on.

`agent.start_session()`/`agent.end_session()` are called here, once each,
bracketing the whole call: this loop owns the agent session's lifetime for
the call it is driving (a fresh session per call, never resumed, is the
project-wide rule `AgentBackend` documents). `end_session()` runs in a
`finally` and swallows its own exceptions so a teardown failure never masks
the call's real outcome; it is skipped entirely if `start_session()` itself
never completed.

`asyncio.CancelledError` is caught explicitly and reported as
`CallResult("cancelled")` — it must never fall into the generic
`except Exception` branch and come out as `"error"` (see lesson #5 in the
plan this task implements).

A caller turn that produces zero agent events (no `TextReply`, no
`ToolUseRequest` at all) is NOT treated as the call ending. `AgentEvent`
(agents/base.py) has no signal by which the agent can say "I'm ending this
call" on its own initiative — `send_message()` on the real adapter can
legitimately yield nothing for a turn for reasons that have nothing to do
with the conversation being over (a model hiccup, a filtered/empty
response). Ending the whole call on that signal would silently hang up on
a caller whenever the agent happened to go quiet for a turn. So an
empty turn is treated the same as the agent choosing to say nothing right
now: the loop just goes back to listening for the caller's next utterance.
One consequence worth being explicit about: as of this module, nothing
reachable from `run_call_session` ever produces `CallResult("completed")`
— that status exists in the interface but has no production code path
here, since there is no "the agent is done" signal to drive it from. That
is intentional, not an oversight; a future task that adds such a signal to
`AgentEvent` would be the place to wire it up.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import Literal, Protocol

from agent_hotline.agents.base import AgentBackend, TextReply, ToolUseRequest
from agent_hotline.ears import Ears
from agent_hotline.mouth import Mouth
from agent_hotline.permission_gate import PermissionAnswer, evaluate_answer

CallStatus = Literal["completed", "quit_phrase", "silence_timeout", "max_length", "cancelled", "error"]

_PERMISSION_REPROMPT = "Sorry, I didn't catch that. Please say yes or no."


@dataclass(frozen=True)
class CallResult:
    status: CallStatus


class AudioSource(Protocol):
    async def receive_chunk(self) -> bytes | None: ...


class AudioSink(Protocol):
    async def send_chunk(self, audio: bytes) -> None: ...


async def run_call_session(
    agent: AgentBackend,
    ears: Ears,
    mouth: Mouth,
    audio_source: AudioSource,
    audio_sink: AudioSink,
    quit_phrases: list[str],
    silence_timeout_s: int,
    max_call_length_s: int,
) -> CallResult:
    start_time = time.monotonic()

    def time_exhausted() -> bool:
        return time.monotonic() - start_time >= max_call_length_s

    session_started = False
    try:
        await agent.start_session()
        session_started = True

        while True:
            if time_exhausted():
                return CallResult(status="max_length")

            chunk = await _receive_with_timeout(audio_source, silence_timeout_s)
            if chunk is None:
                return CallResult(status="silence_timeout")

            transcript = await ears.listen(chunk)
            if not transcript.is_likely_speech:
                return CallResult(status="silence_timeout")

            if _matches_quit_phrase(transcript.text, quit_phrases):
                return CallResult(status="quit_phrase")

            await _drive_agent_turn(
                agent=agent,
                text=transcript.text,
                mouth=mouth,
                ears=ears,
                audio_source=audio_source,
                audio_sink=audio_sink,
                silence_timeout_s=silence_timeout_s,
                time_exhausted=time_exhausted,
            )
            # A turn producing zero agent events is not an end-of-call
            # signal (see module docstring) — just go back to listening.
    except asyncio.CancelledError:
        return CallResult(status="cancelled")
    except Exception:
        return CallResult(status="error")
    finally:
        if session_started:
            with contextlib.suppress(Exception):
                await agent.end_session()


def _matches_quit_phrase(text: str, quit_phrases: list[str]) -> bool:
    lowered = text.lower()
    return any(phrase.lower() in lowered for phrase in quit_phrases)


async def _receive_with_timeout(audio_source: AudioSource, timeout_s: float) -> bytes | None:
    """A single bounded read of one chunk.

    `timeout_s <= 0` is treated as an immediate miss directly, rather than
    handed to `asyncio.wait_for` (whose behaviour at a zero/negative timeout
    is not something worth depending on being exactly "fire instantly").
    """
    if timeout_s <= 0:
        return None
    try:
        return await asyncio.wait_for(audio_source.receive_chunk(), timeout=timeout_s)
    except asyncio.TimeoutError:
        return None


async def _speak(mouth: Mouth, audio_sink: AudioSink, text: str) -> None:
    audio = await mouth.speak(text)
    await audio_sink.send_chunk(audio)


async def _drive_agent_turn(
    *,
    agent: AgentBackend,
    text: str,
    mouth: Mouth,
    ears: Ears,
    audio_source: AudioSource,
    audio_sink: AudioSink,
    silence_timeout_s: int,
    time_exhausted,
) -> None:
    """Drain one caller turn, including any tool-use round trips within it.

    A turn producing zero events (no `TextReply`, no `ToolUseRequest`) is a
    no-op here, by design — see the module docstring for why that must not
    be mistaken for "the call is over".
    """
    while True:
        if time_exhausted():
            # Let the outer loop's own check report this on its next pass.
            return

        paused_on_tool_use = False
        async for event in agent.send_text(text):
            if isinstance(event, TextReply):
                await _speak(mouth, audio_sink, event.text)
            elif isinstance(event, ToolUseRequest):
                approved = await _resolve_permission(
                    tool_use=event,
                    mouth=mouth,
                    ears=ears,
                    audio_source=audio_source,
                    audio_sink=audio_sink,
                    silence_timeout_s=silence_timeout_s,
                    time_exhausted=time_exhausted,
                )
                await agent.resolve_tool_use(approved=approved)
                paused_on_tool_use = True
                break

        if not paused_on_tool_use:
            return

        # Reattach to the same paused turn (see agents/base.py and
        # agents/claude_agent_sdk.py's module docstring): the text argument
        # is ignored on a reattach, so an empty string is fine here.
        text = ""


async def _resolve_permission(
    *,
    tool_use: ToolUseRequest,
    mouth: Mouth,
    ears: Ears,
    audio_source: AudioSource,
    audio_sink: AudioSink,
    silence_timeout_s: int,
    time_exhausted,
) -> bool:
    await _speak(mouth, audio_sink, tool_use.description)
    while True:
        if time_exhausted():
            # Out of call time entirely while still waiting on an answer:
            # deny rather than approve, same as any other no-answer case.
            return False

        chunk = await _receive_with_timeout(audio_source, silence_timeout_s)
        # No chunk at all (a genuine timeout) is passed straight through as
        # None rather than treated as its own special case: evaluate_answer
        # already resolves None to DENY (Task 3), which is exactly the
        # "silence must never approve" guarantee this integration relies on.
        transcript = await ears.listen(chunk) if chunk is not None else None

        decision = evaluate_answer(transcript)
        if decision is PermissionAnswer.APPROVE:
            return True
        if decision is PermissionAnswer.DENY:
            return False
        # UNCLEAR: ask again rather than denying.
        await _speak(mouth, audio_sink, _PERMISSION_REPROMPT)
