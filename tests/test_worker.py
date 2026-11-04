"""Tests for the testable core of the worker entrypoint.

`handle_inbound_job` is the only piece of `agent_hotline/worker.py` covered
by automated tests. The real LiveKit `entrypoint(ctx)` job handler requires
the actual `livekit-agents` SDK and a live job context, so it is exercised
by the manual rollout plan instead (see worker.py's module docstring).

This is also the project's SIP-cleanup safety test: a `participant.remove()`
failure during call cleanup must never propagate out of `handle_inbound_job`
(it must never crash the worker process, which would take down every other
in-flight call) and must never suppress or replace `run_session()`'s own
result or exception.

`FakeParticipant` is a hand-rolled test double (not defined in the task
brief's test snippet, which only names it) implementing
`worker.SipParticipantHandle`.

Deviation from the brief's literal test snippet: the brief's last three
tests use `"+1"` as both the allowlist entry and the caller number. That
fails before `handle_inbound_job` is ever reached — `InboundGate.__init__`
(Task 3) raises `ValueError` for any allowlist entry `normalize_number`
can't validate, and a bare `"+1"` has too few digits for an international
number (`agent_hotline/inbound.py` requires 8-15 digits after a `+`). This
is a stale detail in the brief, written before Task 3's digit-count
validation was finalized, not a real requirement to preserve. Those three
tests use `"+15551234567"` instead (the same valid number already used
elsewhere in this file), which preserves each test's actual intent — an
allowed caller whose session result/exception must survive a
`participant.remove()` failure — without changing what they verify.
"""

from __future__ import annotations

import pytest

from agent_hotline.call_session import CallResult
from agent_hotline.inbound import InboundGate
from agent_hotline.worker import handle_inbound_job


class FakeParticipant:
    """Stands in for `SipParticipantHandle`.

    Records whether `remove()` was called (`removed`), and can be told to
    raise from `remove()` to exercise the cleanup-failure-isolation
    guarantee. `removed` is set True before the configured exception is
    raised, since the removal was genuinely attempted either way.
    """

    def __init__(self, raise_on_remove: bool = False):
        self.removed = False
        self._raise_on_remove = raise_on_remove

    async def remove(self) -> None:
        self.removed = True
        if self._raise_on_remove:
            raise RuntimeError("SIP participant removal failed")


@pytest.mark.asyncio
async def test_disallowed_caller_gets_brush_off_no_session():
    gate = InboundGate(allowed_numbers=["+15551234567"])
    brush_off_called = []

    async def run_session():
        raise AssertionError("must not be called")

    async def brush_off():
        brush_off_called.append(True)

    result = await handle_inbound_job("+15559999999", gate, run_session, FakeParticipant(), brush_off)
    assert result is None
    assert brush_off_called == [True]


@pytest.mark.asyncio
async def test_allowed_caller_runs_session_and_removes_participant():
    gate = InboundGate(allowed_numbers=["+15551234567"])
    participant = FakeParticipant()

    async def run_session():
        return CallResult("completed")

    result = await handle_inbound_job("+15551234567", gate, run_session, participant, lambda: None)
    assert result.status == "completed"
    assert participant.removed is True


@pytest.mark.asyncio
async def test_participant_removal_failure_is_caught_and_logged_not_raised():
    participant = FakeParticipant(raise_on_remove=True)

    async def run_session():
        return CallResult("completed")

    gate = InboundGate(allowed_numbers=["+15551234567"])
    result = await handle_inbound_job("+15551234567", gate, run_session, participant, lambda: None)
    assert result.status == "completed"  # did not raise out to the caller


@pytest.mark.asyncio
async def test_participant_removed_even_if_session_raises():
    participant = FakeParticipant()

    async def run_session():
        raise RuntimeError("boom")

    gate = InboundGate(allowed_numbers=["+15551234567"])
    with pytest.raises(RuntimeError):
        await handle_inbound_job("+15551234567", gate, run_session, participant, lambda: None)
    assert participant.removed is True


@pytest.mark.asyncio
async def test_participant_removal_failure_does_not_mask_session_exception():
    """The cleanup-failure guarantee must not become a mechanism for hiding a
    genuine `run_session()` exception either: when BOTH fail, the caller
    sees the session's exception, and the removal failure is only logged.
    """
    participant = FakeParticipant(raise_on_remove=True)

    async def run_session():
        raise RuntimeError("session boom")

    gate = InboundGate(allowed_numbers=["+15551234567"])
    with pytest.raises(RuntimeError, match="session boom"):
        await handle_inbound_job("+15551234567", gate, run_session, participant, lambda: None)
    assert participant.removed is True
