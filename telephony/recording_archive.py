"""Register consent-gated native RTP lab captures in QueuePilot's recording library.

This is an *in-process*, single-machine integration; it does not create, mix or
transcribe audio. Each direction is labelled independently (trunk/phone); the
files are NOT a synchronized two-channel recording. It deliberately leaves
source captures in place, so a DB failure never silently destroys evidence.
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

MAX_NATIVE_WAV_BYTES = 32 * 1024 * 1024
LEGS = frozenset({'trunk', 'phone'})


class NativeRecordingArchive:
    """Single-host lab archive. Call only after the native call is finalized."""

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
        if set(paths) - LEGS:
            raise ValueError('Unsupported media direction')
        with connection() as db:
            call = db.execute("SELECT id FROM calls WHERE source='queuepilot-native' AND external_id=?",(call_id,)).fetchone()
            if not call:
                raise ValueError('Native call event must be persisted before recording archival')
            internal_id = call['id']
        self.library.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.library, 0o700)
        except OSError:
            pass
        stored=[]
        for side,path in sorted(paths.items()):
            path=Path(path)
            # Do not permit symlinks or arbitrary files outside the lab recording directory.
            if path.is_symlink() or not path.resolve().is_relative_to(self.capture_directory):
                raise ValueError('Invalid capture file location')
            if not path.is_file() or path.suffix.lower()!='.wav':
                raise ValueError('Capture must be a WAV file')
            n=path.stat().st_size
            if not 44 <= n <= MAX_NATIVE_WAV_BYTES:
                raise ValueError('Capture empty or exceeds 32 MiB limit')
            with wave.open(str(path),'rb') as reader:
                if reader.getnchannels()!=1 or reader.getsampwidth()!=2 or reader.getframerate()!=8000 or reader.getnframes()<1:
                    raise ValueError('Capture must contain mono PCM16 8 kHz samples')
                duration=reader.getnframes()/8000
            digest=hashlib.sha256()
            # The media leg is deliberately part of the identity: two identical
            # silence tracks are still distinct directions of the same call.
            source='queuepilot-native-'+side
            with connection() as db:
                duplicate=db.execute('''SELECT id FROM recordings WHERE source=? AND external_call_id=? AND sha256=?''',
                                     (source,call_id,self._sha256(path))).fetchone()
                if duplicate:
                    stored.append(duplicate['id'])
                    continue
            rec_id='rec_'+secrets.token_hex(16)
            target=self.library/(rec_id+'.wav')
            # Create destination exclusively to avoid replacing someone else's media.
            try:
                fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'wb') as dest, path.open('rb') as origin:
                    while chunk:=origin.read(64*1024):
                        dest.write(chunk)
                        digest.update(chunk)
                    dest.flush(); os.fsync(dest.fileno())
                with connection() as db:
                    db.execute('''INSERT INTO recordings(id,source,external_call_id,call_id,original_name,mime_type,
                                file_path,bytes,sha256,recorded_at,duration_seconds,consent_attested,created_at)
                                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                               (rec_id,source,call_id,internal_id,rec_id+'.wav','audio/wav',
                                str(target),n,digest.hexdigest(),time.time()-duration,duration,1,time.time()))
                    audit(db,'queuepilot-native-engine','recording.archived',rec_id,
                          'Direction '+side+'; unsynchronized lab leg, consent policy acknowledged')
                stored.append(rec_id)
            except Exception:
                target.unlink(missing_ok=True)
                raise
        return stored

    @staticmethod
    def _sha256(path: Path)->str:
        digest=hashlib.sha256()
        with path.open('rb') as f:
            while chunk:=f.read(64*1024):
                digest.update(chunk)
        return digest.hexdigest()
