"""Archive one two-channel WAV per consented native call into QueuePilot's library.

This lab integration never merges mono files after the fact. The native RTP
engine directly produces one stereo call recording: caller on left, agent on
right, silence filled on gaps. Source captures survive failed indexing.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import time
import wave
from pathlib import Path

from app.audit import audit
from app.config import config
from app.db import connection

MAX_NATIVE_WAV_BYTES = 64 * 1024 * 1024  # 30 min at 8 kHz / 16-bit / stereo
RECORDING_SOURCE = 'queuepilot-native-conversation'


class NativeRecordingArchive:
    """Single-host lab archive; require a completed known call first."""

    def __init__(self, capture_directory: Path, *, notice_confirmed: bool):
        if not notice_confirmed:
            raise ValueError('Recording requires explicit notice/consent policy confirmation')
        if not config.recordings_enabled:
            raise ValueError('Set RECORDINGS_ENABLED=true in the management service to index recordings')
        self.capture_directory = capture_directory.resolve(strict=True)
        if not self.capture_directory.is_dir():
            raise ValueError('Capture directory must exist')
        self.library = config.recording_dir.resolve()
        if self.library == self.capture_directory:
            raise ValueError('Capture and library directories must be distinct')

    def store(self, call_id: str, paths: dict[str, Path]) -> list[str]:
        if not paths:
            return []
        if set(paths) != {'conversation'}:
            raise ValueError('Exactly one conversation recording is required per call')
        with connection() as db:
            call = db.execute("SELECT id FROM calls WHERE source='queuepilot-native' AND external_id=?",
                              (call_id,)).fetchone()
            if not call:
                raise ValueError('Native call event must be persisted before recording archival')
            internal_id = call['id']
        path = Path(paths['conversation'])
        if path.is_symlink() or not path.resolve().is_relative_to(self.capture_directory):
            raise ValueError('Invalid capture file location')
        if not path.exists():
            with connection() as db:
                existing = db.execute('''SELECT id FROM recordings WHERE source=? AND external_call_id=?''',
                                      (RECORDING_SOURCE, call_id)).fetchone()
            if existing:
                return [existing['id']]  # Idempotent retry after successful move into the library.
        if not path.is_file() or path.suffix.lower() != '.wav':
            raise ValueError('Capture must be a WAV file')
        size = path.stat().st_size
        if not 44 <= size <= MAX_NATIVE_WAV_BYTES:
            raise ValueError('Capture empty or exceeds 64 MiB limit')
        with wave.open(str(path), 'rb') as source:
            if (source.getnchannels() != 2 or source.getsampwidth() != 2
                    or source.getframerate() != 8000 or source.getnframes() < 1
                    or source.getcomptype() != 'NONE'):
                raise ValueError('Conversation capture must be stereo PCM16 8 kHz WAV')
            duration = source.getnframes() / 8000
        digest = self._sha256(path)
        with connection() as db:
            existing = db.execute('''SELECT id,sha256 FROM recordings
                                     WHERE source=? AND external_call_id=?''',
                                  (RECORDING_SOURCE, call_id)).fetchone()
            if existing:
                if existing['sha256'] == digest:
                    path.unlink(missing_ok=True)
                    return [existing['id']]
                raise ValueError('Call already has a different conversation recording; manual reconciliation required')
        self.library.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.library, 0o700)
        except OSError:
            pass
        rec_id = 'rec_' + secrets.token_hex(16)
        target = self.library / (rec_id + '.wav')
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            copied = hashlib.sha256()
            with os.fdopen(fd, 'wb') as dest, path.open('rb') as origin:
                while chunk := origin.read(64 * 1024):
                    dest.write(chunk)
                    copied.update(chunk)
                dest.flush()
                os.fsync(dest.fileno())
            if copied.hexdigest() != digest or target.stat().st_size != size:
                raise ValueError('Recording changed during archival; cannot index a mismatched file')
            with connection() as db:
                # Fail closed even if a duplicate was created while copying.
                existing = db.execute('''SELECT id FROM recordings
                                         WHERE source=? AND external_call_id=?''',
                                      (RECORDING_SOURCE, call_id)).fetchone()
                if existing:
                    raise ValueError('Recording already archived for this native call')
                db.execute('''INSERT INTO recordings(id,source,external_call_id,call_id,original_name,mime_type,
                            file_path,bytes,sha256,recorded_at,duration_seconds,consent_attested,created_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                           (rec_id, RECORDING_SOURCE, call_id, internal_id, rec_id + '.wav', 'audio/wav',
                            str(target), size, digest, time.time() - duration, duration, 1, time.time()))
                audit(db, 'queuepilot-native-engine', 'recording.archived', rec_id,
                      'One stereo WAV: caller/trunk=left; agent/phone=right; approximate RTP-timeline alignment')
            # Only after the library copy + DB transaction have committed: keep
            # ONE physical WAV, not a source copy and a second archived copy.
            # If unlink fails, the recording is still playable; cleanup is pending.
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return [rec_id]
        except Exception:
            target.unlink(missing_ok=True)
            raise

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            while chunk := handle.read(64 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
