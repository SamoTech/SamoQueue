"""Single-file RTP capture: channel mapping, time gaps, late RTP and limits."""
from __future__ import annotations

import struct
import wave

from telephony.stereo_recording import StereoCallRecorder, MAX_SECONDS, decode_ulaw


def _samples(path):
    with wave.open(str(path), 'rb') as reader:
        assert (reader.getnchannels(), reader.getframerate(), reader.getsampwidth()) == (2, 8000, 2)
        frames = reader.readframes(reader.getnframes())
        return [pair for pair in struct.iter_unpack('<hh', frames)]


def test_one_file_separate_channels_with_delayed_agent(tmp_path):
    now = [1.0]
    path = tmp_path / 'one-call.wav'
    recorder = StereoCallRecorder(path, clock=lambda: now[0])
    recorder.add('trunk', 30_000, bytes([0]) * 160, 0)
    now[0] += 0.04  # Agent RTP arrives 40ms after the caller.
    recorder.add('phone', 500_000, bytes([0]) * 160, 0)
    now[0] += 0.04
    assert recorder.close() == path
    assert len(list(tmp_path.glob('*.wav'))) == 1
    samples = _samples(path)
    assert len(samples) >= 640
    assert samples[0] == (decode_ulaw(0), 0)
    assert samples[160] == (0, 0)  # Caller stopped; silence padded.
    assert samples[320] == (0, decode_ulaw(0))
    assert samples[479] == (0, decode_ulaw(0))


def test_rtp_timestamps_fill_gaps_and_reorder_inside_hold_window(tmp_path):
    now = [10.0]
    path = tmp_path / 'one-call.wav'
    recorder = StereoCallRecorder(path, clock=lambda: now[0])
    recorder.add('trunk', 1000, bytes([0]) * 160, 0)
    now[0] += 0.02
    recorder.add('trunk', 1320, bytes([0]) * 160, 0)
    now[0] += 0.02
    recorder.add('trunk', 1160, bytes([0]) * 160, 0)  # Reordered packet fills gap.
    now[0] += 0.02
    recorder.close()
    samples = _samples(path)
    assert all(x[0] == decode_ulaw(0) for x in samples[:480])
    assert all(x[1] == 0 for x in samples[:480])


def test_rtp_timestamp_wrap_and_one_way_media(tmp_path):
    now = [5.0]
    recorder = StereoCallRecorder(tmp_path/'wrap.wav', clock=lambda: now[0])
    recorder.add('trunk', 0xFFFFFF80, bytes([0]) * 160, 0)
    now[0] += 0.02
    recorder.add('trunk', 0x00000020, bytes([0]) * 160, 0)
    recorder.close()
    samples = _samples(tmp_path/'wrap.wav')
    assert len(samples) >= 320
    assert all(x == (decode_ulaw(0), 0) for x in samples[:320])


def test_no_audio_no_file_and_limit_discards_incomplete(tmp_path):
    now = [1.0]
    empty = StereoCallRecorder(tmp_path/'empty.wav', clock=lambda: now[0])
    assert empty.close() is None
    assert not (tmp_path/'empty.wav').exists()
    rec = StereoCallRecorder(tmp_path/'truncated.wav', clock=lambda: now[0])
    now[0] += MAX_SECONDS + 1
    rec.add('trunk', 1000, bytes([0]) * 160, 0)
    assert rec.truncated
    assert rec.close() is None
    assert not (tmp_path/'truncated.wav').exists()
