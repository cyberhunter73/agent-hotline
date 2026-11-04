"""Tests for the outbound notify() trigger.

OutboundNotifier is the only path in the codebase that can place a real
outbound phone call. It must consult the OutboundGate first and touch the SIP
client only when the gate returns CALL_NOW.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from agent_hotline.config import EventTypeConfig, OutboundConfig, QuietHours
from agent_hotline.outbound import OutboundNotifier
from agent_hotline.outbound_gate import OutboundDecision


class _AnyStr:
    """Equality matcher: compares equal to any string."""

    def __eq__(self, other):
        return isinstance(other, str)

    def __repr__(self):
        return "ANY_STR"


pytest_any_str = _AnyStr()


def make_config(enabled=True, max_calls=3, event_types=None):
    return OutboundConfig(
        enabled=enabled,
        destination_number="+15555550123",
        max_calls_per_24h=max_calls,
        quiet_hours=QuietHours(start="22:00", end="07:00", timezone="UTC"),
        event_types=event_types if event_types is not None else {"critical-error": EventTypeConfig(urgent=True)},
    )


class FakeSipClient:
    def __init__(self):
        self.calls = []

    async def create_participant(self, trunk_id: str, number: str, room_name: str) -> None:
        self.calls.append((trunk_id, number, room_name))


class AlwaysDecideGate:
    """Gate stub that always returns a fixed decision."""

    def __init__(self, decision: OutboundDecision):
        self._decision = decision

    def decide(self, event_type, call_history):
        return self._decision


class RecordingGate:
    """Gate stub that always returns a fixed decision and records the history it was given."""

    def __init__(self, decision: OutboundDecision):
        self._decision = decision
        self.seen_call_history = []

    def decide(self, event_type, call_history):
        self.seen_call_history = list(call_history)
        return self._decision


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
    notifier = OutboundNotifier(
        gate=AlwaysDecideGate(OutboundDecision.REJECTED_UNKNOWN_EVENT_TYPE), sip_client=fake_sip, config=make_config()
    )
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


@pytest.mark.asyncio
async def test_notify_non_call_now_decisions_do_not_record_history():
    fake_sip = FakeSipClient()
    gate = RecordingGate(OutboundDecision.QUEUE_FOR_LATER)
    notifier = OutboundNotifier(gate=gate, sip_client=fake_sip, config=make_config())
    await notifier.notify("critical-error", "disk full")
    await notifier.notify("critical-error", "disk full again")
    assert fake_sip.calls == []
    assert gate.seen_call_history == []


@pytest.mark.asyncio
async def test_notify_with_real_gate_rejects_unknown_event_without_calling_sip():
    from agent_hotline.outbound_gate import OutboundGate

    fake_sip = FakeSipClient()
    gate = OutboundGate(make_config(), clock=lambda: datetime(2026, 11, 4, 12, 0))
    notifier = OutboundNotifier(gate=gate, sip_client=fake_sip, config=make_config())
    decision = await notifier.notify("not-a-type", "whatever")
    assert decision == OutboundDecision.REJECTED_UNKNOWN_EVENT_TYPE
    assert fake_sip.calls == []
