# QueuePilot independent contact-center architecture — v0.4 foundation

## Decision (11 November 2026)

**Supersedes the Aura-managed ACD model** in `AURA_DEPLOYMENT.md`, `AVAYA_INTEGRATION.md` and the original v0.2/v0.3 dashboard explanations. These remain for reference only; do **not** treat Avaya AES/WTI/DMCC/CMS as required dependencies for the new target system.

**Avaya Aura / Communication Manager handles only inbound/outbound network interconnect**, and forwards selected calls to QueuePilot over a standard SIP trunk. From the moment a call arrives in QueuePilot, it owns **IVR, ACD, agent sessions, media bridging, recording, supervisor listening, call events and reporting**. QueuePilot must continue operating in lab tests with no Avaya components at all (using a local SIP test trunk).

This is operational independence, not the claim that Avaya is physically absent from upstream calls. If Avaya is the upstream PSTN gateway, its outage can still prevent delivery of **new** calls. Design an alternate ingress/failover path if that single point of failure is unacceptable.

## Reference topology

```text
PSTN / carrier -> Avaya Aura CM -> Avaya Session Manager -> approved SIP trunk -> SBC
                                                                       |
                                                                       v
                                             QueuePilot voice edge (Asterisk/PJSIP)
                                               | SIP signaling / RTP-SRTP media
                                 +-------------+---------------------+
                                 |             |                     |
                          IVR/menu/media   call bridges          media recorder
                                 |             |                     |
                          ACD controller      agent WebRTC softphone / SIP phone
                                 |             |                     |
                                 +-------- QueuePilot API -----------+
                                                  |
                            PostgreSQL event/CDR store + recording object storage
                                                  |
                                    Admin, Agent, Supervisor web apps
                                                  |
                                   Reporting / CSV / XLSX / BI exports
```

Asterisk ARI exposes the call and bridge primitives to the QueuePilot application. Asterisk PJSIP supports the SIP trunk and agents' SIP/WebRTC endpoints. Asterisk `Stasis(queuepilot,...)` hands inbound channels to the QueuePilot controller; if there is no controller, the dialplan must fail safely to a configured announcement or alternative destination instead of silently trapping calls. Do not deploy the example dialplan to an active production trunk.

## Functional ownership

| Function | Owned by | Integrations |
|---|---|---|
| Receive public telephone numbers / route into platform | Avaya initially | SIP signaling and media to QueuePilot |
| IVR, DTMF, language/department selection | QueuePilot | Asterisk media engine + IVR workflow |
| Queues, skills, priorities, SLA, overflow, retries | QueuePilot | ACD state machine + durable scheduler |
| Agent identity, shifts, ready/AUX/ACW | QueuePilot | Agent app + real-time presence |
| Browser softphone, SIP device, transfer, hold, conference | QueuePilot | SIP over WSS, WebRTC DTLS-SRTP, ARI |
| Supervisor listen / whisper / barge | QueuePilot | Permissioned media injection/tap and audit |
| Automatic recording | QueuePilot | Locally captured media; record notice, pause/redact |
| CDR, QoS, SLA, historical reports, recordings search | QueuePilot | Own event ledger and report projections |
| Optional outbound calling | QueuePilot | Outbound SIP trunk to Avaya or telecom provider |

**Critical media rule:** Do not let RTP media bypass QueuePilot when recordings or monitoring are required. Asterisk bridge `proxy_media`/`dtmf_events` settings and SIP `direct_media=no` may be needed, subject to lab verification; the exact deployment needs QoS and capacity testing. Recording and listening require successful media routing, not simply call metadata.

## First deployed ingress (Avaya side)

For Aura installations using Session Manager, administer an authorized third-party **SIP Entity**, Entity Link, Routing Policy and Dial Pattern in System Manager, with suitable Communication Manager SIP trunk/signaling group, numbering/VDN routing and codec/media negotiation. Actual configuration depends on the customer's CM/SM software releases, existing SBC and licensed SIP/trunk capacity.

**Do not assume** AES, DMCC or CMS licenses are necessary to operate the new ACD. The standard SIP trunk and its licensed capacity are the key Avaya dependencies. No AES control to distribute/record QueuePilot calls is intended.

Interoperability commissioning checklist:
1. Call from a test PSTN/internal number to a dedicated test DID/VDN, route to QueuePilot SIP ingress.
2. Verify INVITE/200/ACK/BYE, caller ID, called number and Diversion/P-Asserted-Identity treatment.
3. Verify bidirectional audio (RTP/SRTP), G.711 A-law/u-law, DTMF RFC 4733, early media, hold and transfer behavior.
4. Confirm connection/load limits, codec/transcoding cost, packet loss, jitter, NAT/firewall, TLS trust, certificate renewal, SBC topology and clock sync.
5. Verify failed QueuePilot routes return a safe call treatment, and test drain/maintenance mode.

## Technical stack decision

- **SIP and media server:** Asterisk 22 LTS baseline for first production qualification (or 24 LTS following compatibility testing). SIP PJSIP, ARI, media bridges, recording; restrict management to private addresses. FreeSWITCH ESL could be substituted behind a separate adapter, not run two media controllers on the same call.
- **Backend:** retain Python FastAPI for API, user management and dashboard. Introduce a separate stateful telephony-controller worker for ARI events and commands.
- **Database:** PostgreSQL with schema migrations, durable event ledger and idempotency; SQLite only for local demonstration.
- **Work distribution:** Redis/stream or PostgreSQL transactional queue with exactly-one active controller per media channel, fencing on failover, reconciliation of orphaned channels.
- **Agent UI:** browser WebRTC softphone with WSS and DTLS-SRTP (and TURN where needed); SIP phones remain an option. Web UI alone is not a telephone.
- **Recordings:** encrypted object storage, per-tenant access controls, retention/legal hold, playback audit, consent/notice policies, pause/resume for payment data.
- **Analytics:** event-sourced call lifecycle with dashboard snapshots, pre-aggregations, CSV and Excel output, dashboard filtering and eventually scheduled reports.

## Independent call lifecycle

```text
SIP inbound -> call accepted -> IVR -> queue selection -> waiting/MOH
-> ACD selects eligible READY agent -> ring/offer (timeout/retry)
-> agent answers -> QueuePilot media bridge -> optional recording
-> supervisor access (audit) -> transfer/hold/consult if needed
-> hangup -> ACW -> queue/agent/CDR metrics finalization
```

The ACD scheduler is implemented in `telephony/acd.py` and unit tested. It emits deterministic `Offer` decisions. It **does not** send SIP messages, control Asterisk, generate recording audio, handle conferences, or persist state. It is **lab-only** until wired to the ARI worker, durable DB and call/agent events.

## Recording + supervisor functions

- Automatically start recording from the **QueuePilot media bridge** where allowed, and stop/close on call hangup. Link all recordings to a QueuePilot call ID; verify duration/media completeness and retention.
- For supervisor silent listen / whisper / barge, require scoped supervisor permissions, separate live-media authorization and detailed audit; configure explicit local notice/consent rules. Do not support covert recording or monitoring outside authorized organizational policies.
- Authorized supervisors see *live call state* and use an opt-in approved monitor session; the WebRTC stream must originate at QueuePilot's media server and end when the call/session ends. Token expiry and disconnect revocation must terminate media access.

## Reports to implement from QueuePilot events (not from Avaya CMS)

1. Inbound/outbound/all calls: CDR, ANI/DNIS, queue, agent, disposition, timestamp, hold/transfer, duration, ring and talk time.
2. Queue performance: offered, answered, abandoned, overflow, callback, wait and speed of answer, SLA by thresholds.
3. Agent performance: login/logout, ready/AUX/ACW, talk/hold/ring/idle, adherence, offered/answered/missed calls, occupancy.
4. IVR performance: node visits, selections, drop-offs, self-service completion, menu errors, containment.
5. Quality: recording availability, evaluations, disposition, compliance flags, playback access audit.
6. Supervisor: live and historical monitoring sessions, approvals, role-based activity audit.
7. Operational: concurrent calls/channels, failed SIP invites, audio anomalies, trunk reachability, storage capacity.
8. Exports: CSV and XLSX initially, scheduled PDF and BI integration after a reporting store and document rendering pipeline exist.

**Metric contracts must be formalized**: e.g., `service_level = eligible calls answered within X seconds / eligible offered calls` with explicit abandon and short-abandon treatment. Store source events, timezone and threshold versions so reports reconcile.

## Development phases

- **Phase A — done in v0.4 foundation:** architecture decision, standalone ACD decision engine + unit tests, sample SIP route and CLI simulation. No real VoIP calls yet.
- **Phase B — next:** deploy Asterisk in isolated lab; implement ARI WebSocket controller, call/agent channel reconciliation, SIP trunk to local test caller, agent SIP/WebRTC registration, media bridge, single incoming call.
- **Phase C:** IVR flow engine with DTMF/audio prompts, queue distribution/overflow, transfers, hold/MOH, ACW, agent presence.
- **Phase D:** local recording pipeline, supervisor listen/whisper/barge controlled at media server, role-based security/consent, storage lifecycle.
- **Phase E:** complete PostgreSQL event schema, KPI/report warehouse, historical exports, performance and HA testing.
- **Phase F:** controlled Avaya SM SIP-trunk handoff in nonproduction; security, capacity, failure handling, then production acceptance.

## Acceptance criteria for true Avaya-independent operation

1. The same QueuePilot queue receives a test call via a non-Avaya SIP trunk and via the Avaya test trunk.
2. Disabling AES/WTI/DMCC/CMS does not affect any QueuePilot feature after call delivery.
3. IVR routes the call correctly and selects an agent in QueuePilot, with verified two-way media.
4. Recording and live listening are served entirely from QueuePilot's media server.
5. All QueuePilot statistics can be rebuilt from QueuePilot's own call events.
6. All external control failures have bounded retries and reconcile channels without double connecting calls.

## Reference documentation

- Avaya Aura Session Manager: https://documentation.avaya.com/en-us/home/bundle/session-manager/maintainingavayaaurasessionmanager_r10-2-x/session-manager-overview.html
- Avaya CM SIP trunks: https://documentation.avaya.com/en-us/home/bundle/communication-manager/administeringnetworkconnectivityoncm_r10-3-x/sip-trunks.html
- Asterisk ARI: https://docs.asterisk.org/Configuration/Interfaces/Asterisk-REST-Interface-ARI/
- Asterisk PJSIP WebRTC: https://docs.asterisk.org/Configuration/WebRTC/Configuring-Asterisk-for-WebRTC-Clients/
- Asterisk recording API: https://docs.asterisk.org/Configuration/Interfaces/Asterisk-REST-Interface-ARI/Introduction-to-ARI-and-Media-Manipulation/ARI-and-Media-Part-1-Recording/
