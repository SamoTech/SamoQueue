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
        if data.get('exp', 0) <= time.time() or data.get('sub') not in (config.admin_user,config.reviewer_user):
            raise ValueError('Expired or unknown user')
        return data['sub']
    except (ValueError, UnicodeError, KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail='Invalid or expired session') from None


def require_admin(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(status_code=401, detail='Login required')
    user = decode_token(authorization.removeprefix('Bearer ').strip())
    if user != config.admin_user:
        raise HTTPException(403, 'Administrator role required')
    return user


def require_supervisor(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401, 'Login required')
    return decode_token(authorization.removeprefix('Bearer ').strip())


def require_reviewer(authorization: str | None = Header(default=None)):
    user = require_supervisor(authorization)
    if not config.reviewer_password or user != config.reviewer_user:
        raise HTTPException(403, 'Separate reviewer role required')
    return user


def require_engine_event(x_engine_key: str | None = Header(default=None)):
    if (not x_engine_key or len(config.engine_event_key) < 32
            or not hmac.compare_digest(x_engine_key, config.engine_event_key)):
        raise HTTPException(status_code=401, detail='Invalid native engine credential')
    return True

