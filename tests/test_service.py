import datetime as dt
import os
import tempfile
from pathlib import Path

os.environ['APP_ADMIN_PASSWORD'] = 'testing-password-not-production'
os.environ['APP_SECRET'] = 'test-secret-key-at-least-thirty-two-characters-long-123'
os.environ['ENGINE_EVENT_KEY'] = 'test-ingestion-key-at-least-thirty-two-characters-long'
os.environ['APP_DEMO_MODE'] = 'true'

from fastapi.testclient import TestClient
from app.config import config
from app.main import app
from app.service import create_queue, create_agent


def test_workflow(tmp_path):
    config.db_path = tmp_path / 'test.sqlite'
    config.admin_password = 'testing-password-not-production'
    config.secret = 'test-secret-key-at-least-thirty-two-characters-long-123'
    config.engine_event_key = 'test-ingestion-key-at-least-thirty-two-characters-long'
    config.demo_mode = True
    with TestClient(app) as client:
        assert client.get('/api/health').status_code == 200
        assert client.get('/api/dashboard').status_code == 401
        login = client.post('/api/login', json={'username':'admin','password':'testing-password-not-production'})
        assert login.status_code == 200
        auth = {'Authorization':'Bearer '+login.json()['token']}
        q = client.post('/api/queues',json={'name':'Care team','skill':'CARE'},headers=auth)
        assert q.status_code == 201, q.text
        qid=q.json()['id']
        agent = client.post('/api/agents',json={'name':'Alice Example','extension':'100','queue_id':qid,'status':'available'},headers=auth)
        assert agent.status_code==201
        aid=agent.json()['id']
        assert client.post('/api/engine/events',json={}).status_code == 401
        key={'X-Engine-Key':config.engine_event_key}
        now=dt.datetime.now(dt.timezone.utc)
        event={'source':'queuepilot-native','event_id':'a1','type':'call.queued','call_id':'call-1','queue_id':qid,
               'caller':'+12025550145','occurred_at':now.isoformat()}
        r=client.post('/api/engine/events',json=event,headers=key)
        assert r.status_code == 200,r.text
        assert r.json()['outcome'] == 'created'
        assert client.post('/api/engine/events',json=event,headers=key).json()['outcome']=='duplicate'
        data=client.get('/api/dashboard',headers=auth).json()
        assert data['totals']['waiting']==1
        assert data['calls'][0]['caller'].endswith('0145')
        assert '+12025550145' not in data['calls'][0]['caller']
        answered={**event,'event_id':'a2','type':'call.answered','agent_id':aid,
                  'occurred_at':(now+dt.timedelta(seconds=10)).isoformat()}
        assert client.post('/api/engine/events',json=answered,headers=key).status_code==200
        completed={**event,'event_id':'a3','type':'call.completed',
                   'occurred_at':(now+dt.timedelta(seconds=20)).isoformat()}
        assert client.post('/api/engine/events',json=completed,headers=key).status_code==200
        data=client.get('/api/dashboard',headers=auth).json()
        assert data['totals']['waiting']==0
        assert data['totals']['answered']==1
        assert data['totals']['sla_percent']==100
        assert client.get('/api/reports/calls.csv',headers=auth).status_code==200
        heartbeat=client.post('/api/engine/heartbeat', json={'source':'queuepilot-native','connected':True}, headers=key)
        assert heartbeat.status_code==200
        assert client.get('/api/dashboard',headers=auth).json()['integration']['connected'] is True
        assert client.patch('/api/agents/'+aid+'/status',json={'status':'busy'},headers=auth).json()['scope']=='directory_only_unmapped'


def test_demo_seed(tmp_path):
    config.db_path=tmp_path/'demo.sqlite'
    config.demo_mode=True
    with TestClient(app) as client:
        token=client.post('/api/login',json={'username':'admin','password':config.admin_password}).json()['token']
        auth={'Authorization':'Bearer '+token}
        seed=client.post('/api/demo/seed',headers=auth)
        assert seed.status_code==200,seed.text
        assert seed.json()['seeded'] is True
        assert len(client.get('/api/dashboard',headers=auth).json()['queues'])==4
        assert client.post('/api/demo/seed',headers=auth).json()['seeded'] is False
        assert client.post('/api/demo/step',headers=auth).status_code==200
