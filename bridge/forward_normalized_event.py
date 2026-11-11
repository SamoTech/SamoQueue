"""Forward canonical CTI bridge events from stdin (JSON lines) to QueuePilot.

Use ONLY after an AES WTI/TSAPI adapter has classified an event correctly.
This does not parse undocumented raw Avaya callback structures.
"""
import json
import os
import sys
import httpx

url=os.environ.get('QUEUEPILOT_URL','').rstrip('/')
key=os.environ.get('AVAYA_INGEST_KEY','')
if not url.startswith('https://') or len(key)<32:
    sys.exit('Requires HTTPS QUEUEPILOT_URL and AVAYA_INGEST_KEY')
with httpx.Client(timeout=10,verify=True) as client:
    for line in sys.stdin:
        if not line.strip():continue
        payload=json.loads(line)
        response=client.post(url+'/api/aura/events',json=payload,headers={'X-Integration-Key':key})
        response.raise_for_status()
        print(json.dumps(response.json()))
