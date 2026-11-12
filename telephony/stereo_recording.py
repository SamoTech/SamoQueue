"""Single-file, two-channel G.711 RTP recorder (isolated QueuePilot lab only).

One WAV per call: channel 0 = inbound trunk/caller, channel 1 = agent phone.
Per-leg RTP timestamps preserve spacing within a stream. First-packet arrival
sets the offset on a shared monotonic clock; missing media becomes silence.
An 0.5s hold window permits modest jitter/reordering before frames are written.
This is not a production jitter-buffer, SRTP or legally compliant recorder.
"""
from __future__ import annotations

import os
import struct
import time
import wave
from pathlib import Path
from typing import Callable


SAMPLE_RATE = 8000
MAX_SECONDS = 1800
HOLD_FRAMES = SAMPLE_RATE // 2
MAX_AHEAD_FRAMES = SAMPLE_RATE * 2
_ZERO_CHUNK = bytes(4096 * 4)


def decode_ulaw(byte: int) -> int:
    v = (~byte) & 255
    segment = (v >> 4) & 7
    t = (((v & 15) << 3) + 132) << segment
    return (132 - t) if v & 128 else (t - 132)


def decode_alaw(byte: int) -> int:
    v = byte ^ 0x55
    segment = (v & 0x70) >> 4
    t = (v & 15) << 4
    if segment == 0:
        t += 8
    elif segment == 1:
        t += 0x108
    else:
        t = (t + 0x108) << (segment - 1)
    return t if v & 128 else -t


_MULAW_PCM = tuple(struct.pack('<h', decode_ulaw(i)) for i in range(256))
_ALAW_PCM = tuple(struct.pack('<h', decode_alaw(i)) for i in range(256))


class StereoCallRecorder:
    """Incrementally construct one bounded WAV from both authorized RTP legs.

    Timestamps are not shared across SIP legs; relative RTP spacing is combined
    with wall-clock arrival of each leg's *first* packet. This is an approximate
    alignment; it cannot correct clock drift without RTCP or acoustic analysis.
    """

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.monotonic):
        self.path = path
        self.clock = clock
        self.started_at = clock()
        self._base_frame = 0
        self._max_frame = 0
        self._buffer = bytearray()
        self._anchors: dict[str, tuple[int, int]] = {}
        self._received = {'trunk': 0, 'phone': 0}
        self.truncated = False
        self.closed = False
        self._file = None
        self._wav = None
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self._file = os.fdopen(fd, 'wb')
            self._wav = wave.open(self._file, 'wb')
            self._wav.setnchannels(2)
            self._wav.setsampwidth(2)
            self._wav.setframerate(SAMPLE_RATE)
        except Exception:
            if self._wav:
                self._wav.close()
            if self._file:
                self._file.close()
            self.path.unlink(missing_ok=True)
            raise

    def _flush_to(self, end: int) -> None:
        """Emit synchronized interleaved samples; absent data is zero/silence."""
        if end <= self._base_frame:
            return
        frames = min(len(self._buffer) // 4, end - self._base_frame)
        if frames:
            n = frames * 4
            self._wav.writeframesraw(self._buffer[:n])
            del self._buffer[:n]
            self._base_frame += frames
        remaining = end - self._base_frame
        while remaining > 0:
            chunk = min(remaining, 4096)
            self._wav.writeframesraw(_ZERO_CHUNK[:chunk * 4])
            self._base_frame += chunk
            remaining -= chunk

    def add(self, side: str, timestamp: int, payload: bytes, codec: int) -> None:
        if self.closed or self.truncated:
            return
        if side not in ('trunk', 'phone') or codec not in (0, 8):
            raise ValueError('Invalid recording media')
        if not payload or len(payload) > 8000:
            return
        now_frames = max(0, round((self.clock() - self.started_at) * SAMPLE_RATE))
        if side not in self._anchors:
            self._anchors[side] = (timestamp, now_frames)
        anchor_ts, anchor_frame = self._anchors[side]
        delta = ((timestamp - anchor_ts + (1 << 31)) % (1 << 32)) - (1 << 31)
        start = anchor_frame + delta
        end = start + len(payload)
        if (end > MAX_SECONDS * SAMPLE_RATE or start > now_frames + MAX_AHEAD_FRAMES):
            self.truncated = True
            return
        if end <= self._base_frame or start < -MAX_AHEAD_FRAMES:
            return  # too late / malformed packet; cannot overwrite committed WAV
        try:
            # Free stale frames *before* extending for a long silent period.
            self._flush_to(max(0, min(now_frames - HOLD_FRAMES, start - HOLD_FRAMES)))
            if start < self._base_frame:
                payload = payload[self._base_frame - start:]
                start = self._base_frame
                if not payload:
                    return
            end = start + len(payload)
            required = (end - self._base_frame) * 4
            if required > (MAX_AHEAD_FRAMES + HOLD_FRAMES + 8000) * 4:
                self.truncated = True
                return
            if len(self._buffer) < required:
                self._buffer.extend(b'\0' * (required - len(self._buffer)))
            table = _MULAW_PCM if codec == 0 else _ALAW_PCM
            channel_offset = 0 if side == 'trunk' else 2
            at = (start - self._base_frame) * 4 + channel_offset
            for sample in payload:
                self._buffer[at:at + 2] = table[sample]
                at += 4
            self._received[side] += len(payload)
            self._max_frame = max(self._max_frame, end)
            self._flush_to(min(self._max_frame, max(0, now_frames - HOLD_FRAMES)))
        except OSError:
            self.truncated = True

    def close(self) -> Path | None:
        if self.closed:
            return None
        self.closed = True
        try:
            if self.clock() - self.started_at > MAX_SECONDS:
                self.truncated = True
            if not self.truncated and sum(self._received.values()) > 0:
                # Close on the last received sample, or the live call's elapsed
                # time, whichever is greater; cap the final duration.
                end = max(self._max_frame, min(round((self.clock() - self.started_at) * SAMPLE_RATE),
                                              MAX_SECONDS * SAMPLE_RATE))
                self._flush_to(end)
            self._wav.close()
            self._file.close()
            if self.truncated or sum(self._received.values()) == 0:
                self.path.unlink(missing_ok=True)
                return None
            return self.path
        except OSError:
            self.truncated = True
            self.path.unlink(missing_ok=True)
            return None
