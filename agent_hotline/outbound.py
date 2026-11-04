"""Outbound call placement for agent-hotline.

OutboundNotifier.notify() is the only code path that can place a real outbound
phone call. It always consults the OutboundGate first, and it touches the SIP
client only when the gate returns CALL_NOW. Every other decision is returned
unchanged and the SIP client is never invoked.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from agent_hotline.config import OutboundConfig
from agent_hotline.outbound_gate import OutboundDecision, OutboundGate


class SipClient(Protocol):
    """Places an outbound SIP call into a LiveKit room via a configured trunk."""

    async def create_participant(self, trunk_id: str, number: str, room_name: str) -> None: ...


class OutboundNotifier:
    """Gate-checked trigger for outbound notification calls."""

    def __init__(
        self,
        gate: OutboundGate,
        sip_client: SipClient,
        config: OutboundConfig,
        *,
        # Placeholder: OutboundConfig does not yet carry a trunk id. Wire the real
        # value from config before any live use; the gate still controls every call.
        trunk_id: str = "trunk-id",
    ):
        self.gate = gate
        self.sip_client = sip_client
        self.config = config
        self.trunk_id = trunk_id
        self._call_history: list[datetime] = []

    async def notify(self, event_type: str, message: str) -> OutboundDecision:
        decision = self.gate.decide(event_type, self._call_history)
        if decision != OutboundDecision.CALL_NOW:
            return decision

        now = datetime.now()
        # Recorded before the SIP await: a call attempt that raises may still have
        # reached the phone, so it must count toward the 24h rate limit.
        self._call_history.append(now)
        room_name = f"outbound-{event_type}-{now.strftime('%Y%m%dT%H%M%S%f')}"
        await self.sip_client.create_participant(
            trunk_id=self.trunk_id,
            number=self.config.destination_number,
            room_name=room_name,
        )
        return decision
