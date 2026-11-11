import pytest

from telephony.acd import ACD
from telephony.native_sip import (IngressRoute, IndependentCallPlan, PhoneContact,
                                  PhoneDirectory, RoutingError, SipIngressPlan)
from telephony.sip_probe import sip_probe_response


class Clock:
    def __init__(self): self.now = 1000.0
    def __call__(self): return self.now


def test_authorized_avaya_did_routed_to_queue():
    plan = SipIngressPlan(['10.22.3.1/32'], [IngressRoute('6000', 'support', frozenset({'en'}))])
    assert plan.admit('10.22.3.1', '6000').queue == 'support'
    with pytest.raises(PermissionError): plan.admit('10.22.3.9', '6000')
    with pytest.raises(RoutingError): plan.admit('10.22.3.1', '6001')


def test_duplicate_did_rejected():
    with pytest.raises(RoutingError):
        SipIngressPlan(['10.0.0.1/32'], [IngressRoute('100', 'q'), IngressRoute('100', 'other')])


def test_phone_contact_validation_and_ipv6_uri():
    assert PhoneContact('1001', '192.168.100.25').sip_uri == 'sip:1001@192.168.100.25:5060;transport=udp'
    assert PhoneContact('1002', 'fd00::25').sip_uri == 'sip:1002@[fd00::25]:5060;transport=udp'
    with pytest.raises(RoutingError): PhoneContact('a100', '192.168.100.25')
    with pytest.raises(RoutingError): PhoneContact('1001', 'not-a-literal-ip')
    with pytest.raises(RoutingError): PhoneContact('1001', '192.168.100.25', port=0)


def test_unverified_or_out_of_vlan_phone_is_rejected():
    clock = Clock(); directory = PhoneDirectory(['192.168.100.0/24'], clock)
    contact = PhoneContact('1001', '192.168.100.25', expires_at=1100)
    with pytest.raises(PermissionError): directory.provision(contact, verified=False)
    with pytest.raises(RoutingError):
        directory.provision(PhoneContact('1002', '192.168.200.25', expires_at=1100), verified=True)
    assert directory.lookup('1001') is None


def test_expired_phone_is_not_dialed():
    clock=Clock(); directory=PhoneDirectory(['10.4.0.0/16'], clock)
    directory.provision(PhoneContact('101', '10.4.1.1', expires_at=1003), verified=True)
    assert directory.lookup('101') is not None
    clock.now = 1003
    assert directory.lookup('101') is None


def test_acd_creates_intent_only_for_authenticated_local_phone():
    clock=Clock(); acd=ACD(clock=clock)
    directory=PhoneDirectory(['10.4.5.0/24'], clock)
    directory.provision(PhoneContact('101', '10.4.5.10', expires_at=1100), verified=True)
    router=SipIngressPlan(['10.1.1.9/32'], [IngressRoute('7000', 'support', frozenset({'support'}))])
    plan=IndependentCallPlan(router, directory, acd)
    acd.upsert_agent('agent-1', {'support'}, 'ready'); plan.bind_agent('agent-1', '101')
    plan.incoming('call-A', '10.1.1.9', '7000')
    intents=plan.plan_offers()
    assert len(intents) == 1
    assert intents[0].destination == 'sip:101@10.4.5.10:5060;transport=udp'
    assert acd.calls['call-A'].state == 'offering'  # NOT answered or bridged


def test_unregistered_agent_is_never_selected_and_stays_ready():
    clock=Clock(); acd=ACD(clock=clock)
    directory=PhoneDirectory(['10.4.5.0/24'], clock)
    router=SipIngressPlan(['10.1.1.9/32'], [IngressRoute('7000','support')])
    plan=IndependentCallPlan(router, directory, acd)
    acd.upsert_agent('agent-1', set(), 'ready'); plan.bind_agent('agent-1','101')
    plan.incoming('call-A', '10.1.1.9','7000')
    assert plan.plan_offers() == []
    assert acd.agents['agent-1'].state == 'ready'
    assert acd.calls['call-A'].state == 'waiting'


SIP_OPTIONS = (b'OPTIONS sip:6000@127.0.0.1 SIP/2.0\r\n'
               b'Via: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bK77\r\n'
               b'From: <sip:probe@127.0.0.1>;tag=abc\r\n'
               b'To: <sip:6000@127.0.0.1>\r\n'
               b'Call-ID: test-123\r\n'
               b'CSeq: 1 OPTIONS\r\n'
               b'Content-Length: 0\r\n\r\n')


def test_probe_answers_options_from_allowed_peer():
    res=sip_probe_response(SIP_OPTIONS, '127.0.0.1')
    assert b'SIP/2.0 200 OK' in res
    assert b'Call-ID: test-123' in res
    assert res.endswith(b'Content-Length: 0\r\n\r\n')


def test_probe_does_not_accept_invite_while_media_missing():
    req=SIP_OPTIONS.replace(b'OPTIONS sip:', b'INVITE sip:').replace(b'1 OPTIONS',b'1 INVITE')
    assert b'SIP/2.0 503 Service Unavailable' in sip_probe_response(req,'127.0.0.1')


def test_probe_rejects_untrusted_and_oversized_requests():
    assert sip_probe_response(SIP_OPTIONS, '10.1.1.1') is None
    assert sip_probe_response(SIP_OPTIONS + b'X'*5000,'127.0.0.1') is None


def test_probe_rejects_header_injection_duplicate_and_cseq_mismatch():
    assert sip_probe_response(SIP_OPTIONS.replace(b'Call-ID: test-123', b'Call-ID: test-123\r\nCall-ID: dup'),'127.0.0.1') is None
    assert sip_probe_response(SIP_OPTIONS.replace(b'1 OPTIONS',b'1 INVITE'),'127.0.0.1') is None
    assert sip_probe_response(SIP_OPTIONS.replace(b'From:',b' From:'),'127.0.0.1') is None
