# QueuePilot v0.6 — native SIP call center (no external PBX/media engine)

## Corrected requirement

**Avaya Aura / Communication Manager is an inbound call handoff source only.** It must not control QueuePilot queues, IVR, agents, recording, live listening, or reports. QueuePilot will route each call to a **SIP-capable IP phone directly on the voice LAN**. There is **no Asterisk, FreeSWITCH, external ACD, or external recording/monitoring server**.

An **embedded SIP B2BUA, registrar/location service, and RTP media engine** must be implemented as components of QueuePilot. These are functions built into QueuePilot, not middleware products. Third-party protocol libraries may be linked directly if license and security allow; they are not separate running PBXs.

```
 PSTN/Caller
     |
 [Avaya Aura CM + optional Session Manager]
     |  approved SIP handoff (INVITE + SDP + RTP)
     v
 [ QueuePilot Native SIP + RTP Core ]
     |  ├── trusted trunk authentication & SIP transaction/dialog state
     |  ├── IVR prompts, DTMF and menus
     |  ├── ACD/skills, queue, hold music, ring offers, timeout/retry
     |  ├── REGISTER/authentication + local endpoint location service
     |  ├── outbound agent INVITE, answers, cancellations, BYE, transfer
     |  ├── anchored, mixed/relayed RTP, codecs and recording
     |  ├── authorized supervisor monitor/whisper/barge and audit
     |  └── event journal, reports, HA & administration
     |
     +── SIP/RTP ──> IP phone 1001 (LAN / VLAN)
     +── SIP/RTP ──> IP phone 1002 (LAN / VLAN)
     +── SIP/RTP ──> IP phone 1003 (LAN / VLAN)
```

### Important: does the IP phone actually speak SIP?

An **IP phone is not automatically a SIP phone**. If it uses Avaya H.323 signaling or a vendor-proprietary station protocol and can only register to CM, QueuePilot cannot originate a standards-based SIP call to it independently. For genuine independence the endpoint must support **generic SIP user-agent mode**, with QueuePilot as its SIP registrar or with permitted direct SIP URI dialing. Compatibility varies by model and firmware. Some Avaya SIP firmware expects Avaya-proprietary PPM/provisioning; do not promise it works with a generic registrar until tested.

If a telephone remains registered only to Avaya, forwarding via Avaya to that extension makes **Avaya the agent-call controller again**, contradicting this requirement. That topology must not be misrepresented as independent.

The physical LAN/VLAN may be the same. SIP traffic still requires agreed transports, trusted source IPs, codecs, RTP port ranges, time synchronization, and firewall rules. Same-subnet location is not a substitute for authorization.

### Avaya handoff condition

- Provision a designated Avaya SIP trunk/dial rule to QueuePilot's SIP listener and its address/port (normally through Aura Session Manager if deployed; exact route depends on your CM release and architecture).
- Pass called number (DID / VDN target) and calling number as specified; QueuePilot maps the called number to its own IVR/queue.
- Confirm negotiated codecs (first lab target G.711 A-law / mu-law), DTMF RFC 4733 or supported SIP INFO, and early media/ringback expectations.
- Design what happens when QueuePilot is down: return a defined SIP error, fail over, or fallback route with agreement from the telephony administrator.
- Avaya remains a necessary upstream network hop **only before SIP handoff**. QueuePilot owns all call logic after the handoff.

### Real-time audio is not optional

Signaling alone will NOT record or let the supervisor listen. QueuePilot must anchor RTP between the Avaya trunk and phone: receive, timestamp/jitter-buffer, optionally transcode, generate hold/IVR media, bridge channels, write recordings, and mix supervisor audio under authorization. A SIP `302` redirect/`REFER` or a direct Avaya-to-phone RTP bypass cannot satisfy QueuePilot-managed recording and monitoring.

### Capability implementation and status (v0.6 lab milestone)

| System component | Status | Production dependency |
|---|---|---|
| ACD skill/priority selection | Tested in-memory model | Durable calls and scheduler |
| Trusted Avaya peer → DID → queue decision | Implemented in `telephony/native_sip.py` | Authenticate real SIP transport/session |
| Approved phone → independent SIP dial intent | Implemented in-memory | Persistence, TLS, heartbeat and re-registration |
| Native UDP SIP OPTIONS probe | Implemented **LAB ONLY** | Do not put live inbound calls on this listener |
| SIP INVITE, ACK, BYE, CANCEL two-leg signaling | **Lab flow implemented** (`telephony/native_switch.py`) | Full RFC 3261 transactions, retransmissions and reliability |
| Phone REGISTER and authentication | **Lab SHA-256 Digest and expiry implemented** | TLS and physical Avaya phone interoperability |
| RTP bridging / G.711 media anchor | **Two-way lab relay implemented** | SRTP, RTCP, jitter handling, DTMF and IVR prompts |
| Per-leg diagnostic WAV / supervisor live listening | **Lab WAV optional (off); live listening NOT connected** | Consent policy, authorized supervisor mixer and playback integration |
| Reporting | Existing dashboard/exports prototype | Events from native SIP/RTP sessions, production DB |
| High availability | **Not implemented** | Node failover, replication, redundant trunk route |

### Operational target beyond MVP

1. Native SIP transaction stack: UDP/TCP/TLS, UAS/UAC, REGISTER, INVITE, CANCEL, ACK, BYE, REFER/re-INVITE/UPDATE as applicable, dialogs, session timers, transaction retransmissions, 100rel when required, SDP offer/answer, call IDs, anti-replay and abuse defense.
2. SIP phone onboarding: extension, per-device authentication, endpoint provisioning, authentication of every REGISTER, expiration and reachability; authorized route to that endpoint.
3. Media engine: RTP/RTCP, G.711 A-law / mu-law, RFC 4733 DTMF, packet loss/jitter, hold, MOH, prompts, recording and mixing, secure storage, SRTP/TLS when endpoints and trunk support them.
4. ACD/controller: add/release call legs, originate outbound agent INVITE, handle timeout/cancel/reject, bridge after 200 OK/ACK and verified media, keep queue/agent state consistent.
5. IVR: editable flow editor **plus runtime execution**, prompts/TTS where licensed, DTMF, business hours, multi-level menu, queue selection.
6. Supervisory capabilities: authorized live listen, whisper and barge with visible policies, audited access, multi-party mixer.
7. Reports: own-source event stream + interaction CDRs + recording metadata + agent states, with CSV, XLSX, PDF and dashboards; authoritative only when SIP/RTP events are in production.
8. Operations: PostgreSQL, retention, disk quotas, time sync, backups, HA, scaling, disaster recovery, RBAC, audit log, load/security and interoperability test matrix.

### Lab milestone / next acceptance

- A standards-compliant softphone/deskphone successfully authenticates/registers to QueuePilot itself.
- A simulated SIP trunk sends an INVITE to QueuePilot (without Avaya), and then Avaya test SIP trunk delivers an equivalent call.
- QueuePilot sends an outbound INVITE to one registered SIP phone; agent answers; QueuePilot bridges **two-way RTP** through its own media engine.
- Caller IVR DTMF selects a queue, call recording is playable in dashboard, observer hears audio only after supervisor authorization, and events reconcile in reports.
- No Asterisk/FreeSWITCH installation/service exists in the deployment.
- Failing the phone/QueuePilot/Avaya link does not corrupt state or leave untracked calls.

### Older lab-only OPTIONS probe

```
python -m telephony.sip_probe
```

Listens on **127.0.0.1:15060/UDP** for local SIP `OPTIONS`, returns `200` on valid OPTIONS and **503 on INVITE** to prevent accidental calls. This is deliberately **NOT** a functioning SIP switch and must **not** be registered as a production Avaya SIP entity. No actual calls/media occur. Only expose a non-loopback bind in an isolated lab with trusted peers.

### Compatibility inventory needed before live development

Collect **IP phone make/model, firmware and SIP mode**, how those phones currently register (Avaya CM/SM, another registrar or standalone), **Avaya CM and Session Manager versions**, existing SIP trunk capability and expected concurrent calls. No credentials required for planning.

### Relevant standards

- SIP: RFC 3261, https://www.rfc-editor.org/rfc/rfc3261
- SIP B2BUA: RFC 7092, https://www.rfc-editor.org/rfc/rfc7092
- SDP offer/answer: RFC 3264, https://www.rfc-editor.org/rfc/rfc3264
- RTP: RFC 3550, https://www.rfc-editor.org/rfc/rfc3550
- DTMF: RFC 4733, https://www.rfc-editor.org/rfc/rfc4733
- SIP digest: RFC 8760, https://www.rfc-editor.org/rfc/rfc8760
- Avaya Aura SM SIP entity links: https://documentation.avaya.com/en-us/home/bundle/session-manager/avayasessionmanageradministering_10-3-x/session-manager-routing/entity-links.html
