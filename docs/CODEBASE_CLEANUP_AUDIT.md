# QueuePilot v0.7 — Architecture and legacy cleanup audit

Date: 2026-11-11. Scope: source tree, application routes, SIP/media/ACD modules, dashboard, SQLite schema, tests and deployment documentation. **No production Avaya or IP handset was contacted.**

## Decision

**Only QueuePilot owns the contact-center platform.** Avaya Aura is allowed to deliver inbound SIP calls to QueuePilot as an upstream peer; it must not administer QueuePilot ACD queues, agent endpoints, IVR routing, recording, live monitoring or reports. The runtime is a built-in Python SIP/media engine (LAB subset), not a wrapper around another PBX.

## Deleted as obsolete

- `app/aura.py` — AES/WTI administration, Aura agent/VDN mappings, command approvals.
- `bridge/` — external AES/WTI workers, action templates and CTI event forwarding.
- `AURA_DEPLOYMENT.md`, `AURA_MEDIA_DEPLOYMENT.md`, `AVAYA_INTEGRATION.md` — former PBX-dependent architecture.
- `examples/` — old external CTI normalization and event-sending utilities.
- `tests/test_aura.py`, `tests/test_bridge.py` — test code for removed features.
- `docs/INDEPENDENT_CONTACT_CENTER_ARCHITECTURE.md` — superseded interim document referencing Asterisk/older interfaces.
- `telephony/demo_acd.py` and `telephony/sip_probe.py` — redundant historical lab tools; live lab SIP server has OPTIONS handling.
- Generated caches and compiled files (excluded from distribution and Git).

## Refactored and retained

- `app/main.py`, `models.py`, `security.py`, `config.py`, `service.py`: generic `EngineEvent`, internal-key-protected native event/heartbeat routes, no AES endpoints.
- `app/contact_center.py`: independent agent extension lookup, native-only monitor requests, internal media-engine workflow, native-only recording imports, local IVR extension validation, generic audit and report claims. **The media engine is not yet connected to these workflows.**
- `app/db.py`: new installations create `engine_heartbeats` and `audit_log`, not AES-specific tables. On existing databases, historical `aura_audit` records are copied to `audit_log` without modifying the legacy source tables.
- `app/static/`: removed Avaya administration pane; replaced instructions and diagnostic labels with native engine semantics.
- `telephony/local_events.py`: optional in-process native event writer with explicit queue/agent mapping validation and heartbeat. This remains **lab-only**, without WAL replay / backpressure / guaranteed exactly-once event capture.
- `tests/`: validates native event lifecycle, reports, removed HTTP paths, ID rejection, old audit preservation, SIP protocol subsets, ACD and supervisor governance.

## Call-path ownership

```
Caller -> Avaya Aura -> approved SIP trunk -> QueuePilot native SIP listener
                                           -> QueuePilot ACD / agent SIP INVITE
                                           -> registered SIP-capable Avaya IP phone
                                           -> QueuePilot-anchored RTP in both directions
                                           -> optional local SQLite event writer -> dashboard/reports
```

This is a **target topology**. Only local simulated SIP phones and calls were tested. Avaya IP phones using H.323 or proprietary registration cannot be assumed compatible with this SIP-only implementation.

## Risk register (must resolve before production)

| Severity | Finding | Required next work |
|---|---|---|
| Critical | SIP implementation lacks full transaction/dialog retransmission, extension negotiation and session recovery | Standards-compliant SIP state machine, interoperability suite, fuzz testing |
| Critical | RTP lacks TLS/SRTP, jitter controls, DTMF, RTCP and policy-gated recording integration | Hardened embedded media stack with tested consent enforcement |
| Critical | No active IVR execution, native supervisor mixer or call control (hold/transfer/conference) | Runtime IVR/DTMF/monitor modules and multi-party media integration |
| High | ACD and SIP registrations held in memory; failover drops calls | Durable state, recovery design and HA trunk strategy |
| High | Dashboard and SIP runtime agent states not synchronized; local event writes are synchronous and not crash durable | Guaranteed event journal, reconciliation and presence service |
| High | Dashboard uses SQLite and basic bearer tokens with limited roles | SSO/RBAC, PostgreSQL, key rotation, CSRF/XSS review, backups and audit retention |
| High | Actual Avaya phone models/firmware and SIP handoff untested | Interop matrix, trunk routing, codec, DTMF and load testing on isolated staging |
| Medium | Legacy database tables remain on upgraded installations | Backup + explicit operator-approved migration/archive and removal later |

## Upgrade safety

Source deletions do **not** mean customer-data deletion. Historical `aura_*` tables in existing databases are left in place. Do not drop them or old recordings automatically. Use a staged backup/migration with data-integrity checks before any future archival cleanup. `.env`, call recordings and actual SIP phone passwords are excluded from Git.

## Verification

- 31 local tests passed at cleanup review, later tests may increase.
- Python compilation and JavaScript syntax validation required.
- No claim of real Avaya telephone interop, recording playback from native RTP, voice IVR, or production readiness.
