"""One-shot authenticated lab hold/resume from dashboard through native RTP.

SIP phone-initiated RFC 3264 hold and attended transfers are intentionally NOT
claimed by this test; it verifies server-controlled media hold only.
"""
from __future__ import annotations

import asyncio
import socket
import struct
import time
import wave
from types import SimpleNamespace

import pytest

from app.config import config
from app.db import connection
from app.security import encode_token
from telephony.native_switch import LiveCall
from telephony.rtp_media import RtpRelay
from telephony.sip_stack import MediaAddress, parse_message
from test_native_switch import request
from test_runtime_sync import setup_runtime


def make_socket():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('127.0.0.1', 0))
    sock.setblocking(False)
    return sock


def frame(seq: int) -> bytes:
    return struct.pack('!BBHII', 0x80, 0, seq, seq * 160, 300)+bytes([0xff])*160


async def verify_hold_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(config,'engine_event_key','E'*64)
    queue, agent, writer, switch, client, admin = setup_runtime(tmp_path, monkeypatch)
    loop = asyncio.get_running_loop()
    caller, telephone = make_socket(), make_socket()
    relay = RtpRelay('127.0.0.1', '127.0.0.1', tmp_path/'captured')
    await relay.open()
    try:
        relay.set_remote('trunk', MediaAddress('127.0.0.1', caller.getsockname()[1], 0))
        relay.set_remote('phone', MediaAddress('127.0.0.1', telephone.getsockname()[1], 0))
        relay.connect(0, 'hold-test')
        invite = parse_message(request('INVITE', 'sip:6000@127.0.0.1', 'call-hold-1', 1,
                        '<sip:customer@127.0.0.1>;tag=abc', '<sip:6000@127.0.0.1>'))
        call = LiveCall('call-hold-1', invite, ('127.0.0.1', 5600), queue, relay,
                        MediaAddress('127.0.0.1', caller.getsockname()[1], 0),
                        phone_addr=('127.0.0.1', telephone.getsockname()[1]),
                        agent_id=agent, state='bridged')
        switch.calls[call.trunk_call_id] = call
        # Real native event journal creates one answered call in dashboard DB.
        writer('call.queued', call)
        writer('call.answered', call)
        writer.heartbeat(True)
        with connection() as db:
            call_row=db.execute("SELECT id FROM calls WHERE external_id='call-hold-1'").fetchone()
            assert call_row
            db_id=call_row['id']
        url=f'/api/engine/calls/{db_id}/control'
        assert client.post(url,json={'action':'hold'}).status_code==401
        reviewer={'Authorization':'Bearer '+encode_token(config.reviewer_user)}
        assert client.post(url,headers=reviewer,json={'action':'hold'}).status_code==403
        assert client.post(url,headers=admin,json={'action':'transfer'}).status_code==422
        assert client.post('/api/engine/calls/no-such-id/control',headers=admin,json={'action':'hold'}).status_code==404
        assert client.post(url,headers=admin,json={'action':'hold'}).status_code==202
        assert client.post(url,headers=admin,json={'action':'resume'}).status_code==409
        before=client.get('/api/dashboard',headers=admin).json()['calls'][0]
        assert before['control']['state']=='pending'
        assert relay.held is False  # admin intent is not an ACK or media action
        writer.sync_call_controls(switch)  # the native process claims under its lock
        assert relay.held and call.on_hold
        view=client.get('/api/dashboard',headers=admin).json()['calls'][0]
        assert view['control']['state']=='applied' and view['hold_active'] is True
        comfort,_=await asyncio.wait_for(loop.sock_recvfrom(caller,2048),1)
        assert len(comfort)==172 and comfort[1]&127==0 and set(comfort[12:])=={0xff}
        # Both directions are muted by the media engine. No tap/record while held.
        await loop.sock_sendto(caller,frame(1),('127.0.0.1',relay.local_port('trunk')))
        await loop.sock_sendto(telephone,frame(2),('127.0.0.1',relay.local_port('phone')))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(loop.sock_recvfrom(telephone,2048),0.07)
        assert relay.packet_counts=={'trunk':0,'phone':0}
        assert client.post(url,headers=admin,json={'action':'resume'}).status_code==202
        writer.sync_call_controls(switch)
        assert not relay.held and not call.on_hold
        assert client.get('/api/dashboard',headers=admin).json()['calls'][0]['hold_active'] is False
        await loop.sock_sendto(caller,frame(3),('127.0.0.1',relay.local_port('trunk')))
        received,_=await asyncio.wait_for(loop.sock_recvfrom(telephone,2048),1)
        assert received==frame(3)
        await loop.sock_sendto(telephone,frame(4),('127.0.0.1',relay.local_port('phone')))
        for _ in range(20):
            received,_=await asyncio.wait_for(loop.sock_recvfrom(caller,2048),1)
            if received==frame(4):
                break
        assert received==frame(4)
        controls=client.get('/api/engine/calls/controls',headers=admin)
        assert controls.status_code==200 and [c['state'] for c in controls.json()['commands'][:2]]==['applied','applied']
        assert [e['event'] for e in switch.events if e['event'].startswith('call.h') or e['event']=='call.resumed']==['call.held','call.resumed']
        # Commands created after call disappearance reject, rather than reapply
        # to a new SIP call that happens to reuse a Call-ID.
        assert client.post(url,headers=admin,json={'action':'hold'}).status_code==202
        switch.calls.pop(call.trunk_call_id)
        writer.sync_call_controls(switch)
        assert client.get('/api/engine/calls/controls',headers=admin).json()['commands'][0]['state']=='rejected'
        relay.close()
        recordings=list((tmp_path/'captured').glob('*conversation.wav'))
        assert len(recordings)==1, 'Holding a call must never split its recording'
        with wave.open(str(recordings[0]),'rb') as sound:
            assert sound.getnchannels()==2 and sound.getframerate()==8000
            assert sound.getnframes()>=320
        with connection() as db:
            assert db.execute("SELECT COUNT(*) FROM audit_log WHERE operation LIKE 'native_call.control_%'").fetchone()[0]==6
    finally:
        relay.close()
        caller.close();telephone.close()
        client.__exit__(None,None,None)


def test_authenticated_hold_resume_media_and_single_wav(tmp_path, monkeypatch):
    asyncio.run(verify_hold_resume(tmp_path,monkeypatch))


def test_heartbeat_required_for_control_and_queued_calls_rejected(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'engine_event_key','E'*64)
    q,a,writer,switch,client,auth=setup_runtime(tmp_path,monkeypatch)
    try:
        call=SimpleNamespace(trunk_call_id='pending-caller',queue=q,agent_id=a)
        writer('call.queued',call)
        with connection() as db:
            cid=db.execute("SELECT id FROM calls WHERE external_id='pending-caller'").fetchone()['id']
        assert client.post(f'/api/engine/calls/{cid}/control',headers=auth,json={'action':'hold'}).status_code==409
        writer('call.answered',call)
        assert client.post(f'/api/engine/calls/{cid}/control',headers=auth,json={'action':'hold'}).status_code==503
        writer.heartbeat(True)
        assert client.post(f'/api/engine/calls/{cid}/control',headers=auth,json={'action':'hold'}).status_code==202
        # A command with stale creation time can never be applied to a future call.
        fake=SimpleNamespace(started_wall=time.time()+10,state='bridged',phone_addr=('127.0.0.1',6000))
        switch.calls['pending-caller']=fake
        writer.sync_call_controls(switch)
        assert client.get('/api/engine/calls/controls',headers=auth).json()['commands'][0]['result']=='call_not_active_or_stale'
    finally:
        client.__exit__(None,None,None)
