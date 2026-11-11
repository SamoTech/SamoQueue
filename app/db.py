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
        CREATE TABLE IF NOT EXISTS connector_heartbeats (
            source TEXT PRIMARY KEY, cti_connected INTEGER NOT NULL,
            last_heartbeat REAL NOT NULL, details TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS aura_queue_bindings (
            queue_id TEXT PRIMARY KEY REFERENCES queues(id) ON DELETE CASCADE,
            vdn TEXT UNIQUE, skill_number TEXT UNIQUE, vector_number TEXT,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS aura_agent_bindings (
            agent_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
            station TEXT UNIQUE NOT NULL, login_id TEXT UNIQUE,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS aura_commands (
            id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE,
            action TEXT NOT NULL, station TEXT NOT NULL, call_id TEXT,
            destination TEXT, agent_login TEXT, desired_state TEXT,
            status TEXT NOT NULL, created_by TEXT NOT NULL,
            created_at REAL NOT NULL, approved_at REAL, leased_at REAL,
            finished_at REAL, result_code TEXT, result_message TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_aura_commands_state ON aura_commands(status,approved_at);
        CREATE TABLE IF NOT EXISTS aura_audit (
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
        CREATE TABLE IF NOT EXISTS aura_routing_plans (
            id TEXT PRIMARY KEY, queue_id TEXT NOT NULL REFERENCES queues(id),
            change_type TEXT NOT NULL, desired TEXT NOT NULL,
            reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'proposed',
            created_at REAL NOT NULL, created_by TEXT NOT NULL
        );
        ''')
