"""End-to-end tests for the Aura mapped-event and guarded-command stages."""
import datetime as dt
import os
import uuid

os.environ['APP_ADMIN_PASSWORD'] = 'testing-password-not-production'
os.environ['APP_SECRET'] = 'test-secret-key-at-least-thirty-two-characters-long-123'
os.environ['AVAYA_INGEST_KEY'] = 'test-ingestion-key-at-least-thirty-two-characters-long'
os.environ['APP_DEMO_MODE'] = 'true'

from fastapi.testclient import TestClient
from app.main import app
from app.config import config
from app.db import connection


def setup_system(tmp_path):
    config.db_path = tmp_path / 'aura.sqlite'
    config.admin_password = 'testing-password-not-production'
    config.secret = 'test-secret-key-at-least-thirty-two-characters-long-123'
    config.ingest_key = 'test-ingestion-key-at-least-thirty-two-characters-long'
    config.aura_command_key = 'test-command-bridge-key-at-least-thirty-two-characters'
    config.aura_control_enabled = False
    client = TestClient(app)
    client.__enter__()
    r = client.post('/api/login',json={'username':'admin','password':config.admin_password})
    admin = {'Authorization':'Bearer '+r.json()['token']}
    bridge = {'X-Integration-Key':config.ingest_key}
    control_bridge = {'X-Aura-Command-Key': config.aura_command_key}
    queue = client.post('/api/queues',json={'name':'Support skill'},headers=admin).json()['id']
    agent = client.post('/api/agents',json={'name':'Support Agent','extension':'4101','queue_id':queue},headers=admin).json()['id']
    return client,admin,bridge,control_bridge,queue,agent


def bind(client,admin,queue,agent):
    assert client.put(f'/api/aura/queues/{queue}/binding',json={'vdn':'65001','skill_number':'87','vector_number':'44'},headers=admin).status_code==200
    assert client.put(f'/api/aura/agents/{agent}/binding',json={'station':'4101','login_id':'2024'},headers=admin).status_code==200


def test_aura_bindings_and_events(tmp_path):
    c,admin,bridge,control_bridge,q,a=setup_system(tmp_path)
    try:
        base={'event_id':'avaya-1','event_type':'call.queued','call_id':'c-1','vdn':'65001',
              'caller':'+201000001234','occurred_at':dt.datetime.now(dt.timezone.utc).isoformat()}
        r=c.post('/api/aura/events',json=base,headers=bridge)
        assert r.status_code==422 and 'unmapped' in r.text
        assert c.post('/api/aura/events',json=base).status_code==401
        bind(c,admin,q,a)
        assert c.post('/api/aura/events',json=base,headers=bridge).json()['outcome']=='created'
        assert c.post('/api/aura/events',json=base,headers=bridge).json()['outcome']=='duplicate'
        answered={**base,'event_id':'avaya-2','event_type':'call.answered','agent_station':'4101'}
        assert c.post('/api/aura/events',json=answered,headers=bridge).status_code==200
        d=c.get('/api/dashboard',headers=admin).json()
        assert d['totals']['active_calls']==1
        assert d['calls'][0]['caller']=='••• ••• 1234'
        # Same station/login resolved to one local agent, but conflicting aliases are rejected.
        a2=c.post('/api/agents',json={'name':'Other agent'},headers=admin).json()['id']
        c.put(f'/api/aura/agents/{a2}/binding',json={'station':'4102','login_id':'2025'},headers=admin)
        bad={**base,'event_id':'bad','event_type':'agent.status','agent_station':'4101','agent_login':'2025','agent_status':'available'}
        assert c.post('/api/aura/events',json=bad,headers=bridge).status_code==422
        assert c.put(f'/api/aura/agents/{a2}/binding',json={'station':'4101'},headers=admin).status_code==409
        state={**base,'event_id':'avaya-agent','event_type':'agent.status','agent_station':'4101',
               'agent_status':'available','occurred_at':(dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=2)).isoformat()}
        state.pop('call_id');state.pop('vdn')
        assert c.post('/api/aura/events',json=state,headers=bridge).status_code==200
        assert next(x for x in c.get('/api/dashboard',headers=admin).json()['agents'] if x['id']==a)['status']=='available'
    finally:c.__exit__(None,None,None)


def test_approval_queue_claim_result_and_no_retries(tmp_path):
    c,admin,bridge,control_bridge,q,a=setup_system(tmp_path)
    try:
        bind(c,admin,q,a)
        other=c.post('/api/agents',json={'name':'Transfer destination'},headers=admin).json()['id']
        assert c.put(f'/api/aura/agents/{other}/binding',json={'station':'4102'},headers=admin).status_code==200
        data={'request_key':'original-'+uuid.uuid4().hex,'action':'call.transfer','station':'4101','call_id':'abc','destination':'4102'}
        r=c.post('/api/aura/commands',json=data,headers=admin)
        assert r.status_code==201,r.text
        cid=r.json()['id']
        assert c.post('/api/aura/commands',json=data,headers=admin).json()['idempotent'] is True
        assert c.post('/api/aura/commands',json={**data,'destination':'4103'},headers=admin).status_code==409
        assert c.post(f'/api/aura/commands/{cid}/approve',json={'confirmation':'APPROVE AVAYA ACTION'},headers=admin).status_code==403
        config.aura_control_enabled=True
        assert c.post(f'/api/aura/commands/{cid}/approve',json={'confirmation':'wrong'},headers=admin).status_code==422
        assert c.post(f'/api/aura/commands/{cid}/approve',json={'confirmation':'APPROVE AVAYA ACTION'},headers=admin).json()['status']=='queued'
        assert c.post('/api/aura/bridge/claim',headers=bridge).status_code==401
        assert c.post('/api/aura/bridge/claim',headers=control_bridge).status_code==503
        assert c.post('/api/integrations/heartbeat',json={'source':'aura-aes','connected':True,'details':'test-only CTI stub'},headers=bridge).status_code==200
        claimed=c.post('/api/aura/bridge/claim',headers=control_bridge).json()['command']
        assert claimed['id']==cid
        assert claimed['action']=='call.transfer'
        assert c.post('/api/aura/bridge/claim',headers=control_bridge).json()['command'] is None
        result=c.post(f'/api/aura/bridge/commands/{cid}/result',json={'result':'succeeded','code':'AES_OK'},headers=control_bridge)
        assert result.json()['bridge_reported'] is True
        assert c.post(f'/api/aura/bridge/commands/{cid}/result',json={'result':'failed'},headers=control_bridge).status_code==409
        assert c.post('/api/aura/commands',json={**data,'request_key':'internal123','destination':'901234567890'},headers=admin).status_code==422
        assert c.post('/api/aura/commands',json={**data,'request_key':'nomap123','station':'9999'},headers=admin).status_code==422
        assert c.post('/api/aura/commands',json={**data,'request_key':'unknown123','destination':'4356'},headers=admin).status_code==422
        entries=c.get('/api/aura/audit',headers=admin).json()['entries']
        assert any(x['operation']=='command.succeeded' for x in entries)
    finally:
        config.aura_control_enabled=False
        c.__exit__(None,None,None)


def test_routing_plan_advisory_only(tmp_path):
    c,admin,bridge,control_bridge,q,a=setup_system(tmp_path)
    try:
        r=c.post('/api/aura/routing-plans',json={'queue_id':q,'change_type':'vector_steps',
                 'desired':'Add overflow after 30 seconds','reason':'Reduce long abandoned calls'},headers=admin)
        assert r.status_code==201 and r.json()['pbx_changed'] is False
        plans=c.get('/api/aura/routing-plans',headers=admin).json()
        assert plans['execution_supported'] is False
        assert plans['plans'][0]['status']=='proposed'
        info=c.get('/api/aura/readiness',headers=admin).json()
        assert info['routing_changes_automated'] is False
        assert info['pbx_connection_verified_independently'] is False
    finally:c.__exit__(None,None,None)
