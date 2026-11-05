"""Outbound call placement for agent-hotline.

OutboundNotifier.notify() is the only code path that can place a real outbound
phone call. It always consults the OutboundGate first, and it touches the SIP
client only when the gate returns CALL_NOW. Every other decision is returned
unchanged and the SIP client is never invoked.

C2 fix: call history used to live in an in-process list (`_call_history`)
that reset to empty on every process start. Since the only shipped trigger is
`python -m agent_hotline.worker notify ...` — a brand-new process per
invocation — that meant `max_calls_per_24h` provided zero real protection: a
repeated CLI invocation was never rate-limited at all. `CallHistoryStore` is
the seam that fixes this: `OutboundGate`'s own decision logic (`decide`)
stays a pure function over a plain `list[datetime]`, fully testable with an
in-memory fake exactly as before; `OutboundNotifier` now reads/writes that
history through a `CallHistoryStore`, and the pinned implementation
(`SqliteCallHistoryStore`) persists it to a real file so the cap holds across
separate CLI invocations and worker restarts.

Race safety: `OutboundNotifier.notify()` performs its read-decide-write
sequence (`recent()` -> `gate.decide()` -> `record()`) inside one
`CallHistoryStore.transaction()` context. `SqliteCallHistoryStore`'s
transaction issues `BEGIN IMMEDIATE`, which acquires sqlite's write lock
immediately rather than lazily — so a second OS process's own `notify()`
call blocks at its own `BEGIN IMMEDIATE` until the first process's
transaction commits, instead of racing it on the read.

Ordering preserved from Task 7's deliberate fail-closed choice: the call
attempt is recorded into history, and that transaction is committed, BEFORE
`sip_client.create_participant(...)` is awaited — a call attempt that raises
after this point may still have reached the phone, so it must already count
toward the 24h rate limit.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator, Protocol

from agent_hotline.config import OutboundConfig
from agent_hotline.outbound_gate import OutboundDecision, OutboundGate

logger = logging.getLogger(__name__)

DEFAULT_HISTORY_PATH = str(Path.home() / ".agent-hotline" / "outbound_calls.sqlite3")


class SipClient(Protocol):
    """Places an outbound SIP call into a LiveKit room via a configured trunk."""

    async def create_participant(self, trunk_id: str, number: str, room_name: str) -> None: ...


class CallHistoryStore(Protocol):
    """Where outbound call-attempt timestamps live, for the 24h rate limit.

    `OutboundGate` itself never depends on this — it stays a pure function
    over whatever plain `list[datetime]` it is handed, so its own tests keep
    using an in-memory fake with no I/O. This protocol exists only for
    `OutboundNotifier`, so the history it reads/writes can be swapped
    between an in-memory fake (unit tests) and a real persisted store
    (`SqliteCallHistoryStore`, production).
    """

    def record(self, timestamp: datetime) -> None: ...

    def recent(self, since: datetime) -> list[datetime]: ...

    def transaction(self) -> "contextlib.AbstractContextManager[None]":
        """Hold exclusive access to this store for the duration of a
        read-then-decide-then-write sequence. The real sqlite-backed
        implementation uses this to serialize concurrent OS processes;
        an in-memory fake can make this a no-op (there is only one
        process/thread to serialize against in a unit test)."""


class InMemoryCallHistoryStore:
    """A `CallHistoryStore` backed by a plain list, for tests. Not
    process-safe — use `SqliteCallHistoryStore` for anything that must
    survive or be shared across separate process invocations."""

    def __init__(self) -> None:
        self._history: list[datetime] = []

    def record(self, timestamp: datetime) -> None:
        self._history.append(timestamp)

    def recent(self, since: datetime) -> list[datetime]:
        return [called_at for called_at in self._history if called_at >= since]

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        yield


class SqliteCallHistoryStore:
    """Persists outbound call-attempt timestamps to a sqlite3 file so
    `max_calls_per_24h` holds across separate `python -m
    agent_hotline.worker notify ...` invocations, not just within one
    process's lifetime.

    Opens (and creates, including the parent directory, if missing) the
    database at construction time. Raises if the path cannot be opened or
    written to — this must never silently fall back to an in-memory/no-op
    store; a rate limit that can silently stop persisting is worse than no
    rate limit at all, because it would look like it is still working.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: autocommit by default, with transactions
        # controlled explicitly via BEGIN IMMEDIATE/COMMIT/ROLLBACK in
        # transaction() below, rather than sqlite3's own implicit
        # (deferred) transaction handling.
        self._conn = sqlite3.connect(str(self._path), isolation_level=None)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS call_history (called_at TEXT NOT NULL)"
        )

    def record(self, timestamp: datetime) -> None:
        self._conn.execute(
            "INSERT INTO call_history (called_at) VALUES (?)", (timestamp.isoformat(),)
        )

    def recent(self, since: datetime) -> list[datetime]:
        cursor = self._conn.execute(
            "SELECT called_at FROM call_history WHERE called_at >= ?", (since.isoformat(),)
        )
        return [datetime.fromisoformat(row[0]) for row in cursor.fetchall()]

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def close(self) -> None:
        self._conn.close()


class OutboundNotifier:
    """Gate-checked trigger for outbound notification calls."""

    def __init__(
        self,
        gate: OutboundGate,
        sip_client: SipClient,
        config: OutboundConfig,
        history_store: CallHistoryStore,
        *,
        trunk_id: str,
        clock: Callable[[], datetime] = datetime.now,
    ):
        self.gate = gate
        self.sip_client = sip_client
        self.config = config
        self.history_store = history_store
        self.trunk_id = trunk_id
        # I4: the SAME clock used here for recorded-call timestamps must be
        # the one the gate uses for its quiet-hours/rate-limit math, or an
        # aware/naive mismatch between them would raise TypeError on
        # comparison the moment a real timezone-aware clock is injected.
        self.clock = clock

    async def notify(self, event_type: str, message: str) -> OutboundDecision:
        now = self.clock()
        since = now - timedelta(hours=24)

        with self.history_store.transaction():
            history = self.history_store.recent(since)
            decision = self.gate.decide(event_type, history)
            if decision == OutboundDecision.CALL_NOW:
                # Recorded (and, for the sqlite store, committed) before the
                # SIP await below: a call attempt that raises after this
                # point may still have reached the phone, so it must count
                # toward the 24h rate limit either way (Task 7's original
                # fail-closed ordering, preserved).
                self.history_store.record(now)

        # M6: log every rejected/queued decision, not just a placed call —
        # config.example.yaml's own comment claims rejected events are
        # "logged, never dialed"; this is what makes that true.
        if decision == OutboundDecision.REJECTED_DISABLED:
            logger.info("outbound notify rejected (outbound disabled): event_type=%r", event_type)
            return decision
        if decision == OutboundDecision.REJECTED_UNKNOWN_EVENT_TYPE:
            logger.warning("outbound notify rejected (unknown event_type): event_type=%r", event_type)
            return decision
        if decision == OutboundDecision.REJECTED_RATE_LIMIT:
            logger.warning("outbound notify rejected (rate limit exceeded): event_type=%r", event_type)
            return decision
        if decision == OutboundDecision.QUEUE_FOR_LATER:
            logger.info(
                "outbound notify queued for later (quiet hours, not urgent): event_type=%r "
                "(not actually re-delivered in this build; see README/SETUP known gaps)",
                event_type,
            )
            return decision

        room_name = f"outbound-{event_type}-{now.strftime('%Y%m%dT%H%M%S%f')}"
        await self.sip_client.create_participant(
            trunk_id=self.trunk_id,
            number=self.config.destination_number,
            room_name=room_name,
        )
        logger.info("outbound call placed: event_type=%r destination=%r", event_type, self.config.destination_number)
        return decision
