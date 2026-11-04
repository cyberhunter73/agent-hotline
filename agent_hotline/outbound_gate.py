"""Outbound safety gate for agent-hotline.

Decides whether the agent is allowed to place a real outbound phone call
right now, given an event type and recent call history. This is a pure
decision function: no I/O, no side effects.

This is the project's core anti-spam safety feature. A tool-wielding agent
that can ring a real phone whenever it judges something "worth a call" is a
foot-gun; this gate keeps that judgment a human-made decision in advance (a
pre-configured, pre-listed event type) rather than the agent's live call.

Check order is fixed and must not be reordered:
    disabled -> unknown event type -> rate limit -> quiet hours -> call now

Two subtleties enforced here:
  - `urgent: true` on a known event type bypasses QUIET HOURS ONLY. It does
    not bypass the unknown-event-type rejection (there is no `urgent` flag to
    read on an event type that was never configured) and does not bypass the
    rate limit (a config-crossed event type being `urgent` doesn't exempt it
    from the 24h cap).
  - The quiet-hours window may wrap past midnight (e.g. 22:00-07:00); a naive
    `start <= now <= end` comparison breaks for a wrapping window, so wrapping
    is detected and handled explicitly.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from enum import Enum
from typing import Callable

from agent_hotline.config import OutboundConfig


class OutboundDecision(Enum):
    CALL_NOW = "call_now"
    QUEUE_FOR_LATER = "queue_for_later"
    REJECTED_DISABLED = "rejected_disabled"
    REJECTED_UNKNOWN_EVENT_TYPE = "rejected_unknown_event_type"
    REJECTED_RATE_LIMIT = "rejected_rate_limit"


def _parse_hhmm(value: str) -> time:
    hour_str, minute_str = value.split(":")
    return time(hour=int(hour_str), minute=int(minute_str))


def _within_quiet_hours(now: datetime, start: time, end: time) -> bool:
    now_time = now.time()
    if start <= end:
        # Normal (non-wrapping) window, e.g. 09:00-17:00.
        return start <= now_time < end
    # Wrapping window, e.g. 22:00-07:00: inside if at/after start (late
    # night) or before end (early morning).
    return now_time >= start or now_time < end


class OutboundGate:
    """Pure decision function for whether an outbound call may be placed now."""

    def __init__(self, config: OutboundConfig, clock: Callable[[], datetime]):
        self._config = config
        self._clock = clock

    def decide(self, event_type: str, call_history: list[datetime]) -> OutboundDecision:
        config = self._config

        if not config.enabled:
            return OutboundDecision.REJECTED_DISABLED

        event_config = config.event_types.get(event_type)
        if event_config is None:
            return OutboundDecision.REJECTED_UNKNOWN_EVENT_TYPE

        now = self._clock()
        window_start = now - timedelta(hours=24)
        recent_calls = sum(1 for called_at in call_history if window_start <= called_at <= now)
        if recent_calls >= config.max_calls_per_24h:
            return OutboundDecision.REJECTED_RATE_LIMIT

        quiet_start = _parse_hhmm(config.quiet_hours.start)
        quiet_end = _parse_hhmm(config.quiet_hours.end)
        if _within_quiet_hours(now, quiet_start, quiet_end) and not event_config.urgent:
            return OutboundDecision.QUEUE_FOR_LATER

        return OutboundDecision.CALL_NOW
