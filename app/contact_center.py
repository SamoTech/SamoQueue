"""Call-center operations domain: recorder ingest, audited playback, monitor gateway,
IVR drafts, and independently exportable event-derived reports.

The native SIP/RTP runtime can archive ONE *lab-only* stereo WAV after
call completion when explicitly enabled and notice policy is acknowledged.
Native RTP recording, local voice IVR and approved live listening have
lab-only integration paths. Dashboard IVR drafts are NOT published to the
SIP runtime. Nothing here certifies real Avaya hardware or production use.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import time
import uuid
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, model_validator

from .audit import audit
from .config import config
from .db import connection
from .security import require_admin, require_reviewer, require_supervisor

router = APIRouter(prefix='/api/cc', tags=['Contact center management'])
SOURCE_NAME = re.compile(r'^[a-zA-Z0-9_-]{1,48}$')
CALL_KEY = re.compile(r'^[a-zA-Z0-9_.:@-]{1,160}$')
MAX_RECORDING_BYTES = 32 * 1024 * 1024
MAX_PCM_FRAME = 64_000  # <=2 seconds at 16 kHz mono PCM16
RECORDING_MIMES = {'audio/wav': ('wav', b'RIFF'), 'audio/x-wav': ('wav', b'RIFF'),
                   'audio/mpeg': ('mp3', None), 'audio/ogg': ('ogg', b'OggS')}


def engine_key(expected: str, provided: str | None):
    if (not provided or len(expected) < 32
            or not hmac.compare_digest(provided.encode(), expected.encode())):
        raise HTTPException(401, 'Invalid internal media engine credential')


def check_record_key(x_recording_engine_key: str | None = Header(default=None)):
    engine_key(config.recording_engine_key, x_recording_engine_key)


def check_monitor_key(x_monitor_engine_key: str | None = Header(default=None)):
    engine_key(config.monitor_engine_key, x_monitor_engine_key)


def audio_signature_ok(mime: str, head: bytes) -> bool:
    spec = RECORDING_MIMES.get(mime)
    if not spec:
        return False
    if spec[0] == 'mp3':
        return head.startswith(b'ID3') or (len(head) > 1 and head[0] == 0xff and (head[1] & 0xe0) == 0xe0)
    return head.startswith(spec[1]) and (spec[0] != 'wav' or head[8:12] == b'WAVE')


@router.post('/recordings/import', status_code=201)
async def import_recording(request: Request, source: str, call_id: str,
                           consent_attested: bool = False,
                           recorded_at: datetime | None = None,
                           duration_seconds: float | None = Query(default=None, ge=0, le=86400),
                           _=Depends(check_record_key)):
    """Authorized recorder uploads a WAV/MP3/OGG payload linked to a known call.

    The explicit attestation must be set by the compliant capture/notice system;
    QueuePilot cannot infer whether call participants were correctly notified.
    """
    if not config.recordings_enabled:
        raise HTTPException(403, 'Recording import is disabled')
    if source != 'queuepilot-native' or not CALL_KEY.fullmatch(call_id):
        raise HTTPException(422, 'Invalid recording source or call identifier')
    if not consent_attested:
        raise HTTPException(422, 'Recorder consent/notice policy attestation required')
    if recorded_at and (not recorded_at.tzinfo or recorded_at.utcoffset() is None):
        raise HTTPException(422, 'recorded_at requires UTC offset')
    mime = request.headers.get('content-type', '').split(';')[0].lower()
    if mime not in RECORDING_MIMES:
        raise HTTPException(415, 'Only WAV, MP3, and OGG are supported')
    with connection() as db:
        call = db.execute('SELECT id FROM calls WHERE source=? AND external_id=?',
                          (source, call_id)).fetchone()
        if not call:
            raise HTTPException(404, 'Call not found; native SIP call events must be ingested first')
    folder = config.recording_dir.resolve()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        folder.chmod(0o700)
    except OSError:
        pass
    recording_id = 'rec_' + uuid.uuid4().hex
    suffix = RECORDING_MIMES[mime][0]
    target = folder / (recording_id + '.' + suffix)
    temp = folder / (recording_id + '.part')
    n = 0
    digest = hashlib.sha256()
    head = b''
    try:
        with open(temp, 'xb') as f:
            os.chmod(temp, 0o600)
            async for chunk in request.stream():
                if not chunk:
                    continue
                n += len(chunk)
                if n > MAX_RECORDING_BYTES:
                    raise HTTPException(413, 'Recording exceeds 32 MiB upload limit')
                if len(head) < 16:
                    head = (head + chunk)[:16]
                f.write(chunk)
                digest.update(chunk)
        if n < 32 or not audio_signature_ok(mime, head):
            raise HTTPException(422, 'Recording file signature does not match MIME type')
        # Check idempotency before rename: a recorder may retry after losing the HTTP response.
        with connection() as db:
            previous = db.execute('''SELECT id FROM recordings WHERE source=? AND external_call_id=? AND sha256=?''',
                                  (source, call_id, digest.hexdigest())).fetchone()
            if previous:
                return {'id': previous['id'], 'duplicate': True, 'stored': True}
            os.replace(temp, target)
            db.execute('''INSERT INTO recordings(id,source,external_call_id,call_id,original_name,mime_type,
                    file_path,bytes,sha256,recorded_at,duration_seconds,consent_attested,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (recording_id, source, call_id, call['id'], recording_id + '.' + suffix, mime,
                        str(target), n, digest.hexdigest(),
                        (recorded_at or datetime.now(timezone.utc)).timestamp(),
                        duration_seconds, 1, time.time()))
            audit(db, 'native-recording-engine', 'recording.imported', recording_id, 'Audio registered')
    finally:
        temp.unlink(missing_ok=True)
    return {'id': recording_id, 'duplicate': False, 'stored': True, 'bytes': n}


@router.get('/recordings')
def recordings(limit: int = Query(default=100, ge=1, le=1000),
               queue_id: str | None = None, agent_id: str | None = None,
               user=Depends(require_supervisor)):
    sql = '''SELECT r.id,r.source,r.external_call_id,r.original_name,r.mime_type,r.bytes,
                r.recorded_at,r.duration_seconds,r.created_at,c.status AS call_status,
                q.name AS queue_name,a.name AS agent_name FROM recordings r
             JOIN calls c ON c.id=r.call_id JOIN queues q ON q.id=c.queue_id
             LEFT JOIN agents a ON a.id=c.agent_id WHERE 1=1'''
    values: list = []
    if queue_id:
        sql += ' AND c.queue_id=?'
        values.append(queue_id)
    if agent_id:
        sql += ' AND c.agent_id=?'
        values.append(agent_id)
    sql += ' ORDER BY r.recorded_at DESC LIMIT ?'
    values.append(limit)
    with connection() as db:
        rows = [dict(r) for r in db.execute(sql, values)]
    return {'recordings': rows, 'recording_enabled': config.recordings_enabled,
            'note': 'Native SIP lab can archive one approximately time-aligned stereo WAV per call (caller left, agent right), when explicitly enabled. Not production-grade recording or consent enforcement.'}


@router.get('/recordings/{recording_id}/audio')
def recording_audio(recording_id: str, download: bool = False, user=Depends(require_supervisor)):
    with connection() as db:
        row = db.execute('SELECT * FROM recordings WHERE id=?', (recording_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Recording not found')
        file_path = Path(row['file_path']).resolve()
        if not file_path.is_relative_to(config.recording_dir.resolve()) or not file_path.is_file():
            raise HTTPException(404, 'Recording media unavailable')
        audit(db, user, 'recording.download' if download else 'recording.playback', recording_id)
        name = row['original_name']
        mime = row['mime_type']
    return FileResponse(path=file_path, media_type=mime, filename=name,
                        content_disposition_type='attachment' if download else 'inline',
                        headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                                 'Referrer-Policy': 'no-referrer'})


class MonitorRequest(BaseModel):
    call_id: str = Field(min_length=1, max_length=160)  # native QueuePilot call UUID
    purpose: Literal['quality_assurance', 'training', 'supervision'] = 'quality_assurance'


class MonitorApproval(BaseModel):
    confirmation: Literal['APPROVE LIVE LISTEN']


@router.post('/monitor/sessions', status_code=201)
def request_monitor(data: MonitorRequest, user=Depends(require_admin)):
    if not config.monitoring_enabled:
        raise HTTPException(403, 'Monitoring disabled by administrator')
    with connection() as db:
        call = db.execute('''SELECT c.*, a.extension AS station FROM calls c JOIN agents a
                          ON c.agent_id=a.id WHERE c.id=?''', (data.call_id,)).fetchone()
        if not call or call['source'] != 'queuepilot-native' or call['status'] != 'answered':
            raise HTTPException(422, 'Requires an active answered native call with a QueuePilot agent extension')
        if db.execute('''SELECT 1 FROM monitor_sessions WHERE call_id=?
                          AND state IN ('pending_approval','queued','leased','active','stop_requested')''',
                      (call['id'],)).fetchone():
            raise HTTPException(409, 'This call already has an open monitoring request')
        sid = 'mon_' + uuid.uuid4().hex[:24]
        db.execute('''INSERT INTO monitor_sessions(id,call_id,target_station,created_by,state,created_at)
                      VALUES(?,?,?,?,?,?)''', (sid, call['id'], call['station'], user,
                                               'pending_approval', time.time()))
        audit(db, user, 'monitor.requested', sid, data.purpose)
    return {'id': sid, 'state': 'pending_approval', 'audio_available': False}


@router.get('/monitor/sessions')
def list_monitors(user=Depends(require_supervisor)):
    with connection() as db:
        rows = [dict(r) for r in db.execute('''SELECT s.*, c.external_id AS native_call_id,
                   q.name AS queue_name,a.name AS agent_name FROM monitor_sessions s
                   JOIN calls c ON c.id=s.call_id JOIN queues q ON q.id=c.queue_id
                   LEFT JOIN agents a ON a.id=c.agent_id ORDER BY s.created_at DESC LIMIT 100''')]
    return {'sessions': rows, 'enabled': config.monitoring_enabled,
            'transport': 'PCM16/16000/mono internal WebSocket relay (opt-in native RTP lab observer)'}


@router.post('/monitor/sessions/{sid}/approve')
def approve_monitor(sid: str, _: MonitorApproval, user=Depends(require_reviewer)):
    if not config.monitoring_enabled:
        raise HTTPException(403, 'Monitoring disabled')
    with connection() as db:
        session = db.execute('''SELECT s.*,c.status AS call_status FROM monitor_sessions s
                            JOIN calls c ON c.id=s.call_id WHERE s.id=?''', (sid,)).fetchone()
        if not session:
            raise HTTPException(404, 'Unknown monitoring request')
        if session['created_by'] == user:
            raise HTTPException(403, 'A separate supervisor must approve monitoring')
        if session['call_status'] != 'answered' or session['state'] != 'pending_approval':
            raise HTTPException(409, 'Call ended or request already processed')
        db.execute("UPDATE monitor_sessions SET state='queued',approved_at=? WHERE id=?",
                   (time.time(), sid))
        audit(db, user, 'monitor.approved', sid)
    return {'id': sid, 'state': 'queued', 'native_media_active': False}


@router.post('/monitor/sessions/{sid}/stop')
def stop_monitor(sid: str, user=Depends(require_supervisor)):
    with connection() as db:
        s = db.execute('SELECT state FROM monitor_sessions WHERE id=?', (sid,)).fetchone()
        if not s:
            raise HTTPException(404, 'Monitoring session not found')
        if s['state'] in ('pending_approval', 'queued'):
            state = 'cancelled'
        elif s['state'] in ('leased', 'active'):
            state = 'stop_requested'
        else:
            raise HTTPException(409, 'Session cannot be stopped in its current state')
        db.execute('UPDATE monitor_sessions SET state=?,stopped_at=? WHERE id=?',
                   (state, time.time(), sid))
        audit(db, user, 'monitor.' + state, sid)
    return {'id': sid, 'state': state, 'note': 'Native media runtime must acknowledge observation termination'}


@router.post('/monitor/engine/claim')
def claim_monitor(_=Depends(check_monitor_key)):
    if not config.monitoring_enabled:
        raise HTTPException(403, 'Monitoring disabled')
    with connection() as db:
        now = time.time()
        hb = db.execute("SELECT * FROM engine_heartbeats WHERE source='queuepilot-native'").fetchone()
        if not hb or not hb['engine_healthy'] or now - hb['last_heartbeat'] > 30:
            raise HTTPException(503, 'Native QueuePilot engine heartbeat missing')
        # Native media engine reconnects must not replay ambiguous listen commands.
        db.execute("UPDATE monitor_sessions SET state='expired',stopped_at=? WHERE state='queued' AND approved_at<?",
                   (now, now - 90))
        db.execute("UPDATE monitor_sessions SET state='uncertain',stopped_at=? WHERE state='leased' AND leased_at<?",
                   (now, now - 90))
        row = db.execute('''SELECT s.*, c.external_id AS external_call_id FROM monitor_sessions s
                            JOIN calls c ON c.id=s.call_id
                            WHERE s.state IN ('stop_requested','queued')
                            ORDER BY CASE s.state WHEN 'stop_requested' THEN 0 ELSE 1 END, s.created_at LIMIT 1''').fetchone()
        if not row:
            return {'command': None}
        if row['state'] == 'queued':
            db.execute("UPDATE monitor_sessions SET state='leased',leased_at=? WHERE id=? AND state='queued'", (now,row['id']))
            action = 'start_listen'
        else:
            db.execute("UPDATE monitor_sessions SET state='stop_leased',leased_at=? WHERE id=? AND state='stop_requested'", (now,row['id']))
            action = 'stop_listen'
        audit(db, 'native-monitor-engine', 'monitor.' + action + '.leased', row['id'])
        return {'command': {'id': row['id'], 'action': action, 'extension': row['target_station'],
                            'call_id': row['external_call_id']},
                'instruction': 'Native RTP media observer only; do not start without validated native mixer'}


class MonitorResult(BaseModel):
    result: Literal['active', 'failed', 'uncertain', 'ended']
    code: str = Field(default='', max_length=64)
    message: str = Field(default='', max_length=160)


@router.post('/monitor/engine/sessions/{sid}/result')
def monitor_engine_result(sid: str, body: MonitorResult, _=Depends(check_monitor_key)):
    with connection() as db:
        row = db.execute('SELECT state FROM monitor_sessions WHERE id=?', (sid,)).fetchone()
        if not row:
            raise HTTPException(404, 'Monitoring session not found')
        allowed = ((row['state'] == 'leased' and body.result in ('active','failed','uncertain'))
                   or (row['state'] == 'active' and body.result in ('ended','failed','uncertain'))
                   or (row['state'] in ('stop_leased','stop_requested') and body.result in ('ended','failed','uncertain')))
        if not allowed:
            raise HTTPException(409, 'Invalid transition for the currently leased monitor operation')
        updated = body.result
        if row['state'] in ('stop_leased','stop_requested') and body.result == 'failed':
            updated = 'stop_failed'  # requires urgent manual PBX verification
        db.execute('''UPDATE monitor_sessions SET state=?,result_code=?,result_message=?,
                     active_at=CASE WHEN ?='active' THEN ? ELSE active_at END,
                     stopped_at=CASE WHEN ?!='active' THEN ? ELSE stopped_at END
                     WHERE id=?''',
                   (updated,body.code,body.message,updated,time.time(),updated,time.time(),sid))
        audit(db, 'native-monitor-engine', 'monitor.' + updated, sid, body.code)
    return {'id': sid, 'state': updated, 'engine_reported': True}


# Memory-only relay for ONE uvicorn worker. Production requires a distributed
# authenticated media gateway and stricter enterprise identity/role enforcement.
_media_subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
_tickets: dict[str, tuple[str, float]] = {}


@router.post('/monitor/sessions/{sid}/audio-ticket')
def audio_ticket(sid: str, user=Depends(require_reviewer)):
    if not config.monitoring_enabled:
        raise HTTPException(403, 'Monitoring disabled')
    with connection() as db:
        s = db.execute('''SELECT s.state,s.created_by,s.active_at,c.status AS call_status FROM monitor_sessions s
                          JOIN calls c ON c.id=s.call_id WHERE s.id=?''', (sid,)).fetchone()
        if not s:
            raise HTTPException(404, 'Monitoring session not found')
        if s['state'] != 'active' or s['call_status'] != 'answered' or not s['active_at'] or time.time()-s['active_at'] > 1800:
            raise HTTPException(409, 'No current active authorized monitoring media session')
        audit(db, user, 'monitor.audio_ticket', sid)
    ticket = secrets.token_urlsafe(32)
    for key,(v,expiry) in list(_tickets.items()):
        if expiry < time.time():
            _tickets.pop(key, None)
    _tickets[hashlib.sha256(ticket.encode()).hexdigest()] = (sid, time.time() + 20)
    return {'ticket': ticket, 'expires_in': 20,
            'audio_format': 's16le_mono_16000', 'note': 'Ticket is one-time, never log it'}


@router.websocket('/monitor/sessions/{sid}/audio/ws')
async def monitor_audio_socket(ws: WebSocket, sid: str):
    if not config.monitoring_enabled:
        await ws.close(code=1008)
        return
    ticket = ws.query_params.get('ticket', '')
    stored = _tickets.pop(hashlib.sha256(ticket.encode()).hexdigest(), None) if ticket else None
    if not stored or stored[0] != sid or time.time() > stored[1]:
        await ws.close(code=1008)
        return
    with connection() as db:
        s = db.execute('''SELECT s.state,s.active_at,c.status AS call_status FROM monitor_sessions s
                           JOIN calls c ON c.id=s.call_id WHERE s.id=?''', (sid,)).fetchone()
        if not s or s['state'] != 'active' or s['call_status'] != 'answered' or not s['active_at'] or time.time()-s['active_at'] > 1800:
            await ws.close(code=1008)
            return
    await ws.accept()
    q = asyncio.Queue(maxsize=8)
    _media_subscribers[sid].add(q)
    try:
        while True:
            try:
                frame = await asyncio.wait_for(q.get(), timeout=0.5)
                with connection() as db:
                    row = db.execute('''SELECT s.state,s.active_at,c.status AS call_status FROM monitor_sessions s
                              JOIN calls c ON c.id=s.call_id WHERE s.id=?''', (sid,)).fetchone()
                if not row or row['state'] != 'active' or row['call_status'] != 'answered' or not config.monitoring_enabled:
                    await ws.close(code=1000)
                    break
                await ws.send_bytes(frame)
            except asyncio.TimeoutError:
                with connection() as db:
                    row = db.execute('''SELECT s.state,s.active_at,c.status AS call_status FROM monitor_sessions s
                              JOIN calls c ON c.id=s.call_id WHERE s.id=?''', (sid,)).fetchone()
                    if not row or row['state'] != 'active' or row['call_status'] != 'answered' or time.time() - (row['active_at'] or 0) > 1800:
                        await ws.close(code=1000)
                        break
    except (WebSocketDisconnect, RuntimeError, OSError):
        pass
    finally:
        _media_subscribers[sid].discard(q)
        if not _media_subscribers[sid]:
            _media_subscribers.pop(sid, None)


@router.post('/monitor/engine/sessions/{sid}/audio', status_code=202)
async def receive_monitor_pcm(sid: str, request: Request, _=Depends(check_monitor_key)):
    if not config.monitoring_enabled:
        raise HTTPException(403, 'Monitoring disabled')
    with connection() as db:
        row = db.execute('''SELECT s.state,s.active_at,c.status AS call_status FROM monitor_sessions s
                              JOIN calls c ON c.id=s.call_id WHERE s.id=?''', (sid,)).fetchone()
        if not row or row['state'] != 'active' or row['call_status'] != 'answered' or time.time()-(row['active_at'] or 0) > 1800:
            raise HTTPException(409, 'Monitoring session inactive or expired')
    if request.headers.get('content-type', '').split(';')[0] != 'application/octet-stream':
        raise HTTPException(415, 'Expected raw signed 16-bit little-endian 16kHz mono PCM')
    packet = bytearray()
    async for chunk in request.stream():
        packet.extend(chunk)
        if len(packet) > MAX_PCM_FRAME:
            raise HTTPException(413, 'PCM frame too large')
    if len(packet) < 2 or len(packet) % 2:
        raise HTTPException(422, 'PCM frame must contain complete 16-bit samples')
    listeners = _media_subscribers.get(sid, set())
    for q in list(listeners):
        if q.full():
            try:
                q.get_nowait()  # Drop stale media instead of growing latency.
            except asyncio.QueueEmpty:
                pass
        q.put_nowait(bytes(packet))
    return {'accepted_bytes': len(packet), 'browser_listeners': len(listeners)}


IVR_NODE_TYPES = {'start', 'menu', 'announcement', 'queue', 'transfer', 'hangup'}
IVR_KEYS = set('0123456789*#')


class IvrNode(BaseModel):
    id: str = Field(min_length=1, max_length=60, pattern=r'^[A-Za-z0-9_-]+$')
    type: Literal['start','menu','announcement','queue','transfer','hangup']
    label: str = Field(min_length=1, max_length=100)
    next: str | None = None
    choices: dict[str,str] = Field(default_factory=dict)
    prompt: str | None = Field(default=None, max_length=300)
    queue_id: str | None = None
    destination: str | None = None


class IvrGraph(BaseModel):
    nodes: list[IvrNode] = Field(min_length=2, max_length=60)

    @model_validator(mode='after')
    def valid_graph(self):
        ids = {n.id for n in self.nodes}
        if len(ids) != len(self.nodes):
            raise ValueError('IVR node IDs must be unique')
        if sum(n.type == 'start' for n in self.nodes) != 1:
            raise ValueError('IVR graph needs exactly one start node')
        for n in self.nodes:
            edges = ([n.next] if n.next else []) + list(n.choices.values())
            if not set(edges).issubset(ids):
                raise ValueError('IVR contains a reference to an unknown node')
            if n.type in ('start','announcement') and not n.next:
                raise ValueError('Start and announcement need next target')
            if n.type == 'menu' and (not n.prompt or not n.choices or not set(n.choices).issubset(IVR_KEYS)):
                raise ValueError('Menu requires prompt and DTMF key choices')
            if n.type == 'queue' and not n.queue_id:
                raise ValueError('Queue node requires QueuePilot queue ID')
            if n.type == 'transfer' and not n.destination:
                raise ValueError('Transfer node requires internal destination')
            if n.type in ('hangup', 'queue', 'transfer') and edges:
                raise ValueError('Terminal nodes cannot have outgoing edges')
        start = next(n.id for n in self.nodes if n.type == 'start')
        by_id = {n.id:n for n in self.nodes}
        visited, todo = set(), [start]
        while todo:
            current = todo.pop()
            if current in visited:
                continue
            visited.add(current)
            n = by_id[current]
            todo.extend(([n.next] if n.next else []) + list(n.choices.values()))
        if visited != ids:
            raise ValueError('All IVR nodes must be reachable from start')
        if not any(by_id[i].type in ('queue','transfer','hangup') for i in visited):
            raise ValueError('IVR graph must have a terminal outcome')
        return self


class IvrCreate(BaseModel):
    name: str = Field(min_length=3, max_length=100)
    description: str = Field(default='', max_length=500)
    graph: IvrGraph


def validate_ivr_targets(db, graph: IvrGraph):
    for n in graph.nodes:
        if n.type == 'queue' and not db.execute('SELECT 1 FROM queues WHERE id=?', (n.queue_id,)).fetchone():
            raise HTTPException(422, f'IVR queue {n.queue_id} does not exist')
        if n.type == 'transfer':
            if not re.fullmatch(r'\d{2,8}', n.destination or ''):
                raise HTTPException(422, 'Transfer destination must be a 2–8 digit internal extension')
            if not db.execute('SELECT 1 FROM agents WHERE extension=?', (n.destination,)).fetchone():
                raise HTTPException(422, 'IVR transfer destination must be a QueuePilot agent extension')


@router.get('/ivr')
def list_ivr(user=Depends(require_admin)):
    with connection() as db:
        rows = [dict(r) for r in db.execute('''SELECT id,name,description,status,version,created_at,
                     updated_at,created_by,updated_by FROM ivr_flows ORDER BY updated_at DESC''')]
    return {'flows': rows, 'deployment_implemented': False}


@router.post('/ivr', status_code=201)
def create_ivr(data: IvrCreate, user=Depends(require_admin)):
    fid = 'ivr_' + uuid.uuid4().hex[:20]
    with connection() as db:
        validate_ivr_targets(db, data.graph)
        now = time.time()
        db.execute('''INSERT INTO ivr_flows(id,name,description,graph,status,version,created_by,updated_by,created_at,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?)''',
                   (fid,data.name,data.description,data.graph.model_dump_json(),'draft',1,user,user,now,now))
        audit(db, user, 'ivr.created', fid)
    return {'id': fid, 'status': 'draft', 'runtime_activated': False}


@router.get('/ivr/{fid}')
def get_ivr(fid: str, user=Depends(require_admin)):
    with connection() as db:
        row = db.execute('SELECT * FROM ivr_flows WHERE id=?', (fid,)).fetchone()
        if not row:
            raise HTTPException(404, 'IVR flow not found')
    result = dict(row)
    result['graph'] = json.loads(result['graph'])
    return result


@router.put('/ivr/{fid}')
def update_ivr(fid: str, data: IvrCreate, user=Depends(require_admin)):
    with connection() as db:
        validate_ivr_targets(db, data.graph)
        row = db.execute("UPDATE ivr_flows SET name=?,description=?,graph=?,status='draft',version=version+1,updated_by=?,updated_at=? WHERE id=? AND status='draft'",
                         (data.name,data.description,data.graph.model_dump_json(),user,time.time(),fid))
        if row.rowcount != 1:
            raise HTTPException(409, 'Flow must be in draft state to edit')
        audit(db, user, 'ivr.updated', fid)
    return {'id': fid, 'status': 'draft', 'runtime_activated': False}


class DeployReview(BaseModel):
    confirmation: Literal['PROPOSE IVR DEPLOYMENT']


@router.post('/ivr/{fid}/propose-deployment')
def propose_ivr(fid: str, _: DeployReview, user=Depends(require_admin)):
    with connection() as db:
        row = db.execute("UPDATE ivr_flows SET status='proposed',updated_at=? WHERE id=? AND status='draft'",
                         (time.time(),fid))
        if row.rowcount != 1:
            raise HTTPException(409, 'IVR flow not found or not in draft')
        audit(db, user, 'ivr.deployment.proposed', fid, 'Draft review only; native IVR runtime not yet connected')
    return {'id': fid, 'status': 'proposed', 'runtime_activated': False,
            'manual_work_required': 'Implement and validate native IVR runtime, prompts, DTMF and activation controls'}


ReportName = Literal['calls','queues','agents','intervals','sla','recordings','monitoring','ivr','audit','events']
REPORT_NAMES = ('calls','queues','agents','intervals','sla','recordings','monitoring','ivr','audit','events')


def report_data(name: str, start: datetime | None = None, end: datetime | None = None):
    if name not in REPORT_NAMES:
        raise HTTPException(404, 'Unknown report')
    now = time.time()
    ts0 = (start.timestamp() if start else now-30*86400)
    ts1 = (end.timestamp() if end else now)
    if (start and (not start.tzinfo or start.utcoffset() is None)) or (end and (not end.tzinfo or end.utcoffset() is None)):
        raise HTTPException(422, 'Date filters must include timezone offset')
    if ts1 <= ts0 or ts1-ts0 > 366*86400:
        raise HTTPException(422, 'Report range must be positive and no more than 366 days')
    with connection() as db:
        if name == 'calls':
            query = '''SELECT c.source,c.external_id AS call_id,q.name AS queue,a.name AS agent,
                 c.status,c.caller,c.arrived_at,c.answered_at,c.ended_at,
                 CASE WHEN c.answered_at IS NULL THEN NULL ELSE ROUND(c.answered_at-c.arrived_at) END AS wait_seconds,
                 CASE WHEN c.ended_at IS NULL OR c.answered_at IS NULL THEN NULL
                      ELSE ROUND(c.ended_at-c.answered_at) END AS talk_seconds
                 FROM calls c JOIN queues q ON c.queue_id=q.id LEFT JOIN agents a ON c.agent_id=a.id
                 WHERE c.arrived_at BETWEEN ? AND ? ORDER BY c.arrived_at DESC LIMIT 50000'''
            rows = db.execute(query, (ts0,ts1))
        elif name in ('queues','sla'):
            query = '''SELECT q.name AS queue,q.skill,COUNT(c.id) AS offered,
                       COALESCE(SUM(CASE WHEN c.answered_at IS NOT NULL THEN 1 ELSE 0 END),0) AS answered,
                       COALESCE(SUM(CASE WHEN c.status='abandoned' THEN 1 ELSE 0 END),0) AS abandoned,
                       COALESCE(SUM(CASE WHEN c.status='queued' THEN 1 ELSE 0 END),0) AS waiting,
                       ROUND(AVG(CASE WHEN c.answered_at IS NOT NULL THEN c.answered_at-c.arrived_at END),1) AS avg_speed_answer_seconds,
                       COALESCE(SUM(CASE WHEN c.answered_at IS NOT NULL AND c.answered_at-c.arrived_at<=q.sla_seconds THEN 1 ELSE 0 END),0) AS in_sla,
                       q.sla_seconds AS sla_threshold_seconds,q.target_percent AS sla_target_percent
                       FROM queues q LEFT JOIN calls c ON c.queue_id=q.id AND c.arrived_at BETWEEN ? AND ?
                       GROUP BY q.id ORDER BY q.name'''
            rows = db.execute(query, (ts0,ts1))
        elif name == 'agents':
            rows = db.execute('''SELECT a.name AS agent,a.extension,q.name AS assigned_queue,
                         a.status AS current_status,COUNT(c.id) AS handled_calls,
                         ROUND(AVG(CASE WHEN c.ended_at IS NOT NULL AND c.answered_at IS NOT NULL
                                     THEN c.ended_at-c.answered_at END),1) AS avg_talk_seconds,
                         ROUND(SUM(CASE WHEN c.ended_at IS NOT NULL AND c.answered_at IS NOT NULL
                                     THEN c.ended_at-c.answered_at ELSE 0 END),1) AS total_talk_seconds
                         FROM agents a LEFT JOIN queues q ON a.queue_id=q.id
                         LEFT JOIN calls c ON c.agent_id=a.id AND c.answered_at BETWEEN ? AND ?
                         GROUP BY a.id ORDER BY handled_calls DESC''', (ts0,ts1))
        elif name == 'intervals':
            rows = db.execute('''SELECT CAST(c.arrived_at/1800 AS INTEGER)*1800 AS interval_start_utc,
                         q.name AS queue,COUNT(c.id) AS offered,
                         SUM(CASE WHEN c.answered_at IS NOT NULL THEN 1 ELSE 0 END) AS answered,
                         SUM(CASE WHEN c.status='abandoned' THEN 1 ELSE 0 END) AS abandoned
                         FROM calls c JOIN queues q ON q.id=c.queue_id
                         WHERE c.arrived_at BETWEEN ? AND ? GROUP BY interval_start_utc,c.queue_id
                         ORDER BY interval_start_utc DESC LIMIT 50000''',(ts0,ts1))
        elif name == 'recordings':
            rows = db.execute('''SELECT r.id,r.source,r.external_call_id AS call_id,r.recorded_at,
                       r.duration_seconds,r.mime_type,r.bytes,q.name AS queue,a.name AS agent,
                       r.consent_attested FROM recordings r JOIN calls c ON c.id=r.call_id
                       JOIN queues q ON q.id=c.queue_id LEFT JOIN agents a ON a.id=c.agent_id
                       WHERE r.recorded_at BETWEEN ? AND ? ORDER BY r.recorded_at DESC LIMIT 50000''',(ts0,ts1))
        elif name == 'monitoring':
            rows = db.execute('''SELECT s.id,s.target_station,s.created_by,s.state,s.created_at,
                            s.approved_at,s.active_at,s.stopped_at,s.result_code,
                            q.name AS queue,a.name AS agent
                            FROM monitor_sessions s JOIN calls c ON c.id=s.call_id
                            JOIN queues q ON q.id=c.queue_id LEFT JOIN agents a ON a.id=c.agent_id
                            WHERE s.created_at BETWEEN ? AND ? ORDER BY s.created_at DESC''',(ts0,ts1))
        elif name == 'ivr':
            rows = db.execute('''SELECT id,name,description,status,version,created_by,
                            updated_by,created_at,updated_at FROM ivr_flows
                            WHERE updated_at BETWEEN ? AND ? ORDER BY updated_at DESC''',(ts0,ts1))
        elif name == 'audit':
            rows = db.execute('''SELECT at,actor,operation,entity_id,details FROM audit_log
                            WHERE at BETWEEN ? AND ? ORDER BY at DESC LIMIT 50000''',(ts0,ts1))
        else:  # events
            rows = db.execute('''SELECT event_id,source,event_type,call_id,agent_id,occurred_at,
                            received_at,outcome FROM events WHERE received_at BETWEEN ? AND ?
                            ORDER BY received_at DESC LIMIT 50000''',(ts0,ts1))
        columns = [col[0] for col in rows.description]
        result = [dict(r) for r in rows]
    if name == 'sla':
        columns += ['sla_percent']
        for item in result:
            denom = (item['answered'] or 0) + (item['abandoned'] or 0)
            item['sla_percent'] = round(100*item['in_sla']/denom, 1) if denom else None
    return {'report': name, 'columns': columns, 'rows': result, 'from_utc': ts0, 'to_utc': ts1,
            'max_rows': 50000, 'authority': 'QueuePilot event-derived data; incomplete until SIP runtime emits every lifecycle event'}


def safe_csv_value(value):
    if value is None:
        return ''
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r', '\n')):
        return "'" + value
    return value


def csv_bytes(report):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(report['columns'])
    for row in report['rows']:
        writer.writerow([safe_csv_value(row[col]) for col in report['columns']])
    return output.getvalue().encode('utf-8-sig')


@router.get('/reports')
def report_catalog(user=Depends(require_supervisor)):
    return {'reports': list(REPORT_NAMES), 'formats': ['json','csv','zip'],
            'note': 'Event-derived; not complete until native SIP runtime event delivery is validated'}


@router.get('/reports/all.zip')
def all_reports_zip(start: datetime | None = None, end: datetime | None = None,
                    user=Depends(require_supervisor)):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in REPORT_NAMES:
            archive.writestr(name+'.csv', csv_bytes(report_data(name,start,end)))
    with connection() as db:
        audit(db, user, 'reports.export.all', 'zip')
    return Response(content=data.getvalue(), media_type='application/zip',
                    headers={'Content-Disposition':'attachment; filename="queuepilot-all-reports.zip"',
                             'Cache-Control':'no-store'})


@router.get('/reports/{name}.csv')
def report_csv(name: str, start: datetime | None = None, end: datetime | None = None,
               user=Depends(require_supervisor)):
    report = report_data(name,start,end)
    with connection() as db:
        audit(db, user, 'reports.export', name)
    return Response(content=csv_bytes(report), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition':f'attachment; filename="queuepilot-{name}.csv"',
                             'Cache-Control':'no-store'})


@router.get('/reports/{name}')
def report_json(name: str, start: datetime | None = None, end: datetime | None = None,
                user=Depends(require_supervisor)):
    return report_data(name,start,end)
