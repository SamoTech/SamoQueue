"""Deterministic ACD decision engine for a QueuePilot-owned call center.

No PBX, SIP, RTP or Avaya SDK dependency. This module emits *decisions*, not calls.
A telephony controller must carry out each offer/bridge/stop operation and reconcile
failures with the state machine. In-memory state is intentionally lab-only; deploy
only with durable state, a single-writer strategy and media reconciliation.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Literal

AgentState = Literal['offline', 'ready', 'aux', 'ringing', 'talking', 'acw']
CallState = Literal['waiting', 'offering', 'talking', 'completed', 'abandoned']


@dataclass
class Agent:
    id: str
    skills: frozenset[str] = field(default_factory=frozenset)
    state: AgentState = 'offline'
    available_since: float = 0.0
    assigned_call: str | None = None
    answered: int = 0
    offered: int = 0
    acw_until: float = 0.0


@dataclass
class Call:
    id: str
    queue: str
    arrived_at: float
    required_skills: frozenset[str] = field(default_factory=frozenset)
    priority: int = 0
    state: CallState = 'waiting'
    agent_id: str | None = None
    offer_deadline: float | None = None
    answered_at: float | None = None
    ended_at: float | None = None
    attempted_agents: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class Offer:
    call_id: str
    queue: str
    agent_id: str
    timeout_at: float


class ACD:
    """Single-writer reference scheduler; all methods are synchronous.

    Queue order is priority descending then earliest arrival. Agents are matched
    using skill-set inclusion and longest-idle ordering. A rejected/timed-out offer
    tries other eligible agents before retrying those already attempted.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 offer_seconds: float = 20, after_call_work_seconds: float = 20):
        if offer_seconds <= 0 or after_call_work_seconds < 0:
            raise ValueError('Invalid timeout')
        self.clock = clock
        self.offer_seconds = offer_seconds
        self.acw_seconds = after_call_work_seconds
        self.agents: dict[str, Agent] = {}
        self.calls: dict[str, Call] = {}

    def upsert_agent(self, agent_id: str, skills: set[str] | frozenset[str],
                     state: AgentState = 'offline') -> Agent:
        if not agent_id:
            raise ValueError('Agent ID required')
        if agent_id in self.agents:
            agent = self.agents[agent_id]
            agent.skills = frozenset(skills)
            # Do not change an assigned agent's state, even on upsert.
            if agent.assigned_call is None:
                self.set_agent_state(agent_id, state)
            return agent
        now = self.clock()
        agent = Agent(id=agent_id, skills=frozenset(skills), state=state,
                      available_since=now if state == 'ready' else 0)
        self.agents[agent_id] = agent
        return agent

    def set_agent_state(self, agent_id: str, state: AgentState) -> None:
        if state not in {'offline', 'ready', 'aux', 'ringing', 'talking', 'acw'}:
            raise ValueError('Unknown agent state')
        agent = self.agents[agent_id]
        if agent.assigned_call is not None:
            raise ValueError('Cannot override an agent with an active call/offer')
        if state in {'ringing', 'talking'}:
            raise ValueError('Media events control ringing/talking transitions')
        agent.state = state
        if state == 'ready':
            agent.available_since = self.clock()
        else:
            agent.available_since = 0
        if state != 'acw':
            agent.acw_until = 0

    def enqueue(self, call_id: str, queue: str,
                required_skills: set[str] | frozenset[str] = frozenset(),
                priority: int = 0) -> Call:
        if not call_id or not queue:
            raise ValueError('Call ID and queue required')
        if call_id in self.calls:
            existing = self.calls[call_id]
            if existing.queue != queue:
                raise ValueError('Call ID collision across queues')
            return existing
        call = Call(call_id, queue, self.clock(), frozenset(required_skills), priority)
        self.calls[call_id] = call
        return call

    def dispatch(self) -> list[Offer]:
        """Return actions for media layer to originate; do not claim ringing success."""
        now = self.clock()
        self.tick()
        offers: list[Offer] = []
        calls = sorted((c for c in self.calls.values() if c.state == 'waiting'),
                       key=lambda c: (-c.priority, c.arrived_at, c.id))
        for call in calls:
            eligible = [a for a in self.agents.values()
                        if a.state == 'ready' and a.assigned_call is None
                        and call.required_skills.issubset(a.skills)]
            if not eligible:
                continue
            fresh = [a for a in eligible if a.id not in call.attempted_agents]
            if not fresh:
                call.attempted_agents.clear()
                fresh = eligible
            agent = min(fresh, key=lambda a: (a.available_since, a.id))
            agent.state = 'ringing'
            agent.assigned_call = call.id
            agent.offered += 1
            call.state = 'offering'
            call.agent_id = agent.id
            call.attempted_agents.add(agent.id)
            call.offer_deadline = now + self.offer_seconds
            offers.append(Offer(call.id, call.queue, agent.id, call.offer_deadline))
        return offers

    def answer(self, call_id: str, agent_id: str) -> None:
        call = self.calls[call_id]
        agent = self.agents[agent_id]
        if call.state != 'offering' or call.agent_id != agent_id or agent.assigned_call != call_id:
            raise ValueError('No active offer for call and agent')
        if call.offer_deadline is None or self.clock() >= call.offer_deadline:
            raise ValueError('Offer expired')
        call.state = 'talking'
        call.answered_at = self.clock()
        call.offer_deadline = None
        agent.state = 'talking'
        agent.answered += 1

    def reject(self, call_id: str, agent_id: str) -> None:
        call = self.calls[call_id]
        if call.state != 'offering' or call.agent_id != agent_id:
            raise ValueError('No matching offer')
        self._release(call, make_ready=True)
        call.state = 'waiting'

    def abandon(self, call_id: str) -> None:
        call = self.calls[call_id]
        if call.state not in {'waiting', 'offering'}:
            raise ValueError('Call is not waiting')
        if call.state == 'offering':
            self._release(call, make_ready=True)
        call.state = 'abandoned'
        call.ended_at = self.clock()

    def complete(self, call_id: str) -> None:
        call = self.calls[call_id]
        if call.state != 'talking':
            raise ValueError('Call not connected')
        agent = self.agents[call.agent_id]  # type: ignore[index]
        call.state = 'completed'
        call.ended_at = self.clock()
        agent.assigned_call = None
        agent.state = 'acw'
        agent.available_since = 0
        agent.acw_until = self.clock() + self.acw_seconds
        call.agent_id = agent.id

    def tick(self) -> list[str]:
        """Expire offers and conclude ACW. Returns IDs of expired offers.

        Caller/agent channels must also be hung up or reconciled by the real media
        adapter; this does not itself send SIP signaling.
        """
        now = self.clock()
        expired = []
        for call in self.calls.values():
            if call.state == 'offering' and call.offer_deadline is not None and now >= call.offer_deadline:
                expired.append(call.id)
                self._release(call, make_ready=True)
                call.state = 'waiting'
        for agent in self.agents.values():
            if agent.state == 'acw' and now >= agent.acw_until:
                agent.state = 'ready'
                agent.available_since = now
                agent.acw_until = 0
        return expired

    def _release(self, call: Call, make_ready: bool) -> None:
        if call.agent_id:
            agent = self.agents[call.agent_id]
            if agent.assigned_call == call.id:
                agent.assigned_call = None
                if make_ready:
                    agent.state = 'ready'
                    agent.available_since = self.clock()
        call.agent_id = None
        call.offer_deadline = None

    def snapshot(self) -> dict:
        return {
            'waiting': sum(1 for c in self.calls.values() if c.state == 'waiting'),
            'ringing': sum(1 for c in self.calls.values() if c.state == 'offering'),
            'talking': sum(1 for c in self.calls.values() if c.state == 'talking'),
            'ready_agents': sum(1 for a in self.agents.values() if a.state == 'ready'),
            'completed': sum(1 for c in self.calls.values() if c.state == 'completed'),
            'abandoned': sum(1 for c in self.calls.values() if c.state == 'abandoned'),
        }
