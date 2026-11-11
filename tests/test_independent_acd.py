import pytest
from telephony.acd import ACD

class Clock:
    def __init__(self): self.value = 100
    def __call__(self): return self.value
    def advance(self, seconds): self.value += seconds

def test_skills_and_priority_dispatch():
    clock = Clock(); acd = ACD(clock=clock)
    acd.upsert_agent('ar', {'arabic'}, 'ready')
    acd.upsert_agent('en', {'english'}, 'ready')
    acd.enqueue('c1', 'support', {'arabic'})
    acd.enqueue('c2', 'sales', {'english'}, priority=5)
    offers = acd.dispatch()
    assert [(x.call_id, x.agent_id) for x in offers] == [('c2','en'),('c1','ar')]
    assert acd.dispatch() == []

def test_longest_idle_is_selected():
    clock=Clock(); acd=ACD(clock=clock)
    acd.upsert_agent('a1', {'s'}, 'ready'); clock.advance(5)
    acd.upsert_agent('a2', {'s'}, 'ready')
    acd.enqueue('c1','q',{'s'})
    assert acd.dispatch()[0].agent_id == 'a1'

def test_accept_complete_and_wrapup():
    clock=Clock(); acd=ACD(clock=clock, after_call_work_seconds=12)
    acd.upsert_agent('a',{'s'},'ready'); acd.enqueue('c','q',{'s'})
    acd.dispatch(); acd.answer('c','a'); acd.complete('c')
    assert acd.snapshot()['completed'] == 1
    assert acd.agents['a'].state == 'acw'
    clock.advance(12); acd.tick()
    assert acd.agents['a'].state == 'ready'

def test_timeouts_retry_different_agent():
    clock=Clock(); acd=ACD(clock=clock, offer_seconds=3)
    acd.upsert_agent('a',{'s'},'ready'); acd.upsert_agent('b',{'s'},'ready')
    acd.enqueue('c','q',{'s'}); assert acd.dispatch()[0].agent_id == 'a'
    clock.advance(3)
    assert acd.tick() == ['c']
    assert acd.dispatch()[0].agent_id == 'b'

def test_reject_releases_and_requeues():
    clock=Clock(); acd=ACD(clock=clock)
    acd.upsert_agent('a',{'s'},'ready'); acd.enqueue('c','q',{'s'})
    acd.dispatch(); acd.reject('c','a')
    assert acd.calls['c'].state == 'waiting'
    assert acd.agents['a'].assigned_call is None

def test_abandon_while_offering_releases_agent():
    clock=Clock(); acd=ACD(clock=clock)
    acd.upsert_agent('a',{'s'},'ready'); acd.enqueue('c','q',{'s'})
    acd.dispatch(); acd.abandon('c')
    assert acd.calls['c'].state == 'abandoned'
    assert acd.agents['a'].state == 'ready'
    assert acd.dispatch() == []

def test_duplicate_enqueue_idempotent_and_collision_rejected():
    clock=Clock(); acd=ACD(clock=clock)
    c=acd.enqueue('c','q'); assert acd.enqueue('c','q') is c
    with pytest.raises(ValueError): acd.enqueue('c','different')

def test_invalid_answer_and_state_overrides_rejected():
    clock=Clock(); acd=ACD(clock=clock)
    acd.upsert_agent('a',{'s'},'ready'); acd.enqueue('c','q',{'s'})
    with pytest.raises(ValueError): acd.answer('c','a')
    acd.dispatch()
    with pytest.raises(ValueError): acd.set_agent_state('a','offline')
    clock.advance(25)
    with pytest.raises(ValueError): acd.answer('c','a')

def test_agent_without_skill_not_offered():
    clock=Clock(); acd=ACD(clock=clock)
    acd.upsert_agent('a',{'billing'},'ready')
    acd.enqueue('c','sales',{'sales'})
    assert acd.dispatch() == []
