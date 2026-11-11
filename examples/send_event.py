"""Send a sample normalized call event to the secure ingestion API.

Usage:
  AVAYA_INGEST_KEY=<key> python examples/send_event.py

This is a bridge connectivity test, not a direct Avaya PBX connector.
"""
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

base = os.getenv('QUEUEPILOT_URL', 'http://127.0.0.1:8000')
key = os.getenv('AVAYA_INGEST_KEY')
queue_id = os.getenv('QUEUEPILOT_QUEUE_ID')
if not key or not queue_id:
    sys.exit('Set AVAYA_INGEST_KEY and QUEUEPILOT_QUEUE_ID to an existing queue ID')
event = {'source': 'avaya-bridge', 'event_id': str(uuid.uuid4()), 'type': 'call.queued',
         'call_id': 'test-' + uuid.uuid4().hex[:10], 'queue_id': queue_id,
         'caller': '+12025550177', 'occurred_at': dt.datetime.now(dt.timezone.utc).isoformat()}
req = urllib.request.Request(base+'/api/integrations/events',
    data=json.dumps(event).encode(),
    headers={'Content-Type':'application/json','X-Integration-Key':key},method='POST')
try:
    with urllib.request.urlopen(req, timeout=10) as response:
        print(response.status,response.read().decode())
except urllib.error.HTTPError as e:
    print(e.code,e.read().decode())
    raise
