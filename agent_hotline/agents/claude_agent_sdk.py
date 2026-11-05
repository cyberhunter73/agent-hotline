"""Claude Agent SDK adapter — the project's pinned default `AgentBackend`.

Safety-critical module. The project's entire security model rests on one
guarantee: zero always-allow rules, ever, on this phone channel's agent
session — no bypass mode, regardless of what any other channel (e.g. a
desktop session) using the same agent backend allows.

Fix-wave rewrite (C1). The previous version of this module validated its own
`permission_mode`/`always_allow_rules` constructor arguments and then
discarded them: it never built a real `ClaudeAgentOptions` object, the
production `client_factory` in `worker.py` constructed `ClaudeSDKClient()`
with no options at all, and the adapter called methods
(`set_can_use_tool_callback`, `send_message`, `answer_can_use_tool`) that do
not exist on the real SDK client. None of that was caught earlier because
`claude-agent-sdk` was not installed while Task 4 was written. It has since
been installed (`claude-agent-sdk==0.2.178`) and inspected directly
(`dataclasses.fields`, `inspect.getsource`) to confirm the real shape used
below.

This adapter now OWNS construction of a real `claude_agent_sdk.ClaudeAgentOptions`
object and is the sole thing that can place a decision in front of the real
SDK's `can_use_tool` hook:

- `permission_mode="default"` — the one safe pinned value. Confirmed from the
  installed SDK's own source (`claude_agent_sdk/types.py`,
  `_get_can_use_tool_shadowed_warning`): `"bypassPermissions"` skips
  `can_use_tool` entirely (the SDK itself warns about this via
  `CanUseToolShadowedWarning`). The SDK's own docstrings additionally
  describe `"acceptEdits"` as auto-accepting file edits without consulting
  the callback, `"auto"` as letting a model classifier decide instead of a
  human, and `"dontAsk"` as denying anything not pre-approved by an allow
  rule (i.e. never routing through `can_use_tool` for a prompt at all) —
  each of those would also create a path that never reaches the human
  caller. `"plan"` mode restricts the agent to planning only and cannot
  execute tools in the first place, which is too restrictive for this
  product's "the agent can act, pending a spoken yes/no" design. `"default"`
  is the only one of the six literal values that preserves "every tool call
  is offered to `can_use_tool` and nothing else auto-decides it".
- `can_use_tool=self._handle_can_use_tool` — the sole arbiter. Every tool use
  is bridged out to this project's own pull-based `AgentBackend` interface
  (see `_handle_can_use_tool` below) rather than decided here.
- `allowed_tools=[]`, `disallowed_tools=[]` — no pre-allowed tool shadows the
  callback (confirmed: `_get_can_use_tool_shadowed_warning` only flags a
  *non-empty* `allowed_tools` entry that fully allows a tool, or
  `bypassPermissions`; an empty list triggers neither).
- `setting_sources=[]` — confirmed directly from the installed SDK's
  docstring for this field: "When `None`, all sources are loaded (matches
  CLI defaults). Pass `[]` to disable filesystem settings (SDK isolation
  mode)." `None` would load `~/.claude/settings.json` /
  `.claude/settings.json` / `.claude/settings.local.json`, any of which
  could contain an always-allow rule that — per the SDK's own comment on
  this exact code path — "can also shadow the callback but are not visible
  here" (i.e. not even caught by the shadowing warning). `[]` is therefore
  the only value that lets this module claim zero settings-based auto-allow
  reaches this channel.

**Documented residual-risk / rollout-verify item** (this project has no live
Claude CLI binary to actually `.connect()` against, in this environment or
in CI — see the project-wide note on this): the analysis above is drawn
directly from the installed SDK's dataclass fields, type literals, and
source comments, which is as far as static inspection can go. It has NOT
been exercised end-to-end against a live CLI session. Specifically
unverified: whether every built-in tool actually calls through to
`can_use_tool` under `permission_mode="default"` with no other settings
present (vs. some read-only tool category the real CLI might special-case
outside the permission system entirely). Re-verify this during the manual
rollout (SETUP.md) on the very first real tool-use call, the same way
`worker.py`'s LiveKit-shape assumptions are flagged there for rollout
verification — do not treat this module's unit tests as proof the live
integration is safe; they prove this adapter builds and re-asserts the
right `ClaudeAgentOptions`, not that the installed Claude CLI binary honors
them exactly as documented.

Construction-time and pre-connect re-assertion. `__init__` builds the pinned
`ClaudeAgentOptions` once. `start_session` calls `client_factory(options)` —
the factory now receives the constructed options, rather than being called
with no arguments — and then, immediately before `connect()`, re-asserts
that the SAME options object this adapter built still satisfies the
zero-always-allow invariant. This closes the gap a prior review flagged:
without the re-check, a `client_factory` could theoretically mutate or
discard the options it was handed (these are plain, non-frozen dataclasses)
and nothing downstream would notice. `AdapterConfigError` is raised if the
re-check ever fails; today nothing in this codebase can make that happen,
so in practice this is a self-test against future regressions, not a
check against untrusted caller input (there is no caller-supplied
`permission_mode`/`always_allow_rules` left to validate — the whole point of
this rewrite is that the adapter pins these itself and no longer exposes a
way to misconfigure them).

Tests (`tests/test_claude_agent_sdk_adapter.py`) use the REAL
`claude_agent_sdk.ClaudeAgentOptions`, `PermissionResultAllow`,
`PermissionResultDeny`, `AssistantMessage`, `TextBlock`, `ResultMessage`, and
`ToolPermissionContext` dataclasses directly (they are plain dataclasses,
not live-session objects, and `claude-agent-sdk` is a declared, installed
dependency of this project) — only the network/subprocess-backed
`ClaudeSDKClient` itself is replaced with a local `FakeSdkClient` double.

Bridging shape, preserved from the prior implementation (the part that
survives this rewrite): the real SDK's `can_use_tool` callback is awaited
directly by the SDK/CLI, not polled from the outside, so this module bridges
it to the project's own pull-based `AgentBackend.send_text()` generator the
same way as before — a session-lifetime `asyncio.Queue` plus a pending
`asyncio.Future` for the one in-flight decision, with the background "pump"
task (driving `client.query()` / `client.receive_response()`) kept as
instance state so a turn paused on a tool-use permission can be resumed by a
later `send_text()` call once `resolve_tool_use()` answers it.

I6 fix: an `asyncio.Lock` is held for the full duration of one
`_handle_can_use_tool` invocation (request -> yield `ToolUseRequest` -> wait
for `resolve_tool_use` -> return the real SDK result), so two permission
prompts can never be outstanding at once and a caller's spoken answer can
never resolve the wrong one. `resolve_tool_use` raises `RuntimeError` if
called with nothing pending, instead of silently forwarding a meaningless
decision.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    TextBlock,
)

from agent_hotline.agents.base import AgentBackend, AgentEvent, TextReply, ToolUseRequest

logger = logging.getLogger(__name__)

# The one permission_mode value that keeps can_use_tool as the sole arbiter.
# See the module docstring for why each of the other five literal values is
# unsafe for this channel.
_PINNED_PERMISSION_MODE = "default"

# Sentinel marking "this turn's pump is finished" on the event queue. Needs to
# be a single stable object (not one created fresh per send_text() call),
# since a pump started by one send_text() call is routinely drained by a
# later one once a paused tool-use turn is resolved and resumes.
_PUMP_DONE = object()


class AdapterConfigError(Exception):
    """Raised if the constructed `ClaudeAgentOptions` ever fails to satisfy
    the zero-always-allow invariant.

    There is no caller-supplied `permission_mode`/`always_allow_rules` left
    to validate: this adapter pins those values itself. This error exists as
    a construction-time and pre-connect self-test against a future
    regression (e.g. someone later adding a constructor parameter that
    weakens the pinned options), not as validation of untrusted input.
    """


def _build_pinned_options(can_use_tool: Callable[..., Any]) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        permission_mode=_PINNED_PERMISSION_MODE,
        can_use_tool=can_use_tool,
        allowed_tools=[],
        disallowed_tools=[],
        setting_sources=[],
    )


def _assert_zero_always_allow_invariant(options: ClaudeAgentOptions) -> None:
    """Re-check the options object itself, not just the arguments used to
    build it originally — see the module docstring's "Construction-time and
    pre-connect re-assertion" section."""
    problems = []
    if options.permission_mode != _PINNED_PERMISSION_MODE:
        problems.append(f"permission_mode must be {_PINNED_PERMISSION_MODE!r}, got {options.permission_mode!r}")
    if options.allowed_tools:
        problems.append(f"allowed_tools must be empty, got {options.allowed_tools!r}")
    if options.disallowed_tools:
        problems.append(f"disallowed_tools must be empty, got {options.disallowed_tools!r}")
    if options.setting_sources != []:
        problems.append(f"setting_sources must be [], got {options.setting_sources!r}")
    if options.can_use_tool is None:
        problems.append("can_use_tool must be set")
    if problems:
        raise AdapterConfigError(
            "ClaudeAgentOptions no longer satisfies the zero-always-allow invariant: " + "; ".join(problems)
        )


def _describe_tool_use(tool_name: str, tool_input: dict) -> str:
    """Build a short, speakable summary of a tool-use request.

    Read aloud verbatim by the spoken permission gate (`permission_gate.py`
    via `call_session.py`), so this must stay a plain sentence, not raw JSON.
    """
    if not tool_input:
        return f"use the {tool_name} tool"
    # Keep it short and speakable: name the input keys, not full values,
    # which may be long file contents or command strings.
    keys = ", ".join(str(k) for k in tool_input.keys())
    return f"use the {tool_name} tool with input: {keys}"


class ClaudeAgentSdkBackend(AgentBackend):
    """Pinned default `AgentBackend`, backed by the real Claude Agent SDK.

    `client_factory(options)` is called lazily (in `start_session`), so
    constructing this adapter never requires the real SDK to be able to
    launch a CLI subprocess — only to be importable (it is a declared,
    installed dependency, so `ClaudeAgentOptions` et al. are imported at
    module level here, unlike `livekit-agents`/`worker.py`'s lazy-import
    pattern for a dependency that is genuinely not always installed).
    """

    def __init__(self, client_factory: Callable[[ClaudeAgentOptions], Any]) -> None:
        self._client_factory = client_factory
        self._options = _build_pinned_options(self._handle_can_use_tool)
        # Fails loudly, at construction time, if the invariant is somehow
        # already broken (defensive; see AdapterConfigError's docstring).
        _assert_zero_always_allow_invariant(self._options)

        self._client: Any | None = None
        self._event_queue: asyncio.Queue[Any] | None = None
        self._pending_decision: asyncio.Future[bool] | None = None
        self._pump_task: asyncio.Task[None] | None = None
        # I6: serializes _handle_can_use_tool end-to-end so two tool-use
        # permission prompts can never be outstanding at the same time.
        self._permission_lock = asyncio.Lock()

    async def start_session(self) -> None:
        client = self._client_factory(self._options)
        # Re-assert the invariant on the SAME options object just handed to
        # client_factory, immediately before connecting — closes the gap
        # where a client_factory could mutate/ignore what it was given.
        _assert_zero_always_allow_invariant(self._options)
        await client.connect()
        self._client = client

    async def _handle_can_use_tool(self, tool_name: str, tool_input: dict, context: Any):
        """The real SDK's `can_use_tool` callback: `async def(tool_name,
        tool_input, context) -> PermissionResultAllow | PermissionResultDeny`.

        Bridges the SDK's direct-call-and-await shape to this project's own
        pull-based `AgentBackend` interface: puts a `ToolUseRequest` on the
        session's event queue for `send_text()` to yield, then blocks on a
        pending decision `resolve_tool_use()` supplies later.

        Held under `self._permission_lock` for its full duration (I6): a
        second concurrent call blocks here until the first one's decision
        has been both supplied and returned, so a caller's spoken answer can
        never resolve the wrong prompt.
        """
        async with self._permission_lock:
            if self._event_queue is None:
                # M8: fail closed AND loud — this should never happen (see
                # send_text(), which always sets the queue before any call
                # that could reach the SDK), but a misbehaving client must
                # never get a silent no-op in place of an explicit deny.
                logger.error(
                    "can_use_tool invoked for %r with no active call session; denying", tool_name
                )
                return PermissionResultDeny(
                    behavior="deny", message="no active call session to ask the caller for permission"
                )

            loop = asyncio.get_running_loop()
            self._pending_decision = loop.create_future()
            description = _describe_tool_use(tool_name, tool_input)
            await self._event_queue.put(ToolUseRequest(description=description))
            approved = await self._pending_decision

        if approved:
            return PermissionResultAllow(behavior="allow")
        return PermissionResultDeny(behavior="deny", message="caller denied this action")

    async def send_text(self, text: str) -> AsyncIterator[AgentEvent]:
        if self._client is None:
            raise RuntimeError("start_session() must be called before send_text()")

        if self._pump_task is None or self._pump_task.done():
            if self._pump_task is not None:
                # Surface (rather than silently swallow) any exception from
                # a previous turn's pump task before replacing it.
                self._pump_task.result()

            # `queue` is a local the pump closes over directly, instead of
            # reading `self._event_queue` back out on every put(). This
            # avoids a prior bug: if a pump task kept running past a
            # send_text() call that nulled out self._event_queue, the next
            # queue.put() would crash with AttributeError. Closing over a
            # stable local reference means pump() is correct regardless of
            # whatever self._event_queue is reassigned to later.
            queue: asyncio.Queue[Any] = asyncio.Queue()
            self._event_queue = queue

            async def pump() -> None:
                try:
                    await self._client.query(text)
                    async for message in self._client.receive_response():
                        if isinstance(message, AssistantMessage):
                            for block in message.content:
                                if isinstance(block, TextBlock):
                                    await queue.put(TextReply(text=block.text))
                finally:
                    await queue.put(_PUMP_DONE)

            self._pump_task = asyncio.create_task(pump())
        else:
            # A previous send_text() call already returned early on a
            # ToolUseRequest from this same turn; its pump task is still
            # running (paused inside the SDK's own processing, waiting on
            # the pending decision _handle_can_use_tool is holding). Keep
            # draining the same queue instead of starting a second,
            # concurrent turn (and do NOT call client.query() again).
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
        if self._pending_decision is None or self._pending_decision.done():
            raise RuntimeError(
                "resolve_tool_use() called with no tool-use permission request pending"
            )
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
