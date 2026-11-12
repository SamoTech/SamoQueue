"""Small standards-aware SIP primitives for QueuePilot's isolated UDP lab.

This is deliberately NOT a production SIP implementation. In particular, RFC 3261
transaction timers, full reliable provisional responses, TLS and session refreshes
need a security-reviewed SIP stack before commercial use.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
import time
from dataclasses import dataclass
from typing import Mapping

MAX_SIP_PACKET = 16384
_ALIASES = {'v':'via','f':'from','t':'to','i':'call-id','m':'contact','l':'content-length','c':'content-type'}


class SipError(ValueError):
    pass


@dataclass(frozen=True)
class Message:
    start: str
    headers: tuple[tuple[str, str], ...]
    body: bytes = b''

    def get(self, key: str, default: str = '') -> str:
        key = key.lower()
        return next((v for k, v in self.headers if k == key), default)

    def all(self, key: str) -> list[str]:
        return [v for k, v in self.headers if k == key.lower()]

    @property
    def method(self) -> str:
        return self.start.split(' ', 1)[0].upper()

    @property
    def uri(self) -> str:
        parts = self.start.split(' ')
        if len(parts) != 3 or parts[2] != 'SIP/2.0':
            raise SipError('Invalid SIP request line')
        return parts[1]


def parse_message(data: bytes) -> Message:
    if not data or len(data) > MAX_SIP_PACKET:
        raise SipError('Invalid SIP packet length')
    head, marker, body = data.partition(b'\r\n\r\n')
    if not marker:
        raise SipError('Missing SIP header separator')
    try:
        lines = head.decode('latin-1').split('\r\n')
    except UnicodeError as exc:
        raise SipError('Invalid header encoding') from exc
    if not lines or not lines[0] or '\x00' in lines[0]:
        raise SipError('Invalid start line')
    fields = []
    for line in lines[1:]:
        if not line or line[0] in ' \t' or ':' not in line:
            raise SipError('Folded or malformed header rejected')
        k, v = line.split(':', 1)
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]*', k):
            raise SipError('Invalid header name')
        if any(ord(c) < 32 and c != '\t' for c in v):
            raise SipError('Invalid control character')
        k = _ALIASES.get(k.lower(), k.lower())
        fields.append((k, v.strip()))
    msg = Message(lines[0], tuple(fields), body)
    for key in ('via','from','to','call-id','cseq','content-length'):
        if len(msg.all(key)) != 1:
            raise SipError(f'Invalid {key} count')
    try:
        n = int(msg.get('content-length'))
    except ValueError as exc:
        raise SipError('Invalid Content-Length') from exc
    if n < 0 or n != len(body):
        raise SipError('SIP body length mismatch')
    if not msg.start.startswith('SIP/2.0 '):
        m = msg.method
        if not re.fullmatch(r'[A-Z]+', m) or not msg.get('cseq').endswith(' '+m):
            raise SipError('CSeq/method mismatch')
        if not msg.get('via').lower().startswith('sip/2.0/udp '):
            raise SipError('UDP lab stack only')
        if 'branch=z9hG4bK' not in msg.get('via'):
            raise SipError('Missing RFC 3261 branch')
    return msg


def packet(start: str, fields: list[tuple[str,str]], body: bytes = b'') -> bytes:
    if any('\r' in k+v or '\n' in k+v for k,v in fields):
        raise SipError('Header injection')
    lines = [start] + [f'{k}: {v}' for k,v in fields]
    lines.append(f'Content-Length: {len(body)}')
    return ('\r\n'.join(lines)+'\r\n\r\n').encode('latin-1') + body


REASONS = {100:'Trying',180:'Ringing',200:'OK',400:'Bad Request',401:'Unauthorized',403:'Forbidden',404:'Not Found',405:'Method Not Allowed',408:'Request Timeout',480:'Temporarily Unavailable',481:'Call/Transaction Does Not Exist',486:'Busy Here',488:'Not Acceptable Here',500:'Server Internal Error',503:'Service Unavailable'}


def response(req: Message, status: int, extra: list[tuple[str,str]] | None = None, body: bytes=b'', to_tag: str='') -> bytes:
    to = req.get('to')
    if to_tag and ';tag=' not in to.lower():
        to += ';tag='+to_tag
    fields = [('Via',v) for v in req.all('via')]
    fields += [('From',req.get('from')), ('To',to), ('Call-ID',req.get('call-id')), ('CSeq',req.get('cseq'))]
    fields += (extra or [])
    return packet(f'SIP/2.0 {status} {REASONS.get(status,"Error")}', fields, body)


def uri_user(uri: str) -> str:
    m = re.match(r'^sips?:([^@;>]+)@',uri,re.IGNORECASE)
    if not m:
        raise SipError('SIP URI requires a user')
    return m.group(1)


def header_uri(header: str) -> str:
    m=re.search(r'<(sips?:[^>]+)>',header,re.IGNORECASE)
    if m:
        return m.group(1)
    m=re.search(r'\b(sips?:[^\s;>]+)',header,re.IGNORECASE)
    if not m:
        raise SipError('Missing URI in SIP header')
    return m.group(1)


@dataclass(frozen=True)
class MediaAddress:
    host: str
    port: int
    codec: int
    dtmf_payload: int | None = None


def parse_sdp(body: bytes, trusted_media_networks: tuple[str,...]) -> MediaAddress:
    try:
        lines = body.decode('ascii').replace('\r','').split('\n')
    except UnicodeDecodeError as exc:
        raise SipError('Non-ASCII SDP') from exc
    host = None
    port = None
    formats = []
    rtpmap = {}
    for line in lines:
        if line.startswith('c=IN IP4 '):
            host=line[9:].strip().split('/')[0]
        elif line.startswith('a=rtpmap:'):
            match = re.fullmatch(r"a=rtpmap:(\d{1,3})\s+([A-Za-z0-9_-]+)/8000", line, re.I)
            if match:
                rtpmap[int(match.group(1))] = match.group(2).lower()
        elif line.startswith('m=audio '):
            parts=line.split()
            if len(parts) < 4 or parts[2] != 'RTP/AVP':
                raise SipError('Unsupported media transport')
            try:
                port=int(parts[1]); formats=[int(x) for x in parts[3:]]
            except ValueError as exc:
                raise SipError('Invalid audio media') from exc
    if host is None or port is None or not (1<=port<=65535):
        raise SipError('Missing audio address')
    try:
        addr=ipaddress.ip_address(host)
        networks=[ipaddress.ip_network(n,strict=False) for n in trusted_media_networks]
    except ValueError as exc:
        raise SipError('Invalid media address') from exc
    if not isinstance(addr,ipaddress.IPv4Address) or not any(addr in n for n in networks):
        raise SipError('Media destination outside trust boundaries')
    available=[n for n in (0,8) if n in formats]
    if not available:
        raise SipError('Only G.711 PCMU or PCMA supported in lab')
    event_pts=[pt for pt in formats if 96 <= pt <= 127 and rtpmap.get(pt) == 'telephone-event']
    if len(event_pts)>1:
        raise SipError('Multiple telephone-event payload types unsupported')
    return MediaAddress(host,port,available[0], event_pts[0] if event_pts else None)


def sdp_offer(host:str, port:int, codecs:tuple[int,...]=(0,8), *, dtmf_payload:int|None=None) -> bytes:
    if not codecs or any(c not in (0,8) for c in codecs):
        raise SipError('Unsupported codec')
    if dtmf_payload is not None and not 96 <= dtmf_payload <= 127:
        raise SipError('Invalid telephone-event payload type')
    attrs = ['a=rtpmap:0 PCMU/8000','a=rtpmap:8 PCMA/8000']
    lines=['v=0',f'o=queuepilot 1 1 IN IP4 {host}','s=QueuePilot','t=0 0',f'c=IN IP4 {host}',f'm=audio {port} RTP/AVP '+ ' '.join(map(str,codecs+(dtmf_payload,) if dtmf_payload is not None else codecs)),'a=sendrecv']
    lines.extend(x for x in attrs if int(x.split(':',1)[1].split()[0]) in codecs)
    if dtmf_payload is not None:
        lines += [f'a=rtpmap:{dtmf_payload} telephone-event/8000', f'a=fmtp:{dtmf_payload} 0-15']
    return ('\r\n'.join(lines)+'\r\n').encode('ascii')


_DIGEST_TOKEN = re.compile(r'(\w+)\s*=\s*(?:"([^"\r\n]*)"|([^,\s]+))')


def parse_digest(text: str) -> dict[str,str]:
    if not text.startswith('Digest '):
        return {}
    return {m.group(1).lower(): m.group(2) if m.group(2) is not None else m.group(3) for m in _DIGEST_TOKEN.finditer(text[7:])}


class RegistrarAuth:
    """SHA-256 digest auth for isolated SIP registration; nonces have time bounds."""

    def __init__(self, realm: str, users: Mapping[str,str], secret: bytes, clock=time.time):
        if not users or not secret or len(secret)<32:
            raise ValueError('Explicit users and 32+ byte nonce secret required')
        self.realm=realm
        self.users=dict(users)
        self.secret=secret
        self.clock=clock
        self._seen:dict[tuple[str,str,str],int]={}

    def nonce(self) -> str:
        ts=str(int(self.clock()))
        salt=secrets.token_hex(12)
        data=f'{ts}:{salt}'
        sig=hmac.new(self.secret,data.encode(),hashlib.sha256).hexdigest()
        return f'{data}:{sig}'

    def _valid_nonce(self, nonce: str) -> bool:
        try:
            ts,salt,sig=nonce.split(':')
            t=int(ts)
            if not 0 <= self.clock()-t <= 120:
                return False
            expected=hmac.new(self.secret,f'{ts}:{salt}'.encode(),hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected,sig)
        except (ValueError,TypeError):
            return False

    def challenge(self)->str:
        return f'Digest realm="{self.realm}",nonce="{self.nonce()}",algorithm=SHA-256,qop="auth"'

    def verify(self, msg: Message, expected_user: str) -> bool:
        a=parse_digest(msg.get('authorization'))
        required=('username','realm','nonce','uri','response','qop','nc','cnonce','algorithm')
        if any(key not in a for key in required): return False
        if a['username']!=expected_user or a['realm']!=self.realm or a['algorithm'].upper()!='SHA-256' or a['qop']!='auth' or a['uri']!=msg.uri:
            return False
        if not self._valid_nonce(a['nonce']): return False
        try:
            nc=int(a['nc'],16)
            if nc<=0: return False
        except ValueError:
            return False
        key=(a['nonce'],expected_user,a['cnonce'])
        if nc<=self._seen.get(key,0): return False
        pwd=self.users.get(expected_user)
        if pwd is None: return False
        h=lambda s:hashlib.sha256(s.encode('utf-8')).hexdigest()
        ha1=h(f'{expected_user}:{self.realm}:{pwd}')
        ha2=h(f'{msg.method}:{msg.uri}')
        response_expected=h(f'{ha1}:{a["nonce"]}:{a["nc"]}:{a["cnonce"]}:auth:{ha2}')
        if not hmac.compare_digest(response_expected,a['response'].lower()): return False
        # bounded-memory replay store
        if len(self._seen)>40000: self._seen.clear()
        self._seen[key]=nc
        return True
