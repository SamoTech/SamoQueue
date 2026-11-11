"""QueuePilot's vendor-independent endpoint/ingress decision layer.

This module does NOT implement SIP transactions, a registrar, RTP, recording,
B2BUA dialogs, or a production media path. It represents the trusted inputs
that a future in-process native SIP stack must use. In particular, a phone
being in the same subnet is NOT sufficient authorization to call it.
"""
from __future__ import annotations

import ipaddress
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Literal

from .acd import ACD, Offer

Transport = Literal['udp', 'tcp', 'tls']
_EXTENSION = re.compile(r'^[0-9]{2,12}$')
_DID = re.compile(r'^\+?[0-9]{2,20}$')


class RoutingError(ValueError):
    """The SIP ingress peer, DID, or phone contact cannot be accepted."""


def _network_list(networks: Iterable[str]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    parsed = tuple(ipaddress.ip_network(item, strict=False) for item in networks)
    if not parsed:
        raise RoutingError('At least one allowed network is required')
    return parsed


def _allowed(ip: str, networks: tuple) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(address in net for net in networks)


@dataclass(frozen=True)
class PhoneContact:
    """A phone endpoint authenticated by a future SIP registrar or static provisioner."""

    extension: str
    host: str
    port: int = 5060
    transport: Transport = 'udp'
    expires_at: float = 0.0

    def __post_init__(self):
        if not _EXTENSION.fullmatch(self.extension):
            raise RoutingError('Phone extension must contain 2–12 digits')
        try:
            ipaddress.ip_address(self.host)
        except ValueError as exc:
            raise RoutingError('Phone host must be a literal IP address') from exc
        if not (1 <= self.port <= 65535):
            raise RoutingError('Invalid phone SIP port')
        if self.transport not in ('udp', 'tcp', 'tls'):
            raise RoutingError('Unknown SIP transport')

    @property
    def sip_uri(self) -> str:
        addr = f'[{self.host}]' if ':' in self.host else self.host
        return f'sip:{self.extension}@{addr}:{self.port};transport={self.transport}'


class PhoneDirectory:
    """Lab-memory phone location service; authenticated writers only.

    Verification of SIP Digest credentials and TLS transport is the responsibility
    of the future registrar. A caller must never pass ``verified=True`` based
    solely on an IP packet or on the LAN source address.
    """

    def __init__(self, allowed_phone_networks: Iterable[str], clock: Callable[[], float] = time.time):
        self.networks = _network_list(allowed_phone_networks)
        self.clock = clock
        self.contacts: dict[str, PhoneContact] = {}

    def provision(self, contact: PhoneContact, *, verified: bool) -> None:
        if not verified:
            raise PermissionError('Only authenticated registration or authorized provisioning')
        if not _allowed(contact.host, self.networks):
            raise RoutingError('Phone outside approved voice VLAN ranges')
        if contact.expires_at <= self.clock():
            raise RoutingError('Phone contact expired')
        self.contacts[contact.extension] = contact

    def lookup(self, extension: str) -> PhoneContact | None:
        contact = self.contacts.get(extension)
        if contact and contact.expires_at > self.clock():
            return contact
        if contact:
            self.contacts.pop(extension, None)
        return None

    def revoke(self, extension: str) -> None:
        self.contacts.pop(extension, None)


@dataclass(frozen=True)
class IngressRoute:
    did: str
    queue: str
    skills: frozenset[str] = field(default_factory=frozenset)
    priority: int = 0

    def __post_init__(self):
        if not _DID.fullmatch(self.did):
            raise RoutingError('Invalid dialed-number rule')
        if not self.queue or len(self.queue) > 80:
            raise RoutingError('Invalid target queue')
        if not (-100 <= self.priority <= 100):
            raise RoutingError('Invalid priority')


class SipIngressPlan:
    """Only configured Avaya SIP peers may hand off DID calls.

    The real SIP listener must additionally validate SIP dialog/transaction IDs,
    transport peer authentication, and SDP. These rules are admission policy only.
    """

    def __init__(self, avaya_peer_networks: Iterable[str], routes: Iterable[IngressRoute]):
        self.peers = _network_list(avaya_peer_networks)
        self.routes: dict[str, IngressRoute] = {}
        for route in routes:
            if route.did in self.routes:
                raise RoutingError('Duplicate DID mapping')
            self.routes[route.did] = route

    def admit(self, peer_ip: str, called_number: str) -> IngressRoute:
        if not _allowed(peer_ip, self.peers):
            raise PermissionError('SIP INVITE not from an approved trunk peer')
        try:
            return self.routes[called_number]
        except KeyError as exc:
            raise RoutingError('No local queue matches the called number') from exc


@dataclass(frozen=True)
class OutboundOfferIntent:
    """ACD decision for a future SIP B2BUA, NOT an actual phone call."""

    call_id: str
    agent_id: str
    destination: str
    timeout_at: float


class IndependentCallPlan:
    """Connect QueuePilot-owned ACD decisions to registered phone contacts.

    This class NEVER claims SIP ringing/answer/media/recording succeeded.
    A production SIP controller must actually originate and confirm each leg,
    manage cancellation, and correlate RTP before updating the call state.
    """

    def __init__(self, ingress: SipIngressPlan, directory: PhoneDirectory, acd: ACD):
        self.ingress = ingress
        self.directory = directory
        self.acd = acd
        self.agent_extensions: dict[str, str] = {}

    def bind_agent(self, agent_id: str, extension: str) -> None:
        if not _EXTENSION.fullmatch(extension):
            raise RoutingError('Invalid agent extension')
        self.agent_extensions[agent_id] = extension

    def incoming(self, call_id: str, avaya_ip: str, called_number: str):
        route = self.ingress.admit(avaya_ip, called_number)
        return self.acd.enqueue(call_id, route.queue, route.skills, route.priority)

    def plan_offers(self) -> list[OutboundOfferIntent]:
        # Fail closed: never select an agent without an approved, unexpired
        # phone contact. Do not mutate agent availability to filter offers.
        def registered(agent) -> bool:
            extension = self.agent_extensions.get(agent.id)
            return bool(extension and self.directory.lookup(extension))

        offers: list[Offer] = self.acd.dispatch(eligible_agent=registered)
        intents: list[OutboundOfferIntent] = []
        for offer in offers:
            contact = self.directory.lookup(self.agent_extensions[offer.agent_id])
            if contact is None:
                # A contact may expire between selection and use.
                self.acd.reject(offer.call_id, offer.agent_id)
                continue
            intents.append(OutboundOfferIntent(offer.call_id, offer.agent_id,
                                              contact.sip_uri, offer.timeout_at))
        return intents
