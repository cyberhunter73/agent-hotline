# agent-hotline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a public, MIT-licensed toolkit that gives any AI agent a real phone number in both directions — inbound (a person calls in and can approve/direct the agent by voice) and outbound (the agent places a pre-authorized call out) — on a pinned LiveKit SIP + Telnyx + Claude Agent SDK stack with swappable STT/TTS/agent-backend seams.

**Architecture:** A LiveKit Agents worker process answers dispatched inbound rooms and, separately, can originate outbound rooms via `CreateSIPParticipant`. Both directions share one call-session loop (STT → agent backend → TTS, with a spoken permission gate on every tool use) built from small, independently testable modules: config, ears/mouth (speech), an agent-backend adapter, a permission gate, an inbound allowlist gate, and an outbound safety gate (event-type allowlist + rate limit + quiet hours). The worker module is the only piece that touches the real LiveKit/SIP APIs directly; everything else is tested against fakes.

**Tech Stack:** Python, `livekit-agents`, `claude-agent-sdk`, `faster-whisper`, Kokoro TTS, PyYAML, pytest.

**Spec:** `C:\Users\jrubi\Desktop\Claude-Works\PROJECTS\agent-hotline\docs\superpowers\specs\2026-11-04-agent-hotline-design.md`

## Global Constraints

- License: MIT.
- Zero always-allow rules on this channel's agent session, ever — no bypass mode, regardless of what any other channel the same agent backend serves allows.
- Fresh agent session per call, never resumed from a previous call.
- `outbound.enabled` defaults to `false`; outbound must be explicitly turned on in config.
- STT device must be set explicitly (never `"auto"`); pinned default is `"cpu"`.
- No-speech filtering uses `no_speech_prob > 0.6` or `avg_logprob < -1.0` as the "not real speech" thresholds (telekit's proven defaults).
- Rate limit and quiet-hours enforcement live in the worker/gate code, never delegated to the agent backend's own judgment.
- Per-call logging (number, timestamp, transcript, allowlisted or not, inbound or outbound) stays separate from any other channel's logs.
- Pinned reference stack: LiveKit SIP (call routing), Telnyx (telephony), Claude Agent SDK (agent backend), faster-whisper (STT), Kokoro (TTS) — each non-LiveKit piece sits behind a swappable interface per the spec's Components table.

## Review Focus

- Outbound rate limit / enablement defaulting open instead of closed if config omits a value (a person who forgets a field should get a silent phone line, not a spammy one) — pinned to Task 2.
- Caller-ID allowlist number-format mismatches (allowlist stores one format, real caller ID arrives in another) silently rejecting a legitimate caller or, worse, matching the wrong one — pinned to Task 8.
- `urgent: true` on an outbound event type bypassing quiet hours but being mistaken for bypassing the event-type allowlist itself — pinned to Task 6.
- A permission-gate prompt met with silence (no answer at all, not just an unclear one) defaulting to approve-by-timeout instead of deny-by-timeout — pinned to Task 4.
- A SIP-participant-removal failure during call cleanup propagating and crashing the worker process instead of being caught and logged, which would take down every other in-flight call — pinned to Task 10.

---

## Task 1: Project scaffolding + config loader

**Files:**
- Create: `pyproject.toml`, `LICENSE` (MIT), `.gitignore`
- Create: `agent_hotline/__init__.py`, `agent_hotline/config.py`
- Create: `config.example.yaml`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `agent_hotline.config.Config` (dataclass) with fields `telephony_provider: str`, `agent_backend: str`, `stt: SttConfig`, `tts: TtsConfig`, `inbound: InboundConfig`, `outbound: OutboundConfig`
  - `SttConfig(engine: str, device: str)`
  - `TtsConfig(engine: str, voice: str)`
  - `InboundConfig(allowed_numbers: list[str], quit_phrases: list[str], silence_timeout_s: int, max_call_length_s: int)`
  - `OutboundConfig(enabled: bool, destination_number: str, max_calls_per_24h: int, quiet_hours: QuietHours, event_types: dict[str, EventTypeConfig])`
  - `QuietHours(start: str, end: str, timezone: str)`
  - `EventTypeConfig(urgent: bool)`
- Produces: `agent_hotline.config.load_config(path: str) -> Config`
- Produces: `agent_hotline.config.ConfigError(Exception)`

- [ ] **Step 1: Write the failing tests**

```python
def test_load_config_valid_example():
    config = load_config("config.example.yaml")
    assert config.inbound.allowed_numbers == ["+15551234567"]
    assert config.outbound.enabled is False
    assert config.stt.device == "cpu"

def test_load_config_missing_allowed_numbers_raises():
    # a config.yaml-shaped dict missing inbound.allowed_numbers
    with pytest.raises(ConfigError):
        load_config_from_dict({"inbound": {}, "outbound": {}})

def test_load_config_outbound_enabled_defaults_false_when_absent():
    config = load_config_from_dict({"inbound": {"allowed_numbers": ["+1"]}, "outbound": {}})
    assert config.outbound.enabled is False

def test_load_config_max_calls_per_24h_defaults_to_one_when_absent():
    config = load_config_from_dict({"inbound": {"allowed_numbers": ["+1"]}, "outbound": {"enabled": True}})
    assert config.outbound.max_calls_per_24h == 1

def test_load_config_stt_device_auto_rejected():
    with pytest.raises(ConfigError):
        load_config_from_dict({"inbound": {"allowed_numbers": ["+1"]}, "stt": {"device": "auto"}})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config.py -v`
Expected: FAIL (module/functions not defined)

- [ ] **Step 3: Implement `agent_hotline/config.py`**

Dataclasses and `load_config`/`load_config_from_dict` as above. Defaults when a key is absent: `outbound.enabled = False`, `outbound.max_calls_per_24h = 1`, `stt.device` must be present and must not equal `"auto"` (raise `ConfigError` if it is or if absent — no silent fallback). `load_config` reads YAML from `path` and delegates to `load_config_from_dict`.

- [ ] **Step 4: Write `config.example.yaml`**

Cover every field above with the values from the spec's example (allowlist of one test number, `outbound.enabled: false`, `max_calls_per_24h: 3`, quiet hours `22:00`–`07:00`, two event types `approval-needed` (not urgent) and `critical-error` (urgent)).

- [ ] **Step 5: Write `pyproject.toml`, `LICENSE`, `.gitignore`**

`pyproject.toml` declares the package and pinned dependencies: `livekit-agents`, `claude-agent-sdk`, `faster-whisper`, a Kokoro TTS package, `pyyaml`, `pytest` (dev). `LICENSE` is the standard MIT text with the copyright holder's name and 2026. `.gitignore` excludes `__pycache__/`, `.venv/`, `*.log`, local secrets files.

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml LICENSE .gitignore agent_hotline/__init__.py agent_hotline/config.py config.example.yaml tests/test_config.py
git commit -m "feat: project scaffolding and config loader"
```

---

## Task 2: Speech interfaces — ears (STT) and mouth (TTS)

**Files:**
- Create: `agent_hotline/ears.py`, `agent_hotline/mouth.py`
- Test: `tests/test_ears.py`, `tests/test_mouth.py`

**Interfaces:**
- Consumes: `agent_hotline.config.SttConfig`, `TtsConfig` (Task 1)
- Produces: `agent_hotline.ears.TranscriptResult(text: str, no_speech_prob: float, avg_logprob: float)` with property `is_likely_speech -> bool` (False when `no_speech_prob > 0.6` or `avg_logprob < -1.0`)
- Produces: `agent_hotline.ears.Ears` — constructor takes an injected `engine` object exposing `transcribe(audio: bytes) -> TranscriptResult`; method `async def listen(self, audio: bytes) -> TranscriptResult` runs `engine.transcribe` via `loop.run_in_executor`
- Produces: `agent_hotline.ears.make_ears(config: SttConfig) -> Ears` (constructs the real faster-whisper engine; only this function imports `faster_whisper`)
- Produces: `agent_hotline.mouth.Mouth` — constructor takes an injected `engine` object exposing `synthesize(text: str) -> bytes`; method `async def speak(self, text: str) -> bytes` runs `engine.synthesize` via `loop.run_in_executor`
- Produces: `agent_hotline.mouth.make_mouth(config: TtsConfig) -> Mouth` (constructs the real Kokoro engine; only this function imports the Kokoro package)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ears.py
def test_transcript_not_likely_speech_on_high_no_speech_prob():
    result = TranscriptResult(text="Thanks for watching!", no_speech_prob=0.95, avg_logprob=-0.2)
    assert result.is_likely_speech is False

def test_transcript_not_likely_speech_on_low_avg_logprob():
    result = TranscriptResult(text="You", no_speech_prob=0.1, avg_logprob=-1.5)
    assert result.is_likely_speech is False

def test_transcript_likely_speech_when_both_within_bounds():
    result = TranscriptResult(text="yes", no_speech_prob=0.05, avg_logprob=-0.3)
    assert result.is_likely_speech is True

@pytest.mark.asyncio
async def test_ears_listen_delegates_to_engine_off_loop():
    fake_engine = FakeSttEngine(returns=TranscriptResult("yes", 0.05, -0.3))
    ears = Ears(engine=fake_engine)
    result = await ears.listen(b"audio-bytes")
    assert result.text == "yes"
    assert fake_engine.received_audio == b"audio-bytes"
```

```python
# tests/test_mouth.py
@pytest.mark.asyncio
async def test_mouth_speak_delegates_to_engine_off_loop():
    fake_engine = FakeTtsEngine(returns=b"wav-bytes")
    mouth = Mouth(engine=fake_engine)
    audio = await mouth.speak("hello")
    assert audio == b"wav-bytes"
    assert fake_engine.received_text == "hello"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ears.py tests/test_mouth.py -v`
Expected: FAIL (module/classes not defined)

- [ ] **Step 3: Implement `agent_hotline/ears.py` and `agent_hotline/mouth.py`**

`TranscriptResult.is_likely_speech` applies the two thresholds above. `Ears.listen`/`Mouth.speak` call the injected engine's method via `asyncio.get_running_loop().run_in_executor(None, ...)` — this is the fix for lessons #2 and #6 (STT/TTS must never block the event loop). `make_ears`/`make_mouth` build the real engines lazily inside the function body so importing `agent_hotline.ears`/`mouth` doesn't require the heavy ML packages to be installed for unit tests that only exercise the fake-engine path.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ears.py tests/test_mouth.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent_hotline/ears.py agent_hotline/mouth.py tests/test_ears.py tests/test_mouth.py
git commit -m "feat: speech interfaces with off-loop execution and no-speech filtering"
```

---

## Task 3: Permission gate

**Files:**
- Create: `agent_hotline/permission_gate.py`
- Test: `tests/test_permission_gate.py`

**Interfaces:**
- Consumes: `agent_hotline.ears.TranscriptResult` (Task 2)
- Produces: `agent_hotline.permission_gate.PermissionAnswer` enum: `APPROVE`, `DENY`, `UNCLEAR`
- Produces: `agent_hotline.permission_gate.evaluate_answer(transcript: TranscriptResult) -> PermissionAnswer`
  - `DENY` on no speech at all (`is_likely_speech is False`) or on timeout (represented by `transcript is None`)
  - `APPROVE` only on an exact case-insensitive "yes" (stripped of whitespace/punctuation)
  - `DENY` on an exact deny keyword ("no", "deny", "nope", "stop")
  - `UNCLEAR` on anything else (a caller talking/clarifying is not a denial)

- [ ] **Step 1: Write the failing tests**

```python
def test_exact_yes_approves():
    assert evaluate_answer(TranscriptResult("yes", 0.05, -0.3)) == PermissionAnswer.APPROVE
    assert evaluate_answer(TranscriptResult("Yes.", 0.05, -0.3)) == PermissionAnswer.APPROVE

def test_deny_keywords_deny():
    for word in ["no", "deny", "nope", "stop"]:
        assert evaluate_answer(TranscriptResult(word, 0.05, -0.3)) == PermissionAnswer.DENY

def test_clarifying_sentence_is_unclear_not_deny():
    assert evaluate_answer(TranscriptResult("what do you mean by that", 0.05, -0.3)) == PermissionAnswer.UNCLEAR

def test_non_speech_transcript_denies():
    assert evaluate_answer(TranscriptResult("Thanks for watching!", 0.95, -0.2)) == PermissionAnswer.DENY

def test_timeout_with_no_transcript_denies():
    assert evaluate_answer(None) == PermissionAnswer.DENY
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_permission_gate.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `agent_hotline/permission_gate.py`**

`evaluate_answer` per the rules above. This is lesson #4 (forgiving parsing — UNCLEAR, not DENY, for clarifying speech) combined with the Review Focus item on silence-at-timeout defaulting to DENY, not approve.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_permission_gate.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent_hotline/permission_gate.py tests/test_permission_gate.py
git commit -m "feat: spoken permission gate with forgiving-but-safe answer parsing"
```

---

## Task 4: Agent backend adapter interface + Claude Agent SDK adapter

**Files:**
- Create: `agent_hotline/agents/__init__.py`, `agent_hotline/agents/base.py`, `agent_hotline/agents/claude_agent_sdk.py`
- Test: `tests/test_claude_agent_sdk_adapter.py`

**Interfaces:**
- Produces: `agent_hotline.agents.base.AgentEvent` — union type: `TextReply(text: str)` or `ToolUseRequest(description: str)`
- Produces: `agent_hotline.agents.base.AgentBackend` (ABC):
  - `async def start_session(self) -> None`
  - `async def send_text(self, text: str) -> AsyncIterator[AgentEvent]`
  - `async def resolve_tool_use(self, approved: bool) -> None`
  - `async def end_session(self) -> None`
- Produces: `agent_hotline.agents.claude_agent_sdk.AdapterConfigError(Exception)`
- Produces: `agent_hotline.agents.claude_agent_sdk.ClaudeAgentSdkBackend(AgentBackend)` — constructor takes `(client_factory: Callable[[], Any], permission_mode: str, always_allow_rules: list)`; raises `AdapterConfigError` at construction if `permission_mode != "ask"` or `always_allow_rules` is non-empty

- [ ] **Step 1: Write the failing tests**

```python
def test_rejects_non_ask_permission_mode():
    with pytest.raises(AdapterConfigError):
        ClaudeAgentSdkBackend(client_factory=lambda: FakeSdkClient(), permission_mode="bypassPermissions", always_allow_rules=[])

def test_rejects_any_always_allow_rule():
    with pytest.raises(AdapterConfigError):
        ClaudeAgentSdkBackend(client_factory=lambda: FakeSdkClient(), permission_mode="ask", always_allow_rules=["Bash(ls)"])

def test_accepts_ask_mode_no_allow_rules():
    backend = ClaudeAgentSdkBackend(client_factory=lambda: FakeSdkClient(), permission_mode="ask", always_allow_rules=[])
    assert backend is not None

@pytest.mark.asyncio
async def test_send_text_yields_tool_use_request_from_can_use_tool_callback():
    fake_client = FakeSdkClient(tool_use_description="write a file")
    backend = ClaudeAgentSdkBackend(client_factory=lambda: fake_client, permission_mode="ask", always_allow_rules=[])
    await backend.start_session()
    events = [e async for e in backend.send_text("do the thing")]
    assert ToolUseRequest(description="write a file") in events

@pytest.mark.asyncio
async def test_resolve_tool_use_forwards_decision_to_client():
    fake_client = FakeSdkClient(tool_use_description="write a file")
    backend = ClaudeAgentSdkBackend(client_factory=lambda: fake_client, permission_mode="ask", always_allow_rules=[])
    await backend.start_session()
    [e async for e in backend.send_text("do the thing")]
    await backend.resolve_tool_use(approved=True)
    assert fake_client.last_tool_decision is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_claude_agent_sdk_adapter.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `agent_hotline/agents/base.py` and `agent_hotline/agents/claude_agent_sdk.py`**

`base.py` defines `AgentEvent`, `TextReply`, `ToolUseRequest`, and the `AgentBackend` ABC. `claude_agent_sdk.py`'s constructor validates `permission_mode`/`always_allow_rules` before anything else (the Global Constraint "zero always-allow rules, ever" is enforced here, not just documented). The real client is built lazily via `client_factory()` so tests never import the real SDK. The adapter's `can_use_tool` callback (registered with the underlying client) converts a tool-use request into a `ToolUseRequest` event yielded from `send_text`, and `resolve_tool_use` forwards the boolean decision back to whatever mechanism the real/fake client exposes for answering that callback.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_claude_agent_sdk_adapter.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent_hotline/agents/ tests/test_claude_agent_sdk_adapter.py
git commit -m "feat: agent backend adapter interface and Claude Agent SDK default adapter"
```

---

## Task 5: Inbound allowlist gate

**Files:**
- Create: `agent_hotline/inbound.py`
- Test: `tests/test_inbound.py`

**Interfaces:**
- Consumes: `agent_hotline.config.InboundConfig` (Task 1)
- Produces: `agent_hotline.inbound.normalize_number(raw: str) -> str` (strips spaces/parens/dashes, ensures a leading `+`, assumes US country code `1` when a 10-digit number has none)
- Produces: `agent_hotline.inbound.InboundGate(allowed_numbers: list[str])` with `is_allowed(caller_number: str) -> bool`

- [ ] **Step 1: Write the failing tests**

```python
def test_normalize_strips_formatting():
    assert normalize_number("(555) 123-4567") == "+15551234567"

def test_normalize_adds_country_code_when_missing():
    assert normalize_number("5551234567") == "+15551234567"

def test_normalize_leaves_already_normalized_number():
    assert normalize_number("+15551234567") == "+15551234567"

def test_allowed_number_passes_regardless_of_format():
    gate = InboundGate(allowed_numbers=["+15551234567"])
    assert gate.is_allowed("(555) 123-4567") is True

def test_unlisted_number_rejected():
    gate = InboundGate(allowed_numbers=["+15551234567"])
    assert gate.is_allowed("+15559999999") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_inbound.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `agent_hotline/inbound.py`**

`normalize_number` and `InboundGate.is_allowed` (normalizes both the stored allowlist entries and the incoming caller number before comparing — this is the Review Focus item on format mismatches wrongly rejecting or wrongly matching a caller).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_inbound.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent_hotline/inbound.py tests/test_inbound.py
git commit -m "feat: inbound caller-ID allowlist gate with number normalization"
```

---

## Task 6: Outbound safety gate

**Files:**
- Create: `agent_hotline/outbound_gate.py`
- Test: `tests/test_outbound_gate.py`

**Interfaces:**
- Consumes: `agent_hotline.config.OutboundConfig` (Task 1)
- Produces: `agent_hotline.outbound_gate.OutboundDecision` enum: `CALL_NOW`, `QUEUE_FOR_LATER`, `REJECTED_DISABLED`, `REJECTED_UNKNOWN_EVENT_TYPE`, `REJECTED_RATE_LIMIT`
- Produces: `agent_hotline.outbound_gate.OutboundGate(config: OutboundConfig, clock: Callable[[], datetime])` with `decide(event_type: str, call_history: list[datetime]) -> OutboundDecision`
  - `config.enabled is False` → `REJECTED_DISABLED` (checked first, before event type)
  - `event_type not in config.event_types` → `REJECTED_UNKNOWN_EVENT_TYPE` (checked before quiet hours/rate limit — an `urgent` flag on a known event type never overrides this)
  - count of `call_history` entries within the last 24h (relative to `clock()`) `>= config.max_calls_per_24h` → `REJECTED_RATE_LIMIT` (checked before quiet hours; applies even to `urgent` event types)
  - now within `config.quiet_hours` window and `event_types[event_type].urgent is False` → `QUEUE_FOR_LATER`
  - otherwise → `CALL_NOW`

- [ ] **Step 1: Write the failing tests**

```python
def make_config(enabled=True, max_calls=3, event_types=None, quiet_start="22:00", quiet_end="07:00"):
    ...  # builds an OutboundConfig with the given overrides

def test_disabled_rejects_even_known_event_type():
    gate = OutboundGate(make_config(enabled=False), clock=lambda: datetime(2026, 11, 4, 12, 0))
    assert gate.decide("critical-error", []) == OutboundDecision.REJECTED_DISABLED

def test_unknown_event_type_rejected_even_if_marked_urgent_elsewhere():
    gate = OutboundGate(make_config(event_types={"critical-error": EventTypeConfig(urgent=True)}), clock=lambda: datetime(2026, 11, 4, 12, 0))
    assert gate.decide("not-a-real-type", []) == OutboundDecision.REJECTED_UNKNOWN_EVENT_TYPE

def test_rate_limit_rejects_even_urgent_event_type():
    history = [datetime(2026, 11, 4, 11, 0), datetime(2026, 11, 4, 10, 0), datetime(2026, 11, 4, 9, 0)]
    gate = OutboundGate(make_config(max_calls=3, event_types={"critical-error": EventTypeConfig(urgent=True)}), clock=lambda: datetime(2026, 11, 4, 12, 0))
    assert gate.decide("critical-error", history) == OutboundDecision.REJECTED_RATE_LIMIT

def test_calls_older_than_24h_do_not_count_against_rate_limit():
    history = [datetime(2026, 11, 3, 11, 0)]  # 25 hours before clock below
    gate = OutboundGate(make_config(max_calls=1, event_types={"approval-needed": EventTypeConfig(urgent=False)}), clock=lambda: datetime(2026, 11, 4, 12, 0))
    assert gate.decide("approval-needed", history) == OutboundDecision.CALL_NOW

def test_quiet_hours_non_urgent_queues():
    gate = OutboundGate(make_config(event_types={"approval-needed": EventTypeConfig(urgent=False)}), clock=lambda: datetime(2026, 11, 4, 23, 0))
    assert gate.decide("approval-needed", []) == OutboundDecision.QUEUE_FOR_LATER

def test_quiet_hours_urgent_calls_now():
    gate = OutboundGate(make_config(event_types={"critical-error": EventTypeConfig(urgent=True)}), clock=lambda: datetime(2026, 11, 4, 23, 0))
    assert gate.decide("critical-error", []) == OutboundDecision.CALL_NOW

def test_outside_quiet_hours_calls_now():
    gate = OutboundGate(make_config(event_types={"approval-needed": EventTypeConfig(urgent=False)}), clock=lambda: datetime(2026, 11, 4, 12, 0))
    assert gate.decide("approval-needed", []) == OutboundDecision.CALL_NOW
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_outbound_gate.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `agent_hotline/outbound_gate.py`**

`OutboundGate.decide` checks in the exact order above (disabled → unknown event type → rate limit → quiet hours → call now). The quiet-hours window check must correctly handle wrapping past midnight (e.g. `22:00`–`07:00`). The 24h rate-limit window is `[clock() - timedelta(hours=24), clock()]`, counting `call_history` entries within it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_outbound_gate.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent_hotline/outbound_gate.py tests/test_outbound_gate.py
git commit -m "feat: outbound safety gate (event-type allowlist, rate limit, quiet hours)"
```

---

## Task 7: Outbound call placement (notify trigger)

**Files:**
- Create: `agent_hotline/outbound.py`
- Test: `tests/test_outbound.py`

**Interfaces:**
- Consumes: `agent_hotline.outbound_gate.OutboundGate`, `OutboundDecision` (Task 6); `agent_hotline.config.OutboundConfig` (Task 1)
- Produces: `agent_hotline.outbound.SipClient` (Protocol): `async def create_participant(self, trunk_id: str, number: str, room_name: str) -> None`
- Produces: `agent_hotline.outbound.OutboundNotifier(gate: OutboundGate, sip_client: SipClient, config: OutboundConfig)` with:
  - `async def notify(self, event_type: str, message: str) -> OutboundDecision`
  - On `CALL_NOW`: calls `sip_client.create_participant(...)`, appends `clock()`-equivalent timestamp to internal `call_history`, returns `CALL_NOW`
  - On any other decision: does not call `sip_client`, returns that decision unchanged

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_notify_call_now_invokes_sip_client():
    fake_sip = FakeSipClient()
    notifier = OutboundNotifier(gate=AlwaysDecideGate(OutboundDecision.CALL_NOW), sip_client=fake_sip, config=make_config())
    decision = await notifier.notify("critical-error", "disk full")
    assert decision == OutboundDecision.CALL_NOW
    assert fake_sip.calls == [("trunk-id", make_config().destination_number, pytest_any_str)]

@pytest.mark.asyncio
async def test_notify_rejected_never_invokes_sip_client():
    fake_sip = FakeSipClient()
    notifier = OutboundNotifier(gate=AlwaysDecideGate(OutboundDecision.REJECTED_UNKNOWN_EVENT_TYPE), sip_client=fake_sip, config=make_config())
    decision = await notifier.notify("not-a-type", "whatever")
    assert decision == OutboundDecision.REJECTED_UNKNOWN_EVENT_TYPE
    assert fake_sip.calls == []

@pytest.mark.asyncio
async def test_notify_call_now_records_history_for_future_rate_limit_checks():
    fake_sip = FakeSipClient()
    gate = RecordingGate(OutboundDecision.CALL_NOW)
    notifier = OutboundNotifier(gate=gate, sip_client=fake_sip, config=make_config())
    await notifier.notify("critical-error", "disk full")
    assert len(gate.seen_call_history) == 0  # history passed to decide() before this call
    await notifier.notify("critical-error", "disk full again")
    assert len(gate.seen_call_history) == 1  # grew after the first successful call
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_outbound.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `agent_hotline/outbound.py`**

`SipClient` as a `typing.Protocol`. `OutboundNotifier.notify` calls `self.gate.decide(event_type, self._call_history)`, and only on `CALL_NOW` calls `self.sip_client.create_participant(trunk_id=..., number=self.config.destination_number, room_name=f"outbound-{event_type}-{timestamp}")` and appends to `self._call_history`. This is the function the agent backend (or an external monitoring script) calls — it is the only path that can place an outbound call, and it always goes through the gate first.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_outbound.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent_hotline/outbound.py tests/test_outbound.py
git commit -m "feat: outbound notify() trigger wired through the safety gate"
```

---

## Task 8: Shared call session loop

**Files:**
- Create: `agent_hotline/call_session.py`
- Test: `tests/test_call_session.py`

**Interfaces:**
- Consumes: `agent_hotline.agents.base.AgentBackend`, `TextReply`, `ToolUseRequest` (Task 4); `agent_hotline.ears.Ears`, `TranscriptResult` (Task 2); `agent_hotline.mouth.Mouth` (Task 2); `agent_hotline.permission_gate.evaluate_answer`, `PermissionAnswer` (Task 3)
- Produces: `agent_hotline.call_session.CallResult(status: str)` where `status` is one of `"completed"`, `"quit_phrase"`, `"silence_timeout"`, `"max_length"`, `"cancelled"`, `"error"`
- Produces: `agent_hotline.call_session.AudioSource` (Protocol): `async def receive_chunk(self) -> bytes | None` (returns `None` on silence-timeout-worthy silence)
- Produces: `agent_hotline.call_session.AudioSink` (Protocol): `async def send_chunk(self, audio: bytes) -> None`
- Produces: `agent_hotline.call_session.run_call_session(agent: AgentBackend, ears: Ears, mouth: Mouth, audio_source: AudioSource, audio_sink: AudioSink, quit_phrases: list[str], silence_timeout_s: int, max_call_length_s: int) -> CallResult`

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_call_session.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `agent_hotline/call_session.py`**

`run_call_session` loops: read audio from `audio_source`, transcribe with `ears`, check for a quit phrase (case-insensitive substring match against `quit_phrases`) before handing text to the agent, enforce `silence_timeout_s` (no chunk / no speech within the window → `"silence_timeout"`) and a wall-clock `max_call_length_s` cap, and for each `ToolUseRequest` event from the agent: speak the description via `mouth`, read the caller's answer via `ears`, run it through `permission_gate.evaluate_answer`, loop on `UNCLEAR` (ask again) rather than denying, and call `agent.resolve_tool_use(approved=...)` on `APPROVE`/`DENY`. Catch `asyncio.CancelledError` specifically and return `CallResult("cancelled")` rather than letting a generic `except Exception` reclassify it as `"error"` (lesson #5).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_call_session.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent_hotline/call_session.py tests/test_call_session.py
git commit -m "feat: shared inbound/outbound call session loop"
```

---

## Task 9: Worker entrypoint (LiveKit integration)

**Files:**
- Create: `agent_hotline/worker.py`
- Test: `tests/test_worker.py`

**Interfaces:**
- Consumes: everything from Tasks 1–8: `Config`, `InboundGate`, `OutboundGate`, `OutboundNotifier`, `run_call_session`, `make_ears`, `make_mouth`, `ClaudeAgentSdkBackend`
- Produces: `agent_hotline.worker.SipParticipantHandle` (Protocol): `async def remove(self) -> None`
- Produces: `agent_hotline.worker.handle_inbound_job(caller_number: str, inbound_gate: InboundGate, run_session: Callable[[], Awaitable[CallResult]], participant: SipParticipantHandle, play_brush_off: Callable[[], Awaitable[None]]) -> CallResult | None`
  - Not allowed → calls `play_brush_off()`, never calls `run_session`, returns `None`
  - Allowed → calls `run_session()`, then **always** calls `participant.remove()` in a `finally` block, catching and logging (not raising) any exception `participant.remove()` itself throws

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_disallowed_caller_gets_brush_off_no_session():
    gate = InboundGate(allowed_numbers=["+15551234567"])
    brush_off_called = []
    async def run_session(): raise AssertionError("must not be called")
    async def brush_off(): brush_off_called.append(True)
    result = await handle_inbound_job("+15559999999", gate, run_session, FakeParticipant(), brush_off)
    assert result is None
    assert brush_off_called == [True]

@pytest.mark.asyncio
async def test_allowed_caller_runs_session_and_removes_participant():
    gate = InboundGate(allowed_numbers=["+15551234567"])
    participant = FakeParticipant()
    async def run_session(): return CallResult("completed")
    result = await handle_inbound_job("+15551234567", gate, run_session, participant, lambda: None)
    assert result.status == "completed"
    assert participant.removed is True

@pytest.mark.asyncio
async def test_participant_removal_failure_is_caught_and_logged_not_raised():
    participant = FakeParticipant(raise_on_remove=True)
    async def run_session(): return CallResult("completed")
    gate = InboundGate(allowed_numbers=["+1"])
    result = await handle_inbound_job("+1", gate, run_session, participant, lambda: None)
    assert result.status == "completed"  # did not raise out to the caller

@pytest.mark.asyncio
async def test_participant_removed_even_if_session_raises():
    participant = FakeParticipant()
    async def run_session(): raise RuntimeError("boom")
    gate = InboundGate(allowed_numbers=["+1"])
    with pytest.raises(RuntimeError):
        await handle_inbound_job("+1", gate, run_session, participant, lambda: None)
    assert participant.removed is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_worker.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `agent_hotline/worker.py`**

`handle_inbound_job` as specified — the `finally` block calls `participant.remove()` wrapped in its own `try`/`except Exception` that logs and swallows (this is the Review Focus item: one call's cleanup failure must never take down the worker or any other concurrent call). Add the real LiveKit Agents job-handler entrypoint (`async def entrypoint(ctx)`, per `livekit-agents`' own worker registration pattern) that loads `Config` via `load_config`, builds `make_ears`/`make_mouth`/`ClaudeAgentSdkBackend`/`InboundGate` from it, extracts the caller number from the SIP participant on `ctx`, and calls `handle_inbound_job`. Also add `async def run_outbound_worker(notifier: OutboundNotifier) -> None` or equivalent wiring so `OutboundNotifier.notify` is reachable as the `notify()` trigger the spec calls for (expose it as a small async function or socket/CLI entrypoint — implementer's choice of transport, since the spec only fixes the gate logic, not the trigger's transport). No test for the real LiveKit `entrypoint` function itself (it requires the actual SDK and a live job context); it is covered by the manual rollout plan in `SETUP.md` (Task 11), not automated tests.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_worker.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent_hotline/worker.py tests/test_worker.py
git commit -m "feat: worker entrypoint wiring inbound gate, call session, and cleanup"
```

---

## Task 10: Documentation and packaging

**Files:**
- Create: `README.md`, `SETUP.md`
- Modify: `config.example.yaml` (if any field was added/renamed since Task 1)

**Interfaces:**
- Consumes: the finished `config.example.yaml` schema (Task 1) and the rollout/testing plan from the spec

- [ ] **Step 1: Write `README.md`**

What it is, the architecture diagram (from the spec), a quickstart pointing at `SETUP.md`, and the Components table with pinned-vs-swappable columns (from the spec).

- [ ] **Step 2: Write `SETUP.md`**

Step-by-step, matching the spec's Dependencies-to-provision and Rollout/testing-plan sections exactly: (1) get a Telnyx account and number, (2) create/configure a LiveKit Cloud project with SIP enabled, inbound trunk + dispatch rule, outbound trunk, (3) install the package and its dependencies, (4) copy and fill in `config.example.yaml`, (5) plumbing-only smoke test with a trivial echo agent, (6) real inbound test checklist (allowlist both ways, permission prompts, barge-in, quit phrase, timeouts), (7) real outbound test checklist (valid event type calls, invalid event type never calls, rate limit blocks a burst, quiet-hours queues unless urgent), (8) stay single-number before any wider use.

- [ ] **Step 3: Review `config.example.yaml` against README/SETUP for consistency**

Confirm every field documented in `README.md`/`SETUP.md` exists in `config.example.yaml` with a matching name, and vice versa.

- [ ] **Step 4: Commit**

```bash
git add README.md SETUP.md config.example.yaml
git commit -m "docs: README and step-by-step setup guide"
```

---

## Self-Review Notes

- **Spec coverage:** Architecture/components (Tasks 8–9), outbound design (Tasks 6–7), security model (Task 4's zero-always-allow check, Task 8's cancelled-vs-error handling, Task 9's participant removal), speech pipeline (Task 2), lessons 1–6 (Tasks 1, 2, 3, 8), repo structure (all tasks' file paths match the spec's tree), rollout/testing plan (Task 10's `SETUP.md`), risks (documented in `SETUP.md`/`README.md`, not code — nothing further to implement). No spec section is without an owning task.
- **Type consistency:** `TranscriptResult`, `PermissionAnswer`, `OutboundDecision`, `CallResult`, `AgentEvent`/`TextReply`/`ToolUseRequest` are each defined exactly once (Tasks 2–4, 6, 8) and every later task's tests import and use those same names — no renaming drift.
- **Review Focus coverage:** all five items each map to one task's tests (Tasks 2, 5/8, 6, 3, 9) as listed in the Review Focus section above.
- **Proportion:** this plan is longer than the spec because each task's tests are written out in full (per the step-granularity requirement) — code bodies themselves are described by signature and behavior, not transcribed, except where the spec pins an exact threshold or ordering.

---

Plan complete and saved to `docs/superpowers/plans/2026-11-04-agent-hotline-implementation.md`. Please review the plan. Which execution approach would you prefer?

- **Subagent-driven** — a fresh subagent implements each task and a fresh reviewer checks it before the next one starts, then a whole-branch review at the end. Most thorough; costs a fresh context per task and per review.
- **Native** — implemented directly in this session task-by-task, then one fresh reviewer on the most capable model checks the whole branch at the end. Cheapest and fastest; no independent review until the end.

For this plan I recommend **subagent-driven**, because several tasks (4, 6, 7, 8, 9) build directly on exact interfaces from earlier tasks (the `AgentBackend` event types, the `OutboundGate`/`OutboundNotifier` split, `CallResult`), and a security-relevant mistake here (the zero-always-allow check, the outbound rate limit, participant cleanup) is exactly the kind of thing worth a fresh reviewer's eyes before the next task builds on top of it.

Since this is build/dev work, once you've confirmed the plan and execution approach, I'll hand it to **Bob** to actually run — building and pushing to GitHub is outside this planning session.
