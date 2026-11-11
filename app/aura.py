"""Avaya Aura integration boundary.

This module never calls the PBX directly. It accepts strictly mapped, normalized
AES bridge events and gates *requested* commands for an independently installed,
authorized AES WTI/TSAPI bridge. State `succeeded` is bridge-reported only.
"""
import re
import sqlite3
import time
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from .config import config
from .db import connection
from .models import AvayaEvent
from .security import require_admin, require_ingest, require_aura_control_bridge
from .service import ingest

router = APIRouter(prefix='/api/aura', tags=['Avaya Aura integration'])
IDENTIFIER = re.compile(r'^\d{1,12}$')
INTERNAL_EXTENSION = re.compile(r'^\d{2,8}$')


def digits(value: str | None, field: str) -> str | None:
    if value is not None and not IDENTIFIER.fullmatch(value):
        raise HTTPException(422, f'{field} must be 1–12 digits')
    return value


def audit(db, actor: str, operation: str, entity: str, details: str = ''):
    db.execute('INSERT INTO aura_audit(at,actor,operation,entity_id,details) VALUES(?,?,?,?,?)',
               (time.time(), actor[:80], operation[:80], entity[:160], details[:180]))


class QueueBinding(BaseModel):
    vdn: str | None = None
    skill_number: str | None = None
    vector_number: str | None = None

    @model_validator(mode='after')
    def validate_binding(self):
        for attr in ('vdn', 'skill_number', 'vector_number'):
            digits(getattr(self, attr), attr)
        if not self.vdn and not self.skill_number:
            raise ValueError('Provide a VDN, skill number, or both')
        return self


class AgentBinding(BaseModel):
    station: str
    login_id: str | None = None

    @model_validator(mode='after')
    def validate_binding(self):
        digits(self.station, 'station')
        digits(self.login_id, 'login_id')
        return self


class AuraBridgeEvent(BaseModel):
    """Canonical data expected AFTER vendor WTI/TSAPI callbacks are parsed.

    This is intentionally not claimed as the raw Avaya WTI JSON schema.
    """
    event_id: str = Field(min_length=1, max_length=160)
    event_type: Literal['call.queued','call.answered','call.abandoned','call.completed','agent.status']
    occurred_at: datetime
    call_id: str | None = Field(default=None, max_length=160)
    vdn: str | None = None
    skill_number: str | None = None
    agent_station: str | None = None
    agent_login: str | None = None
    agent_status: Literal['available','busy','acw','away','offline'] | None = None
    caller: str | None = Field(default=None, max_length=120)

    @field_validator('occurred_at')
    @classmethod
    def timezone_required(cls, val):
        if val.tzinfo is None or val.utcoffset() is None:
            raise ValueError('A timezone-aware timestamp is required')
        return val

    @model_validator(mode='after')
    def validate_event(self):
        for attr in ('vdn', 'skill_number', 'agent_station', 'agent_login'):
            digits(getattr(self, attr), attr)
        if self.event_type.startswith('call.') and (not self.call_id or not (self.vdn or self.skill_number)):
            raise ValueError('Call event requires call_id and VDN or skill_number')
        if self.event_type == 'agent.status' and (not (self.agent_station or self.agent_login) or not self.agent_status):
            raise ValueError('Agent status requires a mapped station/login and status')
        return self


@router.get('/bindings')
def list_bindings(_=Depends(require_admin)):
    with connection() as db:
        queues = [dict(r) for r in db.execute('''SELECT q.id,q.name,b.vdn,b.skill_number,b.vector_number
                   FROM queues q LEFT JOIN aura_queue_bindings b ON b.queue_id=q.id ORDER BY q.name''')]
        agents = [dict(r) for r in db.execute('''SELECT a.id,a.name,a.extension,b.station,b.login_id
                   FROM agents a LEFT JOIN aura_agent_bindings b ON b.agent_id=a.id ORDER BY a.name''')]
    return {'queues': queues, 'agents': agents}


@router.put('/queues/{qid}/binding')
def bind_queue(qid: str, data: QueueBinding, user=Depends(require_admin)):
    with connection() as db:
        if not db.execute('SELECT 1 FROM queues WHERE id=?', (qid,)).fetchone():
            raise HTTPException(404, 'Queue not found')
        try:
            db.execute('''INSERT INTO aura_queue_bindings(queue_id,vdn,skill_number,vector_number,updated_at)
                       VALUES(?,?,?,?,?) ON CONFLICT(queue_id) DO UPDATE SET
                       vdn=excluded.vdn,skill_number=excluded.skill_number,
                       vector_number=excluded.vector_number,updated_at=excluded.updated_at''',
                       (qid, data.vdn, data.skill_number, data.vector_number, time.time()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, 'VDN or skill number is already mapped') from None
        audit(db, user, 'queue.binding.updated', qid, 'VDN/skill mapping updated')
    return {'queue_id': qid, 'binding': data.model_dump()}


@router.put('/agents/{aid}/binding')
def bind_agent(aid: str, data: AgentBinding, user=Depends(require_admin)):
    with connection() as db:
        if not db.execute('SELECT 1 FROM agents WHERE id=?', (aid,)).fetchone():
            raise HTTPException(404, 'Agent not found')
        try:
            db.execute('''INSERT INTO aura_agent_bindings(agent_id,station,login_id,updated_at)
                       VALUES(?,?,?,?) ON CONFLICT(agent_id) DO UPDATE SET
                       station=excluded.station,login_id=excluded.login_id,updated_at=excluded.updated_at''',
                       (aid, data.station, data.login_id, time.time()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, 'Station or login ID is already mapped') from None
        audit(db, user, 'agent.binding.updated', aid, 'Station/login mapping updated')
    return {'agent_id': aid, 'binding': data.model_dump()}


@router.post('/events')
def mapped_aura_event(event: AuraBridgeEvent, _=Depends(require_ingest)):
    with connection() as db:
        if event.event_type == 'agent.status':
            matches = set()
            if event.agent_station:
                matches.update(r['agent_id'] for r in db.execute('SELECT agent_id FROM aura_agent_bindings WHERE station=?', (event.agent_station,)))
            if event.agent_login:
                matches.update(r['agent_id'] for r in db.execute('SELECT agent_id FROM aura_agent_bindings WHERE login_id=?', (event.agent_login,)))
            if len(matches) != 1:
                raise HTTPException(422, 'Agent station/login is unmapped or inconsistent')
            agent_id, queue_id = matches.pop(), None
        else:
            matches = set()
            if event.vdn:
                matches.update(r['queue_id'] for r in db.execute('SELECT queue_id FROM aura_queue_bindings WHERE vdn=?', (event.vdn,)))
            if event.skill_number:
                matches.update(r['queue_id'] for r in db.execute('SELECT queue_id FROM aura_queue_bindings WHERE skill_number=?', (event.skill_number,)))
            if len(matches) != 1:
                raise HTTPException(422, 'VDN/skill is unmapped or inconsistent')
            queue_id = matches.pop()
            agent_id = None
            if event.agent_station or event.agent_login:
                ids = set()
                if event.agent_station:
                    ids.update(r['agent_id'] for r in db.execute('SELECT agent_id FROM aura_agent_bindings WHERE station=?', (event.agent_station,)))
                if event.agent_login:
                    ids.update(r['agent_id'] for r in db.execute('SELECT agent_id FROM aura_agent_bindings WHERE login_id=?', (event.agent_login,)))
                if len(ids) != 1:
                    raise HTTPException(422, 'Agent station/login is unmapped or inconsistent')
                agent_id = ids.pop()
    normalized = AvayaEvent(event_id=event.event_id, source='aura-aes', type=event.event_type,
                            occurred_at=event.occurred_at, call_id=event.call_id,
                            queue_id=queue_id, agent_id=agent_id,
                            agent_status=event.agent_status, caller=event.caller)
    return ingest(normalized)


CommandAction = Literal['call.make','call.answer','call.hold','call.retrieve',
                        'call.transfer','call.drop','agent.ready','agent.aux']


class CommandRequest(BaseModel):
    request_key: str = Field(min_length=8, max_length=100, pattern=r'^[a-zA-Z0-9_-]+$')
    action: CommandAction
    station: str
    call_id: str | None = Field(default=None, max_length=160)
    destination: str | None = None

    @model_validator(mode='after')
    def valid_command(self):
        digits(self.station, 'station')
        if self.action.startswith('call.') and self.action != 'call.make' and not self.call_id:
            raise ValueError('call_id is required for this call action')
        if self.action in ('call.make', 'call.transfer'):
            if not self.destination or not INTERNAL_EXTENSION.fullmatch(self.destination):
                raise ValueError('Destination must be an internal 2–8 digit extension; PSTN blocked')
        elif self.destination:
            raise ValueError('Destination is not valid for this action')
        if self.action.startswith('agent.') and self.call_id:
            raise ValueError('Agent actions cannot contain call_id')
        return self


@router.get('/commands')
def list_commands(_=Depends(require_admin)):
    with connection() as db:
        result = [dict(row) for row in db.execute('''SELECT * FROM aura_commands ORDER BY created_at DESC LIMIT 100''')]
    return {'commands': result, 'control_enabled': config.aura_control_enabled}


@router.post('/commands', status_code=201)
def create_command(data: CommandRequest, user=Depends(require_admin)):
    with connection() as db:
        if not db.execute('SELECT 1 FROM aura_agent_bindings WHERE station=?', (data.station,)).fetchone():
            raise HTTPException(422, 'Station must be registered in the Avaya agent bindings first')
        prior = db.execute('SELECT * FROM aura_commands WHERE request_key=?', (data.request_key,)).fetchone()
        if prior:
            if (prior['action'], prior['station'], prior['call_id'], prior['destination']) != (data.action, data.station, data.call_id, data.destination):
                raise HTTPException(409, 'request_key was used for different command content')
            return {'id': prior['id'], 'status': prior['status'], 'idempotent': True}
        # Never let a transfer/originate reach an unreviewed destination.
        # A configured station or VDN is an explicit local allowlist entry.
        if data.destination:
            approved_station = db.execute('SELECT 1 FROM aura_agent_bindings WHERE station=?', (data.destination,)).fetchone()
            approved_vdn = db.execute('SELECT 1 FROM aura_queue_bindings WHERE vdn=?', (data.destination,)).fetchone()
            if not approved_station and not approved_vdn:
                raise HTTPException(422, 'Destination is not a registered internal station or VDN')
        cid = 'cmd_' + uuid.uuid4().hex[:16]
        db.execute('''INSERT INTO aura_commands(id,request_key,action,station,call_id,destination,status,created_by,created_at)
                      VALUES(?,?,?,?,?,?,?,?,?)''',
                   (cid, data.request_key, data.action, data.station, data.call_id,
                    data.destination, 'pending_approval', user, time.time()))
        audit(db, user, 'command.proposed', cid, data.action)
    return {'id': cid, 'status': 'pending_approval'}


class Approval(BaseModel):
    confirmation: Literal['APPROVE AVAYA ACTION']


@router.post('/commands/{command_id}/approve')
def approve_command(command_id: str, _: Approval, user=Depends(require_admin)):
    if not config.aura_control_enabled:
        raise HTTPException(403, 'Avaya control is disabled in server configuration')
    with connection() as db:
        r = db.execute('''UPDATE aura_commands SET status='queued', approved_at=?
                       WHERE id=? AND status='pending_approval' ''', (time.time(), command_id))
        if r.rowcount != 1:
            raise HTTPException(409, 'Command not found or not pending approval')
        audit(db, user, 'command.approved', command_id)
    return {'id': command_id, 'status': 'queued', 'note': 'Queued for bridge; NOT executed on Avaya'}


@router.post('/commands/{command_id}/cancel')
def cancel_command(command_id: str, user=Depends(require_admin)):
    with connection() as db:
        r = db.execute('''UPDATE aura_commands SET status='cancelled',finished_at=?
                    WHERE id=? AND status IN ('pending_approval','queued')''', (time.time(), command_id))
        if r.rowcount != 1:
            raise HTTPException(409, 'Only pending or queued commands can be cancelled')
        audit(db, user, 'command.cancelled', command_id)
    return {'id': command_id, 'status': 'cancelled'}


@router.post('/bridge/claim')
def claim_command(_=Depends(require_aura_control_bridge)):
    """Atomic one-time lease. Never re-issue an unacknowledged telephony action."""
    if not config.aura_control_enabled:
        raise HTTPException(403, 'Avaya call control disabled')
    with connection() as db:
        now = time.time()
        # Stale approved requests must not suddenly execute when the bridge reconnects.
        expired = db.execute("SELECT id FROM aura_commands WHERE status='queued' AND approved_at<?", (now-120,)).fetchall()
        for item in expired:
            db.execute("UPDATE aura_commands SET status='expired',finished_at=? WHERE id=? AND status='queued'", (now,item['id']))
            audit(db, 'system', 'command.expired', item['id'])
        # A claimed command is never automatically reissued; uncertain means manual PBX reconciliation.
        unknown = db.execute("SELECT id FROM aura_commands WHERE status='leased' AND leased_at<?", (now-180,)).fetchall()
        for item in unknown:
            db.execute("UPDATE aura_commands SET status='uncertain',finished_at=?,result_code='LEASE_TIMEOUT' WHERE id=? AND status='leased'", (now,item['id']))
            audit(db, 'system', 'command.uncertain', item['id'], 'Lease timed out; no retry')
        hb = db.execute("SELECT * FROM connector_heartbeats WHERE source='aura-aes'").fetchone()
        if not hb or not hb['cti_connected'] or time.time()-hb['last_heartbeat'] > 30:
            raise HTTPException(503, 'No recent active AES bridge heartbeat')
        row = db.execute("SELECT * FROM aura_commands WHERE status='queued' ORDER BY approved_at LIMIT 1").fetchone()
        if not row:
            return {'command': None}
        now = time.time()
        update = db.execute("UPDATE aura_commands SET status='leased',leased_at=? WHERE id=? AND status='queued'", (now, row['id']))
        if update.rowcount != 1:
            return {'command': None}
        audit(db, 'aura-aes', 'command.leased', row['id'])
        command = {k: row[k] for k in ('id','action','station','call_id','destination')}
    return {'command': command, 'warning': 'Bridge must verify CTI state; do not auto-retry if result is unknown'}


class CommandResult(BaseModel):
    result: Literal['succeeded','failed','uncertain']
    code: str = Field(default='', max_length=48)
    message: str = Field(default='', max_length=160)


@router.post('/bridge/commands/{command_id}/result')
def bridge_command_result(command_id: str, result: CommandResult, _=Depends(require_aura_control_bridge)):
    with connection() as db:
        row = db.execute("UPDATE aura_commands SET status=?,finished_at=?,result_code=?,result_message=? WHERE id=? AND status='leased'",
                         (result.result, time.time(), result.code, result.message, command_id))
        if row.rowcount != 1:
            raise HTTPException(409, 'Command is not currently leased; outcome was not changed')
        audit(db, 'aura-aes', 'command.'+result.result, command_id, result.code)
    return {'id': command_id, 'status': result.result, 'bridge_reported': True}


class RoutingPlan(BaseModel):
    queue_id: str
    change_type: Literal['vdn_destination','vector_steps','skill_membership','hours_of_operation']
    desired: str = Field(min_length=4, max_length=1000)
    reason: str = Field(min_length=8, max_length=500)


@router.post('/routing-plans', status_code=201)
def propose_routing_change(data: RoutingPlan, user=Depends(require_admin)):
    """Change advisory only; never writes vector/SMS/SAT configuration."""
    pid = 'plan_' + uuid.uuid4().hex[:16]
    with connection() as db:
        if not db.execute('SELECT 1 FROM queues WHERE id=?', (data.queue_id,)).fetchone():
            raise HTTPException(404, 'Queue not found')
        db.execute('''INSERT INTO aura_routing_plans(id,queue_id,change_type,desired,reason,status,created_at,created_by)
                  VALUES(?,?,?,?,?,?,?,?)''', (pid, data.queue_id, data.change_type, data.desired,
                                             data.reason, 'proposed', time.time(), user))
        audit(db, user, 'routing.proposed', pid, data.change_type)
    return {'id': pid, 'status': 'proposed', 'pbx_changed': False}


@router.get('/routing-plans')
def list_routing_changes(_=Depends(require_admin)):
    with connection() as db:
        rows = [dict(r) for r in db.execute('SELECT * FROM aura_routing_plans ORDER BY created_at DESC LIMIT 100')]
    return {'plans': rows, 'execution_supported': False}


@router.get('/audit')
def audit_log(_=Depends(require_admin)):
    with connection() as db:
        rows = [dict(r) for r in db.execute('SELECT * FROM aura_audit ORDER BY id DESC LIMIT 150')]
    return {'entries': rows}


@router.get('/readiness')
def readiness(_=Depends(require_admin)):
    now = time.time()
    with connection() as db:
        n_queues = db.execute('SELECT COUNT(*) FROM aura_queue_bindings').fetchone()[0]
        n_agents = db.execute('SELECT COUNT(*) FROM aura_agent_bindings').fetchone()[0]
        hb = db.execute("SELECT * FROM connector_heartbeats WHERE source='aura-aes'").fetchone()
        pending = db.execute("SELECT COUNT(*) FROM aura_commands WHERE status IN ('pending_approval','queued','leased')").fetchone()[0]
    return {'provider': 'Avaya Aura AES / Communication Manager',
            'control_enabled': config.aura_control_enabled,
            'queues_mapped': n_queues, 'agents_mapped': n_agents,
            'bridge_reported_connected': bool(hb and hb['cti_connected'] and now-hb['last_heartbeat']<=30),
            'commands_in_progress': pending,
            'pbx_connection_verified_independently': False,
            'routing_changes_automated': False,
            'requirements': ['AES CTI link and configured TLink', 'Licensed WTI/TSAPI monitors',
                             'Authorized integration account and trusted TLS',
                             'Verified VDN/skill/station inventory', 'Non-production commissioning tests']}
