"""Native SIP lab recording-to-library tests, with no external PBX or CTI."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
import wave

import pytest
from fastapi.testclient import TestClient

from app.config import config
from app.db import connection, init_db
from app.main import app
from app.service import create_agent, create_queue
from telephony.local_events import LocalEventWriter
from telephony.native_sip import IngressRoute
from telephony.recording_archive import NativeRecordingArchive
from telephony.native_switch import Config


def _make_wav(path: Path):
    with wave.open(str(path),'wb') as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(bytes(640))


def _setup(tmp_path):
    config.db_path=tmp_path/'calls.sqlite'
    config.recording_dir=tmp_path/'recording-library'
    config.recordings_enabled=True
    config.admin_password='another-strong-password-test'
    config.secret='S'*48
    config.engine_event_key='E'*48
    init_db()
    qid=create_queue('Support')
    aid=create_agent('Native Agent','1001',qid)
    captures=tmp_path/'raw-captures'
    captures.mkdir()
    writer=LocalEventWriter([IngressRoute('6000',qid)],{aid:('1001',frozenset())},
                            recording_capture_dir=captures, recording_notice_confirmed=True)
    return writer,qid,aid,captures


def test_one_stereo_recording_dedup_and_browser_playback(tmp_path):
    writer,queue,agent,captures=_setup(tmp_path)
    call=SimpleNamespace(trunk_call_id='lab-recorded-001',queue=queue,agent_id=agent)
    writer('call.queued',call)
    writer('call.answered',call)
    writer('call.completed',call)
    audio=captures/'conversation.wav'
    _make_wav(audio)
    original=audio.read_bytes()
    ids=writer.archive_media(call,{'conversation':audio})
    assert len(ids)==1
    assert not audio.exists(), 'Successful archive must leave only the library WAV'
    assert writer.archive_media(call,{'conversation':audio})==ids
    with connection() as db:
        recordings=[dict(r) for r in db.execute('SELECT * FROM recordings')]
        assert len(recordings)==1
        assert recordings[0]['source']=='queuepilot-native-conversation'
        assert recordings[0]['external_call_id']=='lab-recorded-001'
        assert recordings[0]['consent_attested']
        assert len(list(config.recording_dir.glob('*.wav')))==1
        assert db.execute('SELECT COUNT(*) FROM audit_log WHERE operation=?',('recording.archived',)).fetchone()[0]==1
    with TestClient(app) as client:
        assert client.get('/api/cc/recordings').status_code==401
        login=client.post('/api/login',json={'username':'admin','password':config.admin_password})
        assert login.status_code==200
        h={'Authorization':'Bearer '+login.json()['token']}
        listing=client.get('/api/cc/recordings',headers=h)
        assert listing.status_code==200 and len(listing.json()['recordings'])==1
        recorded=client.get('/api/cc/recordings/'+ids[0]+'/audio',headers=h)
        assert recorded.status_code==200 and recorded.content==original
        with wave.open(str(Path(recordings[0]['file_path'])),'rb') as w:
            assert w.getnchannels()==2 and w.getframerate()==8000
    config.recordings_enabled=False


def test_refuse_index_when_recording_disabled_or_notice_not_confirmed(tmp_path):
    writer,q,a,captures=_setup(tmp_path)
    config.recordings_enabled=False
    with pytest.raises(ValueError,match='RECORDINGS_ENABLED'):
        LocalEventWriter([IngressRoute('6000',q)],{a:('1001',frozenset())},
                         recording_capture_dir=captures,recording_notice_confirmed=True)
    with pytest.raises(ValueError,match='policy'):
        NativeRecordingArchive(captures,notice_confirmed=False)
    with pytest.raises(ValueError,match='notice/consent'):
        Config('127.0.0.1',15060,'127.0.0.1',('127.0.0.1/32',),('127.0.0.1/32',),('127.0.0.1/32',),
               (IngressRoute('6000',q),),{'a':('1001',frozenset())},{'1001':'a-long-sip-password'},
               'queuepilot.local',b'a'*32,record_directory=captures)


def test_archive_rejects_unknown_call_and_outside_files(tmp_path):
    writer,q,a,captures=_setup(tmp_path)
    track=captures/'trunk.wav'
    _make_wav(track)
    unknown=SimpleNamespace(trunk_call_id='nonexistent',queue=q,agent_id=a)
    with pytest.raises(ValueError,match='must be persisted'):
        writer.archive_media(unknown,{'conversation':track})
    call=SimpleNamespace(trunk_call_id='call-ok',queue=q,agent_id=a)
    writer('call.queued',call)
    outside=tmp_path/'outside.wav'
    _make_wav(outside)
    with pytest.raises(ValueError,match='location'):
        writer.archive_media(call,{'conversation':outside})
    config.recordings_enabled=False


def test_archive_does_not_accept_non_wav_or_unknown_direction(tmp_path):
    writer,q,a,captures=_setup(tmp_path)
    call=SimpleNamespace(trunk_call_id='call-okay',queue=q,agent_id=a)
    writer('call.queued',call)
    bad=captures/'untrusted.wav'
    bad.write_bytes(b'untrusted bytes')
    with pytest.raises((ValueError,EOFError,wave.Error)):
        writer.archive_media(call,{'conversation':bad})
    with pytest.raises(ValueError,match='Exactly one conversation'):
        writer.archive_media(call,{'other':bad})
    config.recordings_enabled=False


def test_actual_native_sip_lab_rtp_flows_into_recording_library(tmp_path):
    """Real UDP REGISTER -> INVITE -> two-way RTP -> BYE -> recorded playback."""
    from test_native_switch import exercise
    writer,_,_,captures=_setup(tmp_path)
    # The SIP integration test uses stable lab queue/agent IDs. Ingest needs the
    # *same* IDs as the dashboard before a SIP call can be reconciled.
    with connection() as db:
        db.execute("INSERT INTO queues(id,name,skill,sla_seconds,target_percent,enabled,created_at) VALUES('support','Support','',20,80,1,0)")
        db.execute("INSERT INTO agents(id,name,extension,queue_id,status,last_change) VALUES('agent1','Agent','1001','support','available',0)")
    call_writer=LocalEventWriter([IngressRoute('6000','support')],{'agent1':('1001',frozenset())},
                                 recording_capture_dir=tmp_path, recording_notice_confirmed=True)
    asyncio.run(exercise(tmp_path,event_writer=call_writer))
    with connection() as db:
        call=db.execute("SELECT status,agent_id FROM calls WHERE external_id='customer-1'").fetchone()
        assert call is not None and call['status']=='completed'
        assert call['agent_id']=='agent1'
        rows=[dict(row) for row in db.execute(
            "SELECT source,external_call_id,file_path FROM recordings WHERE external_call_id='customer-1' ORDER BY source")]
        assert len(rows)==1
        assert [r['source'] for r in rows]==['queuepilot-native-conversation']
        assert all(Path(r['file_path']).exists() for r in rows)
        assert not list(tmp_path.glob('customer-1-*-conversation.wav'))
        with wave.open(rows[0]['file_path'],'rb') as recorded:
            assert recorded.getnchannels()==2
            assert recorded.getnframes()>=160
    config.recordings_enabled=False
