"""Claude Agent SDK adapter — the project's pinned default `AgentBackend`.

Safety-critical module. The project's entire security model rests on one
guarantee: zero always-allow rules, ever, on this phone channel's agent
session — no bypass mode, regardless of what any other channel (e.g. a
desktop session) using the same agent backend allows.

That matters concretely because, in the real Claude Agent SDK, `can_use_tool`
is evaluated LAST in the permission-decision order and is silently bypassed
by any always-allow rule or a permissive mode like `bypassPermissions` — a
config mistake elsewhere would not just weaken the permission gate, it would
make the gate never even get consulted, with no error and no warning, just
silent bypass. `ClaudeAgentSdkBackend.__init__` therefore validates
`permission_mode` and `always_allow_rules` BEFORE constructing or touching
anything else, and refuses to even construct the adapter if either check
fails. There is no deferred/first-tool-use version of this check.

The real SDK is never imported at module level here — only `client_factory()`,
called lazily from `start_session`, would ever touch it in production. Every
test in `tests/test_claude_agent_sdk_adapter.py` runs against a local
`FakeSdkClient` test double instead.

Documented SDK-shape assumption (the real `claude_agent_sdk` package's exact
API surface was not available to consult while writing this adapter — this is
a judgment call, flagged for review, not a confirmed fact about the real
SDK). The client object returned by `client_factory()` is assumed to expose:

- `async def connect(self) -> None` — open/start the underlying SDK session.
- `def set_can_use_tool_callback(self, callback) -> None` — register a single
  callback the client/SDK invokes mid-turn, as
  `await callback(description: str) -> bool`, whenever the agent wants to
  use a tool and needs a permission decision. This mirrors the real SDK's
  documented `can_use_tool` hook, simplified to a single human-readable
  description string in and a bool decision out (rather than the real SDK's
  richer `(tool_name, input, context) -> PermissionResultAllow |
  PermissionResultDeny` shape), since the hotline only ever needs to speak
  the description aloud and relay a yes/no answer.
- `async def send_message(self, text: str) -> AsyncIterator[str]` — drive one
  conversational turn, yielding text chunks; if a tool use comes up mid-turn,
  it calls the registered `can_use_tool` callback and (like the real SDK)
  awaits the callback's return value before it can continue.
- `async def answer_can_use_tool(self, approved: bool) -> None` — a second,
  explicit channel the adapter uses to push the human's decision back to the
  client/SDK from `resolve_tool_use`, in addition to resolving whatever the
  `can_use_tool` callback itself is internally still awaiting.
- `async def disconnect(self) -> None` — close the session.

`send_text` surfaces a `ToolUseRequest` as soon as the `can_use_tool`
callback fires and then stops (the turn is paused on a pending permission
decision at that point); it does not block waiting for that decision inside
the same generator, since the human's answer arrives later via a separate
call to `resolve_tool_use`, not by continuing to iterate the same `send_text`
call.

The background task driving `client.send_message()` (the "pump") and the
queue it feeds are therefore kept as session-lifetime instance state, not
local to one `send_text()` call: a paused turn's pump keeps running after
`send_text()` returns early, and a later `send_text()` call (once the human
has answered via `resolve_tool_use()`) reattaches to that same pump/queue to
deliver whatever the agent produces next, instead of starting a brand-new
turn on top of one that is still in flight.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any, Callable

from agent_hotline.agents.base import AgentBackend, AgentEvent, TextReply, ToolUseRequest

# Sentinel marking "this turn's pump is finished" on the event queue. Needs to
# be a single stable object (not one created fresh per send_text() call),
# since a pump started by one send_text() call is routinely drained by a
# later one once a paused tool-use turn is resolved and resumes.
_PUMP_DONE = object()


class AdapterConfigError(Exception):
    """Raised when the adapter is configured in a way that could weaken or
    bypass the spoken permission gate.

    Always raised at construction time, before any session exists — never
    deferred to first tool-use.
    """


class ClaudeAgentSdkBackend(AgentBackend):
    """Pinned default `AgentBackend`, backed by the Claude Agent SDK.

    `client_factory` is called lazily (in `start_session`), so constructing
    this adapter — and therefore its config validation — never requires the
    real SDK to be importable.
    """

    def __init__(
        self,
        client_factory: Callable[[], Any],
        permission_mode: str,
        always_allow_rules: list,
    ) -> None:
        # Both checks are independent and unconditional: each is evaluated
        # on its own, and neither one's outcome can suppress or skip the
        # other. This pair of checks is the entire enforcement of the
        # project's "zero always-allow rules, ever" guarantee — nothing else
        # in this class, or in any caller, is allowed to be the backstop.
        if permission_mode != "ask":
            raise AdapterConfigError(
                f"permission_mode must be 'ask' on this channel, got {permission_mode!r}. "
                "Any other mode (e.g. 'bypassPermissions', 'acceptEdits') lets the SDK "
                "skip can_use_tool entirely, with no error and no warning."
            )
        if always_allow_rules:
            raise AdapterConfigError(
                f"always_allow_rules must be empty on this channel, got {always_allow_rules!r}. "
                "Any always-allow rule makes the SDK bypass can_use_tool for matching tool "
                "calls, silently and without error."
            )

        self._client_factory = client_factory
        self._permission_mode = permission_mode
        self._always_allow_rules = list(always_allow_rules)
        self._client: Any | None = None
        self._event_queue: asyncio.Queue[Any] | None = None
        self._pending_decision: asyncio.Future[bool] | None = None
        self._pump_task: asyncio.Task[None] | None = None

    async def start_session(self) -> None:
        self._client = self._client_factory()
        self._client.set_can_use_tool_callback(self._handle_can_use_tool)
        await self._client.connect()

    async def _handle_can_use_tool(self, description: str) -> bool:
        """Registered with the client as its `can_use_tool` callback.

        Surfaces the request as a `ToolUseRequest` event for `send_text` to
        yield, then (like the real SDK's permission callback) blocks until
        `resolve_tool_use()` supplies a decision.
        """
        loop = asyncio.get_running_loop()
        self._pending_decision = loop.create_future()
        # self._event_queue is always set by send_text() before
        # client.send_message() (and therefore this callback) can ever run,
        # so this should never actually be None. The guard is defensive
        # only, in case a misbehaving client invoked the callback outside of
        # a send_text() call.
        if self._event_queue is not None:
            await self._event_queue.put(ToolUseRequest(description=description))
        return await self._pending_decision

    async def send_text(self, text: str) -> AsyncIterator[AgentEvent]:
        if self._client is None:
            raise RuntimeError("start_session() must be called before send_text()")

        if self._pump_task is None or self._pump_task.done():
            if self._pump_task is not None:
                # Surface (rather than silently swallow) any exception from
                # a previous turn's pump task before replacing it.
                self._pump_task.result()

            # `queue` is a local the pump closes over directly, instead of
            # reading `self._event_queue` back out on every put(). That is
            # the fix for the bug this replaced: send_text() used to null
            # out self._event_queue as soon as it returned early on a
            # ToolUseRequest, but the backgrounded pump task (deliberately
            # left running past that point, see the module docstring) would
            # then crash with AttributeError the moment it next tried to
            # queue.put() — which, for any client that keeps producing
            # output after a tool-use permission is granted, is immediately,
            # not just in some edge case. Closing over a stable local
            # reference means pump() is correct regardless of whatever
            # self._event_queue is reassigned to later.
            queue: asyncio.Queue[Any] = asyncio.Queue()
            self._event_queue = queue

            async def pump() -> None:
                try:
                    async for chunk in self._client.send_message(text):
                        await queue.put(TextReply(text=chunk))
                finally:
                    await queue.put(_PUMP_DONE)

            self._pump_task = asyncio.create_task(pump())
        else:
            # A previous send_text() call already returned early on a
            # ToolUseRequest from this same turn; its pump task is still
            # running (paused on the pending decision). Keep draining the
            # same queue instead of starting a second, concurrent turn.
            queue = self._event_queue

        while True:
            item = await queue.get()
            if item is _PUMP_DONE:
                return
            yield item
            if isinstance(item, ToolUseRequest):
                # The turn is now paused on a pending permission decision
                # (see _handle_can_use_tool). Stop here rather than block
                # this generator on that decision: resolve_tool_use()
                # answers it, the pump task (left running in the
                # background) resumes on its own once that answer arrives,
                # and the next send_text() call drains whatever it produces.
                return

    async def resolve_tool_use(self, approved: bool) -> None:
        # Forwarding to the client is harmless even if nothing is actually
        # pending (e.g. a caller bug calling this with no outstanding
        # ToolUseRequest); resolving the local future is simply skipped when
        # there isn't one.
        if self._client is not None:
            await self._client.answer_can_use_tool(approved)
        if self._pending_decision is not None and not self._pending_decision.done():
            self._pending_decision.set_result(approved)
        self._pending_decision = None

    async def end_session(self) -> None:
        if self._pending_decision is not None and not self._pending_decision.done():
            # No decision ever arrived for a pending tool use; cancel rather
            # than leave it dangling across session teardown.
            self._pending_decision.cancel()
        self._pending_decision = None
        if self._pump_task is not None and not self._pump_task.done():
            self._pump_task.cancel()
            with contextlib.suppress(BaseException):
                await self._pump_task
        self._pump_task = None
        self._event_queue = None
        if self._client is not None:
            await self._client.disconnect()
        self._client = None
