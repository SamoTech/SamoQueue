"""Lab-only in-process G.711 RTP anchoring, with optional per-leg WAV diagnostics.

It relays packet payloads only after both call legs agree on one G.711 codec.
No SRTP, RTCP, jitter buffer, PLC, resampling or conference mixing yet.
Recordings are disabled by default and intended ONLY for consented lab calls.
"""
from __future__ import annotations

import asyncio
import os
import struct
import wave
from dataclasses import dataclass
from pathlib import Path

from .sip_stack import MediaAddress


def decode_ulaw(byte:int) -> int:
    v=(~byte)&255
    segment=(v>>4)&7
    t=((v&15)<<3)+132
    t<<=segment
    return (132-t) if v&128 else (t-132)


def decode_alaw(byte:int) -> int:
    v=byte^0x55
    segment=(v&0x70)>>4
    t=(v&15)<<4
    if segment==0: t+=8
    elif segment==1: t+=0x108
    else: t=(t+0x108)<<(segment-1)
    return t if v&128 else -t


_MULAW_PCM=tuple(struct.pack('<h',decode_ulaw(x)) for x in range(256))
_ALAW_PCM=tuple(struct.pack('<h',decode_alaw(x)) for x in range(256))


@dataclass
class MediaLeg:
    remote: MediaAddress
    local_port: int
    transport: asyncio.DatagramTransport


class _Receiver(asyncio.DatagramProtocol):
    def __init__(self, owner:'RtpRelay', name:str):
        self.owner=owner
        self.name=name

    def datagram_received(self,data:bytes,addr):
        self.owner.forward(self.name,data,addr)


class RtpRelay:
    """A media anchor whose addresses belong to QueuePilot, not Avaya/phones.

    Rejects unsolicited RTP and unexpected payload type; relays only on
    established calls. Records each direction separately so there is no claim
    of time-aligned/stereo or conference-quality capture.
    """

    def __init__(self, bind_host:str, advertised_host:str, recording_directory:Path|None=None):
        self.bind_host=bind_host
        self.advertised_host=advertised_host
        self.recording_directory=recording_directory
        self.legs:dict[str,MediaLeg]={}
        self.enabled=False
        self.codec:int|None=None
        self._writers:dict[str,wave.Wave_write]={}
        self.packet_counts={'trunk':0,'phone':0}
        self.recorded_paths:dict[str,Path]={}
        self._recorded_frames={'trunk':0,'phone':0}
        self.recording_truncated:set[str]=set()

    async def open(self)->None:
        loop=asyncio.get_running_loop()
        for side in ('trunk','phone'):
            t,_=await loop.create_datagram_endpoint(lambda side=side:_Receiver(self,side),local_addr=(self.bind_host,0))
            self.legs[side]=MediaLeg(MediaAddress('127.0.0.1',1,0),t.get_extra_info('sockname')[1],t)

    def local_port(self,side:str)->int:
        return self.legs[side].local_port

    def set_remote(self,side:str,remote:MediaAddress):
        self.legs[side].remote=remote

    def connect(self,codec:int,call_tag:str)->None:
        if codec not in (0,8) or self.legs['trunk'].remote.codec!=codec or self.legs['phone'].remote.codec!=codec:
            raise ValueError('RTP codec mismatch')
        self.codec=codec
        if self.recording_directory is not None:
            directory=self.recording_directory
            directory.mkdir(parents=True,exist_ok=True,mode=0o700)
            os.chmod(directory,0o700)
            for side in ('trunk','phone'):
                path=directory/f'{call_tag}-{side}.wav'
                fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                os.close(fd)
                writer=wave.open(str(path),'wb')
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(8000)
                self._writers[side]=writer
                self.recorded_paths[side]=path
        self.enabled=True

    def forward(self,side:str,packet:bytes,src:tuple[str,int])->None:
        if not self.enabled or self.codec is None or len(packet)<12 or packet[0]>>6!=2:
            return
        leg=self.legs.get(side)
        if leg is None or src!=(leg.remote.host,leg.remote.port):
            return
        # RTP CSRC count, extension and padding are validated, rather than
        # interpreting arbitrary trailing bytes as speech samples.
        if packet[1]&0x7f != self.codec:
            return
        n=12+4*(packet[0]&15)
        if n>len(packet): return
        if packet[0]&16:
            if len(packet)<n+4:return
            length=int.from_bytes(packet[n+2:n+4],'big')*4
            n+=4+length
        if n>len(packet):return
        stop=len(packet)
        if packet[0]&32:
            pad=packet[-1]
            if not pad or stop-pad<n:return
            stop-=pad
        payload=packet[n:stop]
        if not payload:return
        other='phone' if side=='trunk' else 'trunk'
        target=self.legs.get(other)
        if not target:return
        target.transport.sendto(packet,(target.remote.host,target.remote.port))
        self.packet_counts[side]+=1
        if side in self._writers:
            table=_MULAW_PCM if self.codec==0 else _ALAW_PCM
            # Keep captures bounded; oversized legs are not archived as complete calls.
            if self._recorded_frames[side]+len(payload) <= 8_000 * 1_800:
                self._writers[side].writeframesraw(b''.join(table[c] for c in payload))
                self._recorded_frames[side]+=len(payload)
            else:
                self.recording_truncated.add(side)

    def close(self):
        self.enabled=False
        for writer in self._writers.values():
            writer.close()
        self._writers.clear()
        # A call with no RTP has no playable recording to archive.
        self.recorded_paths={side:path for side,path in self.recorded_paths.items()
                             if self._recorded_frames.get(side,0)>0 and side not in self.recording_truncated}
        for leg in self.legs.values():
            leg.transport.close()
        self.legs.clear()
