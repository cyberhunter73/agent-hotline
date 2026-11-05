# agent-hotline — Design Spec

**Date:** 2026-11-04
**Status:** Approved for planning (brainstorming stage complete)

## Purpose

A public, reusable, open-source toolkit that gives any AI agent (default: Claude Agent
SDK) a real phone number, in both directions:

- **Inbound** — a person calls the number, talks to their agent, and can approve or
  direct actions by voice.
- **Outbound** — the agent places a call to a person's phone when something it's
  pre-authorized to raise comes up (an error, something needing approval), rather than
  waiting to be asked.

This generalizes a private, already-proven system (internally called `telekit`, built
2026-11-03 for the author's own personal AI assistant) into something anyone can clone,
configure with their own phone number and agent, and run. The license is MIT.

## Background: what already exists and what's new

`telekit` (private, in `my-agent/.worktrees/telekit/`, not yet merged) already proves
the inbound half end-to-end on a real phone call: a Telnyx number bridged through
LiveKit's SIP telephony into a LiveKit room, a Python worker (LiveKit Agents framework)
that spawns a Claude Agent SDK session per call, local STT (faster-whisper) and TTS
(Kokoro) for the call audio, and a spoken permission gate before every tool use. Six
real bugs were found and fixed during live-call testing (listed under Lessons below).

**Outbound calling does not exist anywhere yet** — it was explicitly scoped out of
telekit's original spec as a separate future piece. This spec is where it gets designed
for the first time.

**This project is a fresh, generic build, not a fork.** telekit's code is entangled
with one person's assistant identity, vault paths, and reuse of a separate desktop
voice-line project (`backtalk`). Rather than surgically extracting it, `agent-hotline`
is written from scratch, config-driven and provider-agnostic where it matters, but
directly informed by telekit's proven architecture and every lesson learned building
it — so a new user doesn't have to rediscover those bugs.

## Scope

**In scope:**
- Inbound calling: dial the number, caller-ID-allowlist gate, talk to the agent, spoken
  permission gate on every tool use.
- Outbound calling: a narrow, pre-authorized `notify(event_type, message)` trigger the
  agent backend can call, which places a real call under strict limits (see Outbound
  design below).
- A pinned, working reference stack (LiveKit SIP + Telnyx + Claude Agent SDK +
  faster-whisper + Kokoro), with documented seams so each piece can be swapped.
- Full setup documentation (`SETUP.md`): step-by-step from "no phone number" to "a real
  call works."

**Explicitly out of scope (future work, not this spec):**
- A hosted/managed version of this (bring-your-own LiveKit Cloud + Telnyx account only).
- Mobile app / non-telephony channels.
- Video or camera access through LiveKit.
- Multi-tenant / multi-user support — this is a single operator's own number and own
  agent.

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

Inbound and outbound share the same call-session pipeline underneath (ears, mouth,
permission gate, agent adapter) — LiveKit's SIP integration is symmetric, so the only
real difference is how a call starts: waiting for a dispatched room (inbound) versus
actively placing one via `CreateSIPParticipant` against an outbound trunk (outbound).

## Components

| Component | Role | Pinned default | Swappable? |
|---|---|---|---|
| Telephony | Issues the number, terminates PSTN | Telnyx | Documented seam, not built day one |
| Call routing | SIP trunk, dispatch rule (in), outbound trunk + API (out) | LiveKit Cloud | No — LiveKit is the integration point itself |
| Worker | Joins/places calls, drives the session | `livekit-agents` (Python) | No |
| STT / TTS | Speech in/out | faster-whisper / Kokoro | Yes, behind a thin interface (`ears.py`/`mouth.py`) |
| Agent backend | The actual "brain" | Claude Agent SDK adapter | Yes, behind `agents/base.py` interface |
| Permission gate | Speaks every tool-use decision | Built-in | No bypass mode, ever, on this channel |

## Outbound calling design (the new piece)

A tool-wielding agent that can ring a person's phone whenever it judges something
"worth a call" is a foot-gun. Outbound is deliberately narrower than inbound:

1. **Off by default.** `outbound.enabled: false` until explicitly turned on in config.
2. **Trigger is a declared event type, not the agent's free judgment.** The agent
   backend gets a `notify(event_type: str, message: str)` call. Only `event_type`
   values the user has pre-listed in `config.yaml` (e.g. `"approval-needed"`,
   `"critical-error"`) are allowed to place a real call. Any other `event_type` is
   logged, never dialed. This keeps "is this worth interrupting someone's day" a
   decision the human made in advance, not one the agent makes live.
3. **Rate limit.** A configurable max-calls-per-rolling-24h cap, enforced by the
   worker itself (not by the agent backend, which should never be trusted to
   self-limit).
4. **Quiet hours.** A configurable do-not-call time window (local time); calls
   triggered during it queue a notification for the next allowed window instead of
   dialing immediately, unless `event_type` is marked `urgent: true` in config.
5. **Opens with the reason, not small talk.** The outbound greeting states the
   triggering event immediately: "This is an automated call from \<agent name\> —
   \<reason\> — do you want to discuss it now, or should I call back later?" A "call
   back later" response ends the call cleanly without treating it as a dropped line.
6. **Same session rules as inbound once connected**: fresh session, spoken permission
   gate on every tool use, quit phrase and timeouts.

## Security model

Carried forward from telekit's design and its own field research into LiveKit SIP +
agent-SDK pairings (no official or community example combines the two, so these are
documented as hard-won, not assumed):

- **Caller-ID allowlist is a coarse pre-filter, not identity proof.** PSTN caller ID is
  trivially spoofable per LiveKit's own documentation. `SETUP.md` says this plainly —
  don't gate anything more sensitive than "answer the call" on it alone.
- **Ending a session must explicitly remove the SIP participant from the room**, or the
  PSTN leg sits silently connected. This is a real gap in naive LiveKit Agents usage,
  not a hypothetical; the worker's call-end path handles it explicitly.
- **Zero always-allow rules on this channel's agent session**, regardless of what the
  same agent backend's other channels (e.g. a desktop session) allow. `can_use_tool` is
  evaluated last in the Claude Agent SDK's permission order and is silently bypassed by
  any always-allow rule or a permissive mode — the reference adapter's config is
  audited to guarantee zero blind spots here.
- **Fresh session per call, never resumed.** No call can see context from a previous
  call, and a call session can never leak into or pollute a long-running desktop
  session's own memory.
- **Per-call logging**, separate from any other channel's logs: number, timestamp,
  transcript, allowlisted or not, inbound or outbound.
- **Quit phrase and hard timeouts** (silence timeout, total call-length cap) end the
  session cleanly so a dropped call or stuck line can't leave a session running
  indefinitely.

## Speech pipeline

**Pinned default: local STT/TTS** (faster-whisper, Kokoro) — no per-minute speech cost
beyond the telephony/LiveKit usage itself, proven on telekit's real calls. Phone audio
is noisier than desktop mic audio; local STT/TTS showed more latency/lower accuracy
risk on calls in telekit's own testing, which is why the interface stays swappable.

**Documented, not built day one:** a cloud STT/TTS swap (e.g. Deepgram, ElevenLabs)
behind the same `ears.py`/`mouth.py` interface, for anyone who finds local quality
insufficient on their own phone audio.

## Lessons carried over from telekit (apply as defaults, not rediscovered)

1. Force the STT device explicitly (e.g. `"cpu"`) rather than `"auto"` — auto-detection
   can crash on a missing CUDA/cuBLAS DLL.
2. Run blocking STT transcription in `loop.run_in_executor`, not inline — otherwise it
   blocks the event loop and races the permission gate's own timeout.
3. Filter STT output on `no_speech_prob` — near-silent audio produces confident-sounding
   hallucinated transcripts ("Thanks for watching!") that must not be treated as real
   speech, including as permission answers.
4. Make permission-gate parsing forgiving of anything that isn't an exact "yes" — a
   caller clarifying or thinking out loud should not be scored as an outright deny.
5. Distinguish a worker-shutdown `CancelledError` from a generic call error in logs and
   call-end handling — they are different failure modes.
6. Apply the same event-loop-blocking fix to the TTS output side as the STT input side.

## Repo structure

```
agent-hotline/
  README.md               — what it is, quickstart, architecture diagram
  SETUP.md                — step-by-step: Telnyx number → LiveKit project →
                             trunks/dispatch rules → config → first test call
  config.example.yaml      — provider creds, allowlist, outbound event types,
                             rate limits, quiet hours, STT/TTS/agent backend choice
  LICENSE                  — MIT
  agent_hotline/
    worker.py              — entrypoint, LiveKit Agents job handler
    inbound.py             — dispatch-rule call handling + allowlist gate
    outbound.py            — notify() trigger, event-type gate, rate limit, quiet
                             hours, call placement
    call_session.py        — shared session loop (used by both directions)
    ears.py                — STT, swappable interface
    mouth.py                — TTS, swappable interface
    permission_gate.py     — spoken tool-use gate, no bypass
    agents/
      base.py               — agent backend adapter interface
      claude_agent_sdk.py   — pinned default adapter
  tests/
    test_allowlist.py
    test_outbound_gate.py   — event-type allowlist, rate limit, quiet hours
    test_session_lifecycle.py
```

## Rollout / testing plan

1. **Plumbing-only smoke test:** Telnyx number, LiveKit SIP trunk, dispatch rule, and a
   trivial echo agent (repeats what it hears) — proves the telephony path before any
   real agent backend is involved.
2. **Real inbound test:** swap the echo agent for the real worker, allowlist one test
   number. Verify: allowlist gate works both ways (admitted and rejected), `ask`-mode
   permission prompts work over call audio, interrupting the agent mid-reply (barge-in)
   actually cuts it off, the quit phrase ends the call and session cleanly, silence and
   max-length timeouts fire correctly.
3. **Real outbound test:** trigger a fake `"critical-error"` event — confirm it places
   a call; confirm an event type *not* on the allowlist never dials; confirm the rate
   limit blocks a burst; confirm a trigger inside configured quiet hours queues instead
   of calling (unless marked urgent).
4. **Stay single-number** (one allowlisted inbound caller, one outbound destination)
   for several days of real use before any wider rollout — same caution telekit itself
   is still under as of this writing.

## Risks

- **Worker uptime dependency.** The worker needs to run somewhere always-on (a desktop
  that stays on 24/7, or a small always-on server); a reboot or network blip takes the
  phone line down with it. `SETUP.md` documents both options, defaults to whichever the
  user already has.
- **Local STT/TTS quality on phone audio** is unproven outside telekit's own testing
  environment — see Speech pipeline above.
- **Outbound as a phone-spam vector** if the event-type gate, rate limit, or quiet
  hours are misconfigured or bypassed — these three controls are treated as the
  project's core safety feature, not an optional extra, and `SETUP.md` calls this out
  explicitly rather than burying it in a config comment.
- **Caller-ID spoofing** against the inbound allowlist — accepted as a reasonable risk
  for a personal line, documented honestly; a spoken PIN layer is noted as a future
  option that doesn't require restructuring anything above.
- **Cost:** telephony provider number + per-minute PSTN rates, plus LiveKit Cloud usage
  — both free-tier-viable at low call volume, documented in `SETUP.md` with real
  numbers once confirmed.

## Dependencies to provision before implementation

- A Telnyx account and a purchased phone number (or documented equivalent for another
  provider, if the implementer chooses not to use Telnyx).
- A LiveKit Cloud account/project with SIP enabled.
- Python `livekit-agents` SDK, `claude-agent-sdk`, `faster-whisper`, and a Kokoro TTS
  package added to the new project's dependencies.

## Project location

New standalone repository, developed at
`C:\Users\jrubi\Desktop\Claude-Works\PROJECTS\agent-hotline\` and pushed to a new public
GitHub repo named `agent-hotline` under the maintainer's own GitHub account, MIT licensed. Does
not modify `telekit`, `backtalk`, or any other existing project.
