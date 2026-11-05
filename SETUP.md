# SETUP: from no phone number to a real call

This guide takes you from nothing to a working call. Work through the steps in order. Do not skip the plumbing-only test (step 5) before the real-agent tests (steps 6 and 7).

Read [README.md](README.md) first, especially **Status of this build** and **Security model**. Several parts of the system are not finished in this build, and the test checklists below will fail where those gaps are.

Everything here is local. You run the worker on your own machine or server, using your own Telnyx and LiveKit accounts.

---

## Before you start: known gaps that affect testing

| Gap in this build | What it means for you |
|---|---|
| Live audio capture/playback not implemented (`_RoomAudioSource` / `_RoomAudioSink` raise `NotImplementedError`) | No call can carry audio yet. Steps 5 through 7 cannot pass until this is wired. |
| No echo agent included | Step 5 needs one. You write it against `AgentBackend` (see step 5). |
| Outbound rate limit is in memory only | Each `notify` CLI run starts with an empty call history, so the 24-hour cap does not hold across separate invocations or worker restarts. A burst run as separate commands will not be blocked. |
| Queued quiet-hours notifications are not stored or re-sent | A trigger inside quiet hours returns `queue_for_later` and is dropped. Nothing calls later. |
| `outbound.quiet_hours.timezone` is ignored | Quiet hours use the host machine's local clock. Set the machine's clock and timezone to match what you want. |
| Outbound greeting not implemented | An outbound call, once it connects, has no "automated call from ..." opener. |
| Per-call logging not implemented | There is no per-call transcript or direction log yet. Keep your own notes during tests. |
| `telephony_provider`, `agent_backend`, `stt.engine`, `tts.engine` are not used to select implementations | Changing them does nothing. Leave them at the example values. |

Do not enable outbound (`outbound.enabled: true`) on any real number until you have read the outbound section of the Security model and understand the rate-limit and quiet-hours gaps above.

---

## Step 1: Get a Telnyx account and phone number

Telnyx is the pinned telephony provider. You can use another provider if you connect it through LiveKit SIP, but the steps below assume Telnyx.

1. Create a Telnyx account and complete identity verification.
2. Buy a phone number that supports voice (inbound PSTN). Note the E.164 form, for example `+15551234567`. You will need it for LiveKit's inbound trunk.
3. Create a SIP connection (a credential-authenticated SIP trunk) in Telnyx and assign your number to it. Use the SIP termination/ingress details LiveKit gives you in step 2. Exact Telnyx menu labels change, so follow the current Telnyx and LiveKit SIP documentation for this part.
4. Record the SIP credentials you create. Store them in a password manager or in your own secrets file, never in `config.example.yaml` or any committed file.

**Cost.** Telnyx charges a monthly number fee plus per-minute PSTN rates. LiveKit Cloud bills SIP and agent usage. The design expects both to be viable at low volume on free or low tiers. **No real figures have been confirmed for this build yet.** Check both providers' current pricing pages before you place your first test call, and record the actual numbers here once you have them.

**Test number.** Use a phone you control as your one allowlisted caller. Use a second phone you control as your one outbound destination.

---

## Step 2: Create and configure a LiveKit Cloud project with SIP

1. Create a LiveKit Cloud account and a project.
2. Enable SIP for the project (LiveKit Cloud project settings, SIP section). Note the project's SIP URI and API credentials.
3. **Inbound trunk.** Create an inbound SIP trunk for the Telnyx number. Restrict it to your Telnyx SIP source addresses or credentials as LiveKit's SIP docs describe.
4. **Dispatch rule.** Create an inbound dispatch rule that sends each incoming call from that trunk into a new room. The worker in this build does not set an explicit `agent_name`, so dispatch depends on how your LiveKit worker registration and dispatch rule are configured. Confirm this against LiveKit's current agent dispatch documentation. If the agent does not join calls, this is the first place to check.
5. **Outbound trunk.** Create an outbound SIP trunk that points at Telnyx, with the Telnyx credentials from step 1. Copy its trunk ID. It typically begins with `ST_`.
6. Note the LiveKit server URL (`wss://...`), API key, and API secret. You will set these as environment variables in step 4.

Keep the outbound trunk ID handy. The worker reads it from an environment variable, not from `config.yaml`.

---

## Step 3: Install the package and its dependencies

**Requirements:** Python 3.11 or newer (`requires-python = ">=3.11"`). The virtual environment below is recommended. It is gitignored as `.venv/`.

From the repository root:

```bash
python -m venv .venv
# Windows (Git Bash):   source .venv/Scripts/activate
# Windows (PowerShell): .venv\Scripts\Activate.ps1
# macOS / Linux:        source .venv/bin/activate

pip install -e ".[dev]"
```

This installs the runtime dependencies pinned in `pyproject.toml`. You do not need to type these versions; pip reads them from the file. They are listed here for reference:

| Package | Pin in `pyproject.toml` | Role |
|---|---|---|
| `livekit-agents` | `~=1.8.10` | LiveKit Agents worker and SIP client |
| `claude-agent-sdk` | `~=0.2.178` | Default agent backend |
| `faster-whisper` | `~=1.2.1` | Speech-to-text |
| `kokoro` | `~=0.7.16` | Text-to-speech (PyPI distribution name is `kokoro`) |
| `pyyaml` | `~=6.0` | Config loading |
| `pytest` (dev) | `~=9.2` | Tests |
| `pytest-asyncio` (dev) | `~=1.4` | Async tests (strict mode) |

Notes:

- **faster-whisper** downloads its model on first use. The worker loads model `"small"` (hardcoded in `make_ears`; there is no config field for it). The first run needs network access.
- **Kokoro** may need system packages (for example `espeak-ng`) depending on the language. Check Kokoro's own installation notes for your platform.
- **Claude Agent SDK** authentication is handled by the SDK, following its own documentation. This repo does not configure it.

Run the unit tests to confirm the install:

```bash
python -m pytest
```

All tests should pass. They run without live LiveKit or Claude credentials. A pass here does not mean live calls work. See **Before you start** above.

---

## Step 4: Copy and fill in `config.example.yaml`

Copy the template to `config.yaml` in the repository root. `config.yaml` is gitignored, so your real numbers stay local.

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml`, then work through each section below. Every field the example contains is covered here.

### Top-level

| Field | Example | What it does | If absent |
|---|---|---|---|
| `telephony_provider` | `telnyx` | Informational. Parsed, not used to select anything. | Defaults to `telnyx`. No effect either way. |
| `agent_backend` | `claude-agent-sdk` | Informational. The worker always uses the Claude Agent SDK adapter. | Defaults to `claude-agent-sdk`. No effect either way. |

### `stt` (speech-to-text)

| Field | Example | What it does | If absent |
|---|---|---|---|
| `stt.engine` | `faster-whisper` | Informational. `make_ears` always uses faster-whisper. | Defaults to `faster-whisper`. |
| `stt.device` | `cpu` | Device passed to `WhisperModel`. Must be explicit. | If the `stt` section is present but `device` is missing, config load raises an error. If `device` is `auto`, config load raises an error (auto-detection can crash on a missing CUDA/cuBLAS DLL). If the whole `stt` section is absent, device is `None` and the worker's `make_ears` raises `ValueError` on every call. **Set `device: cpu` (or `cuda` if you have a working CUDA install).** |

### `tts` (text-to-speech)

| Field | Example | What it does | If absent |
|---|---|---|---|
| `tts.engine` | `kokoro` | Informational. `make_mouth` always uses Kokoro. | Defaults to `kokoro`. |
| `tts.voice` | `af_heart` | Kokoro voice. The first character is used as the Kokoro language code (`a` for American English). | Defaults to `af_heart`. |

### `inbound` (phone calls to your number)

| Field | Example | What it does | If absent |
|---|---|---|---|
| `inbound.allowed_numbers` | `["+15551234567"]` | Caller-ID allowlist, in E.164. Entries are normalized before comparison. A malformed entry raises an error at startup. | **Required.** Config load fails if absent. An empty list admits no caller. |
| `inbound.quit_phrases` | `["goodbye", "end call"]` | If the caller says any of these (case-insensitive substring), the call ends cleanly. | Defaults to `["goodbye"]`. |
| `inbound.silence_timeout_s` | `30` | Per-read window. During normal conversation, silence past this ends the call. During a permission prompt, a missed answer counts as a denial and the call continues. | Defaults to `30`. |
| `inbound.max_call_length_s` | `1800` | Hard cap on total call length, in seconds. | Defaults to `1800` (30 minutes). |

Use your own test phone number for `allowed_numbers`. Caller ID is spoofable, so this list is a coarse pre-filter only. See the Security model in the README.

### `outbound` (calls the agent places to you)

| Field | Example | What it does | If absent |
|---|---|---|---|
| `outbound.enabled` | `false` | Master switch. When `false`, `notify` never dials. Must be a real boolean; a string like `"false"` is a config error. | Defaults to **`false`**. Outbound is off unless you turn it on. |
| `outbound.destination_number` | `"+15557654321"` | The one number outbound calls dial. | Defaults to an empty string. **The worker does not check this before dialing.** Set it before enabling outbound. |
| `outbound.max_calls_per_24h` | `3` | Rolling 24-hour cap on placed calls. **In this build the count is held in memory per process and does not persist across CLI runs or restarts.** | Defaults to `1`. |
| `outbound.quiet_hours.start` | `"22:00"` | Start of do-not-call window, `HH:MM`. Wrap-around past midnight is supported. | Defaults to `"22:00"`. |
| `outbound.quiet_hours.end` | `"07:00"` | End of do-not-call window, `HH:MM`. | Defaults to `"07:00"`. |
| `outbound.quiet_hours.timezone` | `"America/New_York"` | **Parsed but not used in this build.** Quiet hours follow the machine's local clock. | Defaults to `"UTC"`, which is also not used. |
| `outbound.event_types` | map (below) | The only event types that can ever place a call. | No event types listed means **every event type is rejected**. Nothing dials. |
| `outbound.event_types.<name>.urgent` | `true` / `false` | `true` lets that event type bypass quiet hours. It does **not** bypass the event-type allowlist or the rate limit. | Defaults to `false`. |

The example lists two event types:

- `approval-needed`, `urgent: false`: queued during quiet hours (in this build, dropped; see gaps above).
- `critical-error`, `urgent: true`: may dial during quiet hours, subject to the rate limit.

Add only event types you have decided, in advance, are worth interrupting someone for.

### Environment variables

These are read from the environment, not from `config.yaml`. Set them in your shell or in a local, gitignored `.env` file that your shell loads. Do not commit them.

| Variable | Used by | What it does | If absent |
|---|---|---|---|
| `LIVEKIT_URL` | LiveKit SDK (worker and `notify`) | Your LiveKit server URL (`wss://...`). | The LiveKit SDK fails to connect. This build does not set a default. |
| `LIVEKIT_API_KEY` | LiveKit SDK | LiveKit API key. | Same as above. |
| `LIVEKIT_API_SECRET` | LiveKit SDK | LiveKit API secret. | Same as above. |
| `LIVEKIT_SIP_OUTBOUND_TRUNK_ID` | `notify` CLI and `build_outbound_notifier` | The outbound SIP trunk ID from step 2. | **`notify` raises `WorkerConfigError` before evaluating any event type.** This is deliberate: there is no placeholder fallback. |
| `AGENT_HOTLINE_CONFIG` | worker and `notify` | Path to the YAML config. | Defaults to `config.yaml` relative to the current working directory. Run from the repo root, or set this explicitly. |

The LiveKit variable names follow LiveKit's own SDK convention. This build was written without the LiveKit SDK in the environment, so confirm them against the installed `livekit-agents` documentation.

### Quick config check

After editing, confirm the file loads and the outbound state is what you intend:

```bash
python -c "from agent_hotline.config import load_config; c = load_config('config.yaml'); print(c.inbound.allowed_numbers, c.outbound.enabled)"
```

It should print your allowlist and `False`.

---

## Step 5: Plumbing-only smoke test with a trivial echo agent

**Purpose:** prove the telephony path (Telnyx number, SIP trunk, dispatch rule, room, worker joining) before any real agent backend is involved.

**Current status:** this step is blocked until live audio capture and playback are wired (`_RoomAudioSource` / `_RoomAudioSink` in `worker.py`). The repo does not include an echo agent.

When that work is done:

1. Write a minimal agent implementing `AgentBackend` from `agent_hotline/agents/base.py`. It should start and end a session, and for each caller turn yield a `TextReply` that repeats the transcript. It should never yield a `ToolUseRequest`. Use a stub `Ears`/`Mouth` if you want to test the telephony path separately from speech.
2. Keep `inbound.allowed_numbers` set to your test phone.
3. Start the worker in LiveKit's development mode and confirm it registers. The LiveKit CLI subcommand is provided by `livekit-agents`; check `python -m agent_hotline.worker --help` for the exact form in your installed version.

   ```bash
   python -m agent_hotline.worker dev
   ```

4. Call the Telnyx number from your allowlisted phone.

**Pass criteria:** the call connects, you hear the echo, and hanging up removes the SIP participant from the room. Confirm the last point in the LiveKit dashboard: the participant should disappear when the call ends. If it stays connected, stop and fix this before going further (see the Security model in the README).

---

## Step 6: Real inbound test checklist

**Purpose:** swap the echo agent for the real worker and allowlist one test number.

Before you begin, confirm: the real call audio path is wired, and `inbound.allowed_numbers` contains only your one test phone.

Check each of the following on real calls. Record each result.

- [ ] **Allowlist admits a listed caller.** Call from the allowlisted number. The call reaches the agent.
- [ ] **Allowlist rejects an unlisted caller.** Call from a different phone that is not on the list. The worker plays the brush-off and never starts a session. Confirm the SIP participant is removed.
- [ ] **Ask-mode permission prompts work over call audio.** Ask the agent to do something that triggers a tool use. It should speak the request and wait for an answer.
  - [ ] Answer "yes" and confirm the tool runs.
  - [ ] Answer "no" and confirm the tool does not run.
  - [ ] Stay silent and confirm no tool runs (silence is a denial).
  - [ ] Ask a clarifying question instead of answering and confirm the agent reprompts rather than denying outright.
- [ ] **Barge-in cuts the agent off.** Interrupt the agent mid-reply and confirm it stops speaking and listens to you.
- [ ] **Quit phrase ends the call and session cleanly.** Say `goodbye` (or `end call`). The call ends, the SIP participant is removed, and the session ends. Confirm the next call starts a fresh session with no memory of the previous one.
- [ ] **Silence timeout fires.** Stay silent through a normal turn and confirm the call ends after `inbound.silence_timeout_s`.
- [ ] **Max-length timeout fires.** Set `inbound.max_call_length_s` to a small value for this test, such as `60`, keep talking, and confirm the call ends at the cap. Restore the real value afterward.

If any item fails, do not proceed to step 7. Fix it, then rerun the full checklist.

---

## Step 7: Real outbound test checklist

**Purpose:** prove that outbound is gated by the event-type allowlist, rate limit, and quiet hours, and that it dials only when all three allow it.

**Before you begin:**

- Set `outbound.enabled: true` only for this test.
- Set `outbound.destination_number` to your one test phone.
- Set `LIVEKIT_SIP_OUTBOUND_TRUNK_ID` in your environment.
- Keep `outbound.event_types` to the two examples unless you need to test a specific event.
- Remember that the `notify` CLI reports a decision and places a call only when the decision is `call_now`. It does not start the inbound worker.

Trigger events with:

```bash
python -m agent_hotline.worker notify critical-error "Nightly backup failed"
```

The command prints the gate's decision (for example `queue_for_later`; see `OutboundDecision` in `agent_hotline/outbound_gate.py` for all values).

Check each of the following:

- [ ] **Valid event type places a real call.** Trigger `critical-error` outside quiet hours. The decision is `call_now`, and your test phone rings. The outbound greeting is **not implemented** in this build, so the call will not open with the automated-call message. Note that as a gap.
- [ ] **Event type not on the allowlist never dials.** Trigger a name that is not in `event_types`, such as `made-up-event`. The decision is a rejection and no call is placed. Confirm no call came in.
- [ ] **Rate limit blocks a burst.** Set `outbound.max_calls_per_24h` to `2` and trigger `critical-error` three times in quick succession. **Expected result in this build: the third call is not blocked**, because each CLI invocation starts with an empty history and the cap is not persisted. Record this as a failure. Do not treat the rate limit as working until this check passes.
- [ ] **Quiet-hours trigger does not dial.** Set `outbound.quiet_hours` to a window that includes the current local time, then trigger `approval-needed` (non-urgent). Expected: the decision is `queue_for_later` and no call is placed. **Note that in this build the notification is not sent later.** The "queued for the next allowed window" half of this check cannot pass.
- [ ] **Urgent event bypasses quiet hours.** With the same quiet window active, trigger `critical-error` (`urgent: true`). Expected: `call_now`, and the call rings, subject to the rate limit.

Restore your real `outbound` settings and set `outbound.enabled: false` when testing is finished.

---

## Step 8: Stay single-number before any wider use

Once both checklists pass, do not widen the setup yet. For **several days of real use**, keep:

- one allowlisted inbound caller (your own test phone), and
- one outbound destination (your own phone).

Watch for:

- calls that drop or hang without ending,
- participants left connected after a call,
- outbound calls you did not expect,
- silence or timeout behavior that does not match the configured values.

This is the same caution the original private system is still under. Do not add callers, destinations, or event types until several days pass without an unexplained call.

---

## Running the worker: uptime

The worker must be running for inbound calls to be answered. A reboot or network drop takes the phone line down with it. Pick one:

- **A desktop that stays on 24/7.** Simple, but it must not sleep, and it must restart the worker after a reboot.
- **A small always-on server.** More reliable, with the same restart requirement.

Use whichever you already have. Set up a process supervisor or a scheduled task so the worker restarts after a reboot. Document your choice in your own notes; this repo does not configure one.

---

## Cost and rollout reminders

- Telnyx and LiveKit Cloud both bill per use. Confirm current prices before testing.
- Outbound is the project's core spam control. Keep `outbound.enabled: false` unless you are actively testing it, and keep the event-type list short.
- A phone that places an unexpected outbound call is a bug in the gate, not a feature. Stop the worker and investigate.
