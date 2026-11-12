"""Real UDP/SIP/RTP lab IVR flow: local prompt -> RFC4733 digit -> ACD -> ONE WAV."""
import asyncio
import socket
import struct
import wave

import pytest

from telephony.ivr import IvrMenu, encode_prompt
from telephony.native_sip import IngressRoute
from telephony.native_switch import Config, NativeSwitch
from telephony.sip_stack import SipError, parse_sdp, parse_message, response, sdp_offer
from telephony.stereo_recording import decode_ulaw, decode_alaw
from test_native_switch import request, digest_value, udp_socket, receive


def prompt(path):
    with wave.open(str(path), 'wb') as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(8000)
        f.writeframes(struct.pack('<h', 2000)*800)  # 0.1 second lab tone
    return path


def cfg(tmp_path):
    menu=IvrMenu('6000', prompt(tmp_path/'welcome.wav'), {'1':'support','2':'sales'}, 5, 2, 'support')
    return Config('127.0.0.1', 15060, '127.0.0.1',
                  ('127.0.0.1/32',), ('127.0.0.1/32',), ('127.0.0.1/32',),
                  (IngressRoute('6000','support',frozenset({'english'})),
                   IngressRoute('6001','sales',frozenset({'sales'}))),
                  {'agent2':('1002',frozenset({'sales'}))},
                  {'1002':'long-password-for-agent2'}, 'queuepilot.local', b'a'*32,
                  record_directory=tmp_path/'recordings', recording_notice_confirmed=True, ivr_menus=(menu,))


def make_rtp(pt,seq,ts,ssrc,payload):
    return struct.pack('!BBHII', 0x80, pt, seq, ts, ssrc)+payload


async def ivr_call(tmp_path):
    config=cfg(tmp_path)
    free=udp_socket();port=free.getsockname()[1];free.close()
    from dataclasses import replace
    config=replace(config,listen_port=port)
    server=NativeSwitch(config)
    phone=udp_socket();trunk=udp_socket();trunk_rtp=udp_socket();phone_rtp=udp_socket()
    loop=asyncio.get_running_loop()
    await server.start()
    try:
        # Register the sales agent via QueuePilot, not via Avaya.
        sip_uri='sip:queuepilot.local'
        contact=f'<sip:1002@127.0.0.1:{phone.getsockname()[1]}>'
        from_h='<sip:1002@queuepilot.local>;tag=abc';to_h='<sip:1002@queuepilot.local>'
        reg=request('REGISTER',sip_uri,'ivr-reg',1,from_h,to_h,[('Contact',contact)])
        await loop.sock_sendto(phone,reg,('127.0.0.1',port))
        challenge=await receive(loop,phone)
        nonce=challenge.get('www-authenticate').split('nonce="')[1].split('"')[0]
        auth=digest_value('1002','long-password-for-agent2','queuepilot.local',sip_uri,nonce)
        reg=request('REGISTER',sip_uri,'ivr-reg',2,from_h,to_h,[('Contact',contact),('Authorization',auth)])
        await loop.sock_sendto(phone,reg,('127.0.0.1',port))
        assert (await receive(loop,phone)).start.startswith('SIP/2.0 200')
        invite=request('INVITE','sip:6000@127.0.0.1','ivr-caller',1,
                       '<sip:caller@127.0.0.1>;tag=orig','<sip:6000@127.0.0.1>',
                       [('Contact',f'<sip:caller@127.0.0.1:{trunk.getsockname()[1]}>'),('Content-Type','application/sdp')],
                       sdp_offer('127.0.0.1',trunk_rtp.getsockname()[1],(0,),dtmf_payload=101))
        await loop.sock_sendto(trunk,invite,('127.0.0.1',port))
        assert (await receive(loop,trunk)).start.startswith('SIP/2.0 100')
        answered=await receive(loop,trunk)
        assert answered.start.startswith('SIP/2.0 200')
        offered=parse_sdp(answered.body,('127.0.0.1/32',))
        assert offered.dtmf_payload==101
        assert server.calls['ivr-caller'].state=='ivr_wait_ack'
        assert 'ivr-caller' not in server.acd.calls  # no agent ringing before selection
        ack=request('ACK','sip:6000@127.0.0.1','ivr-caller',1,
                    '<sip:caller@127.0.0.1>;tag=orig',answered.get('to'))
        await loop.sock_sendto(trunk,ack,('127.0.0.1',port))
        welcome,_=await asyncio.wait_for(loop.sock_recvfrom(trunk_rtp,2048),1)
        assert len(welcome)==172 and welcome[1]&127==0
        assert any(decode_ulaw(c)>1000 for c in welcome[12:])
        assert server.calls['ivr-caller'].state=='ivr'
        # RTP telephone-event 2 (sales); retransmitted event must be idempotent.
        digit=make_rtp(101,2,160,123,bytes([2,0x80|10,0,160]))
        for _ in range(2):
            await loop.sock_sendto(trunk_rtp,digit,('127.0.0.1',offered.port))
        phone_offer=await receive(loop,phone)
        assert phone_offer.method=='INVITE'
        assert server.calls['ivr-caller'].queue=='sales'
        assert server.acd.calls['ivr-caller'].state=='offering'
        assert [e['event'] for e in server.events].count('call.queued')==1
        offer_media=parse_sdp(phone_offer.body,('127.0.0.1/32',))
        phone_ok=response(phone_offer,200,[('Contact',contact),('Content-Type','application/sdp')],
                          sdp_offer('127.0.0.1',phone_rtp.getsockname()[1],(0,)),to_tag='phone-tag')
        await loop.sock_sendto(phone,phone_ok,('127.0.0.1',port))
        assert (await receive(loop,phone)).method=='ACK'
        # Trunk was already answered for IVR: no second SIP 200 is allowed.
        assert server.calls['ivr-caller'].state=='bridged'
        media=make_rtp(0,3,320,123,bytes([0xff])*160)
        await loop.sock_sendto(trunk_rtp,media,('127.0.0.1',offered.port))
        assert (await asyncio.wait_for(loop.sock_recvfrom(phone_rtp,2048),1))[0]==media
        await loop.sock_sendto(phone_rtp,media,('127.0.0.1',offer_media.port))
        assert (await asyncio.wait_for(loop.sock_recvfrom(trunk_rtp,2048),1))[0]==media
        bye=request('BYE','sip:6000@127.0.0.1','ivr-caller',2,
                    '<sip:caller@127.0.0.1>;tag=orig',answered.get('to'))
        await loop.sock_sendto(trunk,bye,('127.0.0.1',port))
        assert (await receive(loop,trunk)).start.startswith('SIP/2.0 200')
        assert (await receive(loop,phone)).method=='BYE'
        assert server.acd.calls['ivr-caller'].state=='completed'
        audio=list((tmp_path/'recordings').glob('ivr-caller-*-conversation.wav'))
        assert len(audio)==1
        with wave.open(str(audio[0]),'rb') as f:
            assert f.getnchannels()==2 and f.getframerate()==8000 and f.getnframes()>=160
    finally:
        await server.stop()
        for sock in (phone,trunk,trunk_rtp,phone_rtp):sock.close()


def test_native_ivr_greeting_dtmf_queue_and_one_stereo_recording(tmp_path):
    asyncio.run(ivr_call(tmp_path))


def test_ivr_validation_and_dtmf_negotiation(tmp_path):
    c=cfg(tmp_path)
    assert c.ivr_menus[0].pcm
    with pytest.raises(ValueError,match='approved'):
        from dataclasses import replace
        replace(c,ivr_menus=(IvrMenu('6000',tmp_path/'welcome.wav',{'1':'untrusted'}),))
    with pytest.raises(ValueError,match='mono'):
        with wave.open(str(tmp_path/'bad.wav'),'wb') as f:
            f.setnchannels(2);f.setsampwidth(2);f.setframerate(8000);f.writeframes(b'\x00'*800)
        IvrMenu('6000',tmp_path/'bad.wav',{'1':'sales'})
    sdp=sdp_offer('127.0.0.1',20000,(0,8),dtmf_payload=109)
    answer=parse_sdp(sdp,('127.0.0.1/32',))
    assert answer.dtmf_payload==109
    assert parse_sdp(sdp_offer('127.0.0.1',20000,(0,)),('127.0.0.1/32',)).dtmf_payload is None
    with pytest.raises(SipError):sdp_offer('127.0.0.1',20000,(0,),dtmf_payload=10)


def test_audio_encoding_is_compatible_with_existing_decoder():
    pcm=struct.pack('<5h',0,1000,-1000,9000,-9000)
    for codec,decoder in [(0,decode_ulaw),(8,decode_alaw)]:
        encoded=encode_prompt(pcm,codec)
        assert len(encoded)==5
        for wanted,compressed in zip((0,1000,-1000,9000,-9000),encoded):
            assert abs(decoder(compressed)-wanted)<500


def test_ivr_retry_timeout_fallback_and_invalid_digit(tmp_path):
    from unittest.mock import Mock
    from telephony.ivr import IvrSession
    from telephony.native_switch import LiveCall
    from telephony.sip_stack import MediaAddress, parse_message
    c=cfg(tmp_path)
    server=NativeSwitch(c)
    audio=Mock()
    audio.recording_truncated=set()
    audio.recorded_paths={}
    invite=parse_message(request('INVITE','sip:6000@127.0.0.1','fallback-call',1,
                     '<sip:caller@127.0.0.1>;tag=a','<sip:6000@127.0.0.1>'))
    call=LiveCall('fallback-call',invite,('127.0.0.1',5060),'support',audio,MediaAddress('127.0.0.1',21000,0,101),state='ivr')
    call.ivr=IvrSession(c.ivr_menus[0])
    server.calls[call.trunk_call_id]=call
    async def exercise():
        await server._handle_ivr_digit('fallback-call','9')
        assert call.ivr.attempts==1
        assert audio.play_prompt.call_count==1
        await server._retry_ivr(call)
    asyncio.run(exercise())
    assert call.state=='offering'
    assert call.queue=='support'
    audio.stop_ivr.assert_called_once()
    assert server.acd.calls['fallback-call'].state=='waiting'
    assert [e['event'] for e in server.events].count('call.queued')==1


def test_ivr_missing_dtmf_offer_fails_closed(tmp_path):
    from dataclasses import replace
    async def exercise():
        c=cfg(tmp_path)
        free=udp_socket();port=free.getsockname()[1];free.close()
        server=NativeSwitch(replace(c,listen_port=port))
        trunk=udp_socket()
        loop=asyncio.get_running_loop()
        await server.start()
        try:
            invite=request('INVITE','sip:6000@127.0.0.1','no-dtmf',1,
                           '<sip:caller@127.0.0.1>;tag=a','<sip:6000@127.0.0.1>',
                           [('Content-Type','application/sdp')],
                           sdp_offer('127.0.0.1',21000,(0,)))
            await loop.sock_sendto(trunk,invite,('127.0.0.1',port))
            failure=await receive(loop,trunk)
            assert failure.start.startswith('SIP/2.0 488')
            assert server.calls=={}
        finally:
            await server.stop();trunk.close()
    asyncio.run(exercise())
