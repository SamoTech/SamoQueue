"""Security and workflow regression for v0.3 contact-center features."""
import io
import os
import wave
import zipfile
from datetime import datetime, timezone

os.environ['APP_ADMIN_PASSWORD'] = 'testing-password-not-production'
os.environ['APP_SECRET'] = 'test-secret-key-at-least-thirty-two-characters-long-123'
os.environ['AVAYA_INGEST_KEY'] = 'test-ingestion-key-at-least-thirty-two-characters-long'

from fastapi.testclient import TestClient
from app.main import app
from app.config import config
from app.db import connection


def build(tmp_path):
    config.db_path = tmp_path / 'queue.db'
    config.admin_password = 'testing-password-not-production'
    config.secret = 'test-secret-key-at-least-thirty-two-characters-long-123'
    config.ingest_key = 'test-ingestion-key-at-least-thirty-two-characters-long'
    config.reviewer_user = 'reviewer'
    config.reviewer_password = 'separate-reviewer-password-at-least-12'
    config.recordings_enabled = True
    config.recording_bridge_key = 'record-key-at-least-32-chars-not-in-other-keys'
    config.recording_dir = tmp_path / 'media'
    config.monitoring_enabled = True
    config.monitor_bridge_key = 'monitor-key-at-least-32-chars-not-in-other-keys'
    config.aura_control_enabled = False
    with TestClient(app) as client:
        admin_login=client.post('/api/login',json={'username':'admin','password':config.admin_password})
        assert admin_login.status_code == 200, admin_login.text
        admin={'Authorization':'Bearer '+admin_login.json()['token']}
        reviewer_login=client.post('/api/login',json={'username':'reviewer','password':config.reviewer_password})
        assert reviewer_login.status_code == 200
        reviewer={'Authorization':'Bearer '+reviewer_login.json()['token']}
        assert client.get('/api/dashboard',headers=reviewer).status_code == 200
        queue=client.post('/api/queues',headers=admin,json={'name':'Customer support'}).json()['id']
        agent=client.post('/api/agents',headers=admin,json={'name':'QA Agent','extension':'4101','queue_id':queue}).json()['id']
        assert client.put(f'/api/aura/queues/{queue}/binding',headers=admin,json={'vdn':'65001','skill_number':'87'}).status_code==200
        assert client.put(f'/api/aura/agents/{agent}/binding',headers=admin,json={'station':'4101','login_id':'2024'}).status_code==200
        yield client,admin,reviewer,queue,agent
    config.monitoring_enabled=False
    config.recordings_enabled=False


def create_call(client,queue,agent,bridge,kind='answered'):
    now=datetime.now(timezone.utc).isoformat()
    call='cti-call-123'
    queued={'event_id':'e-q','event_type':'call.queued','call_id':call,'vdn':'65001','occurred_at':now}
    answered={'event_id':'e-a','event_type':'call.answered','call_id':call,'vdn':'65001','agent_station':'4101','occurred_at':now}
    assert client.post('/api/aura/events',json=queued,headers=bridge).status_code==200
    assert client.post('/api/aura/events',json=answered,headers=bridge).status_code==200
    with connection() as db:
        return db.execute('SELECT id FROM calls WHERE external_id=?',(call,)).fetchone()['id']


def wav_bytes():
    buf=io.BytesIO()
    with wave.open(buf,'wb') as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000)
        f.writeframes(bytes(32000))
    return buf.getvalue()


def test_recording_import_playback_dedup_auth_and_validation(tmp_path):
    for c,admin,reviewer,q,a in build(tmp_path):
        bridge={'X-Integration-Key':config.ingest_key}
        create_call(c,q,a,bridge)
        path='/api/cc/recordings/import?source=aura-aes&call_id=cti-call-123&consent_attested=true'
        auth={'X-Recording-Bridge-Key':config.recording_bridge_key,'Content-Type':'audio/wav'}
        audio=wav_bytes()
        assert c.post(path,content=audio).status_code==401
        assert c.post(path.replace('true','false'),headers=auth,content=audio).status_code==422
        assert c.post(path,headers={**auth,'Content-Type':'text/html'},content=b'<html>xx</html>').status_code==415
        first=c.post(path,headers=auth,content=audio)
        assert first.status_code==201,first.text
        second=c.post(path,headers=auth,content=audio)
        assert second.status_code==201 and second.json()['duplicate']
        rid=first.json()['id']
        assert len(c.get('/api/cc/recordings',headers=admin).json()['recordings'])==1
        assert c.get('/api/cc/recordings',headers=reviewer).status_code==200
        assert c.get('/api/cc/recordings/'+rid+'/audio').status_code==401
        played=c.get('/api/cc/recordings/'+rid+'/audio',headers=reviewer)
        assert played.status_code==200 and played.content==audio
        assert played.headers['cache-control']=='no-store'
        assert c.get('/api/cc/recordings/'+rid+'/audio?download=true',headers=admin).headers['content-disposition'].startswith('attachment;')
        assert c.post(path,headers=auth,content=b'not an audio file').status_code==422
        assert not list(config.recording_dir.glob('*.part'))
        with connection() as db:
            events=[x['operation'] for x in db.execute('SELECT operation FROM aura_audit')]
        assert 'recording.imported' in events and 'recording.playback' in events


def test_ivr_graph_validation_approval_and_report_exports(tmp_path):
    for c,admin,reviewer,q,a in build(tmp_path):
        graph={'nodes':[
            {'id':'entry','type':'start','label':'Inbound','next':'menu'},
            {'id':'menu','type':'menu','label':'Menu','prompt':'Press 1 or 0','choices':{'1':'queue','0':'end'}},
            {'id':'queue','type':'queue','label':'Customer service','queue_id':q},
            {'id':'end','type':'hangup','label':'Bye'}]}
        body={'name':'Main IVR','graph':graph,'description':'business hours inbound'}
        assert c.post('/api/cc/ivr',json=body,headers=reviewer).status_code==403
        r=c.post('/api/cc/ivr',json=body,headers=admin)
        assert r.status_code==201,r.text
        fid=r.json()['id']
        bad={'name':'Bad menu','graph':{'nodes':[{'id':'entry','type':'start','label':'entry','next':'missing'},
            {'id':'end','type':'hangup','label':'end'}]}}
        assert c.post('/api/cc/ivr',json=bad,headers=admin).status_code==422
        assert c.post(f'/api/cc/ivr/{fid}/propose-deployment',json={'confirmation':'PROPOSE IVR DEPLOYMENT'},headers=admin).json()['pbx_changed'] is False
        assert c.put(f'/api/cc/ivr/{fid}',json=body,headers=admin).status_code==409
        all_zip=c.get('/api/cc/reports/all.zip',headers=reviewer)
        assert all_zip.status_code==200
        with zipfile.ZipFile(io.BytesIO(all_zip.content)) as bundle:
            assert len(bundle.namelist())==10
            assert 'ivr.csv' in bundle.namelist() and b'Main IVR' in bundle.read('ivr.csv')
        assert c.get('/api/cc/reports/sla',headers=reviewer).status_code==200
        assert c.get('/api/cc/reports/queues.csv',headers=reviewer).status_code==200
        assert c.get('/api/cc/reports/missing',headers=admin).status_code==404
        assert c.get('/api/cc/reports/calls?start=2020-01-01T00:00:00Z&end=2026-11-01T00:00:00Z',headers=admin).status_code==422
        assert c.get('/api/cc/reports/calls.csv').status_code==401


def test_monitoring_requires_second_identity_and_real_bridge_ack(tmp_path):
    for c,admin,reviewer,q,a in build(tmp_path):
        bridge={'X-Integration-Key':config.ingest_key}
        monitoring_bridge={'X-Monitor-Bridge-Key':config.monitor_bridge_key}
        callid=create_call(c,q,a,bridge)
        res=c.post('/api/cc/monitor/sessions',json={'call_id':callid},headers=admin)
        assert res.status_code==201,res.text
        sid=res.json()['id']
        assert c.post(f'/api/cc/monitor/sessions/{sid}/approve',json={'confirmation':'APPROVE LIVE LISTEN'},headers=admin).status_code==403
        assert c.post(f'/api/cc/monitor/sessions/{sid}/approve',json={'confirmation':'NO'},headers=reviewer).status_code==422
        assert c.post(f'/api/cc/monitor/sessions/{sid}/approve',json={'confirmation':'APPROVE LIVE LISTEN'},headers=reviewer).status_code==200
        assert c.post('/api/cc/monitor/bridge/claim',headers=bridge).status_code==401
        assert c.post('/api/cc/monitor/bridge/claim',headers=monitoring_bridge).status_code==503
        assert c.post('/api/integrations/heartbeat',json={'source':'aura-aes','connected':True,'details':'test stub'},headers=bridge).status_code==200
        claimed=c.post('/api/cc/monitor/bridge/claim',headers=monitoring_bridge).json()['command']
        assert claimed['id']==sid and claimed['action']=='start_listen'
        assert c.post('/api/cc/monitor/bridge/claim',headers=monitoring_bridge).json()['command'] is None
        assert c.post(f'/api/cc/monitor/sessions/{sid}/audio-ticket',headers=reviewer).status_code==409
        assert c.post(f'/api/cc/monitor/bridge/sessions/{sid}/result',headers=monitoring_bridge,json={'result':'active','code':'DMCC_CONFIRMED'}).status_code==200
        assert c.post(f'/api/cc/monitor/sessions/{sid}/audio-ticket',headers=admin).status_code==403
        ticket=c.post(f'/api/cc/monitor/sessions/{sid}/audio-ticket',headers=reviewer)
        assert ticket.status_code==200,ticket.text
        t=ticket.json()['ticket']
        with c.websocket_connect(f'/api/cc/monitor/sessions/{sid}/audio/ws?ticket={t}') as ws:
            packet=bytes(3200)
            result=c.post(f'/api/cc/monitor/bridge/sessions/{sid}/audio',headers={**monitoring_bridge,'Content-Type':'application/octet-stream'},content=packet)
            assert result.status_code==202,result.text
            assert result.json()['browser_listeners']==1
            assert ws.receive_bytes()==packet
        assert c.post(f'/api/cc/monitor/sessions/{sid}/audio-ticket',headers=reviewer).status_code==200
        # CTI call completion revokes playback and creates an Avaya observer-stop instruction.
        end={'event_id':'e-completed','event_type':'call.completed','call_id':'cti-call-123',
             'vdn':'65001','occurred_at':datetime.now(timezone.utc).isoformat()}
        assert c.post('/api/aura/events',json=end,headers=bridge).status_code==200
        assert c.post(f'/api/cc/monitor/sessions/{sid}/audio-ticket',headers=reviewer).status_code==409
        stop=c.post('/api/cc/monitor/bridge/claim',headers=monitoring_bridge).json()['command']
        assert stop['action']=='stop_listen'
        assert c.post(f'/api/cc/monitor/bridge/sessions/{sid}/result',json={'result':'ended'},headers=monitoring_bridge).status_code==200
        assert c.get('/api/cc/monitor/sessions',headers=reviewer).json()['sessions'][0]['state']=='ended'
