"""Example of adapting one *hypothetical* upstream CTI JSON event.

The input schema is intentionally illustrative, NOT an official Avaya schema.
Customize this mapping with the event fields documented by your Avaya product/SDK.
"""
from datetime import datetime, timezone
import uuid

AVAYA_QUEUE_MAP = {'6001': 'q_REPLACE_WITH_REAL_QUEUE_ID'}
AVAYA_AGENT_MAP = {'2101': 'a_REPLACE_WITH_REAL_AGENT_ID'}
AVAYA_EVENT_MAP = {'queued': 'call.queued', 'answered': 'call.answered',
                   'abandoned': 'call.abandoned', 'released': 'call.completed'}

def normalize(raw: dict, source='avaya-aura-wti') -> dict:
    event_type = AVAYA_EVENT_MAP[raw['state']]
    return {'source': source, 'event_id': str(raw.get('event_sequence') or uuid.uuid4()),
            'type': event_type, 'call_id': str(raw['call_reference']),
            'queue_id': AVAYA_QUEUE_MAP[str(raw['skill'])],
            'agent_id': AVAYA_AGENT_MAP.get(str(raw.get('agent_extension',''))),
            'caller': raw.get('calling_number'),
            'occurred_at': raw.get('event_time') or datetime.now(timezone.utc).isoformat()}
