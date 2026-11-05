"""Tests for the Claude Agent SDK adapter.

Fix-wave rewrite (C1): these tests now use the REAL `claude_agent_sdk`
dataclasses (`ClaudeAgentOptions`, `AssistantMessage`, `TextBlock`,
`ResultMessage`, `PermissionResultAllow`, `PermissionResultDeny`,
`ToolPermissionContext`) directly, since `claude-agent-sdk` is a declared,
installed dependency of this project and these are plain dataclasses, not
live-session objects. Only `ClaudeSDKClient` itself (which would launch a
real CLI subprocess) is replaced with `FakeSdkClient`, a local test double.

This replaces the prior version of this file, which tested an invented API
shape (`set_can_use_tool_callback`, `send_message`, `answer_can_use_tool`,
a `permission_mode="ask"` / `always_allow_rules` constructor) that does not
exist on the real SDK — confirmed by inspecting the installed package
directly. Adapting these tests' mechanics to the real API shape is exactly
what the fix-wave brief calls for; the properties each test protects
(zero-always-allow at construction, ToolUseRequest/TextReply bridging, the
pump-task-survives-a-paused-turn regression, resolve_tool_use forwarding)
are preserved, not dropped.
"""

from __future__ import annotations

import asyncio

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
)

from agent_hotline.agents.base import TextReply, ToolUseRequest
from agent_hotline.agents.claude_agent_sdk import AdapterConfigError, ClaudeAgentSdkBackend


class FakeSdkClient:
    """Stands in for the real `claude_agent_sdk.ClaudeSDKClient`.

    Real shape (confirmed against the installed `claude-agent-sdk==0.2.178`
    package): `connect()`/`disconnect()` bookend a session; `query(prompt,
    session_id)` sends one turn; `receive_response()` is an async iterator
    of SDK message types. `can_use_tool` is not a method on the client —
    it lives on the `ClaudeAgentOptions` the client was constructed with,
    and the real SDK/CLI invokes it directly while producing a turn's
    response. This fake simulates that by calling
    `self.options.can_use_tool(...)` itself, mid-`receive_response()`,
    exactly where the real CLI would.
    """

    def __init__(
        self,
        options: ClaudeAgentOptions,
        *,
        text_chunks: list[str] | None = None,
        tool_use: tuple[str, dict] | None = None,
        chunks_after_tool_use: list[str] | None = None,
    ):
        self.options = options
        self.text_chunks = text_chunks or []
        self.tool_use = tool_use
        self.chunks_after_tool_use = chunks_after_tool_use or []
        self.connected = False
        self.last_permission_result = None
        self.queries: list[str] = []

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self.queries.append(prompt)

    async def receive_response(self):
        for chunk in self.text_chunks:
            yield AssistantMessage(content=[TextBlock(text=chunk)], model="test-model")
        if self.tool_use is not None:
            tool_name, tool_input = self.tool_use
            self.tool_use = None  # consumed: only fires once
            result = await self.options.can_use_tool(tool_name, tool_input, ToolPermissionContext())
            self.last_permission_result = result
            if isinstance(result, PermissionResultAllow):
                for chunk in self.chunks_after_tool_use:
                    yield AssistantMessage(content=[TextBlock(text=chunk)], model="test-model")
        yield ResultMessage(
            subtype="success", duration_ms=0, duration_api_ms=0, is_error=False, num_turns=1, session_id="test-session"
        )


def _factory(fake_client_holder: list):
    """Builds a client_factory(options) that records the fake client it
    creates, so a test can reach into it after start_session()."""

    def factory(options: ClaudeAgentOptions):
        client = FakeSdkClient(options)
        fake_client_holder.append(client)
        return client

    return factory


def test_constructor_pins_safe_permission_mode_and_zero_allow_rules():
    backend = ClaudeAgentSdkBackend(client_factory=lambda options: FakeSdkClient(options))
    assert backend is not None
    assert backend._options.permission_mode == "default"
    assert backend._options.allowed_tools == []
    assert backend._options.disallowed_tools == []
    assert backend._options.setting_sources == []
    assert backend._options.can_use_tool is not None


def test_client_factory_receives_the_constructed_options():
    seen: list[ClaudeAgentOptions] = []

    def factory(options: ClaudeAgentOptions):
        seen.append(options)
        return FakeSdkClient(options)

    ClaudeAgentSdkBackend(client_factory=factory)
    # Factory is lazy: not called until start_session().
    assert seen == []


@pytest.mark.asyncio
async def test_start_session_passes_options_to_factory_and_reasserts_invariant():
    seen: list[ClaudeAgentOptions] = []

    def factory(options: ClaudeAgentOptions):
        seen.append(options)
        return FakeSdkClient(options)

    backend = ClaudeAgentSdkBackend(client_factory=factory)
    await backend.start_session()
    assert len(seen) == 1
    assert seen[0].permission_mode == "default"
    assert seen[0].can_use_tool is not None


@pytest.mark.asyncio
async def test_start_session_rejects_a_client_factory_that_mutates_options_before_connect():
    """A client_factory that tampers with the options object it was handed
    must not be able to silently weaken the zero-always-allow guarantee —
    the pre-connect re-assertion must catch it."""

    def tampering_factory(options: ClaudeAgentOptions):
        options.permission_mode = "bypassPermissions"
        return FakeSdkClient(options)

    backend = ClaudeAgentSdkBackend(client_factory=tampering_factory)
    with pytest.raises(AdapterConfigError):
        await backend.start_session()


@pytest.mark.asyncio
async def test_send_text_yields_tool_use_request_from_can_use_tool_callback():
    holder: list[FakeSdkClient] = []
    backend = ClaudeAgentSdkBackend(client_factory=_factory(holder))
    # Build the fake with a tool_use configured, bypassing the no-arg
    # factory above by constructing it directly and re-pointing the
    # backend's internal client (simplest way to parameterize the fake
    # without complicating the factory signature every test needs).
    await backend.start_session()
    fake_client = holder[0]
    fake_client.tool_use = ("Write", {"path": "/tmp/x"})

    events = [e async for e in backend.send_text("do the thing")]
    assert ToolUseRequest(description="use the Write tool with input: path") in events


@pytest.mark.asyncio
async def test_resolve_tool_use_forwards_real_permission_result_to_client():
    holder: list[FakeSdkClient] = []
    backend = ClaudeAgentSdkBackend(client_factory=_factory(holder))
    await backend.start_session()
    fake_client = holder[0]
    fake_client.tool_use = ("Write", {"path": "/tmp/x"})

    [e async for e in backend.send_text("do the thing")]
    await backend.resolve_tool_use(approved=True)
    # Let the pump task actually run far enough to receive the result.
    await asyncio.sleep(0)
    assert isinstance(fake_client.last_permission_result, PermissionResultAllow)


@pytest.mark.asyncio
async def test_resolve_tool_use_denied_forwards_permission_result_deny():
    holder: list[FakeSdkClient] = []
    backend = ClaudeAgentSdkBackend(client_factory=_factory(holder))
    await backend.start_session()
    fake_client = holder[0]
    fake_client.tool_use = ("Bash", {"command": "rm -rf /"})

    [e async for e in backend.send_text("do the thing")]
    await backend.resolve_tool_use(approved=False)
    await asyncio.sleep(0)
    assert isinstance(fake_client.last_permission_result, PermissionResultDeny)


@pytest.mark.asyncio
async def test_resolve_tool_use_with_nothing_pending_raises():
    backend = ClaudeAgentSdkBackend(client_factory=lambda options: FakeSdkClient(options))
    await backend.start_session()
    with pytest.raises(RuntimeError):
        await backend.resolve_tool_use(approved=True)


@pytest.mark.asyncio
async def test_send_text_yields_text_replies_with_no_tool_use():
    def factory(options: ClaudeAgentOptions):
        return FakeSdkClient(options, text_chunks=["hello", " there"])

    backend = ClaudeAgentSdkBackend(client_factory=factory)
    await backend.start_session()
    events = [e async for e in backend.send_text("hi")]
    assert events == [TextReply(text="hello"), TextReply(text=" there")]


@pytest.mark.asyncio
async def test_start_session_connects_and_end_session_disconnects():
    holder: list[FakeSdkClient] = []
    backend = ClaudeAgentSdkBackend(client_factory=_factory(holder))
    await backend.start_session()
    assert holder[0].connected is True
    await backend.end_session()
    assert holder[0].connected is False


@pytest.mark.asyncio
async def test_pump_delivers_follow_up_text_after_tool_use_resolved(caplog):
    """Regression test for the pump-task-vs-nulled-queue bug: once a tool
    use is approved and the agent keeps talking, that follow-up reply must
    reach the caller through a later send_text() call, and the backgrounded
    pump task must not crash doing it."""
    holder: list[FakeSdkClient] = []
    backend = ClaudeAgentSdkBackend(client_factory=_factory(holder))
    await backend.start_session()
    fake_client = holder[0]
    fake_client.tool_use = ("Write", {"path": "/tmp/x"})
    fake_client.chunks_after_tool_use = ["done!"]

    first_events = [e async for e in backend.send_text("do the thing")]
    assert first_events == [ToolUseRequest(description="use the Write tool with input: path")]

    await backend.resolve_tool_use(approved=True)

    # Drains the same paused turn's pump/queue rather than starting a new
    # one; this is where the follow-up chunk the fake client produces after
    # the callback resolves must show up.
    follow_up_events = [e async for e in backend.send_text("do the thing")]
    assert TextReply(text="done!") in follow_up_events

    assert not any(record.levelname == "ERROR" for record in caplog.records)


@pytest.mark.asyncio
async def test_concurrent_permission_prompts_are_serialized_by_the_lock():
    """I6: a second tool-use permission prompt must not even reach the event
    queue until the first one's decision has been both supplied and
    returned — a caller's spoken "yes" must never be able to resolve the
    wrong prompt."""
    backend = ClaudeAgentSdkBackend(client_factory=lambda options: FakeSdkClient(options))
    await backend.start_session()
    # send_text() normally sets up self._event_queue; drive the callback
    # directly here to test the lock in isolation from the pump.
    backend._event_queue = asyncio.Queue()

    context = ToolPermissionContext()
    task1 = asyncio.create_task(backend._handle_can_use_tool("First", {}, context))
    task2 = asyncio.create_task(backend._handle_can_use_tool("Second", {}, context))

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    first_request = await backend._event_queue.get()
    assert first_request.description == "use the First tool"
    # The second call must still be blocked on the lock: nothing else is on
    # the queue yet.
    assert backend._event_queue.empty()

    await backend.resolve_tool_use(approved=True)
    result1 = await task1
    assert isinstance(result1, PermissionResultAllow)

    # Now the second call can proceed and put its own request on the queue.
    second_request = await backend._event_queue.get()
    assert second_request.description == "use the Second tool"
    await backend.resolve_tool_use(approved=False)
    result2 = await task2
    assert isinstance(result2, PermissionResultDeny)


@pytest.mark.asyncio
async def test_can_use_tool_denies_and_logs_when_no_session_active(caplog):
    """M8: a can_use_tool invocation with no active event queue must deny
    and log loudly, not silently no-op."""
    import logging

    backend = ClaudeAgentSdkBackend(client_factory=lambda options: FakeSdkClient(options))
    await backend.start_session()
    assert backend._event_queue is None

    with caplog.at_level(logging.ERROR):
        result = await backend._handle_can_use_tool("Bash", {"command": "ls"}, ToolPermissionContext())

    assert isinstance(result, PermissionResultDeny)
    assert any(record.levelname == "ERROR" for record in caplog.records)
