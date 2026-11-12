import sqlite3
from contextlib import contextmanager
from .config import config

@contextmanager
def connection():
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(config.db_path), timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=10000')
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def init_db():
    with connection() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
        CREATE TABLE IF NOT EXISTS queues (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, skill TEXT NOT NULL DEFAULT '',
            sla_seconds INTEGER NOT NULL DEFAULT 20 CHECK(sla_seconds > 0),
            target_percent INTEGER NOT NULL DEFAULT 80 CHECK(target_percent BETWEEN 1 AND 100),
            enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, extension TEXT NOT NULL DEFAULT '',
            queue_id TEXT REFERENCES queues(id),
            status TEXT NOT NULL DEFAULT 'offline', last_change REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS calls (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
            queue_id TEXT NOT NULL REFERENCES queues(id), caller TEXT NOT NULL DEFAULT 'Private caller',
            status TEXT NOT NULL, arrived_at REAL NOT NULL, answered_at REAL,
            ended_at REAL, agent_id TEXT REFERENCES agents(id), last_event_at REAL NOT NULL,
            UNIQUE(source, external_id)
        );
        CREATE INDEX IF NOT EXISTS idx_calls_queue_arrived ON calls(queue_id,arrived_at);
        CREATE INDEX IF NOT EXISTS idx_calls_status ON calls(status);
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT NOT NULL, source TEXT NOT NULL, event_type TEXT NOT NULL,
            call_id TEXT, agent_id TEXT, occurred_at REAL NOT NULL, received_at REAL NOT NULL,
            outcome TEXT NOT NULL, PRIMARY KEY (source,event_id)
        );
        CREATE INDEX IF NOT EXISTS idx_events_occurred ON events(occurred_at);
        -- Local-only runtime coordination: never a remote PBX command channel.
        CREATE TABLE IF NOT EXISTS native_agent_bindings (
            agent_id TEXT PRIMARY KEY REFERENCES agents(id),
            extension TEXT NOT NULL, bound_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS native_agent_commands (
            agent_id TEXT PRIMARY KEY REFERENCES agents(id),
            desired_state TEXT NOT NULL CHECK(desired_state IN ('ready','aux','offline')),
            revision INTEGER NOT NULL DEFAULT 1,
            applied_revision INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','applied','deferred')),
            requested_at REAL NOT NULL, applied_at REAL
        );
        -- Supervisor-requested, local-only server-side RTP hold/resume.
        -- Not an Avaya command and not yet RFC 3264 SIP endpoint hold.
        CREATE TABLE IF NOT EXISTS native_call_controls (
            id TEXT PRIMARY KEY,
            call_id TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('hold','resume')),
            actor TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','applied','rejected')),
            requested_at REAL NOT NULL,
            processed_at REAL,
            result TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_native_call_control_pending
            ON native_call_controls(call_id) WHERE state='pending';
        CREATE INDEX IF NOT EXISTS idx_native_call_control_recent
            ON native_call_controls(requested_at DESC);
        -- A persisted event outbox allows replay after a transient DB ingest failure.
        -- No caller identity or audio is held in the journal.
        CREATE TABLE IF NOT EXISTS native_event_outbox (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            call_id TEXT NOT NULL,
            queue_id TEXT NOT NULL,
            agent_id TEXT,
            occurred_at REAL NOT NULL,
            delivered_at REAL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_native_outbox_pending ON native_event_outbox(delivered_at,seq);
        CREATE TABLE IF NOT EXISTS engine_heartbeats (
            source TEXT PRIMARY KEY, engine_healthy INTEGER NOT NULL,
            last_heartbeat REAL NOT NULL, details TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
            actor TEXT NOT NULL, operation TEXT NOT NULL,
            entity_id TEXT NOT NULL, details TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS recordings (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, external_call_id TEXT NOT NULL,
            call_id TEXT NOT NULL REFERENCES calls(id), original_name TEXT NOT NULL,
            mime_type TEXT NOT NULL, file_path TEXT NOT NULL, bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL, recorded_at REAL NOT NULL, duration_seconds REAL,
            consent_attested INTEGER NOT NULL, created_at REAL NOT NULL,
            UNIQUE(source, external_call_id, sha256)
        );
        CREATE INDEX IF NOT EXISTS idx_recordings_call ON recordings(call_id,recorded_at);
        -- New native engine stores precisely one conversation WAV per call.
        -- Legacy per-leg v0.8 recordings remain untouched for history.
        CREATE UNIQUE INDEX IF NOT EXISTS uq_native_conversation_per_call
            ON recordings(source,external_call_id)
            WHERE source='queuepilot-native-conversation';
        CREATE TABLE IF NOT EXISTS monitor_sessions (
            id TEXT PRIMARY KEY, call_id TEXT NOT NULL REFERENCES calls(id),
            target_station TEXT NOT NULL, created_by TEXT NOT NULL,
            state TEXT NOT NULL, created_at REAL NOT NULL, approved_at REAL,
            leased_at REAL, active_at REAL, stopped_at REAL,
            result_code TEXT, result_message TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_monitor_sessions_state ON monitor_sessions(state, created_at);
        CREATE TABLE IF NOT EXISTS ivr_flows (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
            graph TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft', version INTEGER NOT NULL DEFAULT 1,
            created_by TEXT NOT NULL, updated_by TEXT NOT NULL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        ''')

    # Safe migration of *audit history only*. Obsolete PBX tables in existing databases
    # are retained (not used) to avoid destructive upgrades without an operator backup.
    with connection() as db:
        legacy = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='aura_audit'").fetchone()
        if legacy:
            db.execute('''INSERT OR IGNORE INTO audit_log(id,at,actor,operation,entity_id,details)
                          SELECT id,at,actor,operation,entity_id,details FROM aura_audit''')
