import csv
import datetime as dt
import hmac
import io
import random
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from .config import config, validate_config
from .db import connection, init_db
from .models import AgentIn, AgentStatusIn, EngineEvent, EngineHeartbeat, Login, QueueIn
from .security import encode_token, require_admin, require_engine_event, require_supervisor
from .service import create_agent, create_queue, dashboard, ingest, seed_demo
from .contact_center import router as contact_center_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_config()
    init_db()
    yield

app = FastAPI(title='QueuePilot Independent Contact Center', version='0.9.0-lab', lifespan=lifespan,
              description='Independent QueuePilot SIP/ACD laboratory architecture; no AES/WTI management APIs. Not production-ready.')
app.include_router(contact_center_router)
static_root = Path(__file__).parent / 'static'
app.mount('/static', StaticFiles(directory=static_root), name='static')

@app.get('/', include_in_schema=False)
def index():
    return FileResponse(static_root / 'index.html')

@app.get('/api/health')
def health():
    return {'status': 'ok', 'demo_mode': config.demo_mode, 'version': '0.9.0-lab'}

# Single administrator for development/MVP. Use enterprise SSO and proper RBAC in production.
login_attempts = {}
@app.post('/api/login')
def login(body: Login):
    now = time.time()
    # Basic per-user cooldown, not a replacement for reverse-proxy IP rate limiting.
    failures = [t for t in login_attempts.get(body.username[:128], []) if t > now - 600]
    if len(failures) >= 8:
        raise HTTPException(429, 'Too many login attempts; retry later')
    is_admin = (hmac.compare_digest(body.username.encode(), config.admin_user.encode())
                and hmac.compare_digest(body.password.encode(), config.admin_password.encode()))
    is_reviewer = (len(config.reviewer_password) >= 12
                   and hmac.compare_digest(body.username.encode(), config.reviewer_user.encode())
                   and hmac.compare_digest(body.password.encode(), config.reviewer_password.encode()))
    if not (is_admin or is_reviewer):
        failures.append(now)
        login_attempts[body.username[:128]] = failures
        raise HTTPException(401, 'Invalid credentials')
    login_attempts.pop(body.username[:128], None)
    return {'token': encode_token(body.username), 'expires_in': 28800, 'user': body.username, 'role': 'admin' if is_admin else 'reviewer'}

@app.get('/api/dashboard')
def overview(_=Depends(require_supervisor)):
    return dashboard()

@app.post('/api/queues', status_code=201)
def add_queue(q: QueueIn, _=Depends(require_admin)):
    qid = create_queue(**q.model_dump())
    return {'id': qid}

@app.put('/api/queues/{qid}')
def edit_queue(qid: str, q: QueueIn, _=Depends(require_admin)):
    with connection() as db:
        cursor = db.execute('''UPDATE queues SET name=?,skill=?,sla_seconds=?,target_percent=?,enabled=? WHERE id=?''',
                            (q.name, q.skill, q.sla_seconds, q.target_percent, int(q.enabled), qid))
        if cursor.rowcount != 1:
            raise HTTPException(404, 'Queue not found')
    return {'id': qid, 'updated': True}

@app.post('/api/agents', status_code=201)
def add_agent(agent: AgentIn, _=Depends(require_admin)):
    aid = create_agent(**agent.model_dump())
    return {'id': aid}

@app.patch('/api/agents/{aid}/status')
def edit_status(aid: str, status: AgentStatusIn, _=Depends(require_admin)):
    # Dashboard directory only. Native SIP agent state synchronization is not yet wired.
    with connection() as db:
        cursor = db.execute('UPDATE agents SET status=?,last_change=? WHERE id=?',
                            (status.status, time.time(), aid))
        if cursor.rowcount != 1:
            raise HTTPException(404, 'Agent not found')
    return {'id': aid, 'updated': True, 'scope': 'directory_only_not_sip_runtime'}

@app.post('/api/engine/events')
def receive_event(event: EngineEvent, _=Depends(require_engine_event)):
    if event.source != 'queuepilot-native':
        raise HTTPException(422, 'Only native QueuePilot engine events are accepted')
    return ingest(event)

@app.post('/api/engine/heartbeat')
def engine_heartbeat(body: EngineHeartbeat, _=Depends(require_engine_event)):
    if body.source != 'queuepilot-native':
        raise HTTPException(422, 'Only the native QueuePilot engine may report health')
    # This is reported runtime health, not an independently validated SIP/media session.
    with connection() as db:
        db.execute('''INSERT INTO engine_heartbeats(source,engine_healthy,last_heartbeat,details)
                      VALUES(?,?,?,?) ON CONFLICT(source) DO UPDATE SET
                      engine_healthy=excluded.engine_healthy,
                      last_heartbeat=excluded.last_heartbeat,
                      details=excluded.details''',
                   (body.source, int(body.connected), time.time(), body.details))
    return {'status': 'received', 'source': body.source}

@app.get('/api/reports/calls.csv')
def export_calls(_=Depends(require_admin)):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(['Source','Call ID','Queue','Status','Arrived UTC','Answered UTC','Ended UTC','Agent','Wait seconds'])
    with connection() as db:
        rows = db.execute('''SELECT calls.*,queues.name AS queue_name FROM calls JOIN queues ON queues.id=calls.queue_id
                             ORDER BY arrived_at DESC LIMIT 50000''').fetchall()
        for x in rows:
            fmt = lambda v: dt.datetime.fromtimestamp(v, dt.timezone.utc).isoformat() if v else ''
            end = x['answered_at'] or x['ended_at']
            writer.writerow([x['source'], x['external_id'], x['queue_name'], x['status'], fmt(x['arrived_at']),
                             fmt(x['answered_at']), fmt(x['ended_at']), x['agent_id'] or '',
                             round(max(0, end-x['arrived_at'])) if end else ''])
    result = io.BytesIO(out.getvalue().encode('utf-8-sig'))
    return StreamingResponse(result, media_type='text/csv; charset=utf-8', headers={
        'Content-Disposition': 'attachment; filename="call-queue-report.csv"',
        'Cache-Control': 'no-store'})

@app.post('/api/demo/seed')
def demo_seed(_=Depends(require_admin)):
    if not config.demo_mode:
        raise HTTPException(403, 'Demo endpoints are disabled')
    return seed_demo()

@app.post('/api/demo/step')
def demo_step(_=Depends(require_admin)):
    if not config.demo_mode:
        raise HTTPException(403, 'Demo endpoints are disabled')
    with connection() as db:
        qs = [r['id'] for r in db.execute('SELECT id FROM queues WHERE enabled=1')]
        open_calls = [dict(r) for r in db.execute("SELECT * FROM calls WHERE source='demo' AND status='queued' ORDER BY arrived_at")]
        agents = [r['id'] for r in db.execute('SELECT id FROM agents')]
    if not qs:
        raise HTTPException(409, 'Seed demo or create queues first')
    now = dt.datetime.now(dt.timezone.utc)
    decision = random.random()
    if open_calls and decision < .48:
        c = random.choice(open_calls)
        event_type = 'call.abandoned' if decision < .13 else 'call.answered'
        event = EngineEvent(event_id=uuid.uuid4().hex, source='demo', type=event_type,
                           call_id=c['external_id'], queue_id=c['queue_id'],
                           agent_id=random.choice(agents) if event_type == 'call.answered' and agents else None,
                           occurred_at=now)
        result = ingest(event)
    elif decision < .75:
        with connection() as db:
            talking = [dict(r) for r in db.execute("SELECT * FROM calls WHERE source='demo' AND status='answered'")]
        if talking:
            c = random.choice(talking)
            result = ingest(EngineEvent(event_id=uuid.uuid4().hex, source='demo', type='call.completed',
                                       call_id=c['external_id'], queue_id=c['queue_id'], occurred_at=now))
        else:
            result = {'outcome': 'no_calls_to_complete'}
    else:
        cid = 'demo-' + uuid.uuid4().hex[:12]
        result = ingest(EngineEvent(event_id=uuid.uuid4().hex, source='demo', type='call.queued',
                                   call_id=cid, queue_id=random.choice(qs), caller='2025550100', occurred_at=now))
    return result
