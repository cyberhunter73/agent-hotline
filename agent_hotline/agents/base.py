"""Agent backend adapter interface for agent-hotline.

Defines the abstraction `call_session.py` (Task 8) uses to drive an agent
"brain" over the phone without knowing which backend implementation it is
talking to. The pinned default (`claude_agent_sdk.ClaudeAgentSdkBackend`) and
any future alternative both implement `AgentBackend`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True)
class TextReply:
    """A piece (or the whole) of the agent's spoken reply, to be read aloud
    by `mouth.py`."""

    text: str


@dataclass(frozen=True)
class ToolUseRequest:
    """The agent wants to use a tool and a human must approve or deny it
    before the backend proceeds.

    `description` is a short, speakable summary of the requested tool use
    (e.g. "write a file") — not raw tool-call JSON — since it is read aloud
    verbatim by the spoken permission gate (`permission_gate.py`).
    """

    description: str


# The union of events a backend can stream out of `send_text`. Callers
# pattern-match (e.g. `isinstance(event, ToolUseRequest)`) rather than
# checking against this alias directly.
AgentEvent = Union[TextReply, ToolUseRequest]


class AgentBackend(ABC):
    """An agent "brain" that `call_session.py` can drive without knowing
    which concrete implementation it is.

    Safety contract relied on by every caller of this interface: a backend
    must never let a tool actually run before `resolve_tool_use` has been
    called for the matching `ToolUseRequest` it yielded, and must never
    resume a previous call's session state (see the project's "fresh session
    per call, never resumed" rule).
    """

    @abstractmethod
    async def start_session(self) -> None:
        """Start a brand-new session. Must never resume or see context from
        a prior call."""

    @abstractmethod
    def send_text(self, text: str) -> AsyncIterator[AgentEvent]:
        """Send caller speech (already transcribed to text) to the agent and
        stream back events: `TextReply` chunks to speak, or a
        `ToolUseRequest` that must be answered via `resolve_tool_use()`
        before the backend can proceed further.
        """

    @abstractmethod
    async def resolve_tool_use(self, approved: bool) -> None:
        """Answer the most recently yielded `ToolUseRequest` with the
        human's spoken decision."""

    @abstractmethod
    async def end_session(self) -> None:
        """Tear down the session. Must leave nothing resumable."""
