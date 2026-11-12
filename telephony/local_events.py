"""QueuePilot on-host lab event outbox + native ACD control synchronizer.

Only local SQLite, with no external PBX or middleware. This is NOT production HA.
The outbox survives process restart after enqueue, but cannot restore an RTP
call or guarantee events not written before a process crash.
"""
from __future__ import annotations

from datetime import datetime, timezone
import time
import uuid
from pathlib import Path

from app.config import config
from app.audit import audit
from app.db import connection, init_db
from app.models import EngineEvent
from app.service import ingest
from .recording_archive import NativeRecordingArchive


class LocalEventWriter:
    def __init__(self, routes, agents, *, recording_capture_dir: Path | None = None,
                 recording_notice_confirmed: bool = False):
        if recording_capture_dir is not None and (not recording_notice_confirmed or not config.recordings_enabled):
            raise ValueError('Recording requires explicit notice policy approval and RECORDINGS_ENABLED=true')
        self.recording_capture_dir = recording_capture_dir
        self.recording_notice_confirmed = recording_notice_confirmed
        init_db()
        with connection() as db:
            for route in routes:
                if not db.execute('SELECT 1 FROM queues WHERE id=?', (route.queue,)).fetchone():
                    raise ValueError(f'Native SIP route references unknown QueuePilot queue: {route.queue}')
            for agent_id,(extension,_) in agents.items():
                row = db.execute('SELECT extension FROM agents WHERE id=?', (agent_id,)).fetchone()
                if not row or row['extension'] != extension:
                    raise ValueError(f'Native SIP agent mapping/extension mismatch: {agent_id}')
            now=time.time()
            # Binding rows are ephemeral; keep pending command history on restart.
            db.execute('DELETE FROM native_agent_bindings')
            for agent_id,(extension,_) in agents.items():
                db.execute('''INSERT INTO native_agent_bindings(agent_id,extension,bound_at)
                              VALUES(?,?,?) ON CONFLICT(agent_id) DO UPDATE SET
                              extension=excluded.extension,bound_at=excluded.bound_at''',
                           (agent_id,extension,now))

    def __call__(self, kind: str, call) -> None:
        if kind not in {'call.queued','call.answered','call.abandoned','call.completed'}:
            return
        # Durable before attempted delivery; private caller number is never journaled.
        event_id='native-'+uuid.uuid4().hex
        with connection() as db:
            db.execute('''INSERT INTO native_event_outbox
                       (event_id,event_type,call_id,queue_id,agent_id,occurred_at)
                       VALUES(?,?,?,?,?,?)''',
                       (event_id,kind,call.trunk_call_id,call.queue,
                        call.agent_id if kind=='call.answered' else None,time.time()))
        if self.drain():
            raise RuntimeError('Native event delivery delayed; stored for replay')

    def drain(self, limit: int = 100) -> int:
        if not 1 <= limit <= 1000:
            raise ValueError('Invalid drain batch size')
        with connection() as db:
            pending=[dict(x) for x in db.execute('''SELECT * FROM native_event_outbox
                               WHERE delivered_at IS NULL ORDER BY seq LIMIT ?''',(limit,))]
        for row in pending:
            try:
                event=EngineEvent(source='queuepilot-native',event_id=row['event_id'],type=row['event_type'],
                                  call_id=row['call_id'],queue_id=row['queue_id'],agent_id=row['agent_id'],
                                  occurred_at=datetime.fromtimestamp(row['occurred_at'],timezone.utc))
                ingest(event)  # idempotent event_id: safe if prior delivery succeeded but ACK failed.
            except Exception as exc:
                with connection() as db:
                    db.execute('''UPDATE native_event_outbox SET attempts=attempts+1,last_error=?
                                  WHERE seq=? AND delivered_at IS NULL''',(type(exc).__name__,row['seq']))
                break  # Never reorder per-call lifecycle events past a failed earlier event.
            else:
                with connection() as db:
                    db.execute('''UPDATE native_event_outbox SET delivered_at=?,attempts=attempts+1,
                                  last_error=NULL WHERE seq=? AND delivered_at IS NULL''',(time.time(),row['seq']))
        with connection() as db:
            return db.execute('SELECT COUNT(*) FROM native_event_outbox WHERE delivered_at IS NULL').fetchone()[0]

    def sync_controls(self, server) -> None:
        """Apply latest admin requests only if idle; execute under NativeSwitch lock.

        Snapshot reads are local, no network operations. Never force ringing/talking
        states. Commands remain pending through an active call and are retried.
        """
        with connection() as db:
            qs=[dict(x) for x in db.execute('SELECT id,enabled,skill FROM queues')]
            commands=[dict(x) for x in db.execute('''SELECT c.* FROM native_agent_commands c
                            JOIN native_agent_bindings b ON b.agent_id=c.agent_id
                            WHERE c.revision>c.applied_revision ORDER BY c.requested_at''')]
        server.enabled_queues={q['id'] for q in qs if q['enabled']}
        server.queue_skill_overrides={q['id']:frozenset([q['skill']]) for q in qs if q['skill']}
        for cmd in commands:
            agent=server.acd.agents.get(cmd['agent_id'])
            if agent is None:
                continue
            extension=server.plan.agent_extensions.get(agent.id)
            unregistered=cmd['desired_state']=='ready' and (not extension or not server.directory.lookup(extension))
            if unregistered or agent.assigned_call is not None or agent.state in ('ringing','talking','acw'):
                # Deferral includes ACW, which the call engine controls; don't interrupt it.
                with connection() as db:
                    db.execute('''UPDATE native_agent_commands SET state='deferred'
                                  WHERE agent_id=? AND revision=?''',(cmd['agent_id'],cmd['revision']))
                continue
            server.acd.set_agent_state(agent.id,cmd['desired_state'])
            actual={'ready':'available','aux':'away','offline':'offline'}[cmd['desired_state']]
            with connection() as db:
                # A superseding admin request may arrive during sync; keep it pending.
                applied=db.execute('''UPDATE native_agent_commands SET applied_revision=?,
                                      state='applied',applied_at=? WHERE agent_id=? AND revision=?''',
                                   (cmd['revision'],time.time(),agent.id,cmd['revision']))
                if applied.rowcount:
                    db.execute('UPDATE agents SET status=?,last_change=? WHERE id=?',
                               (actual,time.time(),agent.id))
        # The runtime (not an admin directory value) owns actual availability.
        # No SIP registration => not Available even when the ACD policy is Ready.
        states={'ready':'available','ringing':'busy','talking':'busy',
                'acw':'acw','aux':'away','offline':'offline'}
        observed=[]
        for agent_id,agent in server.acd.agents.items():
            state=states[agent.state]
            if agent.state=='ready':
                extension=server.plan.agent_extensions.get(agent_id)
                if not extension or not server.directory.lookup(extension):
                    state='offline'
            observed.append((state,time.time(),agent_id,state))
        with connection() as db:
            for status,at,agent_id,expected in observed:
                db.execute('''UPDATE agents SET status=?,last_change=?
                           WHERE id=? AND status<>?''',(status,at,agent_id,expected))

    def sync_call_controls(self, server) -> None:
        """Consume local hold/resume intents only under the native switch lock.

        Commands are one-shot, never blindly replayed after a crash. Any old or
        invalid call is rejected, not applied to a reused SIP Call-ID.
        """
        with connection() as db:
            pending=[dict(row) for row in db.execute("""SELECT * FROM native_call_controls
                                     WHERE state='pending' ORDER BY requested_at,id LIMIT 100""")]
        for cmd in pending:
            call=server.calls.get(cmd['call_id'])
            if call is None or cmd['requested_at'] < call.started_wall - 0.25:
                result='call_not_active_or_stale'
                state='rejected'
            else:
                try:
                    server.apply_call_control(cmd['call_id'],cmd['action'])
                    result='native_rtp_lab'
                    state='applied'
                except (ValueError, RuntimeError, OSError):
                    result='call_not_bridged_or_invalid_state'
                    state='rejected'
            with connection() as db:
                db.execute("""UPDATE native_call_controls SET state=?,processed_at=?,result=?
                              WHERE id=? AND state='pending' """,
                           (state,time.time(),result,cmd['id']))
                audit(db,cmd['actor'],'native_call.control_'+state,cmd['call_id'],
                      f'action={cmd["action"]};command={cmd["id"]};reason={result}')

    def archive_media(self, call, paths: dict[str, Path]) -> list[str]:
        if not paths or self.recording_capture_dir is None:
            return []
        return NativeRecordingArchive(self.recording_capture_dir,
                                      notice_confirmed=self.recording_notice_confirmed).store(call.trunk_call_id,paths)

    def heartbeat(self, connected: bool) -> None:
        with connection() as db:
            db.execute('''INSERT INTO engine_heartbeats(source,engine_healthy,last_heartbeat,details)
                          VALUES('queuepilot-native',?,?,?) ON CONFLICT(source) DO UPDATE SET
                          engine_healthy=excluded.engine_healthy,
                          last_heartbeat=excluded.last_heartbeat,details=excluded.details''',
                       (int(connected),time.time(),'local SIP listener + SQLite outbox; lab only'))
