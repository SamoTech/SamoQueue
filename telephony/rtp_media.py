"""Lab-only in-process G.711 RTP anchoring, with optional ONE stereo WAV.

It relays packet payloads only after both call legs agree on one G.711 codec.
No SRTP, RTCP, jitter buffer, PLC, resampling or conference mixing yet.
Recordings are disabled by default and intended ONLY for consented lab calls.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from .sip_stack import MediaAddress
from .stereo_recording import StereoCallRecorder, decode_ulaw, decode_alaw



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
    established calls. Captures both directions to one approximately-aligned
    stereo WAV (caller left, agent right). No conference mixing is claimed.
    """

    def __init__(self, bind_host:str, advertised_host:str, recording_directory:Path|None=None):
        self.bind_host=bind_host
        self.advertised_host=advertised_host
        self.recording_directory=recording_directory
        self.legs:dict[str,MediaLeg]={}
        self.enabled=False
        self.codec:int|None=None
        self._recorder:StereoCallRecorder|None=None
        self.packet_counts={'trunk':0,'phone':0}
        self.recorded_paths:dict[str,Path]={}
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
            # One recording file per call, no separate WAV legs.
            path=directory/f'{call_tag}-conversation.wav'
            self._recorder=StereoCallRecorder(path)
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
        if self._recorder is not None:
            timestamp=int.from_bytes(packet[4:8], 'big')
            self._recorder.add(side,timestamp,payload,self.codec)
            if self._recorder.truncated:
                self.recording_truncated.add('conversation')

    def close(self):
        self.enabled=False
        if self._recorder is not None:
            path=self._recorder.close()
            if self._recorder.truncated:
                self.recording_truncated.add('conversation')
            if path is not None:
                self.recorded_paths={'conversation':path}
            self._recorder=None
        for leg in self.legs.values():
            leg.transport.close()
        self.legs.clear()
