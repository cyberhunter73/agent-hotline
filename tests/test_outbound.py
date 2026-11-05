"""Tests for the outbound notify() trigger.

OutboundNotifier is the only path in the codebase that can place a real
outbound phone call. It must consult the OutboundGate first and touch the SIP
client only when the gate returns CALL_NOW.

Fix-wave update (C2): `OutboundNotifier` no longer holds its own in-process
`_call_history` list — it reads/writes history through a `CallHistoryStore`
(see `agent_hotline/outbound.py`), so these tests use
`InMemoryCallHistoryStore` in place of the notifier's own former internal
state. `trunk_id` is also now a required keyword argument (M5): production
always supplies a real one via the environment, so the placeholder default
is gone.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from agent_hotline.config import EventTypeConfig, OutboundConfig, QuietHours
from agent_hotline.outbound import InMemoryCallHistoryStore, OutboundNotifier
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
    notifier = OutboundNotifier(
        gate=AlwaysDecideGate(OutboundDecision.CALL_NOW),
        sip_client=fake_sip,
        config=make_config(),
        history_store=InMemoryCallHistoryStore(),
        trunk_id="trunk-id",
    )
    decision = await notifier.notify("critical-error", "disk full")
    assert decision == OutboundDecision.CALL_NOW
    assert fake_sip.calls == [("trunk-id", make_config().destination_number, pytest_any_str)]


@pytest.mark.asyncio
async def test_notify_rejected_never_invokes_sip_client():
    fake_sip = FakeSipClient()
    notifier = OutboundNotifier(
        gate=AlwaysDecideGate(OutboundDecision.REJECTED_UNKNOWN_EVENT_TYPE),
        sip_client=fake_sip,
        config=make_config(),
        history_store=InMemoryCallHistoryStore(),
        trunk_id="trunk-id",
    )
    decision = await notifier.notify("not-a-type", "whatever")
    assert decision == OutboundDecision.REJECTED_UNKNOWN_EVENT_TYPE
    assert fake_sip.calls == []


@pytest.mark.asyncio
async def test_notify_call_now_records_history_for_future_rate_limit_checks():
    fake_sip = FakeSipClient()
    gate = RecordingGate(OutboundDecision.CALL_NOW)
    notifier = OutboundNotifier(
        gate=gate,
        sip_client=fake_sip,
        config=make_config(),
        history_store=InMemoryCallHistoryStore(),
        trunk_id="trunk-id",
    )
    await notifier.notify("critical-error", "disk full")
    assert len(gate.seen_call_history) == 0  # history passed to decide() before this call
    await notifier.notify("critical-error", "disk full again")
    assert len(gate.seen_call_history) == 1  # grew after the first successful call


@pytest.mark.asyncio
async def test_notify_non_call_now_decisions_do_not_record_history():
    fake_sip = FakeSipClient()
    gate = RecordingGate(OutboundDecision.QUEUE_FOR_LATER)
    notifier = OutboundNotifier(
        gate=gate,
        sip_client=fake_sip,
        config=make_config(),
        history_store=InMemoryCallHistoryStore(),
        trunk_id="trunk-id",
    )
    await notifier.notify("critical-error", "disk full")
    await notifier.notify("critical-error", "disk full again")
    assert fake_sip.calls == []
    assert gate.seen_call_history == []


@pytest.mark.asyncio
async def test_notify_with_real_gate_rejects_unknown_event_without_calling_sip():
    from agent_hotline.outbound_gate import OutboundGate

    fake_sip = FakeSipClient()
    gate = OutboundGate(make_config(), clock=lambda: datetime(2026, 11, 4, 12, 0))
    notifier = OutboundNotifier(
        gate=gate,
        sip_client=fake_sip,
        config=make_config(),
        history_store=InMemoryCallHistoryStore(),
        trunk_id="trunk-id",
        clock=lambda: datetime(2026, 11, 4, 12, 0),
    )
    decision = await notifier.notify("not-a-type", "whatever")
    assert decision == OutboundDecision.REJECTED_UNKNOWN_EVENT_TYPE
    assert fake_sip.calls == []


@pytest.mark.asyncio
async def test_notify_logs_rejected_and_queued_decisions(caplog):
    """M6: config.example.yaml claims a rejected/queued event is "logged,
    never dialed" — make that true."""
    import logging

    fake_sip = FakeSipClient()
    notifier = OutboundNotifier(
        gate=AlwaysDecideGate(OutboundDecision.REJECTED_RATE_LIMIT),
        sip_client=fake_sip,
        config=make_config(),
        history_store=InMemoryCallHistoryStore(),
        trunk_id="trunk-id",
    )
    with caplog.at_level(logging.INFO):
        await notifier.notify("critical-error", "disk full")
    assert any("critical-error" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_sqlite_history_rate_limits_across_separate_notifier_instances(tmp_path):
    """C2 integration test: the real safety property this fix exists for.

    Two SEPARATE `OutboundNotifier` instances (simulating two separate
    `python -m agent_hotline.worker notify ...` CLI invocations, each a
    fresh process in production) share the same sqlite history path. The
    rate limit set by the first instance's call must be honored by the
    second instance, which never shared any in-memory state with the
    first — only the file on disk.
    """
    from agent_hotline.outbound_gate import OutboundGate
    from agent_hotline.outbound import SqliteCallHistoryStore

    history_path = tmp_path / "outbound_calls.sqlite3"
    config = make_config(max_calls=1, event_types={"critical-error": EventTypeConfig(urgent=True)})
    fixed_now = datetime(2026, 11, 4, 12, 0)

    # First "process": places one call, which should exhaust the cap of 1.
    store1 = SqliteCallHistoryStore(history_path)
    gate1 = OutboundGate(config, clock=lambda: fixed_now)
    fake_sip1 = FakeSipClient()
    notifier1 = OutboundNotifier(
        gate=gate1, sip_client=fake_sip1, config=config, history_store=store1, trunk_id="trunk-id", clock=lambda: fixed_now
    )
    decision1 = await notifier1.notify("critical-error", "disk full")
    assert decision1 == OutboundDecision.CALL_NOW
    assert len(fake_sip1.calls) == 1
    store1.close()

    # Second "process": brand-new store/gate/notifier objects, same path,
    # shortly after (well within the 24h window).
    store2 = SqliteCallHistoryStore(history_path)
    gate2 = OutboundGate(config, clock=lambda: fixed_now)
    fake_sip2 = FakeSipClient()
    notifier2 = OutboundNotifier(
        gate=gate2, sip_client=fake_sip2, config=config, history_store=store2, trunk_id="trunk-id", clock=lambda: fixed_now
    )
    decision2 = await notifier2.notify("critical-error", "disk full again")
    assert decision2 == OutboundDecision.REJECTED_RATE_LIMIT
    assert fake_sip2.calls == []
    store2.close()
