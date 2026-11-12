"""End-to-end QueuePilot on-host runtime control and durable event outbox tests."""
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import config
from app.db import connection, init_db
from app.main import app
from app.service import create_agent, create_queue, dashboard
from telephony.local_events import LocalEventWriter
from telephony.native_sip import IngressRoute, PhoneContact
from telephony.native_switch import Config, NativeSwitch


def setup_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(config,'db_path',tmp_path/'sync.sqlite')
    monkeypatch.setattr(config,'admin_password','strong-test-dashboard-password')
    monkeypatch.setattr(config,'secret','S'*64)
    monkeypatch.setattr(config,'demo_mode',False)
    init_db()
    queue=create_queue('Support')
    agent=create_agent('Local SIP Agent','1001',queue, status='offline')
    routes=(IngressRoute('6000',queue,frozenset({'english'})),)
    bindings={agent:('1001',frozenset({'english'}))}
    writer=LocalEventWriter(routes,bindings)
    switch=NativeSwitch(Config('127.0.0.1',15060,'127.0.0.1',('127.0.0.1/32',),('127.0.0.1/32',),('127.0.0.1/32',),
        routes,bindings,{'1001':'correct-long-sip-password'},'queuepilot.local',b'x'*40),writer)
    client=TestClient(app)
    client.__enter__()
    token=client.post('/api/login',json={'username':'admin','password':config.admin_password}).json()['token']
    return queue,agent,writer,switch,client,{'Authorization':'Bearer '+token}


def test_admin_commands_are_durable_deferred_and_applied_when_agent_idle(tmp_path,monkeypatch):
    queue,agent,writer,switch,client,auth=setup_runtime(tmp_path,monkeypatch)
    try:
        assert client.patch(f'/api/agents/{agent}/status',headers=auth,json={'status':'busy'}).status_code==422
        r=client.patch(f'/api/agents/{agent}/status',headers=auth,json={'status':'available'})
        assert r.status_code==200 and r.json()['scope']=='pending_native_runtime'
        assert dashboard()['agents'][0]['control_state']=='pending'
        # Phone not registered yet: cannot falsely claim the operator is ready.
        writer.sync_controls(switch)
        assert dashboard()['agents'][0]['control_state']=='deferred'
        assert dashboard()['agents'][0]['status']=='offline'
        switch.directory.provision(PhoneContact('1001','127.0.0.1',5060,expires_at=time.time()+500),verified=True)
        writer.sync_controls(switch)
        assert switch.acd.agents[agent].state=='ready'
        assert dashboard()['agents'][0]['status']=='available'
        assert dashboard()['agents'][0]['control_state']=='applied'
        # Offer the call to the registered agent and request AUX while ringing.
        switch.acd.enqueue('live-1',queue,{'english'})
        offered=switch.acd.dispatch()
        assert len(offered)==1 and offered[0].agent_id==agent
        r=client.patch(f'/api/agents/{agent}/status',headers=auth,json={'status':'away'})
        assert r.json()['revision']==2
        writer.sync_controls(switch)
        assert switch.acd.agents[agent].state=='ringing'
        assert dashboard()['agents'][0]['control_state']=='deferred'
        # No command is applied mid-call, including after call transitions into ACW.
        switch.acd.answer('live-1',agent)
        writer.sync_controls(switch)
        assert switch.acd.agents[agent].state=='talking'
        switch.acd.complete('live-1')
        writer.sync_controls(switch)
        assert switch.acd.agents[agent].state=='acw'
        switch.acd.agents[agent].acw_until=switch.acd.clock()-1
        switch.acd.tick()
        writer.sync_controls(switch)
        assert switch.acd.agents[agent].state=='aux'
        assert dashboard()['agents'][0]['status']=='away'
        assert dashboard()['agents'][0]['control_state']=='applied'
        with connection() as db:
            assert db.execute("SELECT count(*) FROM audit_log WHERE operation='native_agent.status_requested'").fetchone()[0]==2
        # Restart of persistence adapter does not lose the last acknowledged state.
        second=LocalEventWriter((IngressRoute('6000',queue),),{agent:('1001',frozenset())})
        assert second.drain()==0
        assert dashboard()['agents'][0]['control_state']=='applied'
    finally:
        client.__exit__(None,None,None)


def test_queue_disable_and_skill_settings_sync_to_native_scheduler(tmp_path,monkeypatch):
    q,a,writer,switch,client,auth=setup_runtime(tmp_path,monkeypatch)
    try:
        with connection() as db:
            db.execute("UPDATE queues SET enabled=0,skill='spanish' WHERE id=?",(q,))
        writer.sync_controls(switch)
        assert q not in switch.enabled_queues
        assert switch.queue_skill_overrides[q]==frozenset({'spanish'})
        with connection() as db:
            db.execute('UPDATE queues SET enabled=1 WHERE id=?',(q,))
        writer.sync_controls(switch)
        assert q in switch.enabled_queues
        assert client.put(f'/api/queues/{q}',headers=auth,json={
            'name':'Support','skill':'english','sla_seconds':30,'target_percent':80,'enabled':False
        }).status_code==200
        writer.sync_controls(switch)
        assert q not in switch.enabled_queues
        with connection() as db:
            assert db.execute("SELECT count(*) FROM audit_log WHERE operation='queue.configuration.updated'").fetchone()[0]==1
    finally:
        client.__exit__(None,None,None)


def test_event_outbox_replays_without_duplicate_after_transient_failure(tmp_path,monkeypatch):
    q,a,writer,switch,client,auth=setup_runtime(tmp_path,monkeypatch)
    import telephony.local_events as module
    original=module.ingest
    def fail(_):raise OSError('Transient sqlite failure')
    monkeypatch.setattr(module,'ingest',fail)
    call=SimpleNamespace(trunk_call_id='native-call-9',queue=q,agent_id=a)
    try:
        with pytest.raises(RuntimeError,match='stored for replay'):
            writer('call.queued',call)
        with connection() as db:
            row=db.execute('SELECT event_id,delivered_at,attempts FROM native_event_outbox').fetchone()
            assert row and row['delivered_at'] is None and row['attempts']==1
        monkeypatch.setattr(module,'ingest',original)
        restarted=LocalEventWriter((IngressRoute('6000',q),),{a:('1001',frozenset())})
        assert restarted.drain()==0
        with connection() as db:
            assert db.execute('SELECT count(*) FROM calls WHERE external_id=?',('native-call-9',)).fetchone()[0]==1
            assert db.execute('SELECT count(*) FROM events WHERE event_id=?',(row['event_id'],)).fetchone()[0]==1
            # Simulate crash after ingest succeeded but before marking the outbox delivered.
            db.execute('UPDATE native_event_outbox SET delivered_at=NULL')
        assert restarted.drain()==0
        with connection() as db:
            assert db.execute('SELECT count(*) FROM events WHERE event_id=?',(row['event_id'],)).fetchone()[0]==1
        restarted('call.answered',call)
        restarted('call.completed',call)
        assert dashboard()['totals']['answered']==1
    finally:
        client.__exit__(None,None,None)


def test_disabled_queue_rejects_invite_before_allocating_media(tmp_path, monkeypatch):
    """An admin-disabled queue cannot open a new SIP dialog or RTP socket."""
    import asyncio
    from telephony.sip_stack import packet, parse_message, sdp_offer
    q,a,writer,switch,client,auth=setup_runtime(tmp_path,monkeypatch)
    try:
        with connection() as db:
            db.execute('UPDATE queues SET enabled=0 WHERE id=?',(q,))
        writer.sync_controls(switch)
        received=[]
        switch.send=lambda data,addr: received.append(parse_message(data))
        incoming=packet('INVITE sip:6000@127.0.0.1 SIP/2.0',[
            ('Via','SIP/2.0/UDP 127.0.0.1:5050;branch=z9hG4bKtest'),
            ('From','<sip:caller@127.0.0.1>;tag=orig'),('To','<sip:6000@127.0.0.1>'),
            ('Call-ID','disabled-call'),('CSeq','1 INVITE'),('Content-Type','application/sdp'),
        ],sdp_offer('127.0.0.1',40000,(0,)))
        asyncio.run(switch._invite(parse_message(incoming),('127.0.0.1',5050)))
        assert received and received[-1].start.startswith('SIP/2.0 503')
        assert switch.calls=={} and switch.acd.calls=={}
    finally:
        client.__exit__(None,None,None)


def test_native_presence_tracks_registration_and_unexpired_contact(tmp_path,monkeypatch):
    q,a,writer,switch,client,auth=setup_runtime(tmp_path,monkeypatch)
    try:
        writer.sync_controls(switch)
        assert dashboard()['agents'][0]['status']=='offline'
        switch.directory.provision(PhoneContact('1001','127.0.0.1',5060,expires_at=time.time()+200),verified=True)
        writer.sync_controls(switch)
        assert dashboard()['agents'][0]['status']=='available'
        switch.directory.revoke('1001')
        writer.sync_controls(switch)
        assert dashboard()['agents'][0]['status']=='offline'
    finally:
        client.__exit__(None,None,None)
