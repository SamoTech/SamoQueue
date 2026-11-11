import json
from pathlib import Path

import httpx
import pytest
from bridge.wti_driver import WTIDriver, WTIConfigurationError, WTIUncertain, template, value_at
from bridge import worker


def test_safe_template_and_json_path():
    params={'station':'4101','destination':'4102','call_id':'abc','device':'4101:CM1::0'}
    assert template({'a':'{station}', 'b':['{device}','{call_id}']},params)=={'a':'4101','b':['4101:CM1::0','abc']}
    assert value_at({'Result':{'state':'UP'}},'Result.state')=='UP'
    with pytest.raises(WTIConfigurationError): template('{not_allowed}',params)


def test_driver_refuses_incomplete_configuration(monkeypatch,tmp_path):
    path=tmp_path/'actions.json'
    path.write_text(json.dumps({'probe':{'purpose':'cti_tlink_status'},'operations':{}}))
    monkeypatch.setenv('AES_WTI_URL','https://aes.example.internal')
    monkeypatch.setenv('AES_WTI_BEARER_TOKEN','fake-test-token')
    with pytest.raises(WTIConfigurationError): WTIDriver(str(path))


def test_driver_verified_transport(monkeypatch,tmp_path):
    cfg={'probe':{'purpose':'cti_tlink_status','method':'POST','path':'/api/v1/TLinkState',
                  'body':{},'success':{'path':'result.state','equals':'UP'}},
         'operations':{'call.hold':{'method':'POST','path':'/api/v1/CallHold',
                    'body':{'device':'{station}','call':'{call_id}'},
                    'success':{'path':'result.accepted','equals':True}}}}
    path=tmp_path/'ops.json';path.write_text(json.dumps(cfg))
    monkeypatch.setenv('AES_WTI_URL','https://aes.example.internal')
    monkeypatch.setenv('AES_WTI_BEARER_TOKEN','a-token')
    d=WTIDriver(str(path))
    seen=[]
    def fake_request(method,url,**kw):
        seen.append((method,url,kw))
        body={'result':{'state':'UP'}} if url.endswith('TLinkState') else {'result':{'accepted':True}}
        return httpx.Response(200,json=body,request=httpx.Request(method,url))
    monkeypatch.setattr(d.client,'request',fake_request)
    assert d.probe() is True
    assert 'confirmation' in d.execute({'action':'call.hold','station':'4101','call_id':'abc','destination':None})
    assert seen[1][2]['json']=={'device':'4101','call':'abc'}
    assert seen[1][2]['headers']['Authorization']=='Bearer a-token'
    # A mismatched response is UNKNOWN, never a success.
    def wrong_response(method,url,**kw):
        return httpx.Response(200,json={'result':{'accepted':False}},request=httpx.Request(method,url))
    monkeypatch.setattr(d.client,'request',wrong_response)
    with pytest.raises(WTIUncertain):d.execute({'action':'call.hold','station':'4101','call_id':'abc'})
    d.close()


def test_worker_reports_unknown_without_retry(monkeypatch):
    calls=[]
    class StubDriver:
        def probe(self):return True
        def execute(self,command):
            calls.append(('execute',command['id']))
            raise WTIUncertain('timeout')
    claimed=False
    def fake_post(client,base,key,path,body):
        nonlocal claimed
        calls.append(('api',path,body))
        if path.endswith('/claim'):
            if claimed:return {'command':None}
            claimed=True
            return {'command':{'id':'cmd_test','action':'call.transfer','station':'4101','call_id':'one','destination':'4102'}}
        return {'status':'ok'}
    monkeypatch.setattr(worker,'post',fake_post)
    assert worker.work_once(StubDriver(),None,'https://queuepilot.local','testkey') is True
    assert any(item[0]=='api' and item[1].endswith('/result') and item[2]['result']=='uncertain' for item in calls)
    assert worker.work_once(StubDriver(),None,'https://queuepilot.local','testkey') is False
    assert calls.count(('execute','cmd_test'))==1
