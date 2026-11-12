"""QueuePilot-owned, RFC4733-based, audio-prompt IVR for isolated SIP labs.

This module is not a full IVR/call-processing platform. Prompts must be local
8kHz signed 16-bit mono WAV; options route to approved QueuePilot queues.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import struct
import wave

MAX_PROMPT_SECONDS = 30


def _read_prompt(path: Path) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise ValueError('IVR prompt must be an existing regular WAV file')
    try:
        with wave.open(str(path), 'rb') as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 8000, 'NONE'):
                raise ValueError('IVR prompts must be mono PCM16 8000 Hz WAV')
            if not 0 < audio.getnframes() <= 8000 * MAX_PROMPT_SECONDS:
                raise ValueError('IVR prompt duration must be 0-30 seconds')
            payload = audio.readframes(audio.getnframes())
            if len(payload) != audio.getnframes() * 2:
                raise ValueError('Truncated IVR WAV')
            return payload
    except (wave.Error, EOFError, OSError) as exc:
        raise ValueError('Invalid IVR prompt WAV') from exc


@dataclass(frozen=True)
class IvrMenu:
    did: str
    prompt: Path
    digits: dict[str, str]
    timeout_seconds: int = 8
    max_attempts: int = 2
    fallback_queue: str | None = None
    pcm: bytes = field(init=False, repr=False)

    def __post_init__(self):
        if not self.did.isdecimal() or not 2 <= len(self.did) <= 20:
            raise ValueError('IVR DID must be a dialed number')
        if not self.digits or any(len(digit) != 1 or digit not in '0123456789*#' or not queue for digit, queue in self.digits.items()):
            raise ValueError('IVR choices must map a single DTMF digit to a queue')
        if not 2 <= self.timeout_seconds <= 60 or not 1 <= self.max_attempts <= 5:
            raise ValueError('Invalid IVR timeout / attempt limit')
        if self.fallback_queue is not None and not self.fallback_queue:
            raise ValueError('Invalid fallback queue')
        object.__setattr__(self, 'pcm', _read_prompt(self.prompt))


@dataclass
class IvrSession:
    menu: IvrMenu
    attempts: int = 0
    deadline: float = 0.0
    seen_digits: list[str] = field(default_factory=list)


# Lab-only PCM16 -> G.711 companding; local audio must never be executable.
def encode_ulaw(value: int) -> int:
    sign = 0x80 if value < 0 else 0
    magnitude = min(abs(value), 32635) + 132
    exponent = max(0, magnitude.bit_length() - 8)
    exponent = min(exponent, 7)
    mantissa = (magnitude >> (exponent + 3)) & 15
    return (~(sign | exponent << 4 | mantissa)) & 0xff


def encode_alaw(value: int) -> int:
    sign = 0x80 if value >= 0 else 0
    magnitude = max(0, value if value >= 0 else -value - 1)
    magnitude = min(magnitude, 32767)
    if magnitude < 256:
        code = magnitude >> 4
    else:
        exponent = min(7, magnitude.bit_length() - 8)
        code = (exponent << 4) | ((magnitude >> (exponent + 3)) & 15)
    return (code | sign) ^ 0x55


def encode_prompt(pcm: bytes, codec: int) -> bytes:
    if codec not in (0, 8) or len(pcm) % 2:
        raise ValueError('Expected G.711 and aligned PCM16 samples')
    encode = encode_ulaw if codec == 0 else encode_alaw
    return bytes(encode(sample[0]) for sample in struct.iter_unpack('<h', pcm))
