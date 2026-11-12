"""Native lab RTP observation tests; no PBX or external media product required."""
import asyncio
from datetime import datetime, timezone
import struct
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import config
from app.db import connection
from app.main import app
from telephony.live_monitor import LiveAudioMixer, NativeMonitorWorker
from telephony.rtp_media import MediaLeg, RtpRelay
from telephony.sip_stack import MediaAddress


MONITOR_KEY = 'native-monitor-test-credential-at-least-32-characters'


def test_mixer_separates_call_legs_and_is_bounded():
    mixer = LiveAudioMixer(max_frames=2)
    assert mixer.frame() is None
    mixer.ingest('invalid', bytes([0xff] * 160), 0)
    assert mixer.frame() is None
    mixer.ingest('trunk', bytes([0x00] * 160), 0)
    pcm = mixer.frame()
    assert pcm is not None and len(pcm) == 640
    # 8k to 16k duplicate-sample resampling, signed little-endian.
    assert struct.unpack('<hh', pcm[:4]) == (-32124, -32124)
    mixer.ingest('trunk', bytes([0x00] * (160 * 10)), 0)
    assert len(mixer._buffers['trunk']) == 2
    mixer.ingest('phone', bytes([0x80] * 160), 0)
    mixed = mixer.frame()
    assert len(mixed) == 640
    assert struct.unpack('<h', mixed[:2])[0] == 0
    mixer.close()
    assert mixer.frame() is None
    mixer.ingest('trunk', bytes([0xff] * 160), 0)
    assert not mixer._buffers['trunk']


class _FakeTransport:
    def __init__(self):
        self.sent = []
        self.closed = False

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def close(self):
        self.closed = True


def test_only_valid_native_rtp_is_observable_and_recording_unchanged(tmp_path):
    relay = RtpRelay('127.0.0.1', '127.0.0.1', tmp_path)
    trunk_t = _FakeTransport()
    phone_t = _FakeTransport()
    relay.legs = {
        'trunk': MediaLeg(MediaAddress('127.0.0.1', 45000, 0), 10001, trunk_t),
        'phone': MediaLeg(MediaAddress('127.0.0.1', 46000, 0), 10002, phone_t),
    }
    relay.connect(0, 'listen-single-file-test')
    seen = []
    relay.on_audio = lambda side, payload, codec: seen.append((side, len(payload), codec))
    packet = struct.pack('!BBHII', 0x80, 0, 1, 160, 111) + bytes([0xff] * 160)
    relay.forward('trunk', packet, ('127.0.0.2', 45000))  # spoofed source
    relay.forward('trunk', packet[:2], ('127.0.0.1', 45000))  # malformed
    relay.forward('trunk', bytes([0x80, 8]) + packet[2:], ('127.0.0.1', 45000))  # codec mismatch
    assert seen == []
    relay.forward('trunk', packet, ('127.0.0.1', 45000))
    relay.forward('phone', packet, ('127.0.0.1', 46000))
    assert seen == [('trunk', 160, 0), ('phone', 160, 0)]
    assert len(trunk_t.sent) == len(phone_t.sent) == 1
    relay.close()
    assert relay.on_audio is None
    assert list(tmp_path.glob('*-conversation.wav')) and len(list(tmp_path.glob('*.wav'))) == 1


@pytest.fixture
def monitor_setup(tmp_path, monkeypatch):
    values = {
        'db_path': tmp_path / 'cc.sqlite3',
        'admin_password': 'testing-native-monitor-password',
        'reviewer_user': 'reviewer',
        'reviewer_password': 'different-reviewer-strong-password',
        'secret': 'testing-app-signing-secret-with-32-characters-or-more',
        'engine_event_key': 'testing-engine-key-with-32-characters-or-more',
        'monitoring_enabled': True,
        'monitor_engine_key': MONITOR_KEY,
        'recordings_enabled': False,
    }
    for name, value in values.items():
        monkeypatch.setattr(config, name, value)
    with TestClient(app) as c:
        admin_token = c.post('/api/login', json={'username':config.admin_user,'password':config.admin_password}).json()['token']
        review_token = c.post('/api/login', json={'username':config.reviewer_user,'password':config.reviewer_password}).json()['token']
        admin = {'Authorization': 'Bearer ' + admin_token}
        reviewer = {'Authorization': 'Bearer ' + review_token}
        q = c.post('/api/queues', headers=admin, json={'name':'Support lab'}).json()['id']
        a = c.post('/api/agents', headers=admin, json={'name':'Agent 1001','extension':'1001','queue_id':q}).json()['id']
        now = datetime.now(timezone.utc).isoformat()
        key = {'X-Engine-Key': config.engine_event_key}
        for suffix, kind in [('q','call.queued'),('a','call.answered')]:
            req = {'event_id':suffix+'-monitor-lab','source':'queuepilot-native','type':kind,
                   'call_id':'native-listen-call-1','queue_id':q,'occurred_at':now}
            if kind == 'call.answered': req['agent_id'] = a
            assert c.post('/api/engine/events',headers=key,json=req).status_code == 200
        with connection() as db:
            cid = db.execute('SELECT id FROM calls WHERE external_id=?',('native-listen-call-1',)).fetchone()['id']
            db.execute("""INSERT INTO engine_heartbeats(source,engine_healthy,last_heartbeat,details)
                          VALUES('queuepilot-native',1,?,'lab') ON CONFLICT(source)
                          DO UPDATE SET engine_healthy=1,last_heartbeat=excluded.last_heartbeat""",(time.time(),))
        yield c, admin, reviewer, cid



def test_approved_lab_observer_lifecycle(monitor_setup):
    client, admin, reviewer, cid = monitor_setup
    req = client.post('/api/cc/monitor/sessions',headers=admin,json={'call_id':cid,'purpose':'supervision'})
    assert req.status_code == 201, req.text
    sid = req.json()['id']
    assert client.post(f'/api/cc/monitor/sessions/{sid}/audio-ticket',headers=reviewer).status_code == 409
    assert client.post(f'/api/cc/monitor/sessions/{sid}/approve',headers=admin,json={'confirmation':'APPROVE LIVE LISTEN'}).status_code == 403
    approval = client.post(f'/api/cc/monitor/sessions/{sid}/approve',headers=reviewer,json={'confirmation':'APPROVE LIVE LISTEN'})
    assert approval.status_code == 200, approval.text
    media = SimpleNamespace(on_audio=None)
    call = SimpleNamespace(state='bridged',trunk_established=True,extension='1001',media=media)
    switch = SimpleNamespace(calls={'native-listen-call-1':call})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as local:
            worker = NativeMonitorWorker(switch,'http://127.0.0.1:8000',MONITOR_KEY,client=local)
            # No listening before signed administrative request + independent approval.
            assert media.on_audio is None
            await worker.poll_once()
            assert media.on_audio is not None
            assert sid in worker._sessions
            from app.contact_center import _media_subscribers
            q = asyncio.Queue(maxsize=8)
            _media_subscribers[sid].add(q)
            try:
                media.on_audio('trunk',bytes([0x00]*160),0)
                media.on_audio('phone',bytes([0xff]*160),0)
                await worker.audio_once()
                audio = q.get_nowait()
                assert len(audio) == 5*640
                assert struct.unpack('<h',audio[:2])[0] == -16062
                assert audio != bytes(len(audio))  # Actual audio from synthetic RTP, not a dummy feed
                # Stop immediately removes the RTP tap; engine acknowledges termination.
                stopped = await local.post(f'/api/cc/monitor/sessions/{sid}/stop',headers=admin)
                assert stopped.status_code == 200
                await worker.poll_once()
                assert media.on_audio is None
                assert sid not in worker._sessions
                assert (await local.get('/api/cc/monitor/sessions',headers=reviewer)).json()['sessions'][0]['state']=='ended'
            finally:
                _media_subscribers[sid].discard(q)
    asyncio.run(exercise())
    with connection() as db:
        operations = [r['operation'] for r in db.execute('SELECT operation FROM audit_log')]
        assert 'monitor.active' in operations and 'monitor.ended' in operations


def test_monitor_rejects_wrong_station_and_stops_after_call_end(monitor_setup):
    client, admin, reviewer, cid = monitor_setup
    sid = client.post('/api/cc/monitor/sessions',headers=admin,json={'call_id':cid}).json()['id']
    client.post(f'/api/cc/monitor/sessions/{sid}/approve',headers=reviewer,json={'confirmation':'APPROVE LIVE LISTEN'})
    bad = SimpleNamespace(state='bridged',trunk_established=True,extension='9999',media=SimpleNamespace(on_audio=None))
    switch = SimpleNamespace(calls={'native-listen-call-1':bad})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as local:
            worker=NativeMonitorWorker(switch,'http://127.0.0.1:8000',MONITOR_KEY,client=local)
            await worker.poll_once()
            assert bad.media.on_audio is None
            result = await local.get('/api/cc/monitor/sessions',headers=reviewer)
            assert result.json()['sessions'][0]['state'] == 'failed'
            assert not worker._sessions
    asyncio.run(exercise())


def test_monitor_api_strict_loopback_and_key():
    switch=SimpleNamespace(calls={})
    for origin in ('https://example.com', 'http://example.com:8000', 'http://192.168.1.50:8000',
                   'http://127.0.0.1:8000/path', 'http://user:secret@127.0.0.1:8000'):
        with pytest.raises(ValueError):
            NativeMonitorWorker(switch,origin,MONITOR_KEY)
    with pytest.raises(ValueError):
        NativeMonitorWorker(switch,'http://127.0.0.1:8000','REPLACE_WITH_SECRET')


def test_immediate_media_revocation_does_not_report_stop_failure(monitor_setup):
    client, admin, reviewer, cid = monitor_setup
    sid = client.post('/api/cc/monitor/sessions',headers=admin,json={'call_id':cid}).json()['id']
    client.post(f'/api/cc/monitor/sessions/{sid}/approve',headers=reviewer,json={'confirmation':'APPROVE LIVE LISTEN'})
    media = SimpleNamespace(on_audio=None)
    switch = SimpleNamespace(calls={'native-listen-call-1': SimpleNamespace(
        state='bridged',trunk_established=True,extension='1001',media=media)})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as local:
            worker = NativeMonitorWorker(switch,'http://127.0.0.1:8000',MONITOR_KEY,client=local)
            await worker.poll_once()
            media.on_audio('trunk',bytes([0x01]*160),0)
            assert (await local.post(f'/api/cc/monitor/sessions/{sid}/stop',headers=admin)).status_code == 200
            # On next media tick, revocation denies audio before it reaches browser.
            await worker.audio_once()
            assert media.on_audio is None
            assert sid not in worker._sessions
            rows = (await local.get('/api/cc/monitor/sessions',headers=reviewer)).json()['sessions']
            assert rows[0]['state'] == 'ended'  # not stop_failed
    asyncio.run(scenario())


def test_monitor_detaches_on_call_end_without_late_audio(monitor_setup):
    client, admin, reviewer, cid = monitor_setup
    sid = client.post('/api/cc/monitor/sessions',headers=admin,json={'call_id':cid}).json()['id']
    client.post(f'/api/cc/monitor/sessions/{sid}/approve',headers=reviewer,json={'confirmation':'APPROVE LIVE LISTEN'})
    media = SimpleNamespace(on_audio=None)
    switch = SimpleNamespace(calls={'native-listen-call-1': SimpleNamespace(
        state='bridged',trunk_established=True,extension='1001',media=media)})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as local:
            worker = NativeMonitorWorker(switch,'http://127.0.0.1:8000',MONITOR_KEY,client=local)
            await worker.poll_once()
            assert media.on_audio is not None
            switch.calls['native-listen-call-1'].state='ended'
            await worker.poll_once()
            assert sid not in worker._sessions
            assert media.on_audio is None
            assert (await local.get('/api/cc/monitor/sessions',headers=reviewer)).json()['sessions'][0]['state']=='ended'
    asyncio.run(scenario())
