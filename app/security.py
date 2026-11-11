import base64
import hashlib
import hmac
import json
import secrets
import time
from fastapi import Header, HTTPException
from .config import config


def encode_token(user: str) -> str:
    data = {'sub': user, 'exp': int(time.time()) + 8 * 3600, 'nonce': secrets.token_urlsafe(12)}
    payload = base64.urlsafe_b64encode(json.dumps(data, separators=(',', ':')).encode()).decode().rstrip('=')
    sig = hmac.new(config.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return payload + '.' + sig


def decode_token(token: str) -> str:
    try:
        payload, signature = token.rsplit('.', 1)
        expected = hmac.new(config.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError('Bad signature')
        raw = base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4))
        data = json.loads(raw)
        if data.get('exp', 0) <= time.time() or data.get('sub') != config.admin_user:
            raise ValueError('Expired or unknown user')
        return data['sub']
    except (ValueError, UnicodeError, KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail='Invalid or expired session') from None


def require_admin(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(status_code=401, detail='Login required')
    return decode_token(authorization.removeprefix('Bearer ').strip())


def require_ingest(x_integration_key: str | None = Header(default=None)):
    if not x_integration_key or not hmac.compare_digest(x_integration_key, config.ingest_key):
        raise HTTPException(status_code=401, detail='Invalid integration key')
    return True



def require_aura_control_bridge(x_aura_command_key: str | None = Header(default=None)):
    """Separate credential; event ingestion alone must never grant call control."""
    if (not x_aura_command_key or len(config.aura_command_key)<32
            or not hmac.compare_digest(x_aura_command_key, config.aura_command_key)):
        raise HTTPException(status_code=401, detail='Invalid Avaya command bridge key')
    return True
