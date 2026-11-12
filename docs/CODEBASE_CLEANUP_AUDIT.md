# QueuePilot v1.2 — Codebase and Architecture Audit

Reviewed 12 November 2026 against QueuePilot v1.1 on PR #2. Only the native QueuePilot SIP/RTP + ACD + IVR + management stack is retained. No physical Avaya PBX, handset, or customer call was tested. This is a laboratory-only development release.

## Confirmed cleanup

- Removed orphan imports of FastAPI Response and Pydantic BaseModel in app/main.py, StreamingResponse in app/contact_center.py, and unused codec decoder re-exports in telephony/rtp_media.py. The dependent test now imports decoders from the canonical stereo_recording module.
- Removed pytest from production runtime requirements; requirements-dev.txt is used by GitHub Actions. Kept httpx because the native supervisor monitoring worker actively uses it.
- Replaced stale API 0.9.0-lab version reporting with a shared 1.2.0-lab version constant.
- Corrected misleading recording, live-listening, and engine-integration notices in the dashboard: single stereo WAV and authorized RTP listening are integrated in the isolated lab with explicit opt-in, not production-certified.
- Added repository hygiene tests for absent legacy modules, matching static HTML IDs, proper development/runtime dependency separation and consistent app version.
- A code/tree scan found no additional unrelated standalone source file safe to delete. FastAPI route handlers and SIP callbacks are dynamically referenced; static grep-only deletion would break actual functionality.
- No runtime data, SQLite tables, credentials, actual recordings, existing audit history or real configuration were deleted.

## Architecture retained

Caller -> Avaya Aura SIP handoff -> QueuePilot native SIP B2BUA -> local IVR/ACD -> registered SIP-capable phone. Audio remains anchored in QueuePilot RTP. Optional single 8 kHz stereo WAV has caller on the left and agent on the right. Authorized monitoring taps RTP but does not create a second recording. Dashboard and telephony are two processes in one project; not third-party PBXs.

## Test evidence

- Baseline: 52 tests passing before cleanup. New local suite: 56 tests passing, Python compilation passing, and JavaScript syntax validation passing.
- Baseline coverage: 80% across Python source; native_switch.py was 64%, so important failure paths remain untested.
- The Dockerfile intentionally packages only the management web service, not a production SIP switch.

## Risks still open — do not use for real customer traffic

1. CRITICAL: incomplete RFC 3261 SIP transaction/retransmission handling, no production TLS/SRTP/RTCP, failover, jitter controls or real Avaya handset interoperability.
2. CRITICAL: ACD and registration state remain in memory; there is no durable distributed recovery after a crash.
3. HIGH: no production SSO/RBAC, advanced throttling, consent/recording governance or security certification.
4. HIGH: the dashboard IVR graph editor does not publish menus to the native JSON IVR runtime; manually edited dashboard agent states do not control SIP presence.
5. HIGH: --local-events uses same-host SQLite, not a replayable production message bus. Reports can diverge after crashes.
6. HIGH: real-world RTP recording duration, network loss and recovery have not been validated. Native one-WAV archival has a 64 MiB size limit; the separate external recording-import API has a 32 MiB limit.
7. Deployment must validate actual Avaya IP phone model, SIP firmware, independent registration support, trunk routing and negotiated codecs.

## Preservation

Source cleanup is not data deletion. Historical tables including aura_audit are kept to protect older records; do not drop them without a backup and an explicitly approved migration. Capture files, passwords, .env and .local.json are excluded from the shipped source archive.

---

## Historical audit — v0.7
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


## v1.3 follow-up

Added lab-only native Dashboard command synchronization and a persisted SQLite event outbox; see [runtime sync guide](NATIVE_RUNTIME_SYNC_LAB.md). The full SIP/RTP production, HA, safe crash recovery and real Avaya handset interoperability risks **remain open**.
