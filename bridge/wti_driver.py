"""A configurable AE Services WTI REST transport.

The vendor's endpoint/payload definitions vary by release and are intentionally
not guessed here. Exact operations are loaded from an operator-reviewed WTI
OpenAPI-to-template file. Without it, actions are refused (fail-closed).
"""
import json
import os
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx


class WTIConfigurationError(Exception):
    pass


class WTIUncertain(Exception):
    """A request might have reached AES, but execution cannot be confirmed."""
    pass


def template(value, params):
    if isinstance(value, str):
        for key in ('station','call_id','destination','device'):
            value=value.replace('{'+key+'}', str(params.get(key) or ''))
        if '{' in value or '}' in value:
            raise WTIConfigurationError('Unsupported or unresolved template placeholder')
        return value
    if isinstance(value, dict):
        return {k:template(v,params) for k,v in value.items()}
    if isinstance(value,list):
        return [template(v,params) for v in value]
    return value


def value_at(obj, dotted_path):
    if not isinstance(dotted_path, str) or not dotted_path:
        raise WTIConfigurationError('A documented response field path must be configured')
    try:
        for part in dotted_path.split('.'):
            obj=obj[part]
        return obj
    except (TypeError, KeyError, IndexError):
        return None


class WTIDriver:
    def __init__(self, config_path: str):
        self.config=json.loads(Path(config_path).read_text(encoding='utf-8'))
        self.base=os.environ.get('AES_WTI_URL','').rstrip('/')+'/'
        self.token=os.environ.get('AES_WTI_BEARER_TOKEN','')
        ca=os.environ.get('AES_CA_BUNDLE','')
        if not self.base.startswith('https://') or not self.token:
            raise WTIConfigurationError('Provide HTTPS AES_WTI_URL and AES_WTI_BEARER_TOKEN')
        if not self.config.get('probe') or not self.config.get('operations'):
            raise WTIConfigurationError('Reviewed AES WTI probe and operations are required')
        # Trusted CA by default; no verify=False support.
        self.client=httpx.Client(verify=ca if ca else True,timeout=httpx.Timeout(8.0))

    def close(self):
        self.client.close()

    def _request(self, spec, params):
        path=spec.get('path','')
        if not path.startswith('/api/') or 'REPLACE' in path or '://' in path or '..' in path or '?' in path:
            raise WTIConfigurationError('Operation path must be an explicit same-origin /api/... endpoint')
        method=spec.get('method')
        if method not in ('GET','POST'):
            raise WTIConfigurationError('Allowed WTI method must be GET or POST')
        target=urljoin(self.base,path.lstrip('/'))
        base=urlparse(self.base)
        target_parts=urlparse(target)
        if (target_parts.scheme,target_parts.netloc)!=(base.scheme,base.netloc):
            raise WTIConfigurationError('WTI request cannot leave configured AES origin')
        body=template(spec.get('body',{}),params)
        r=self.client.request(method,target,json=body if method=='POST' else None,
                              headers={'Authorization':'Bearer '+self.token,'Accept':'application/json'})
        if r.status_code>=500:
            raise WTIUncertain('AES returned server error; command may have been accepted')
        if r.status_code>=400:
            raise ValueError('AES rejected operation with HTTP '+str(r.status_code))
        try:
            payload=r.json()
        except ValueError:
            raise WTIUncertain('No JSON operation response to validate') from None
        success=spec.get('success')
        if not success or 'path' not in success or ('equals' not in success and success.get('present_nonempty') is not True):
            raise WTIConfigurationError('A reviewed WTI response success field/value is required')
        observed=value_at(payload,success['path'])
        if success.get('present_nonempty') is True:
            matched=observed not in (None, '', False)
        elif 'equals' in success:
            matched=observed==success['equals']
        else:
            raise WTIConfigurationError('Success must configure equals or present_nonempty')
        if not matched:
            raise WTIUncertain('AES response did not match the configured success confirmation')
        return payload

    def probe(self):
        """Only true after a configured, actual AES-to-CM CTI/TLink state check.

        A generic HTTP /health endpoint is intentionally disallowed.
        """
        spec=self.config.get('probe')
        if spec.get('purpose')!='cti_tlink_status':
            raise WTIConfigurationError('Probe must explicitly verify AES-to-CM TLink status')
        self._request(spec,{})
        return True

    def execute(self,command):
        spec=self.config.get('operations',{}).get(command['action'])
        if not spec:
            raise WTIConfigurationError('Unsupported or unconfigured WTI action: '+command['action'])
        params={key:command.get(key) for key in ('station','call_id','destination')}
        # Station device-IDs are not interchangeable with plain extension strings.
        # WTI call-control actions should use the reviewed device-ID lookup below.
        if '{device}' in json.dumps(spec):
            lookup=self.config.get('device_lookup')
            if not lookup:
                raise WTIConfigurationError('device_lookup is required when WTI action uses {device}')
            result=self._request(lookup,params)
            params['device']=value_at(result,lookup['device_path'])
            if not params['device']:
                raise WTIConfigurationError('Device ID missing from AES lookup')
        self._request(spec,params)
        return 'WTI confirmation matched configured operation response'
