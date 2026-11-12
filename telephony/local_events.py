"""Lab-only direct persistence adapter for QueuePilot's own SIP runtime.

No AES, WTI, external PBX, HTTP bridge, or third-party service is involved.
This adapter writes to QueuePilot's local SQLite database, in the same host.
It is deliberately opt-in and does not offer durable cross-process delivery,
crash replay, or production HA guarantees.
"""
from __future__ import annotations

from datetime import datetime, timezone
import time
import uuid

from app.db import connection, init_db
from app.models import EngineEvent
from app.service import ingest


class LocalEventWriter:
    """Optional event sink for NativeSwitch running beside the dashboard."""

    def __init__(self, routes, agents):
        init_db()
        with connection() as db:
            for route in routes:
                if not db.execute('SELECT 1 FROM queues WHERE id=? AND enabled=1', (route.queue,)).fetchone():
                    raise ValueError(f'Native SIP route references unknown/disabled QueuePilot queue: {route.queue}')
            for agent_id in agents:
                if not db.execute('SELECT 1 FROM agents WHERE id=?', (agent_id,)).fetchone():
                    raise ValueError(f'Native SIP agent mapping not in dashboard: {agent_id}')

    def __call__(self, kind: str, call) -> None:
        if kind not in {'call.queued', 'call.answered', 'call.abandoned', 'call.completed'}:
            return
        payload = EngineEvent(
            source='queuepilot-native',
            event_id='native-' + uuid.uuid4().hex,
            type=kind,
            call_id=call.trunk_call_id,
            queue_id=call.queue,
            agent_id=call.agent_id if kind == 'call.answered' else None,
            occurred_at=datetime.now(timezone.utc),
        )
        ingest(payload)

    def heartbeat(self, connected: bool) -> None:
        with connection() as db:
            db.execute('''INSERT INTO engine_heartbeats(source,engine_healthy,last_heartbeat,details)
                          VALUES('queuepilot-native',?,?,?)
                          ON CONFLICT(source) DO UPDATE SET
                          engine_healthy=excluded.engine_healthy,
                          last_heartbeat=excluded.last_heartbeat,
                          details=excluded.details''',
                       (int(connected), time.time(), 'lab SIP listener; NOT proof of Avaya trunk interoperability'))
