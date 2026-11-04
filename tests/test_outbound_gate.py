"""Tests for the outbound safety gate.

This is the project's core anti-spam control: it decides whether the agent
may place a real outbound phone call right now. The check order matters and
must not be reordered: disabled -> unknown event type -> rate limit ->
quiet hours -> call now. `urgent` on a known event type bypasses quiet hours
ONLY; it must never bypass the unknown-event-type rejection or the rate limit.
"""

from __future__ import annotations

from datetime import datetime

from agent_hotline.config import EventTypeConfig, OutboundConfig, QuietHours
from agent_hotline.outbound_gate import OutboundDecision, OutboundGate


def make_config(enabled=True, max_calls=3, event_types=None, quiet_start="22:00", quiet_end="07:00"):
    return OutboundConfig(
        enabled=enabled,
        destination_number="+15555550123",
        max_calls_per_24h=max_calls,
        quiet_hours=QuietHours(start=quiet_start, end=quiet_end, timezone="UTC"),
        event_types=event_types if event_types is not None else {},
    )


def test_disabled_rejects_even_known_event_type():
    gate = OutboundGate(
        make_config(enabled=False, event_types={"critical-error": EventTypeConfig(urgent=True)}),
        clock=lambda: datetime(2026, 11, 4, 12, 0),
    )
    assert gate.decide("critical-error", []) == OutboundDecision.REJECTED_DISABLED


def test_unknown_event_type_rejected_even_if_marked_urgent_elsewhere():
    gate = OutboundGate(
        make_config(event_types={"critical-error": EventTypeConfig(urgent=True)}),
        clock=lambda: datetime(2026, 11, 4, 12, 0),
    )
    assert gate.decide("not-a-real-type", []) == OutboundDecision.REJECTED_UNKNOWN_EVENT_TYPE


def test_rate_limit_rejects_even_urgent_event_type():
    history = [datetime(2026, 11, 4, 11, 0), datetime(2026, 11, 4, 10, 0), datetime(2026, 11, 4, 9, 0)]
    gate = OutboundGate(
        make_config(max_calls=3, event_types={"critical-error": EventTypeConfig(urgent=True)}),
        clock=lambda: datetime(2026, 11, 4, 12, 0),
    )
    assert gate.decide("critical-error", history) == OutboundDecision.REJECTED_RATE_LIMIT


def test_calls_older_than_24h_do_not_count_against_rate_limit():
    history = [datetime(2026, 11, 3, 11, 0)]  # 25 hours before clock below
    gate = OutboundGate(
        make_config(max_calls=1, event_types={"approval-needed": EventTypeConfig(urgent=False)}),
        clock=lambda: datetime(2026, 11, 4, 12, 0),
    )
    assert gate.decide("approval-needed", history) == OutboundDecision.CALL_NOW


def test_quiet_hours_non_urgent_queues():
    gate = OutboundGate(
        make_config(event_types={"approval-needed": EventTypeConfig(urgent=False)}),
        clock=lambda: datetime(2026, 11, 4, 23, 0),
    )
    assert gate.decide("approval-needed", []) == OutboundDecision.QUEUE_FOR_LATER


def test_quiet_hours_urgent_calls_now():
    gate = OutboundGate(
        make_config(event_types={"critical-error": EventTypeConfig(urgent=True)}),
        clock=lambda: datetime(2026, 11, 4, 23, 0),
    )
    assert gate.decide("critical-error", []) == OutboundDecision.CALL_NOW


def test_outside_quiet_hours_calls_now():
    gate = OutboundGate(
        make_config(event_types={"approval-needed": EventTypeConfig(urgent=False)}),
        clock=lambda: datetime(2026, 11, 4, 12, 0),
    )
    assert gate.decide("approval-needed", []) == OutboundDecision.CALL_NOW


def test_quiet_hours_wrap_just_after_midnight_queues():
    # 03:00 falls inside a 22:00-07:00 window that wraps past midnight.
    gate = OutboundGate(
        make_config(event_types={"approval-needed": EventTypeConfig(urgent=False)}),
        clock=lambda: datetime(2026, 11, 4, 3, 0),
    )
    assert gate.decide("approval-needed", []) == OutboundDecision.QUEUE_FOR_LATER


def test_quiet_hours_wrap_late_evening_queues():
    # 23:30 falls inside a 22:00-07:00 window that wraps past midnight.
    gate = OutboundGate(
        make_config(event_types={"approval-needed": EventTypeConfig(urgent=False)}),
        clock=lambda: datetime(2026, 11, 4, 23, 30),
    )
    assert gate.decide("approval-needed", []) == OutboundDecision.QUEUE_FOR_LATER


def test_quiet_hours_wrap_boundary_exactly_at_end_is_outside():
    # 07:00 is the end boundary; treat it as no longer quiet (inclusive-start,
    # inclusive-end would make an instantaneous boundary ambiguous, so pick
    # a clear rule and test it explicitly).
    gate = OutboundGate(
        make_config(event_types={"approval-needed": EventTypeConfig(urgent=False)}),
        clock=lambda: datetime(2026, 11, 4, 7, 0),
    )
    assert gate.decide("approval-needed", []) == OutboundDecision.CALL_NOW


def test_rate_limit_at_boundary_count_not_exceeded_calls_now():
    history = [datetime(2026, 11, 4, 11, 0), datetime(2026, 11, 4, 10, 0)]
    gate = OutboundGate(
        make_config(max_calls=3, event_types={"approval-needed": EventTypeConfig(urgent=False)}),
        clock=lambda: datetime(2026, 11, 4, 12, 0),
    )
    assert gate.decide("approval-needed", history) == OutboundDecision.CALL_NOW
