import datetime as dt
import sqlite3
import time
import uuid
from zoneinfo import ZoneInfo
from fastapi import HTTPException
from .config import config
from .db import connection
from .models import AvayaEvent


def mask_caller(value: str | None) -> str:
    if not value:
        return 'Private caller'
    value = ''.join(x for x in value if x.isdigit() or x == '+')
    digits = ''.join(x for x in value if x.isdigit())
    if len(digits) < 4:
        return 'Private caller'
    return '••• ••• ' + digits[-4:]


def ingest(event: AvayaEvent):
    occurred = event.occurred_at.timestamp()
    now = time.time()
    if occurred > now + 300:
        raise HTTPException(422, 'Event timestamp is too far in the future')
    if occurred < now - 366 * 86400:
        raise HTTPException(422, 'Event timestamp is over one year old')
    with connection() as db:
        try:
            db.execute('''INSERT INTO events(event_id,source,event_type,call_id,agent_id,occurred_at,received_at,outcome)
                          VALUES(?,?,?,?,?,?,?,?)''',
                       (event.event_id, event.source, event.type, event.call_id, event.agent_id, occurred, now, 'accepted'))
        except sqlite3.IntegrityError:
            return {'outcome': 'duplicate', 'event_id': event.event_id}

        if event.type == 'agent.status':
            agent = db.execute('SELECT * FROM agents WHERE id=?', (event.agent_id,)).fetchone()
            if not agent:
                raise HTTPException(404, f'Unknown agent_id {event.agent_id}')
            if occurred >= agent['last_change']:
                db.execute('UPDATE agents SET status=?, last_change=? WHERE id=?',
                           (event.agent_status, occurred, event.agent_id))
                outcome = 'updated'
            else:
                outcome = 'stale'
        else:
            q = db.execute('SELECT 1 FROM queues WHERE id=?', (event.queue_id,)).fetchone()
            if not q:
                raise HTTPException(404, f'Unknown queue_id {event.queue_id}')
            call = db.execute('SELECT * FROM calls WHERE source=? AND external_id=?',
                              (event.source, event.call_id)).fetchone()
            if call and call['queue_id'] != event.queue_id:
                raise HTTPException(409, 'call_id was previously associated with another queue')
            if event.agent_id and not db.execute('SELECT 1 FROM agents WHERE id=?', (event.agent_id,)).fetchone():
                raise HTTPException(404, f'Unknown agent_id {event.agent_id}')
            if call and occurred < call['last_event_at']:
                outcome = 'stale'
            elif call and call['status'] in ('abandoned', 'completed'):
                outcome = 'stale'
            elif event.type == 'call.queued':
                if call:
                    outcome = 'stale'  # no regression from answered to queued
                else:
                    db.execute('''INSERT INTO calls(id,source,external_id,queue_id,caller,status,arrived_at,last_event_at)
                                  VALUES(?,?,?,?,?,?,?,?)''',
                               (str(uuid.uuid4()), event.source, event.call_id, event.queue_id,
                                mask_caller(event.caller), 'queued', occurred, occurred))
                    outcome = 'created'
            elif event.type == 'call.answered':
                if not call:
                    db.execute('''INSERT INTO calls(id,source,external_id,queue_id,caller,status,arrived_at,answered_at,agent_id,last_event_at)
                                  VALUES(?,?,?,?,?,?,?,?,?,?)''',
                               (str(uuid.uuid4()), event.source, event.call_id, event.queue_id,
                                mask_caller(event.caller), 'answered', occurred, occurred, event.agent_id, occurred))
                    outcome = 'created_with_missing_queue_event'
                elif call['status'] == 'queued':
                    db.execute('UPDATE calls SET status=?,answered_at=?,agent_id=?,last_event_at=? WHERE id=?',
                               ('answered', occurred, event.agent_id, occurred, call['id']))
                    outcome = 'updated'
                else:
                    outcome = 'stale'
            elif event.type == 'call.abandoned':
                if not call:
                    db.execute('''INSERT INTO calls(id,source,external_id,queue_id,caller,status,arrived_at,ended_at,last_event_at)
                                  VALUES(?,?,?,?,?,?,?,?,?)''',
                               (str(uuid.uuid4()), event.source, event.call_id, event.queue_id,
                                mask_caller(event.caller), 'abandoned', occurred, occurred, occurred))
                    outcome = 'created_with_missing_queue_event'
                elif call['status'] == 'queued':
                    db.execute('UPDATE calls SET status=?,ended_at=?,last_event_at=? WHERE id=?',
                               ('abandoned', occurred, occurred, call['id']))
                    outcome = 'updated'
                else:
                    outcome = 'stale'
            else:  # completed: after agent handled call
                if not call:
                    db.execute('''INSERT INTO calls(id,source,external_id,queue_id,caller,status,arrived_at,ended_at,agent_id,last_event_at)
                                  VALUES(?,?,?,?,?,?,?,?,?,?)''',
                               (str(uuid.uuid4()), event.source, event.call_id, event.queue_id,
                                mask_caller(event.caller), 'completed', occurred, occurred, event.agent_id, occurred))
                    outcome = 'created_with_missing_start_event'
                else:
                    db.execute('UPDATE calls SET status=?,ended_at=?,last_event_at=? WHERE id=?',
                               ('completed', occurred, occurred, call['id']))
                    outcome = 'updated'
        db.execute('UPDATE events SET outcome=? WHERE source=? AND event_id=?',
                   (outcome, event.source, event.event_id))
    return {'outcome': outcome, 'event_id': event.event_id}


def local_today_start():
    tz = ZoneInfo(config.timezone)
    d = dt.datetime.now(tz).date()
    return dt.datetime.combine(d, dt.time.min, tzinfo=tz).timestamp()


def dashboard():
    now = time.time()
    today = local_today_start()
    with connection() as db:
        queues = [dict(x) for x in db.execute('SELECT * FROM queues ORDER BY name')]
        agents = [dict(x) for x in db.execute('SELECT * FROM agents ORDER BY name')]
        calls = [dict(x) for x in db.execute('SELECT * FROM calls ORDER BY arrived_at DESC LIMIT 60')]
        for call in calls:
            call['wait_seconds'] = round(max(0, (call['answered_at'] or call['ended_at'] or now) - call['arrived_at']))
            call['external_id'] = call['external_id'][-12:]
        active_calls = [dict(x) for x in db.execute("SELECT queue_id,status,arrived_at FROM calls WHERE status IN ('queued','answered')")]
        today_calls = [dict(x) for x in db.execute('''SELECT queue_id,status,arrived_at,answered_at,ended_at
                     FROM calls WHERE arrived_at >= ?''', (today,))]
        event_latest = db.execute('SELECT source,received_at FROM events ORDER BY received_at DESC LIMIT 1').fetchone()
        heartbeat = db.execute('SELECT * FROM connector_heartbeats ORDER BY last_heartbeat DESC LIMIT 1').fetchone()
        daily = [dict(x) for x in db.execute('''SELECT date(arrived_at,'unixepoch') AS utc_day,
                  SUM(CASE WHEN answered_at IS NOT NULL THEN 1 ELSE 0 END) answered,
                  SUM(CASE WHEN status='abandoned' THEN 1 ELSE 0 END) abandoned
                  FROM calls WHERE arrived_at >= ? GROUP BY utc_day ORDER BY utc_day''', (now-7*86400,))]

    def stats(subset, sla):
        answered = [c for c in subset if c['answered_at'] is not None]
        abandoned = [c for c in subset if c['status'] == 'abandoned']
        eligible = len(answered) + len(abandoned)
        within = sum(1 for c in answered if (c['answered_at'] - c['arrived_at']) <= sla)
        return {'answered': len(answered), 'abandoned': len(abandoned),
                'sla_percent': round(within * 100 / eligible) if eligible else None,
                'average_wait_seconds': round(sum(max(0, c['answered_at']-c['arrived_at']) for c in answered)/len(answered)) if answered else None}

    for q in queues:
        q_active = [c for c in active_calls if c['queue_id'] == q['id']]
        q_day = [c for c in today_calls if c['queue_id'] == q['id']]
        q['waiting'] = sum(c['status'] == 'queued' for c in q_active)
        q['in_call'] = sum(c['status'] == 'answered' for c in q_active)
        q['longest_wait_seconds'] = round(max([now-c['arrived_at'] for c in q_active if c['status'] == 'queued'] or [0]))
        q['available_agents'] = sum(a['queue_id'] == q['id'] and a['status'] == 'available' for a in agents)
        q.update(stats(q_day, q['sla_seconds']))

    combined_eligible = sum(q['answered'] + q['abandoned'] for q in queues)
    combined_within = 0
    for q in queues:
        combined_within += sum(c['queue_id'] == q['id'] and c['answered_at'] is not None and
                               c['answered_at'] - c['arrived_at'] <= q['sla_seconds'] for c in today_calls)
    total = {'waiting': sum(q['waiting'] for q in queues),
             'active_calls': sum(q['in_call'] for q in queues),
             'answered': sum(q['answered'] for q in queues),
             'abandoned': sum(q['abandoned'] for q in queues),
             'sla_percent': round(100 * combined_within / combined_eligible) if combined_eligible else None,
             'available_agents': sum(a['status']=='available' for a in agents),
             'total_agents': len(agents),
             'total_queues': len(queues)}
    return {'totals': total, 'queues': queues, 'agents': agents, 'calls': calls,
            'daily': daily, 'integration': {
                'mode': 'DEMO' if config.demo_mode else 'LIVE INGEST',
                'last_event_at': event_latest['received_at'] if event_latest else None,
                'last_source': event_latest['source'] if event_latest else None,
                'connected': bool(heartbeat and heartbeat['cti_connected'] and now-heartbeat['last_heartbeat'] <= 30),
                'bridge_source': heartbeat['source'] if heartbeat else None,
                'bridge_details': heartbeat['details'] if heartbeat else None,
                'last_heartbeat': heartbeat['last_heartbeat'] if heartbeat else None,
                'ingestion_ready': True,
                }, 'server_time': now, 'timezone': config.timezone}


def create_queue(name, skill='', sla_seconds=20, target_percent=80, enabled=True):
    qid = 'q_' + uuid.uuid4().hex[:10]
    with connection() as db:
        db.execute('INSERT INTO queues(id,name,skill,sla_seconds,target_percent,enabled,created_at) VALUES(?,?,?,?,?,?,?)',
                   (qid, name, skill, sla_seconds, target_percent, int(enabled), time.time()))
    return qid


def create_agent(name, extension='', queue_id=None, status='offline'):
    aid = 'a_' + uuid.uuid4().hex[:10]
    with connection() as db:
        if queue_id and not db.execute('SELECT 1 FROM queues WHERE id=?', (queue_id,)).fetchone():
            raise HTTPException(404,'Unknown queue')
        db.execute('INSERT INTO agents(id,name,extension,queue_id,status,last_change) VALUES(?,?,?,?,?,?)',
                   (aid, name, extension, queue_id, status, time.time()))
    return aid


def seed_demo():
    with connection() as db:
        n = db.execute('SELECT COUNT(*) AS n FROM queues').fetchone()['n']
    if n:
        return {'seeded': False, 'message': 'Queues already exist; preserving data'}
    qs = [('Customer Care','SUPPORT',20,80), ('Technical Support','TECH',30,85),
          ('Sales Enquiries','SALES',15,90), ('VIP Priority','VIP',10,95)]
    ids = [create_queue(*q) for q in qs]
    names = [('Maya Chen','101','available',0),('Omar Hassan','102','busy',0),
             ('Nadia Ali','103','available',1),('Daniel Reed','104','acw',1),
             ('Lina Costa','105','available',2),('Adam Wilson','106','away',2),
             ('Sara Patel','107','available',3),('Karim Fadel','108','offline',0)]
    aids = [create_agent(n,ext,ids[i],s) for n,ext,s,i in names]
    moment = dt.datetime.now(dt.timezone.utc)
    for i in range(36):
        queue = ids[(i*7) % len(ids)]
        t = moment - dt.timedelta(minutes=(i+1)*12)
        cid = f'demo-history-{i}'
        ingest(AvayaEvent(event_id=f'{cid}-q', source='demo', type='call.queued', call_id=cid,
                          queue_id=queue, caller='20255501'+str(i%100).zfill(2), occurred_at=t))
        if i % 9 == 0:
            ingest(AvayaEvent(event_id=f'{cid}-a', source='demo', type='call.abandoned', call_id=cid,
                              queue_id=queue, occurred_at=t+dt.timedelta(seconds=8+i%40)))
        else:
            at = t+dt.timedelta(seconds=(i*7)%48+3)
            ingest(AvayaEvent(event_id=f'{cid}-ans', source='demo', type='call.answered', call_id=cid,
                              queue_id=queue, agent_id=aids[i%len(aids)], occurred_at=at))
            ingest(AvayaEvent(event_id=f'{cid}-end', source='demo', type='call.completed', call_id=cid,
                              queue_id=queue, occurred_at=at+dt.timedelta(minutes=2+i%4)))
    for i in range(5):
        t = moment - dt.timedelta(seconds=25+i*13)
        ingest(AvayaEvent(event_id=f'demo-live-{i}', source='demo', type='call.queued',
                          call_id=f'demo-live-{i}', queue_id=ids[i%len(ids)], caller='2025550199', occurred_at=t))
    return {'seeded': True, 'queues': len(ids), 'agents': len(aids)}
