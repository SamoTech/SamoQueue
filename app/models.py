from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator

class Login(BaseModel):
    username: str
    password: str

class QueueIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    skill: str = Field(default='', max_length=60)
    sla_seconds: int = Field(default=20, ge=1, le=3600)
    target_percent: int = Field(default=80, ge=1, le=100)
    enabled: bool = True

class AgentIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    extension: str = Field(default='', max_length=24)
    queue_id: str | None = None
    status: Literal['available', 'busy', 'acw', 'away', 'offline'] = 'offline'

class AgentStatusIn(BaseModel):
    status: Literal['available', 'busy', 'acw', 'away', 'offline']

class AvayaEvent(BaseModel):
    """Vendor-neutral normalized event supplied by a licensed/authorized PBX bridge."""
    event_id: str = Field(min_length=1, max_length=160)
    source: str = Field(min_length=1, max_length=48)
    type: Literal['call.queued', 'call.answered', 'call.abandoned', 'call.completed', 'agent.status']
    occurred_at: datetime
    call_id: str | None = Field(default=None, max_length=160)
    queue_id: str | None = Field(default=None, max_length=100)
    agent_id: str | None = Field(default=None, max_length=100)
    agent_status: Literal['available', 'busy', 'acw', 'away', 'offline'] | None = None
    caller: str | None = Field(default=None, max_length=120)

    @field_validator('occurred_at')
    @classmethod
    def require_timezone(cls, v):
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError('Timestamp must have a timezone offset')
        return v

    @model_validator(mode='after')
    def validate_kind(self):
        if self.type.startswith('call.') and (not self.call_id or not self.queue_id):
            raise ValueError('Call events require call_id and queue_id')
        if self.type == 'agent.status' and (not self.agent_id or not self.agent_status):
            raise ValueError('Agent events require agent_id and agent_status')
        return self

class ConnectorHeartbeat(BaseModel):
    source: str = Field(min_length=1, max_length=48)
    connected: bool
    details: str = Field(default='', max_length=160)
