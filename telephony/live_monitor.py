"""Native RTP supervisor listening bridge, isolated single-host LAB only.

This is part of QueuePilot, not an external PBX/recorder. The engine claims an
approved monitor command from the management API, attaches an audio-only tap to
one live bridged call, mixes G.711 PCM from both legs, and sends bounded PCM16
frames to the existing authenticated supervisor WebSocket relay.

No microphone injection, whisper, barge, SRTP, jitter/PLC or HA is implemented.
Monitoring defaults off and needs a distinct approving reviewer account.
"""
from __future__ import annotations

import asyncio
from collections import deque
import ipaddress
import struct
from urllib.parse import urlsplit
from typing import Any

import httpx

from .stereo_recording import decode_alaw, decode_ulaw


class LiveAudioMixer:
    """Bounded 8kHz per-leg frames -> 16kHz mono PCM16 observation stream.

    Audio is mixed *only* while an approved listening session is active. Missing
    frames are silence; telephone RTP is never modified by this tap.
    """

    def __init__(self, max_frames: int = 12):
        self._buffers: dict[str, deque[bytes]] = {
            'trunk': deque(maxlen=max_frames), 'phone': deque(maxlen=max_frames)
        }
        self._partials: dict[str, bytearray] = {'trunk': bytearray(), 'phone': bytearray()}
        self._started = False
        self.closed = False

    def ingest(self, side: str, payload: bytes, codec: int) -> None:
        if self.closed or side not in self._buffers or codec not in (0, 8) or not payload or len(payload) > 1600:
            return
        decoder = decode_ulaw if codec == 0 else decode_alaw
        partial = self._partials[side]
        partial.extend(b''.join(struct.pack('<h', decoder(value)) for value in payload))
        # 20ms of 8kHz signed 16-bit audio per frame.
        while len(partial) >= 320:
            self._buffers[side].append(bytes(partial[:320]))
            del partial[:320]
        self._started = True

    def frame(self) -> bytes | None:
        if self.closed or not self._started:
            return None
        trunks = self._buffers['trunk']
        phones = self._buffers['phone']
        trunk = trunks.popleft() if trunks else None
        phone = phones.popleft() if phones else None
        if trunk is None and phone is None:
            return bytes(640)  # silence; preserve time in already-started stream
        # 160 mono samples, repeated to 16kHz; simple lab resampling, no claim of HQ audio.
        output = bytearray()
        for i in range(0, 320, 2):
            a = struct.unpack_from('<h', trunk, i)[0] if trunk is not None else 0
            b = struct.unpack_from('<h', phone, i)[0] if phone is not None else 0
            sample = (a + b) // 2 if (trunk is not None and phone is not None) else a + b
            output.extend(struct.pack('<hh', sample, sample))
        return bytes(output)

    def close(self) -> None:
        self.closed = True
        self._buffers['trunk'].clear()
        self._buffers['phone'].clear()
        self._partials['trunk'].clear()
        self._partials['phone'].clear()


class NativeMonitorWorker:
    """Opt-in same-host bridge between the native RTP engine and dashboard API.

    The monitor credential never enters the browser or RTP packet path. In the
    presence of API errors or revoked approval, detach immediately (fail closed).
    """

    def __init__(self, native_switch: Any, api_origin: str, monitor_key: str,
                 *, client: httpx.AsyncClient | None = None):
        parsed = urlsplit(api_origin)
        if parsed.scheme != 'http' or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ValueError('LAB monitor API must be a local http://127.0.0.1:<port> origin')
        try:
            if not ipaddress.ip_address(parsed.hostname or '').is_loopback:
                raise ValueError('Monitor API must bind to loopback')
        except ValueError as exc:
            raise ValueError('Monitor API must be a loopback IP') from exc
        if len(monitor_key) < 32 or monitor_key.startswith('REPLACE_'):
            raise ValueError('Provide a unique MONITOR_ENGINE_KEY before enabling listening')
        self.switch = native_switch
        self.key = monitor_key
        self.client = client or httpx.AsyncClient(base_url=api_origin, timeout=2.0, trust_env=False)
        self._owns_client = client is None
        self._sessions: dict[str, tuple[str, LiveAudioMixer]] = {}
        self._task: asyncio.Task | None = None
        self.errors: list[str] = []
        self._headers = {'X-Monitor-Engine-Key': monitor_key}

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError('Monitor worker already started')
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for sid in list(self._sessions):
            await self._detach(sid, result='ended')
        if self._owns_client:
            await self.client.aclose()

    def _detach_local(self, sid: str) -> None:
        current = self._sessions.pop(sid, None)
        if current is None:
            return
        call_id, mixer = current
        call = self.switch.calls.get(call_id)
        if call is not None and getattr(call.media, 'on_audio', None) == mixer.ingest:
            call.media.on_audio = None
        mixer.close()

    async def _result(self, sid: str, result: str, message: str = '') -> None:
        resp = await self.client.post(f'/api/cc/monitor/engine/sessions/{sid}/result',
                                      headers=self._headers,
                                      json={'result': result, 'code': 'NATIVE_RTP_LAB', 'message': message[:160]})
        resp.raise_for_status()

    async def _detach(self, sid: str, result: str = 'ended') -> None:
        self._detach_local(sid)
        try:
            await self._result(sid, result)
        except (httpx.HTTPError, ValueError) as exc:
            self.errors.append('monitor_end_ack_failed:' + type(exc).__name__)

    async def poll_once(self) -> None:
        # A finished call can never remain attached even if the API is unreachable.
        for sid, (call_id, _) in list(self._sessions.items()):
            call = self.switch.calls.get(call_id)
            if call is None or call.state != 'bridged':
                await self._detach(sid)
        reply = await self.client.post('/api/cc/monitor/engine/claim', headers=self._headers)
        reply.raise_for_status()
        command = reply.json().get('command')
        if command is None:
            return
        sid = command['id']
        if command['action'] == 'stop_listen':
            await self._detach(sid)
            return
        if command['action'] != 'start_listen':
            return
        call_id = command['call_id']
        call = self.switch.calls.get(call_id)
        if (call is None or call.state != 'bridged' or not call.trunk_established
                or call.extension != command['extension'] or call_id in [v[0] for v in self._sessions.values()]):
            await self._result(sid, 'failed', 'No matching established native RTP call')
            return
        mixer = LiveAudioMixer()
        call.media.on_audio = mixer.ingest
        self._sessions[sid] = (call_id, mixer)
        try:
            await self._result(sid, 'active')
        except (httpx.HTTPError, ValueError):
            self._detach_local(sid)
            raise

    async def audio_once(self) -> None:
        for sid, (call_id, mixer) in list(self._sessions.items()):
            call = self.switch.calls.get(call_id)
            if not call or call.state != 'bridged':
                await self._detach(sid)
                continue
            frames = [mixer.frame() for _ in range(5)]
            if all(frame is None for frame in frames):
                continue
            # Never broadcast pre-session RTP. Frames are max 5 x 20ms.
            data = b''.join(frame if frame is not None else bytes(640) for frame in frames)
            try:
                res = await self.client.post(f'/api/cc/monitor/engine/sessions/{sid}/audio',
                                             headers={**self._headers, 'Content-Type': 'application/octet-stream'},
                                             content=data)
                if res.status_code in (403, 409):
                    # Admin revoked approval or call is over. Stop locally first;
                    # never classify a normal Stop as a failed stop operation.
                    await self._detach(sid, result='ended')
                    continue
                res.raise_for_status()
            except (httpx.HTTPError, ValueError) as exc:
                self.errors.append('monitor_stream_failed:' + type(exc).__name__)
                await self._detach(sid, result='uncertain')

    async def _run(self) -> None:
        try:
            while True:
                try:
                    await self.poll_once()
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    self.errors.append('monitor_poll_failed:' + type(exc).__name__)
                    # Control-plane outage must stop every active observer.
                    for sid in list(self._sessions):
                        await self._detach(sid, result='uncertain')
                for _ in range(5):
                    await asyncio.sleep(0.1)
                    await self.audio_once()
        except asyncio.CancelledError:
            raise
