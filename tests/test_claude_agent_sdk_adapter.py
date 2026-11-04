"""Tests for the Claude Agent SDK adapter.

The real `claude_agent_sdk` package is never imported here (and must never be
imported at module level by the adapter itself — only `client_factory()`,
called lazily, would ever touch it in production). These tests run entirely
against `FakeSdkClient`, a local test double shaped to a documented
assumption about the real SDK's `can_use_tool` permission callback (see the
docstring in `agent_hotline/agents/claude_agent_sdk.py`).
"""

from __future__ import annotations

import asyncio
from typing import Callable

import pytest

from agent_hotline.agents.base import TextReply, ToolUseRequest
from agent_hotline.agents.claude_agent_sdk import AdapterConfigError, ClaudeAgentSdkBackend


class FakeSdkClient:
    """Stands in for a real Claude Agent SDK client.

    Assumed shape (see claude_agent_sdk.py's module docstring for the full
    rationale): `connect()`/`disconnect()` bookend a session;
    `set_can_use_tool_callback()` registers the adapter's handler for the
    SDK's `can_use_tool` permission hook; `send_message()` drives one
    conversational turn, yielding text chunks and, if configured with a
    `tool_use_description`, invoking the registered callback mid-turn exactly
    once; `answer_can_use_tool()` is the side channel the adapter uses to
    push the human's decision back to the client.
    """

    def __init__(
        self,
        tool_use_description: str | None = None,
        text_chunks: list[str] | None = None,
        chunks_after_tool_use: list[str] | None = None,
    ):
        self.tool_use_description = tool_use_description
        self.text_chunks = text_chunks or []
        # Chunks the agent "produces" after a tool-use permission decision
        # comes back — i.e. the normal case where the conversation continues
        # once a tool use is approved. This is what exposed the pump-task
        # bug: send_text() used to null out the adapter's event queue as
        # soon as it returned early on the ToolUseRequest, so the
        # backgrounded pump crashed the instant it tried to forward one of
        # these.
        self.chunks_after_tool_use = chunks_after_tool_use or []
        self.connected = False
        self.last_tool_decision: bool | None = None
        self._can_use_tool_callback: Callable[[str], "asyncio.Future[bool]"] | None = None

    def set_can_use_tool_callback(self, callback) -> None:
        self._can_use_tool_callback = callback

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def send_message(self, text: str):
        for chunk in self.text_chunks:
            yield chunk
        if self.tool_use_description is not None:
            assert self._can_use_tool_callback is not None, "callback was never registered"
            await self._can_use_tool_callback(self.tool_use_description)
            for chunk in self.chunks_after_tool_use:
                yield chunk

    async def answer_can_use_tool(self, approved: bool) -> None:
        self.last_tool_decision = approved


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


@pytest.mark.asyncio
async def test_send_text_yields_text_replies_with_no_tool_use():
    fake_client = FakeSdkClient(text_chunks=["hello", " there"])
    backend = ClaudeAgentSdkBackend(client_factory=lambda: fake_client, permission_mode="ask", always_allow_rules=[])
    await backend.start_session()
    events = [e async for e in backend.send_text("hi")]
    assert events == [TextReply(text="hello"), TextReply(text=" there")]


@pytest.mark.asyncio
async def test_start_session_connects_and_end_session_disconnects():
    fake_client = FakeSdkClient()
    backend = ClaudeAgentSdkBackend(client_factory=lambda: fake_client, permission_mode="ask", always_allow_rules=[])
    await backend.start_session()
    assert fake_client.connected is True
    await backend.end_session()
    assert fake_client.connected is False


@pytest.mark.asyncio
async def test_pump_delivers_follow_up_text_after_tool_use_resolved(caplog):
    """Regression test for the pump-task-vs-nulled-queue bug: once a tool
    use is approved and the agent keeps talking, that follow-up reply must
    reach the caller through a later send_text() call, and the backgrounded
    pump task must not crash doing it (previously: AttributeError on
    `None.put()`, logged by asyncio as an unhandled task exception rather
    than raised anywhere a test would see it directly).
    """
    fake_client = FakeSdkClient(tool_use_description="write a file", chunks_after_tool_use=["done!"])
    backend = ClaudeAgentSdkBackend(client_factory=lambda: fake_client, permission_mode="ask", always_allow_rules=[])
    await backend.start_session()

    first_events = [e async for e in backend.send_text("do the thing")]
    assert first_events == [ToolUseRequest(description="write a file")]

    await backend.resolve_tool_use(approved=True)

    # Drains the same paused turn's pump/queue rather than starting a new
    # one; this is where the follow-up chunk the fake client produces after
    # the callback resolves must show up.
    follow_up_events = [e async for e in backend.send_text("do the thing")]
    assert TextReply(text="done!") in follow_up_events

    assert not any(record.levelname == "ERROR" for record in caplog.records)
