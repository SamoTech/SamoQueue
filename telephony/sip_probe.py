"""LAB-ONLY QueuePilot SIP OPTIONS probe (not a call handling gateway).

Demonstrates a native UDP transport endpoint without Asterisk or a third-party
PBX. No INVITE/REGISTER/BYE/dialog/RTP support. Inbound INVITE explicitly
receives 503; never route production calls to this listener. Default binds
loopback and accepts only loopback. No network side effects on import.
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import re
import secrets
from collections.abc import Iterable

_MAX_PACKET = 4096
_REQUIRED = ('via', 'from', 'to', 'call-id', 'cseq')
_HEADER = re.compile(r'^[A-Za-z][A-Za-z0-9-]*$')


def sip_probe_response(packet: bytes, peer: str, *, allowed_peers: Iterable[str] = ('127.0.0.1/32',)) -> bytes | None:
    """Reply only to complete, authorized, bounded SIP OPTIONS packets.

    A trusted caller can use OPTIONS to test UDP reachability. INVITE gets 503,
    because there is no native media/dialog server to accept it yet. Other
    requests get a conservative 405 or 481; ACK gets no response.
    """
    try:
        address = ipaddress.ip_address(peer)
        approved = any(address in ipaddress.ip_network(network, strict=False) for network in allowed_peers)
    except ValueError:
        return None
    if not approved or len(packet) > _MAX_PACKET or b'\x00' in packet:
        return None
    try:
        decoded = packet.decode('latin-1')
        head, _, _body = decoded.partition('\r\n\r\n')
        lines = head.split('\r\n')
        method, uri, version = lines[0].split(' ', 2)
        if version != 'SIP/2.0' or not uri.startswith('sip:'):
            return None
        fields: dict[str, str] = {}
        for line in lines[1:]:
            if not line or line[0] in (' ', '\t') or ':' not in line:
                return None
            key, value = line.split(':', 1)
            if not _HEADER.fullmatch(key):
                return None
            key = key.lower()
            if key in fields or '\n' in value:
                return None
            fields[key] = value.strip()
        if any(not fields.get(key) for key in _REQUIRED):
            return None
        if not re.fullmatch(r'\d+ ' + re.escape(method), fields['cseq']):
            return None
        if '\r\n\r\n' not in decoded:
            return None
    except (UnicodeDecodeError, ValueError, IndexError):
        return None
    if method == 'ACK':
        return None
    if method == 'OPTIONS':
        status, reason = 200, 'OK'
    elif method == 'INVITE':
        status, reason = 503, 'Service Unavailable'
    elif method in ('BYE', 'CANCEL'):
        status, reason = 481, 'Call/Transaction Does Not Exist'
    else:
        status, reason = 405, 'Method Not Allowed'
    tag = secrets.token_hex(7)
    to = fields['to'] if ';tag=' in fields['to'] else fields['to'] + ';tag=' + tag
    result = (f'SIP/2.0 {status} {reason}\r\n'
              f'Via: {fields["via"]}\r\n'
              f'From: {fields["from"]}\r\n'
              f'To: {to}\r\n'
              f'Call-ID: {fields["call-id"]}\r\n'
              f'CSeq: {fields["cseq"]}\r\n'
              f'Server: QueuePilot-LabProbe\r\n'
              f'Content-Length: 0\r\n\r\n')
    return result.encode('latin-1')


class ProbeProtocol(asyncio.DatagramProtocol):
    def __init__(self, peers: tuple[str, ...]):
        self.peers = peers
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        response = sip_probe_response(data, addr[0], allowed_peers=self.peers)
        if response is not None and self.transport is not None:
            self.transport.sendto(response, addr)


async def main():
    parser = argparse.ArgumentParser(description='QueuePilot LAB-ONLY SIP OPTIONS probe (not a PBX)')
    parser.add_argument('--bind', default='127.0.0.1', help='local bind address (defaults to loopback)')
    parser.add_argument('--port', type=int, default=15060, help='nonstandard lab port')
    parser.add_argument('--peer', action='append', default=[], help='allowed SIP source network CIDR')
    args = parser.parse_args()
    if not (1 <= args.port <= 65535):
        parser.error('port out of range')
    peers = tuple(args.peer or ['127.0.0.1/32'])
    for network in peers:
        ipaddress.ip_network(network, strict=False)
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: ProbeProtocol(peers),
                                                       local_addr=(args.bind, args.port))
    print(f'LAB SIP OPTIONS probe on {args.bind}:{args.port} — NOT capable of answering calls')
    try:
        await asyncio.Event().wait()
    finally:
        transport.close()


if __name__ == '__main__':
    asyncio.run(main())
