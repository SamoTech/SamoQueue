"""QueuePilot -> AES call-control bridge. Never retries an uncertain telephony command.

Run ONLY after reviewing the API paths and success fields from the EXACT
installed AES WTI OpenAPI schema and enabling control deliberately.
"""
import logging
import os
import sys
import time

import httpx

try:
    from .wti_driver import WTIDriver, WTIConfigurationError, WTIUncertain
except ImportError:
    from wti_driver import WTIDriver, WTIConfigurationError, WTIUncertain

log=logging.getLogger('queuepilot-bridge')


def post(client,base,key,path,body):
    name='X-Aura-Command-Key' if path.startswith('/api/aura/bridge/') else 'X-Integration-Key'
    response=client.post(base+path,json=body,headers={name:key[1] if name=='X-Aura-Command-Key' else key[0]})
    response.raise_for_status()
    return response.json()


def work_once(driver,client,base,key):
    # Reporting true means a WTI-authenticated TLink status check succeeded, not
    # merely that AES was reachable or QueuePilot accepted the heartbeat.
    try:
        connected=bool(driver.probe())
    except Exception as exc:
        connected=False
        log.warning('AES CTI probe failed: %s',type(exc).__name__)
    post(client,base,key,'/api/integrations/heartbeat',
         {'source':'aura-aes','connected':connected,'details':'AES WTI verified TLink' if connected else 'TLink not verified'})
    if not connected:
        return False
    claim=post(client,base,key,'/api/aura/bridge/claim',{})
    cmd=claim.get('command')
    if not cmd:return False
    result={'result':'uncertain','code':'NO_RESULT','message':'No confirmed outcome'}
    try:
        driver.execute(cmd)
        result={'result':'succeeded','code':'WTI_CONFIRMED','message':'Configured AES response confirmed'}
    except WTIConfigurationError:
        result={'result':'failed','code':'DRIVER_CONFIGURATION','message':'Unsupported/unconfigured operation'}
    except ValueError:
        result={'result':'failed','code':'AES_REJECTED','message':'AES rejected request'}
    except (httpx.HTTPError,WTIUncertain):
        result={'result':'uncertain','code':'AES_RESULT_UNKNOWN','message':'Do not retry without PBX reconciliation'}
    except Exception:
        log.exception('Unexpected bridge driver exception; outcome uncertain')
        result={'result':'uncertain','code':'DRIVER_EXCEPTION','message':'Investigate PBX before retry'}
    post(client,base,key,'/api/aura/bridge/commands/'+cmd['id']+'/result',result)
    log.info('Command %s recorded as %s',cmd['id'],result['result'])
    return True


def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    base=os.environ.get('QUEUEPILOT_URL','').rstrip('/')
    key=(os.environ.get('AVAYA_INGEST_KEY',''),os.environ.get('AURA_COMMAND_BRIDGE_KEY',''))
    template=os.environ.get('AES_WTI_ACTIONS_FILE','')
    if not base.startswith('https://') or min(len(k) for k in key)<32 or key[0]==key[1] or not template:
        sys.exit('Requires HTTPS QUEUEPILOT_URL, distinct AVAYA_INGEST_KEY and AURA_COMMAND_BRIDGE_KEY, and AES_WTI_ACTIONS_FILE')
    try:
        driver=WTIDriver(template)
    except (WTIConfigurationError,ValueError,OSError) as exc:
        sys.exit('WTI configuration refused: '+str(exc))
    with httpx.Client(verify=True,timeout=10.0) as client:
        try:
            while True:
                try:
                    work_once(driver,client,base,key)
                except httpx.HTTPError as exc:
                    log.warning('QueuePilot API unavailable: %s',type(exc).__name__)
                time.sleep(5)
        except KeyboardInterrupt:
            pass
        finally:
            driver.close()


if __name__=='__main__':
    main()
