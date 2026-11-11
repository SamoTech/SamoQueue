"""QueuePilot standalone SIP-to-SIP call controller, isolated UDP/G.711 lab ONLY.

Provides SIP digest REGISTER, Avaya-peer INVITE admission, per-agent INVITE,
200/ACK establishment, BYE/CANCEL and anchored RTP relay. No Asterisk or
external PBX. Unsupported requests fail closed; never deploy on production
Avaya trunks, record customer audio, or advertise as a full RFC 3261 B2BUA.

Known gaps: retransmissions/transaction timers, TLS/SRTP, Avaya SIP extensions,
provisioning, transfer/hold/reINVITE, IVR, transcoding, multi-node, RTP security,
conference monitoring, high availability and durable real-time reports.
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from .acd import ACD
from .native_sip import PhoneDirectory, PhoneContact, SipIngressPlan, IngressRoute, IndependentCallPlan, RoutingError
from .rtp_media import RtpRelay
from .sip_stack import Message, RegistrarAuth, SipError, MediaAddress, packet, parse_message, response, uri_user, header_uri, parse_sdp, sdp_offer


@dataclass(frozen=True)
class Config:
    listen_ip: str
    listen_port: int
    advertised_ip: str
    avaya_peers: tuple[str,...]
    phone_networks: tuple[str,...]
    media_networks: tuple[str,...]
    did_routes: tuple[IngressRoute,...]
    agent_bindings: dict[str,tuple[str,frozenset[str]]]
    extension_passwords: dict[str,str]
    sip_realm: str
    nonce_secret: bytes
    offer_seconds: int=20
    record_directory:Path|None=None

    def __post_init__(self):
        if not self.avaya_peers or not self.phone_networks or not self.media_networks or not self.did_routes or not self.agent_bindings:
            raise ValueError('Explicit trunk, media, phone, DID and agent configuration required')
        for ip in (self.listen_ip,self.advertised_ip):
            if not isinstance(ipaddress.ip_address(ip),ipaddress.IPv4Address):
                raise ValueError('IPv4 lab only')
        if not 1<=self.listen_port<=65535:
            raise ValueError('Invalid SIP listen port')
        if len(self.nonce_secret)<32:
            raise ValueError('Nonce secret must be 32+ bytes')
        for ext,_ in self.agent_bindings.values():
            if ext not in self.extension_passwords:
                raise ValueError(f'Agent extension {ext} missing SIP credentials')
            if self.extension_passwords[ext].startswith('REPLACE_') or len(self.extension_passwords[ext])<16:
                raise ValueError('Weak or placeholder phone credential')
        if self.offer_seconds<2:
            raise ValueError('Invalid offer timeout')


@dataclass
class LiveCall:
    trunk_call_id:str
    request:Message
    trunk_addr:tuple[str,int]
    queue:str
    media:RtpRelay
    trunk_media:MediaAddress
    local_tag:str=field(default_factory=lambda:secrets.token_hex(8))
    phone_call_id:str|None=None
    phone_addr:tuple[str,int]|None=None
    agent_id:str|None=None
    extension:str|None=None
    outbound_branch:str|None=None
    outbound_invite:bytes|None=None
    outbound_cseq:int=1
    outbound_tag:str|None=None
    phone_to:str|None=None
    phone_contact:str|None=None
    state:str='offering'
    invite_response:bytes|None=None
    created_at:float=field(default_factory=time.monotonic)
    answered_at:float|None=None


class _Udp(asyncio.DatagramProtocol):
    def __init__(self,server:'NativeSwitch'):
        self.server=server
    def connection_made(self,transport):
        self.server.transport=transport
    def datagram_received(self,data,addr):
        # A lock protects the ACD single-writer state across inbound datagrams.
        asyncio.create_task(self.server.process(data,addr))


class NativeSwitch:
    def __init__(self,config:Config):
        self.config=config
        self.acd=ACD(offer_seconds=config.offer_seconds)
        self.directory=PhoneDirectory(config.phone_networks)
        self.plan=IndependentCallPlan(SipIngressPlan(config.avaya_peers,config.did_routes),self.directory,self.acd)
        self.auth=RegistrarAuth(config.sip_realm,config.extension_passwords,config.nonce_secret)
        for agent,(extension,skills) in config.agent_bindings.items():
            self.acd.upsert_agent(agent,set(skills),'ready')
            self.plan.bind_agent(agent,extension)
        self.calls:dict[str,LiveCall]={}
        self.phone_calls:dict[str,str]={}
        self.transport:asyncio.DatagramTransport|None=None
        self._lock=asyncio.Lock()
        self._poll_task:asyncio.Task|None=None
        self.errors:list[str]=[]
        self.events:list[dict]=[]
        # A cancelled INVITE may still receive a final 200 OK.
        self._cancelled:dict[str,tuple[tuple[str,int],Message,float]]={}

    async def start(self):
        loop=asyncio.get_running_loop()
        transport,_=await loop.create_datagram_endpoint(lambda:_Udp(self),local_addr=(self.config.listen_ip,self.config.listen_port))
        self.transport=transport
        self._poll_task=asyncio.create_task(self._poll())

    async def stop(self):
        if self._poll_task:
            self._poll_task.cancel()
            try: await self._poll_task
            except asyncio.CancelledError: pass
            self._poll_task=None
        async with self._lock:
            for call in list(self.calls.values()):
                call.media.close()
            self.calls.clear()
            self.phone_calls.clear()
        if self.transport:
            self.transport.close();self.transport=None

    def emit(self,event:str,call:LiveCall):
        self.events.append({'event':event,'call_id':call.trunk_call_id,'queue':call.queue,'agent_id':call.agent_id,'at':time.time()})
        if len(self.events)>1000: self.events.pop(0)

    def send(self,data:bytes,addr:tuple[str,int]):
        if self.transport:
            self.transport.sendto(data,addr)

    async def process(self,data:bytes,addr:tuple[str,int]):
        async with self._lock:
            try:
                msg=parse_message(data)
                if msg.start.startswith('SIP/2.0 '):
                    await self._phone_response(msg,addr)
                elif msg.method=='OPTIONS':
                    if self._trusted(addr[0],self.config.avaya_peers+self.config.phone_networks):
                        self.send(response(msg,200,[('Allow','OPTIONS, REGISTER, INVITE, ACK, BYE, CANCEL')]),addr)
                elif msg.method=='REGISTER':
                    self._register(msg,addr)
                elif msg.method=='INVITE':
                    await self._invite(msg,addr)
                elif msg.method=='ACK':
                    self._ack(msg,addr)
                elif msg.method=='BYE':
                    await self._bye(msg,addr)
                elif msg.method=='CANCEL':
                    await self._cancel(msg,addr)
                else:
                    if self._trusted(addr[0],self.config.avaya_peers+self.config.phone_networks):
                        self.send(response(msg,405,[('Allow','OPTIONS, REGISTER, INVITE, ACK, BYE, CANCEL')]),addr)
            except (SipError,RoutingError,ValueError,PermissionError) as exc:
                # Never reflect raw untrusted SIP contents into error messages.
                self.errors.append(type(exc).__name__)
                if len(self.errors)>1000:self.errors.pop(0)
            except Exception:
                self.errors.append('internal_error')

    @staticmethod
    def _trusted(addr:str,networks:tuple[str,...])->bool:
        try:
            ip=ipaddress.ip_address(addr)
            return any(ip in ipaddress.ip_network(net,strict=False) for net in networks)
        except ValueError:
            return False

    def _register(self,msg:Message,addr:tuple[str,int]):
        if not self._trusted(addr[0],self.config.phone_networks):
            return
        try:
            ext=uri_user(header_uri(msg.get('to')))
            if ext not in self.config.extension_passwords: raise SipError('Unknown user')
        except SipError:
            self.send(response(msg,403),addr);return
        if not self.auth.verify(msg,ext):
            self.send(response(msg,401,[('WWW-Authenticate',self.auth.challenge())]),addr)
            return
        contact_text=msg.get('contact')
        if contact_text=='*':
            if msg.get('expires')!='0':
                self.send(response(msg,400),addr);return
            self.directory.revoke(ext)
            self.send(response(msg,200,[('Expires','0')]),addr);return
        m=re.search(r'<sip:([0-9]+)@([0-9.]+)(?::([0-9]+))?(?:;[^>]*)?>',contact_text,re.I)
        if not m or m.group(1)!=ext or m.group(2)!=addr[0]:
            self.send(response(msg,403),addr);return
        # Contact port may differ from the REGISTER source port, as permitted by SIP.
        try:
            port=int(m.group(3) or 5060)
            expires=min(max(int(msg.get('expires','300')),0),3600)
            if expires==0:
                self.directory.revoke(ext)
            else:
                self.directory.provision(PhoneContact(ext,m.group(2),port,expires_at=time.time()+expires),verified=True)
        except (ValueError,RoutingError):
            self.send(response(msg,400),addr);return
        self.send(response(msg,200,[('Contact',contact_text),('Expires',str(expires))]),addr)

    async def _invite(self,msg:Message,addr:tuple[str,int]):
        if not self._trusted(addr[0],self.config.avaya_peers):
            return
        call_id=msg.get('call-id')
        if call_id in self.calls:
            existing=self.calls[call_id]
            if existing.trunk_addr==addr and existing.request.get('from')==msg.get('from'):
                self.send(existing.invite_response or response(msg,180,to_tag=existing.local_tag),addr)
            else:
                self.send(response(msg,403),addr)
            return
        try:
            called=uri_user(msg.uri)
            route=self.plan.ingress.admit(addr[0],called)
            incoming=parse_sdp(msg.body,self.config.media_networks)
        except PermissionError:
            self.send(response(msg,403),addr);return
        except RoutingError:
            self.send(response(msg,404),addr);return
        except SipError:
            self.send(response(msg,488),addr);return
        relay=RtpRelay(self.config.listen_ip,self.config.advertised_ip,self.config.record_directory)
        try:
            await relay.open()
        except OSError:
            self.send(response(msg,503),addr);return
        relay.set_remote('trunk',incoming)
        call=LiveCall(call_id,msg,addr,route.queue,relay,incoming)
        self.calls[call_id]=call
        self.plan.incoming(call_id,addr[0],called)
        self.emit('call.queued',call)
        self.send(response(msg,100),addr)
        ringing=response(msg,180,to_tag=call.local_tag)
        self.send(ringing,addr)
        call.invite_response=ringing
        await self._dispatch()

    async def _dispatch(self):
        for intent in self.plan.plan_offers():
            call=self.calls.get(intent.call_id)
            if not call or call.state!='offering':continue
            agent= self.acd.agents[intent.agent_id]
            ext=self.plan.agent_extensions[intent.agent_id]
            contact=self.directory.lookup(ext)
            if contact is None or contact.transport!='udp':
                self.acd.reject(call.trunk_call_id,intent.agent_id)
                continue
            call.agent_id=intent.agent_id;call.extension=ext
            call.phone_addr=(contact.host,contact.port)
            call.phone_call_id='qp-'+secrets.token_hex(12)
            call.outbound_branch='z9hG4bK'+secrets.token_hex(9)
            call.outbound_tag=secrets.token_hex(8)
            self.phone_calls[call.phone_call_id]=call.trunk_call_id
            fields=[('Via',f'SIP/2.0/UDP {self.config.advertised_ip}:{self.config.listen_port};branch={call.outbound_branch};rport'),
                    ('Max-Forwards','70'),('From',f'<sip:queuepilot@{self.config.advertised_ip}>;tag={call.outbound_tag}'),
                    ('To',f'<sip:{ext}@{contact.host}>'),('Call-ID',call.phone_call_id),('CSeq','1 INVITE'),
                    ('Contact',f'<sip:queuepilot@{self.config.advertised_ip}:{self.config.listen_port}>'),('Content-Type','application/sdp')]
            call.outbound_invite=packet(f'INVITE {contact.sip_uri} SIP/2.0',fields,
                                       sdp_offer(self.config.advertised_ip,call.media.local_port('phone'),(call.trunk_media.codec,)))
            self.send(call.outbound_invite,call.phone_addr)
            self.emit('call.offered',call)

    def _phone_request(self,method:str,call:LiveCall,request_uri:str,cseq:int=2)->bytes:
        assert call.phone_call_id and call.extension
        from_hdr=f'<sip:queuepilot@{self.config.advertised_ip}>;tag={call.outbound_tag}'
        to_hdr=call.phone_to or f'<sip:{call.extension}@{call.phone_addr[0]}>'
        fields=[('Via',f'SIP/2.0/UDP {self.config.advertised_ip}:{self.config.listen_port};branch=z9hG4bK{secrets.token_hex(8)}'),
                ('From',from_hdr),('To',to_hdr),('Call-ID',call.phone_call_id),('CSeq',f'{cseq} {method}'),('Max-Forwards','70')]
        return packet(f'{method} {request_uri} SIP/2.0',fields)

    async def _phone_response(self,msg:Message,addr:tuple[str,int]):
        ref=self.phone_calls.get(msg.get('call-id'))
        if not ref:
            stale=self._cancelled.get(msg.get('call-id'))
            if stale and stale[0]==addr and msg.get('cseq')=='1 INVITE':
                self._terminate_stale_offer(stale[1],msg,addr)
            return
        call=self.calls.get(ref)
        if not call or addr!=call.phone_addr:return
        try: status=int(msg.start.split(' ')[1])
        except (IndexError,ValueError):return
        cseq=msg.get('cseq')
        if cseq!='1 INVITE':return
        if status<200:return
        contact_uri=header_uri(msg.get('contact')) if msg.get('contact') else f'sip:{call.extension}@{addr[0]}:{addr[1]}'
        if status>=300:
            # ACK for non-2xx retains the INVITE branch (RFC 3261).
            if call.outbound_invite:
                self._terminate_stale_offer(parse_message(call.outbound_invite),msg,addr)
            await self._failed_offer(call)
            return
        # A 200 OK may be retransmitted until ACK. Repeat ACK, not BYE.
        if call.state in ('answered_wait_ack','bridged'):
            self.send(self._phone_request('ACK',call,contact_uri,1),addr)
            return
        if call.state!='offering':
            call.phone_to=msg.get('to')
            self.send(self._phone_request('ACK',call,contact_uri,1),addr)
            self.send(self._phone_request('BYE',call,contact_uri),addr)
            return
        try:
            answer=parse_sdp(msg.body,self.config.media_networks)
            if answer.codec != call.trunk_media.codec:
                raise SipError('Codec mismatch')
            call.media.set_remote('phone',answer)
            call.phone_to=msg.get('to')
            call.phone_contact=contact_uri
            self.acd.answer(call.trunk_call_id,call.agent_id)
            call.state='answered_wait_ack'
            call.answered_at=time.monotonic()
            self.send(self._phone_request('ACK',call,contact_uri,1),addr)
            fields=[('Contact',f'<sip:queuepilot@{self.config.advertised_ip}:{self.config.listen_port}>'),('Content-Type','application/sdp')]
            ok=response(call.request,200,fields,sdp_offer(self.config.advertised_ip,call.media.local_port('trunk'),(answer.codec,)),to_tag=call.local_tag)
            call.invite_response=ok
            self.send(ok,call.trunk_addr)
            self.emit('call.answered',call)
        except (SipError,ValueError):
            call.phone_to=msg.get('to')
            self.send(self._phone_request('ACK',call,contact_uri,1),addr)
            self.send(self._phone_request('BYE',call,contact_uri),addr)
            await self._failed_offer(call)

    def _terminate_stale_offer(self,original:Message,reply:Message,addr:tuple[str,int]):
        status=int(reply.start.split(' ')[1])
        request_uri=header_uri(reply.get('contact')) if reply.get('contact') else original.uri
        fields=[('Via',original.get('via') if status>=300 else f'SIP/2.0/UDP {self.config.advertised_ip}:{self.config.listen_port};branch=z9hG4bK{secrets.token_hex(8)}'),
                ('From',original.get('from')),('To',reply.get('to')),('Call-ID',original.get('call-id')),('CSeq','1 ACK')]
        self.send(packet(f'ACK {original.uri if status>=300 else request_uri} SIP/2.0',fields),addr)
        if status==200:
            fields[0]=('Via',f'SIP/2.0/UDP {self.config.advertised_ip}:{self.config.listen_port};branch=z9hG4bK{secrets.token_hex(8)}')
            fields[-1]=('CSeq','2 BYE')
            self.send(packet(f'BYE {request_uri} SIP/2.0',fields),addr)

    async def _failed_offer(self,call:LiveCall):
        if call.phone_call_id:
            self.phone_calls.pop(call.phone_call_id,None)
            if call.phone_addr and call.outbound_invite:
                self._cancelled[call.phone_call_id]=(call.phone_addr,parse_message(call.outbound_invite),time.monotonic()+64)
        if call.agent_id and self.acd.calls.get(call.trunk_call_id) and self.acd.calls[call.trunk_call_id].state=='offering':
            self.acd.reject(call.trunk_call_id,call.agent_id)
        call.phone_call_id=None;call.phone_addr=None;call.agent_id=None;call.extension=None
        await self._dispatch()

    def _ack(self,msg:Message,addr:tuple[str,int]):
        call=self.calls.get(msg.get('call-id'))
        if not call or addr!=call.trunk_addr:
            return
        if call.state=='answered_wait_ack':
            try:
                call.media.connect(call.trunk_media.codec,re.sub(r'[^a-zA-Z0-9-]','_',call.trunk_call_id)[:80])
                call.state='bridged'
                self.emit('call.bridged',call)
            except (OSError,ValueError):
                call.state='media_error'

    async def _bye(self,msg:Message,addr:tuple[str,int]):
        call=self.calls.get(msg.get('call-id'))
        if call and addr==call.trunk_addr:
            self.send(response(msg,200,to_tag=call.local_tag),addr)
            if call.phone_addr and call.phone_contact and call.phone_to:
                self.send(self._phone_request('BYE',call,call.phone_contact),call.phone_addr)
            await self._finish(call)
            return
        inbound=self.phone_calls.get(msg.get('call-id'))
        call=self.calls.get(inbound or '')
        if call and addr==call.phone_addr:
            self.send(response(msg,200),addr)
            from_header=call.request.get('to')
            if ';tag=' not in from_header.lower():from_header+=';tag='+call.local_tag
            fields=[('Via',f'SIP/2.0/UDP {self.config.advertised_ip}:{self.config.listen_port};branch=z9hG4bK{secrets.token_hex(8)}'),
                    ('From',from_header),('To',call.request.get('from')),('Call-ID',call.trunk_call_id),('CSeq','2 BYE'),('Max-Forwards','70')]
            contact_uri=header_uri(call.request.get('contact')) if call.request.get('contact') else call.request.uri
            self.send(packet(f'BYE {contact_uri} SIP/2.0',fields),call.trunk_addr)
            await self._finish(call)

    async def _cancel(self,msg:Message,addr:tuple[str,int]):
        call=self.calls.get(msg.get('call-id'))
        if not call or addr!=call.trunk_addr:
            if self._trusted(addr[0],self.config.avaya_peers):self.send(response(msg,481),addr)
            return
        if call.state!='offering':
            self.send(response(msg,481),addr);return
        self.send(response(msg,200),addr)
        if call.phone_addr and call.phone_call_id and call.outbound_invite and call.state=='offering':
            # CANCEL uses same Via branch and request URI as INVITE.
            source=parse_message(call.outbound_invite)
            f=[(k.title(),v) for k,v in source.headers if k in ('via','from','to','call-id','max-forwards')]
            f.append(('CSeq','1 CANCEL'))
            self.send(packet(f'CANCEL {source.uri} SIP/2.0',f),call.phone_addr)
        self.send(response(call.request,487,to_tag=call.local_tag),call.trunk_addr)
        await self._finish(call)

    async def _finish(self,call:LiveCall):
        acd_call=self.acd.calls.get(call.trunk_call_id)
        if acd_call and acd_call.state=='talking':
            self.acd.complete(call.trunk_call_id)
            self.emit('call.completed',call)
        elif acd_call and acd_call.state in ('offering','waiting'):
            self.acd.abandon(call.trunk_call_id)
            self.emit('call.abandoned',call)
        if call.phone_call_id and call.phone_addr and call.outbound_invite and call.state=='offering':
            self._cancelled[call.phone_call_id]=(call.phone_addr,parse_message(call.outbound_invite),time.monotonic()+64)
        call.media.close()
        self.calls.pop(call.trunk_call_id,None)
        if call.phone_call_id:self.phone_calls.pop(call.phone_call_id,None)
        call.state='ended'

    async def _poll(self):
        while True:
            await asyncio.sleep(0.2)
            async with self._lock:
                now=self.acd.clock()
                for invite_id,(_,_,until) in list(self._cancelled.items()):
                    if time.monotonic()>until:self._cancelled.pop(invite_id,None)
                for call in list(self.calls.values()):
                    if call.state=='answered_wait_ack' and call.answered_at and time.monotonic()-call.answered_at>15:
                        if call.phone_addr and call.phone_contact:
                            self.send(self._phone_request('BYE',call,call.phone_contact),call.phone_addr)
                        await self._finish(call)
                expired=[c for c in self.calls.values() if c.state=='offering' and c.agent_id and self.acd.calls[c.trunk_call_id].offer_deadline is not None and now>=self.acd.calls[c.trunk_call_id].offer_deadline]
                for call in expired:
                    if call.phone_addr and call.outbound_invite:
                        src=parse_message(call.outbound_invite)
                        fields=[(k.title(),v) for k,v in src.headers if k in ('via','from','to','call-id','max-forwards')]+[('CSeq','1 CANCEL')]
                        self.send(packet(f'CANCEL {src.uri} SIP/2.0',fields),call.phone_addr)
                    await self._failed_offer(call)
                self.acd.tick()
                await self._dispatch()


def config_from_json(path:Path)->Config:
    obj=json.loads(path.read_text(encoding='utf-8'))
    routes=tuple(IngressRoute(str(r['did']),str(r['queue']),frozenset(r.get('skills',[])),int(r.get('priority',0))) for r in obj['routes'])
    bindings={str(k):(str(v['extension']),frozenset(v.get('skills',[]))) for k,v in obj['agents'].items()}
    secret=obj.get('nonce_secret','')
    if secret.startswith('REPLACE_') or len(secret)<32:
        raise ValueError('Configure unique nonce_secret (32+ characters)')
    directory=Path(obj['record_directory']) if obj.get('record_calls',False) else None
    return Config(str(obj.get('listen_ip','127.0.0.1')),int(obj.get('listen_port',15060)),str(obj.get('advertised_ip','127.0.0.1')),
                  tuple(obj['avaya_peers']),tuple(obj['phone_networks']),tuple(obj['media_networks']),routes,
                  bindings,{str(k):str(v) for k,v in obj['extension_passwords'].items()},
                  str(obj.get('sip_realm','queuepilot.local')),secret.encode('utf-8'),int(obj.get('offer_seconds',20)),directory)


async def _main():
    parser=argparse.ArgumentParser(description='QueuePilot native UDP SIP lab gateway (no Asterisk)')
    parser.add_argument('--config',required=True,help='Local JSON configuration (never commit real credentials)')
    parser.add_argument('--lab',action='store_true',help='Explicitly acknowledge nonproduction lab limitations')
    args=parser.parse_args()
    if not args.lab:parser.error('Native SIP runtime is LAB-ONLY; pass --lab')
    config=config_from_json(Path(args.config))
    server=NativeSwitch(config)
    await server.start()
    print(f'QueuePilot LAB SIP gateway listening on {config.listen_ip}:{config.listen_port}; no production support')
    try: await asyncio.Event().wait()
    finally: await server.stop()


if __name__=='__main__':
    asyncio.run(_main())
