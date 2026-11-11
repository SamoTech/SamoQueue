"""A non-telephony smoke test: python -m telephony.demo_acd"""
from .acd import ACD


def main():
    acd = ACD()
    acd.upsert_agent('agent-101', {'arabic', 'support'}, 'ready')
    acd.upsert_agent('agent-102', {'arabic', 'sales'}, 'ready')
    acd.enqueue('sip-call-1', 'support', {'arabic', 'support'})
    acd.enqueue('sip-call-2', 'sales', {'arabic', 'sales'})
    for offer in acd.dispatch():
        print(f'RING decision: {offer.call_id} -> {offer.agent_id} (media not connected)')
        acd.answer(offer.call_id, offer.agent_id)
        acd.complete(offer.call_id)
    print('ACD state:', acd.snapshot())


if __name__ == '__main__':
    main()
