import asyncio
import hashlib
import socket
import struct
import time
import wave
from pathlib import Path

import pytest

from telephony.native_sip import IngressRoute
from telephony.native_switch import Config, NativeSwitch
from telephony.sip_stack import RegistrarAuth, SipError, MediaAddress, parse_message, parse_sdp, packet, response, sdp_offer
from telephony.rtp_media import RtpRelay
from telephony.stereo_recording import decode_ulaw, decode_alaw


def request(method,uri,call_id,cseq,from_header,to_header,additional=None,body=b''):
    return packet(f'{method} {uri} SIP/2.0',
                  [('Via',f'SIP/2.0/UDP 127.0.0.1:5050;branch=z9hG4bK{cseq}{call_id};rport'),
                   ('From',from_header),('To',to_header),('Call-ID',call_id),('CSeq',f'{cseq} {method}')]
                  +(additional or []),body)


def digest_value(username,password,realm,uri,nonce,cseq=1):
    h=lambda x:hashlib.sha256(x.encode()).hexdigest()
    nc=f'{cseq:08x}'
    cn='clientnonce1234'
    val=h(f'{h(f"{username}:{realm}:{password}")}:{nonce}:{nc}:{cn}:auth:{h(f"REGISTER:{uri}")}')
    return f'Digest username="{username}",realm="{realm}",nonce="{nonce}",uri="{uri}",response="{val}",algorithm=SHA-256,qop=auth,nc={nc},cnonce="{cn}"'


def udp_socket():
    s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    s.bind(('127.0.0.1',0));s.setblocking(False)
    return s


async def receive(loop,s,timeout=1.5):
    data,_=await asyncio.wait_for(loop.sock_recvfrom(s,32768),timeout)
    return parse_message(data)


async def exercise(tmp_path,event_writer=None):
    loop=asyncio.get_running_loop()
    free=udp_socket()
    port=free.getsockname()[1]
    free.close()
    server=NativeSwitch(Config('127.0.0.1',port,'127.0.0.1',('127.0.0.1/32',),('127.0.0.1/32',),('127.0.0.1/32',),
                               (IngressRoute('6000','support',frozenset({'english'})),),
                               {'agent1':('1001',frozenset({'english'}))},{'1001':'long-personal-password'},
                               'queuepilot.local',b'0123456789abcdef0123456789abcdef',record_directory=tmp_path,recording_notice_confirmed=True),event_writer=event_writer)
    phone=udp_socket();trunk=udp_socket();phone_media=udp_socket();trunk_media=udp_socket()
    await server.start()
    try:
        ext='1001';reg_uri='sip:queuepilot.local'
        from_h='<sip:1001@queuepilot.local>;tag=reg1'
        to_h='<sip:1001@queuepilot.local>'
        contact=f'<sip:1001@127.0.0.1:{phone.getsockname()[1]}>'
        reg=request('REGISTER',reg_uri,'reg-call',1,from_h,to_h,[('Contact',contact),('Expires','120')])
        await loop.sock_sendto(phone,reg,('127.0.0.1',port))
        challenge=await receive(loop,phone)
        assert challenge.start.startswith('SIP/2.0 401')
        nonce=challenge.get('www-authenticate').split('nonce="')[1].split('"')[0]
        token=digest_value(ext,'long-personal-password','queuepilot.local',reg_uri,nonce)
        reg2=request('REGISTER',reg_uri,'reg-call',2,from_h,to_h,[('Contact',contact),('Expires','120'),('Authorization',token)])
        await loop.sock_sendto(phone,reg2,('127.0.0.1',port))
        ok=await receive(loop,phone)
        assert ok.start.startswith('SIP/2.0 200')
        # Replayed authenticated REGISTER with a different branch must be challenged.
        await loop.sock_sendto(phone,reg2,('127.0.0.1',port))
        replay=await receive(loop,phone)
        assert replay.start.startswith('SIP/2.0 401')
        assert server.directory.lookup(ext).port == phone.getsockname()[1]
        # Avaya hands over INVITE; QueuePilot owns both phone signaling & RTP.
        trunk_rtp=trunk_media.getsockname()[1]
        invite=request('INVITE',f'sip:6000@127.0.0.1','customer-1',1,'<sip:caller@127.0.0.1>;tag=orig','<sip:6000@127.0.0.1>',
                       [('Contact',f'<sip:caller@127.0.0.1:{trunk.getsockname()[1]}>'),('Content-Type','application/sdp')],
                       sdp_offer('127.0.0.1',trunk_rtp,(0,)))
        await loop.sock_sendto(trunk,invite,('127.0.0.1',port))
        trying=await receive(loop,trunk);ringing=await receive(loop,trunk)
        assert trying.start.startswith('SIP/2.0 100')
        assert ringing.start.startswith('SIP/2.0 180')
        phone_invite=await receive(loop,phone)
        assert phone_invite.start.startswith('INVITE ')
        assert phone_invite.get('call-id')!='customer-1'
        offered=parse_sdp(phone_invite.body,('127.0.0.1/32',))
        assert offered.codec==0
        assert server.acd.calls['customer-1'].state=='offering'
        phone_ok=response(phone_invite,200,[('Contact',contact),('Content-Type','application/sdp')],
                          sdp_offer('127.0.0.1',phone_media.getsockname()[1],(0,)),to_tag='phone1')
        await loop.sock_sendto(phone,phone_ok,('127.0.0.1',port))
        phone_ack=await receive(loop,phone)
        assert phone_ack.start.startswith('ACK ')
        inbound_ok=await receive(loop,trunk)
        # Duplicate 200 OK from phone requires another ACK, not a BYE.
        await loop.sock_sendto(phone,phone_ok,('127.0.0.1',port))
        repeated_ack=await receive(loop,phone)
        assert repeated_ack.start.startswith('ACK ')
        assert inbound_ok.start.startswith('SIP/2.0 200')
        trunk_offered=parse_sdp(inbound_ok.body,('127.0.0.1/32',))
        assert trunk_offered.port!=offered.port
        assert server.acd.calls['customer-1'].state=='talking'
        ack=request('ACK','sip:6000@127.0.0.1','customer-1',1,
                    '<sip:caller@127.0.0.1>;tag=orig',inbound_ok.get('to'))
        await loop.sock_sendto(trunk,ack,('127.0.0.1',port))
        for _ in range(20):
            await asyncio.sleep(0.01)
            if server.calls['customer-1'].state=='bridged':break
        assert server.calls['customer-1'].state=='bridged'
        # Bidirectional RTP is anchored to server-assigned media sockets.
        rtp=bytes([0x80,0])+struct.pack('>HII',1,160,17)+bytes([0xff]*160)
        await loop.sock_sendto(trunk_media,rtp,('127.0.0.1',trunk_offered.port))
        media,_=await asyncio.wait_for(loop.sock_recvfrom(phone_media,2048),1)
        assert media==rtp
        await loop.sock_sendto(phone_media,rtp,('127.0.0.1',offered.port))
        media,_=await asyncio.wait_for(loop.sock_recvfrom(trunk_media,2048),1)
        assert media==rtp
        # BYE from inbound leg closes local RTP and ACD state.
        bye=request('BYE','sip:6000@127.0.0.1','customer-1',2,'<sip:caller@127.0.0.1>;tag=orig',inbound_ok.get('to'))
        await loop.sock_sendto(trunk,bye,('127.0.0.1',port))
        bye_ok=await receive(loop,trunk)
        assert bye_ok.start.startswith('SIP/2.0 200')
        phone_bye=await receive(loop,phone)
        assert phone_bye.start.startswith('BYE ')
        assert server.acd.calls['customer-1'].state=='completed'
        assert 'customer-1' not in server.calls
        assert [e['event'] for e in server.events]==['call.queued','call.offered','call.answered','call.bridged','call.completed']
        paths=list(tmp_path.glob('customer-1-*-conversation.wav'))
        if event_writer is None:
            assert len(paths)==1, 'Each call must create exactly one audio file'
            assert not list(tmp_path.glob('customer-1-*-trunk.wav'))
            assert not list(tmp_path.glob('customer-1-*-phone.wav'))
            with wave.open(str(paths[0]),'rb') as w:
                assert w.getnchannels()==2
                assert w.getsampwidth()==2
                assert w.getframerate()==8000
                assert w.getnframes()>=160
                assert len(w.readframes(w.getnframes()))==w.getnframes()*4
        else:
            # After durable archival, only the protected library copy remains.
            assert paths==[]
    finally:
        await server.stop()
        for sock in (phone,trunk,phone_media,trunk_media):sock.close()


def test_full_lab_call_native_sip_and_rtp(tmp_path):
    asyncio.run(exercise(tmp_path))


def test_sip_parser_rejects_malformed_and_duplicate_critical_headers():
    good=request('OPTIONS','sip:6000@127.0.0.1','id',1,'<sip:a@localhost>;tag=x','<sip:b@localhost>')
    assert parse_message(good).method=='OPTIONS'
    with pytest.raises(SipError):parse_message(good.replace(b'Call-ID: id',b'Call-ID: id\r\nCall-ID: duplicate'))
    with pytest.raises(SipError):parse_message(good+b'junk')
    with pytest.raises(SipError):parse_message(good.replace(b'Via:',b' Via:'))
    with pytest.raises(SipError):parse_message(good.replace(b'1 OPTIONS',b'1 INVITE'))


def test_digest_rejects_wrong_password_expired_and_replay():
    clock=lambda: 100000
    auth=RegistrarAuth('queuepilot.local',{'1001':'correct-password'},b'a'*32,clock=clock)
    nonce=auth.nonce()
    uri='sip:queuepilot.local'
    from_h='<sip:1001@queuepilot.local>;tag=1';to_h='<sip:1001@queuepilot.local>'
    bad=request('REGISTER',uri,'id',1,from_h,to_h,[('Authorization',digest_value('1001','wrong-password','queuepilot.local',uri,nonce))])
    assert not auth.verify(parse_message(bad),'1001')
    good=request('REGISTER',uri,'id',1,from_h,to_h,[('Authorization',digest_value('1001','correct-password','queuepilot.local',uri,nonce))])
    assert auth.verify(parse_message(good),'1001')
    assert not auth.verify(parse_message(good),'1001')
    later=RegistrarAuth('queuepilot.local',{'1001':'correct-password'},b'a'*32,clock=lambda: 100500)
    assert not later.verify(parse_message(good),'1001')


def test_sdp_rejects_wrong_peer_or_unsupported_codec():
    sdp=sdp_offer('192.168.9.10',20000,(0,))
    with pytest.raises(SipError):parse_sdp(sdp,('10.0.0.0/8',))
    with pytest.raises(SipError):parse_sdp(sdp.replace(b'RTP/AVP 0',b'RTP/SAVP 0'),('192.168.9.0/24',))
    with pytest.raises(SipError):parse_sdp(sdp.replace(b'RTP/AVP 0',b'RTP/AVP 111'),('192.168.9.0/24',))


def test_g711_codec_silence():
    assert decode_ulaw(0xff)==0
    assert abs(decode_alaw(0xd5))<=16


def test_config_requires_real_credentials():
    with pytest.raises(ValueError):
        Config('127.0.0.1',15060,'127.0.0.1',('127.0.0.1/32',),('127.0.0.1/32',),('127.0.0.1/32',),
               (IngressRoute('6000','support'),),{'a':('1001',frozenset())},{'1001':'REPLACE_WEAK'},
               'queuepilot.local',b'a'*32)


def test_stale_answer_gets_ack_and_bye():
    config=Config('127.0.0.1',15060,'127.0.0.1',('127.0.0.1/32',),('127.0.0.1/32',),('127.0.0.1/32',),
                  (IngressRoute('6000','support'),),{'a':('1001',frozenset())},{'1001':'long-personal-password'},
                  'queuepilot.local',b'a'*32)
    server=NativeSwitch(config)
    sent=[]
    server.send=lambda data,addr: sent.append((parse_message(data),addr))
    original=parse_message(request('INVITE','sip:1001@127.0.0.1','old-offer',1,
                                   '<sip:queuepilot@127.0.0.1>;tag=old','<sip:1001@127.0.0.1>'))
    ok=parse_message(response(original,200,[('Contact','<sip:1001@127.0.0.1:5060>')],to_tag='phone1'))
    server._terminate_stale_offer(original,ok,('127.0.0.1',5060))
    assert [message.method for message,_ in sent]==['ACK','BYE']
    assert sent[0][0].get('call-id')=='old-offer'
