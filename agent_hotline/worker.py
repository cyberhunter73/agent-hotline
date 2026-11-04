"""LiveKit Agents worker entrypoint for agent-hotline.

Wires together everything from Tasks 1-8 into the actual LiveKit Agents
worker process.

- `handle_inbound_job` is the testable core, and this module's Review Focus
  item (the project's SIP-cleanup safety task): given a caller number and
  the collaborators needed to run or decline a call, it decides whether to
  run the call session at all, and — whichever way that goes — guarantees
  `participant.remove()` is always attempted afterward, with its own
  failure isolated so it can never crash the worker process (which would
  take down every other in-flight call) and never mask `run_session()`'s
  own result or exception.
- `entrypoint(ctx)` is the real LiveKit job-handler entrypoint that drives
  `handle_inbound_job` for an actual inbound call. It has NO automated test
  coverage (per the task brief: it requires the real `livekit-agents` SDK
  and a live job context); it is exercised by the manual rollout plan
  (SETUP.md) instead. Keep any logic worth unit-testing out of this
  function and in `handle_inbound_job` or a small helper.
- `run_outbound_worker` / `_cli_main` expose `OutboundNotifier.notify()` as
  the outbound "notify()" trigger the design calls for.

Judgment call — real `livekit-agents` SDK API surface. This module was
written without the real SDK's exact types available to consult (the same
situation Task 4 was in with the Claude Agent SDK, and `livekit-agents` is
in fact not installed in this dev environment either — see pyproject.toml
vs. what `pip show` reports). Nothing LiveKit-specific is imported at
module level, for the same reason `ears.py`/`mouth.py` defer their heavy
imports into their `make_*` functions: importing this module (to get
`handle_inbound_job` and `SipParticipantHandle` for tests) must never
require the real SDK to be installed. All LiveKit-specific assumptions are
confined to `entrypoint`, `_extract_caller_info`,
`_LiveKitSipParticipantHandle`, `_RoomAudioSource`, and `_RoomAudioSink`,
and are documented there, flagged for review, and are exactly the parts
this task leaves untested:

- Job entrypoint shape: `async def entrypoint(ctx: JobContext) -> None`,
  the function a worker process registers via
  `agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint))` —
  the pattern documented for LiveKit Agents job workers.
- Caller number extraction: the inbound SIP participant is assumed to be
  the job's one remote participant, reachable at
  `ctx.room.remote_participants` (a mapping of identity -> participant),
  and the calling number is assumed to be published on it under the
  attribute key `"sip.phoneNumber"` — LiveKit's documented SIP participant
  attribute convention for the caller's number — via
  `participant.attributes.get("sip.phoneNumber")`.
- SIP participant removal: assumed reachable through the LiveKit server
  API's room service, via `ctx.api.room.remove_participant(...)`, wrapped
  here in `_LiveKitSipParticipantHandle` so it satisfies the
  `SipParticipantHandle` Protocol `handle_inbound_job` depends on instead
  of the concrete SDK type.
- Room audio I/O: `_RoomAudioSource`/`_RoomAudioSink` are deliberately
  minimal adapters satisfying `call_session.AudioSource`/`AudioSink`
  (whole-utterance `bytes` in, whole-utterance WAV `bytes` out — the same
  shape `ears.py`/`mouth.py` already use), not a tuned voice-activity
  detector. They are the right scope for what this task needs (import
  wiring, not a verified audio pipeline) and are exactly what manual
  rollout testing against a real call must re-check first.

Outbound trunk id judgment call (carried forward from Task 7, where
`OutboundNotifier.__init__` defaults `trunk_id` to the literal placeholder
string `"trunk-id"`, with a comment flagging that the real value must be
wired before any live use — see outbound.py). `OutboundConfig` (Task 1) has
no trunk id field. Two reasonable options:

  (a) Read it from an environment variable, matching this project's
      existing pattern for LiveKit deployment credentials
      (`LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, per the
      reference implementation "telekit" this project is modeled on) —
      i.e. infra ids are deployment-specific and shouldn't be committed to
      the YAML config.
  (b) Add a `trunk_id` field to `OutboundConfig` in `config.py`.

This module chooses (a): `_outbound_trunk_id()` below reads
`LIVEKIT_SIP_OUTBOUND_TRUNK_ID` from the environment and raises
`WorkerConfigError` if it is unset, rather than silently falling back to
`OutboundNotifier`'s placeholder default — a real outbound call must never
go out against a placeholder trunk id. (b) was not chosen because it would
mean editing a Task 1 file for a decision that is really about deployment
configuration, not call-safety policy; it remains a reasonable alternative
if a future task decides config-file placement is preferable for this
project.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Callable, Protocol

from agent_hotline.agents.claude_agent_sdk import ClaudeAgentSdkBackend
from agent_hotline.call_session import CallResult, run_call_session
from agent_hotline.config import Config, load_config
from agent_hotline.ears import make_ears
from agent_hotline.inbound import InboundGate
from agent_hotline.mouth import make_mouth
from agent_hotline.outbound import OutboundNotifier
from agent_hotline.outbound_gate import OutboundDecision, OutboundGate

if TYPE_CHECKING:  # pragma: no cover - type-checking only, never imported at runtime
    from livekit.agents import JobContext

logger = logging.getLogger(__name__)

_SIP_PHONE_NUMBER_ATTRIBUTE = "sip.phoneNumber"
_TRUNK_ID_ENV_VAR = "LIVEKIT_SIP_OUTBOUND_TRUNK_ID"
_CONFIG_PATH_ENV_VAR = "AGENT_HOTLINE_CONFIG"
_DEFAULT_CONFIG_PATH = "config.yaml"


class WorkerConfigError(Exception):
    """Raised when the worker process cannot be wired up safely: a required
    piece of deployment configuration (e.g. the outbound trunk id) or job
    context (e.g. a caller number) is missing."""


class SipParticipantHandle(Protocol):
    """Whatever `handle_inbound_job` needs in order to remove the caller's
    SIP participant from the room at the end of a call, abstracted away
    from the concrete LiveKit type so the gate-and-cleanup logic below can
    be unit tested without the real SDK."""

    async def remove(self) -> None: ...


async def handle_inbound_job(
    caller_number: str,
    inbound_gate: InboundGate,
    run_session: Callable[[], Awaitable[CallResult]],
    participant: SipParticipantHandle,
    play_brush_off: Callable[[], Awaitable[None]],
) -> CallResult | None:
    """Decide whether an inbound caller may be connected to a call session,
    and run it if so.

    Not allowed: calls `play_brush_off()`, never calls `run_session`,
    returns `None`.

    Allowed: calls `run_session()` and returns its `CallResult` (or lets
    its exception propagate), and in BOTH cases always attempts
    `participant.remove()` in a `finally` block first. That removal
    attempt has its own `try`/`except Exception`, logged and swallowed: a
    failure removing the SIP participant must never propagate out of this
    function (it must never crash the worker process, which would take
    down every other in-flight call) and must never suppress or replace
    whatever `run_session()` itself returned or raised.
    """
    if not inbound_gate.is_allowed(caller_number):
        await play_brush_off()
        return None

    try:
        return await run_session()
    finally:
        try:
            await participant.remove()
        except Exception:
            # Logged, never raised: see the safety contract in this
            # function's docstring and the module docstring's Review Focus
            # note. This is the entire enforcement of that guarantee —
            # nothing else in this function, or in any caller, is allowed
            # to be the backstop.
            logger.exception(
                "failed to remove SIP participant for caller %r after call; "
                "continuing without crashing the worker",
                caller_number,
            )


def _extract_caller_info(ctx: "JobContext") -> tuple[str, object]:
    """Extract the calling number and the SIP participant it came from, off
    the job context.

    Documented assumption (see module docstring): the inbound SIP
    participant is the room's one remote participant at job-entry time,
    and the caller's number is published on it as the
    `"sip.phoneNumber"` attribute. Returns `(caller_number, sip_participant)`
    since `entrypoint` needs both: the number for `handle_inbound_job`, and
    the participant's identity for building its `SipParticipantHandle`.
    """
    participants = list(ctx.room.remote_participants.values())
    if not participants:
        raise WorkerConfigError(
            "no remote participant found on the job context's room; expected the inbound SIP caller"
        )
    sip_participant = participants[0]
    caller_number = sip_participant.attributes.get(_SIP_PHONE_NUMBER_ATTRIBUTE)
    if not caller_number:
        raise WorkerConfigError(
            f"SIP participant has no {_SIP_PHONE_NUMBER_ATTRIBUTE!r} attribute; cannot identify caller"
        )
    return caller_number, sip_participant


class _LiveKitSipParticipantHandle:
    """Adapts a real LiveKit room + participant identity to
    `SipParticipantHandle`.

    Documented assumption: the LiveKit server API's room service exposes
    SIP-participant removal as `ctx.api.room.remove_participant(room=...,
    identity=...)` (the standard LiveKit `RoomService.RemoveParticipant`
    RPC, surfaced through the Python server SDK attached to the job
    context as `ctx.api`).
    """

    def __init__(self, ctx: "JobContext", identity: str) -> None:
        self._ctx = ctx
        self._identity = identity

    async def remove(self) -> None:
        await self._ctx.api.room.remove_participant(
            room=self._ctx.room.name,
            identity=self._identity,
        )


class _RoomAudioSource:
    """Adapts a LiveKit remote audio track to `call_session.AudioSource`.

    Minimal, documented-assumption placeholder (see module docstring): not
    a tuned voice-activity detector. `receive_chunk` is assumed to be
    wired, elsewhere in the real rollout, to hand back one utterance's
    worth of raw audio bytes at a time (mirroring the whole-blob-per-call
    shape `ears.py`'s `Ears.listen` already expects); the exact framing
    strategy is left to be verified during the manual rollout (SETUP.md)
    against a real call, not invented here without the SDK to check it
    against.
    """

    def __init__(self, track: object) -> None:
        self._track = track

    async def receive_chunk(self) -> bytes | None:
        raise NotImplementedError(
            "wire real LiveKit audio frame capture here during manual rollout testing (SETUP.md)"
        )


class _RoomAudioSink:
    """Adapts `call_session.AudioSink` to a published LiveKit audio track.

    `send_chunk` receives one whole WAV blob per utterance (the shape
    `mouth.py`'s `Mouth.speak` produces); decoding it back to raw PCM
    frames for `rtc.AudioSource.capture_frame` is left to the manual
    rollout (see module docstring) rather than guessed at without the real
    SDK to verify sample-rate/frame-size handling against.
    """

    def __init__(self, source: object) -> None:
        self._source = source

    async def send_chunk(self, audio: bytes) -> None:
        raise NotImplementedError(
            "wire real LiveKit audio frame playback here during manual rollout testing (SETUP.md)"
        )


async def _play_brush_off(mouth, audio_sink: _RoomAudioSink) -> None:
    """Speak a short decline message to a caller the inbound gate rejects,
    then hang up. No call session, no agent, is ever started for this
    caller."""
    audio = await mouth.speak("Sorry, this number is not recognized. Goodbye.")
    await audio_sink.send_chunk(audio)


async def entrypoint(ctx: "JobContext") -> None:
    """Real LiveKit Agents job-handler entrypoint for an inbound call.

    NOT unit tested (see module docstring) — requires the real SDK and a
    live job context. Loads `Config`, builds the call-session
    collaborators from it, extracts the caller's number from the SIP
    participant on `ctx`, and delegates the gate-and-cleanup decision to
    `handle_inbound_job`.
    """
    await ctx.connect()

    config = load_config(os.environ.get(_CONFIG_PATH_ENV_VAR, _DEFAULT_CONFIG_PATH))

    caller_number, sip_participant = _extract_caller_info(ctx)
    inbound_gate = InboundGate(allowed_numbers=config.inbound.allowed_numbers)
    participant = _LiveKitSipParticipantHandle(ctx, sip_participant.identity)

    ears = make_ears(config.stt)
    mouth = make_mouth(config.tts)

    def client_factory():
        # Imported lazily so importing this module never requires the real
        # Claude Agent SDK (same pattern as agents/claude_agent_sdk.py).
        from claude_agent_sdk import ClaudeSDKClient  # type: ignore[import-not-found]

        return ClaudeSDKClient()

    agent = ClaudeAgentSdkBackend(
        client_factory=client_factory,
        permission_mode="ask",
        always_allow_rules=[],
    )

    audio_source = _RoomAudioSource(track=None)
    audio_sink = _RoomAudioSink(source=None)

    async def run_session() -> CallResult:
        return await run_call_session(
            agent=agent,
            ears=ears,
            mouth=mouth,
            audio_source=audio_source,
            audio_sink=audio_sink,
            quit_phrases=config.inbound.quit_phrases,
            silence_timeout_s=config.inbound.silence_timeout_s,
            max_call_length_s=config.inbound.max_call_length_s,
        )

    async def play_brush_off() -> None:
        await _play_brush_off(mouth, audio_sink)

    await handle_inbound_job(caller_number, inbound_gate, run_session, participant, play_brush_off)


def _outbound_trunk_id() -> str:
    """Read the real outbound SIP trunk id from the environment.

    See the module docstring's "Outbound trunk id judgment call" section:
    this is deliberately NOT read from `OutboundConfig`, and deliberately
    does not fall back to `OutboundNotifier`'s placeholder default — a
    missing trunk id must fail loudly here, not place a real call to
    nowhere.
    """
    trunk_id = os.environ.get(_TRUNK_ID_ENV_VAR)
    if not trunk_id:
        raise WorkerConfigError(
            f"{_TRUNK_ID_ENV_VAR} must be set to a real LiveKit SIP outbound trunk id; "
            "refusing to fall back to OutboundNotifier's placeholder default"
        )
    return trunk_id


def build_outbound_notifier(config: Config, clock=None) -> OutboundNotifier:
    """Construct a real, live-call-capable `OutboundNotifier` from `Config`.

    Documented assumption: a real `SipClient` is reachable through the
    LiveKit server API's SIP service as
    `LiveKitAPI().sip.create_sip_participant(...)`, wrapped here to satisfy
    `outbound.SipClient`'s `create_participant` shape. Imported lazily so
    importing this module never requires the real `livekit-agents` SDK.
    """
    import datetime as _datetime

    from livekit import api  # type: ignore[import-not-found]

    class _LiveKitSipClient:
        async def create_participant(self, trunk_id: str, number: str, room_name: str) -> None:
            lk_api = api.LiveKitAPI()
            try:
                await lk_api.sip.create_sip_participant(
                    api.CreateSIPParticipantRequest(
                        sip_trunk_id=trunk_id,
                        sip_call_to=number,
                        room_name=room_name,
                    )
                )
            finally:
                await lk_api.aclose()

    gate = OutboundGate(config.outbound, clock=clock or _datetime.datetime.now)
    return OutboundNotifier(
        gate=gate,
        sip_client=_LiveKitSipClient(),
        config=config.outbound,
        trunk_id=_outbound_trunk_id(),
    )


async def run_outbound_worker(notifier: OutboundNotifier, event_type: str, message: str) -> OutboundDecision:
    """Transport-level entrypoint for the outbound `notify()` trigger.

    The design only fixes the gate logic (`OutboundGate`/`OutboundNotifier`),
    not how a trigger reaches it — implementer's choice of transport. This
    project exposes it as a small async function rather than standing up a
    long-lived socket/HTTP server: whatever external process decides "this
    event happened" (a cron job, a monitoring script, another part of this
    codebase) can call this directly, or it can be driven from the command
    line via `_cli_main` below.
    """
    return await notifier.notify(event_type, message)


def _cli_main(argv: list[str]) -> int:
    """`python -m agent_hotline.worker notify <event_type> <message>` —
    the simplest possible transport for the outbound `notify()` trigger
    (see `run_outbound_worker`'s docstring for why this project does not
    stand up a socket/HTTP server for it instead)."""
    import asyncio

    if len(argv) != 4 or argv[1] != "notify":
        print("usage: python -m agent_hotline.worker notify <event_type> <message>", file=sys.stderr)
        return 2

    _, _, event_type, message = argv
    config = load_config(os.environ.get(_CONFIG_PATH_ENV_VAR, _DEFAULT_CONFIG_PATH))
    notifier = build_outbound_notifier(config)
    decision = asyncio.run(run_outbound_worker(notifier, event_type, message))
    print(decision.value)
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI/worker launch glue, not unit tested
    if len(sys.argv) > 1 and sys.argv[1] == "notify":
        sys.exit(_cli_main(sys.argv))
    else:
        from livekit.agents import WorkerOptions, cli  # type: ignore[import-not-found]

        cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
