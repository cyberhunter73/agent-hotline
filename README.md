# agent-hotline

agent-hotline gives a local AI agent a real phone number, in both directions. A person can call the number and talk to their agent, approving or directing its actions by voice. The agent can also place an outbound call to a person's phone when something it has been pre-authorized to raise comes up, such as an error or an approval request.

The default stack is LiveKit Cloud (SIP) with Telnyx as the telephony provider, the Claude Agent SDK as the agent backend, faster-whisper for speech-to-text, and Kokoro for text-to-speech. Each non-LiveKit piece sits behind a documented interface so it can be swapped. The project is MIT licensed. It is a single-operator tool for one person's own number and own agent, not a multi-tenant service.

## Status of this build

This is a local-only build under review. It is not yet ready for a real call. A fix wave following the first whole-branch review closed several gaps below (the zero-always-allow guarantee, the outbound rate limit, rejected-caller cleanup, call-length enforcement, quiet-hours timezone, and config validation, among others — see git history). The following pieces are still described in the design but are **not implemented** in the current code:

- **Live call audio.** `agent_hotline/worker.py` defines `_RoomAudioSource` and `_RoomAudioSink` as placeholders. Their `receive_chunk` and `send_chunk` raise `NotImplementedError`, so no call can carry audio end to end yet. Wiring LiveKit audio capture and playback is a manual rollout step. **Barge-in** (interrupting the agent mid-reply) is grouped with this gap: it cannot work without live audio either.
- **Echo agent for the plumbing smoke test.** The repo does not include one. SETUP.md step 5 depends on the audio work above as well.
- **Outbound greeting.** The "This is an automated call from ... do you want to discuss it now?" opening is not in the code.
- **Queued outbound notifications.** During quiet hours the gate returns `queue_for_later`, but nothing stores or re-delivers the notification later. It is effectively dropped.
- **Per-call transcript logging.** The worker now logs a per-call record (caller number, allowed yes/no, direction, start/end time, final status) and logs call errors/cancellations instead of discarding them, but the full transcript described in the security model is not implemented.
- **Agent-initiated outbound.** The agent backend cannot call `notify()` itself. Outbound is triggered from the command line or another local process.

Fixed in the latest fix wave, no longer a gap:

- **Outbound rate limit now persists.** Call history is stored in a sqlite database (`outbound.history_path`, default `~/.agent-hotline/outbound_calls.sqlite3`), so the 24-hour cap holds across separate `notify` CLI invocations and worker restarts, with a `BEGIN IMMEDIATE` transaction serializing concurrent processes.
- **Quiet-hours timezone is honored.** `build_outbound_notifier` constructs a timezone-aware clock from `outbound.quiet_hours.timezone` and uses it for both the gate's decision and the notifier's recorded call-history timestamps.
- **Config fields are validated, not silently accepted.** `telephony_provider`, `agent_backend`, `stt.engine`, and `tts.engine` are checked against an explicit allowed set at config-load time (currently exactly one value each, since that is all this codebase implements) and `ConfigError` is raised for anything else, instead of being silently ignored.

The 106 unit tests in `tests/` run with `claude-agent-sdk` installed (verified directly against its real API shape) but without the LiveKit SDK installed, and cover the config loader, allowlist, outbound gate and persistence, permission gate, call session loop, worker cleanup, and the Claude adapter's safety checks. They do not cover live audio or real calls.

## Architecture

```
                    ┌─────────────────────────────────────┐
Caller's phone ────►│ Telephony provider (Telnyx, pinned)  │
                    │  SIP trunk ──► LiveKit Cloud project │
                    └──────────────┬────────────────────────┘
                                   │ inbound: dispatch rule → new room
                                   │ outbound: agent-hotline calls
                                   │   CreateSIPParticipant → new room
                                   ▼
                     agent-hotline worker (LiveKit Agents, Python)
                     ┌─────────────────────────────────────────┐
                     │ 1. inbound: check caller ID allowlist     │
                     │    outbound: already authorized (we      │
                     │    dialed it) — no allowlist check needed│
                     │ 2. spawn fresh agent-backend session      │
                     │ 3. bridge audio: STT → agent → TTS        │
                     │ 4. every tool call → spoken permission    │
                     │    gate, no always-allow                  │
                     │ 5. quit phrase / silence / length caps    │
                     │    end session + remove SIP participant   │
                     └─────────────────────────────────────────┘
                                   │
                          Agent backend adapter
                          (pinned default: Claude Agent SDK,
                           documented interface to swap in another)
```

Inbound and outbound share the same call-session pipeline (`call_session.py`: ears, mouth, permission gate, agent adapter). They differ only in how a call starts: waiting for a dispatched room, or placing one with `CreateSIPParticipant` against an outbound trunk.

**Where this build differs from the sketch above:**

- The outbound trigger is not the agent backend. It is `python -m agent_hotline.worker notify <event_type> <message>`, or `run_outbound_worker()` called from another local process. The agent has no `notify()` tool in this build.
- The outbound trunk ID comes from the environment variable `LIVEKIT_SIP_OUTBOUND_TRUNK_ID`, not from `config.yaml`. The worker refuses to start an outbound call without it.
- The worker's `entrypoint` handles inbound jobs only. An outbound room will reach the worker only if LiveKit dispatches an agent job into it, and this build does not configure that. This is unverified.

## Components

| Component | Role | Pinned default | Swappable? |
|---|---|---|---|
| Telephony | Issues the number, terminates PSTN | Telnyx | Documented seam, not built day one. `telephony_provider` is validated against an allowed set (currently only `"telnyx"`) at config load time; any other value is rejected, not silently ignored. |
| Call routing | SIP trunk, dispatch rule (in), outbound trunk + API (out) | LiveKit Cloud | No. LiveKit is the integration point itself. |
| Worker | Joins/places calls, drives the session | `livekit-agents` (Python) | No. |
| STT / TTS | Speech in/out | faster-whisper / Kokoro | Interface-level only. `SttEngine` (`ears.py`) and `TtsEngine` (`mouth.py`) are the seams, but `make_ears` / `make_mouth` hardcode faster-whisper and Kokoro. `stt.engine`/`tts.engine` are validated against an allowed set (currently one value each) at config load time, so an unimplemented value is rejected at load time rather than silently accepted; swapping the actual implementation still requires editing the factory and widening the allowed set. |
| Agent backend | The actual "brain" | Claude Agent SDK adapter | Interface-level only. `AgentBackend` (`agents/base.py`) is the seam, but the entrypoint hardcodes `ClaudeAgentSdkBackend`. `agent_backend` is validated against an allowed set (currently only `"claude-agent-sdk"`) at config load time. |
| Permission gate | Speaks every tool-use decision | Built-in (`permission_gate.py`) | No bypass mode, ever, on this channel. |

## Security model

These rules are safety-critical. Read them before running any part of the system.

- **Caller-ID allowlist is a coarse pre-filter, not identity proof.** PSTN caller ID is trivially spoofable, as LiveKit's own documentation notes. Do not gate anything more sensitive than "answer the call" on it alone.
- **Ending a session must explicitly remove the SIP participant from the room**, or the PSTN leg sits silently connected. `handle_inbound_job` attempts `participant.remove()` in a `finally` block on EVERY path now — including a rejected caller's brush-off, not just an allowed caller's session — with its own failure (and a `play_brush_off()` failure) isolated so neither can crash the worker or skip the removal.
- **Zero always-allow rules on this channel, ever — now bound to the real SDK client, not just checked and discarded.** `ClaudeAgentSdkBackend` constructs the real `claude_agent_sdk.ClaudeAgentOptions` itself: `permission_mode="default"` (the one pinned value, confirmed by reading the installed SDK's own source, that never lets a mode or a filesystem settings file auto-decide a tool call instead of a human — `"bypassPermissions"` skips the permission callback entirely, `"acceptEdits"` auto-accepts file edits, `"auto"` lets a model classifier decide, and `"dontAsk"` denies anything not pre-approved, so `"default"` is the only safe choice of the six), `can_use_tool` set to the adapter's own callback, `allowed_tools=[]`, `disallowed_tools=[]`, and `setting_sources=[]` (no filesystem `settings.json` allow-rules loaded from user/project/local). **What is NOT yet confirmed:** the installed SDK's own docstring for `can_use_tool` states it is invoked for a tool call the CLI's permission rules evaluate to `"ask"` — not necessarily for every tool call outright. Whether the real CLI's default rules already permit some read-only tools (e.g. a file read) without ever reaching this callback has not been verified against a live session (no CLI binary is available in this build/test environment) — see the module's docstring for the full analysis and SETUP.md's rollout checklist, which includes a dedicated check for exactly this before you rely on it. The adapter re-asserts this invariant on the options object the returned client actually holds (checked by identity, not just against the adapter's own copy) immediately before connecting, so a `client_factory` that mutated, discarded, or ignored what it was handed cannot silently weaken it. `worker.py`'s production `client_factory` now builds a real `ClaudeSDKClient(options=options)` with these options, instead of `ClaudeSDKClient()` with none. A caller's spoken decision is also now bound to the one in-flight tool-use prompt via an `asyncio.Lock` held for the whole decision round-trip, so two prompts can never be outstanding at once.
- **Silence never approves.** A timeout, silence, or a non-speech transcript on a permission prompt resolves to deny. Only an exact "yes" approves — including rejecting a rising-intonation "yes...?" as unclear rather than consent. Anything else that is not clearly a yes or no reprompts the caller, so a caller thinking out loud is not scored as a denial.
- **Fresh session per call, never resumed.** No call sees context from a previous call, and a call session cannot leak into a long-running desktop session's memory.
- **Per-call logging, separate from other channels.** The worker logs a per-call record (caller number, allowed yes/no, direction, start/end time, final status) and logs call errors/cancellations instead of discarding them. The full transcript described in the original design is **still not implemented** (see Status above) — do not rely on call logs for transcript content.
- **Quit phrase and hard timeouts.** A quit phrase, a silence timeout (measured from the last real speech, not reset by a single stray noise chunk), or the total call-length cap — now enforced as a true hard cap via `asyncio.timeout` around the whole call body, not just checked between reads/turns — ends the session cleanly, so a dropped call, a stuck line, or a hang inside a single turn can't leave a session running indefinitely.
- **Outbound is the project's core anti-spam control.** Outbound is off by default. Only event types pre-listed in `config.yaml` can ever dial. A per-24-hour cap, now persisted to a sqlite database so it holds across separate CLI invocations and worker restarts, and quiet hours evaluated against the configured timezone (not the host machine's local clock) apply on top. These three controls are the safety feature of the project, not an optional extra.

## Quickstart

Do not start here for the real steps. Follow [SETUP.md](SETUP.md) in order. It covers the Telnyx number, the LiveKit project and trunks, installation, configuration, and the test checklists. Stop at the first unmet prerequisite; do not skip the plumbing test.

## Layout

```
agent_hotline/
  worker.py            LiveKit Agents entrypoint; `notify` CLI for outbound
  inbound.py           caller-ID allowlist gate
  outbound.py          OutboundNotifier.notify(): the only path that places a call
  outbound_gate.py     event-type allowlist, rate limit, quiet hours (pure function)
  call_session.py      shared session loop used by both directions
  ears.py              STT interface and faster-whisper engine
  mouth.py             TTS interface and Kokoro engine
  permission_gate.py   spoken yes/no/unclear parsing for tool-use prompts
  config.py            YAML loader; fail-closed defaults
  agents/base.py       AgentBackend interface
  agents/claude_agent_sdk.py   pinned default adapter
tests/                 unit tests (no live SDKs needed)
config.example.yaml    annotated template; copy to config.yaml
```

## License

MIT. See [LICENSE](LICENSE).
