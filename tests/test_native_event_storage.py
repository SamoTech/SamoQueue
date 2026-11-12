"""End-to-end management persistence tests, independent from any vendor CTI API."""
from datetime import datetime, timezone
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import config
from app.db import connection, init_db
from app.main import app
from app.service import create_agent, create_queue, dashboard
from telephony.local_events import LocalEventWriter
from telephony.native_sip import IngressRoute
from telephony.native_switch import Config, NativeSwitch


def test_local_sip_events_reach_dashboard_and_reports(tmp_path):
    config.db_path=tmp_path/'native.sqlite'
    config.admin_password='test-supervisor-password-long'
    config.secret='S'*48
    config.engine_event_key='E'*48
    config.demo_mode=False
    init_db()
    queue=create_queue('Native Support')
    agent=create_agent('SIP Agent','1001',queue)
    writer=LocalEventWriter([IngressRoute('6000',queue)],{agent:('1001',frozenset())})
    writer.heartbeat(True)
    call=SimpleNamespace(trunk_call_id='lab-call-345',queue=queue,agent_id=agent)
    for kind in ('call.queued','call.answered','call.completed'):
        writer(kind,call)
    assert dashboard()['totals']['answered']==1
    assert dashboard()['integration']['connected'] is True
    with TestClient(app) as c:
        token=c.post('/api/login',json={'username':'admin','password':config.admin_password}).json()['token']
        auth={'Authorization':'Bearer '+token}
        data=c.get('/api/cc/reports/calls',headers=auth).json()
        assert len(data['rows'])==1 and data['rows'][0]['call_id']=='lab-call-345'
        assert c.get('/api/aura/readiness',headers=auth).status_code==404
        assert c.post('/api/integrations/events',headers={'X-Integration-Key':'x'}).status_code==404
        assert c.post('/api/engine/events',headers={'X-Engine-Key':config.engine_event_key},json={
            'source':'aura-aes','event_id':'old-source','type':'call.queued','call_id':'old','queue_id':queue,
            'occurred_at':datetime.now(timezone.utc).isoformat()
        }).status_code==422
    writer.heartbeat(False)
    assert dashboard()['integration']['connected'] is False


def test_native_writer_rejects_unknown_routes_and_agents(tmp_path):
    config.db_path=tmp_path/'refuse.sqlite'
    init_db()
    queue=create_queue('Native Support')
    with pytest.raises(ValueError,match='unknown QueuePilot queue'):
        LocalEventWriter([IngressRoute('6000','not-in-local-db')],{})
    with pytest.raises(ValueError,match='agent mapping'):
        LocalEventWriter([IngressRoute('6000',queue)],{'not-in-db':('1001',frozenset())})


def test_preserve_legacy_audit_without_recreating_legacy_schema(tmp_path):
    config.db_path=tmp_path/'old.sqlite'
    with connection() as db:
        db.execute('''CREATE TABLE aura_audit(id INTEGER PRIMARY KEY,at REAL,actor TEXT,operation TEXT,
                                             entity_id TEXT,details TEXT)''')
        db.execute("INSERT INTO aura_audit VALUES(1,1,'old','historical','id','keep')")
    init_db();init_db()
    with connection() as db:
        assert db.execute('SELECT count(*) FROM audit_log').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM aura_audit').fetchone()[0]==1
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='aura_commands'").fetchone()
