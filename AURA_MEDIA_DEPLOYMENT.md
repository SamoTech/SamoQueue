# QueuePilot v0.3 — Recording, Service Observing, IVR and Reporting

## Implementation status (read before connecting a PBX)

- **Ready locally:** recording import/storage/search, authenticated playback/download with access audit, WAV/MP3/OGG signature and size validation, recorder-upload deduplication, call-ID correlation, separate monitor-request/approval/stop workflow, signed 20-second one-use browser audio tickets, a live 16kHz PCM WebSocket relay, IVR graph authoring/validation/versioning and change proposals, 10 report datasets with CSV/JSON/combined ZIP exports.
- **NOT implemented:** native AES DMCC capture or RTP decoding, in-product telephone recording by itself, Avaya DMCC Service Observing command execution, a working PBX-side listen/stop adapter, Avaya CMS import/reconciliation, VoiceXML media prompts/hosting, automatic Avaya vector/VDN/Experience Portal deployment, recording retention deletion, enterprise SSO/fine-grained RBAC, scalable secure media-gateway cluster, resilient distributed recording backend. Until these integrations are delivered, **there will be no live call audio in QueuePilot**.
- No support claim should be made for phone endpoint types, AES licensing, stereo channel fork, recording consent, AES session management or media codec without an installed-release compatibility test.

## Required Avaya components

1. Communication Manager (CM) release and station types, Call Center Elite vector/VDN/skill inventory.
2. AES release, TSAPI TLink, DMCC service, TSAPI and DMCC entitlements/licenses, CTI service account, TLS certificates, network allowlist.
3. A production recording application or authorized DMCC capture service. DMCC audio arrives as RTP, **not** through QueuePilot's existing WTI command bridge. A deployment-specific recorder must register its approved endpoint, monitor target calls, decode and store RTP, and upload WAV/MP3/OGG through the authenticated import API.
4. For live listening, PBX Service Observing authorization and a DMCC media client that can start/stop the observing station and supply **PCM16 little-endian mono 16,000 Hz** frames into the API. Stop requests must be honored reliably even across network failures. The supervisor and customer notification/consent model must be approved by legal/compliance.
5. For IVR: CM vectoring and announcements for simple DTMF; potentially Avaya Experience Portal/Media Processing Platform, speech/VoiceXML/TTS and certified media for complex prompts and flows.
6. Avaya CMS integration for **authoritative** queue, agent, interval, split/skill and VDN metrics. QueuePilot event-derived data will differ when callbacks are dropped or events are missing; historical CMS reconciliations are required before treating the exports as compliance-grade.

Reference documentation:
- AES DMCC: https://documentation.avaya.com/en-us/home/bundle/application-enablement-services/aesoverviewandspec_r10-3-x/dmcc-service.html
- CM Service Observing: https://documentation.avaya.com/en-us/home/bundle/communication-manager/administeringcommunicationmanager_r10-2-x/managing-group-communications/service-observing-calls.html
- CM CMS reports: https://documentation.avaya.com/en-us/home/bundle/call-center-elite/ccelitefeaturereference_r10-2-x/call-vectoring-features/call-vectoring-interactions/call-vectoring-and-bcms-cms-interactions/call-tracking/using-cms-and-bcms-reports-to-evaluate-call-vector/cms-reports.html

## Local test operation

1. Set a unique admin password, reviewer password, APP_SECRET, AVAYA_INGEST_KEY, RECORDING_BRIDGE_KEY, MONITOR_BRIDGE_KEY and (if applicable) AURA_COMMAND_BRIDGE_KEY. **Never commit `.env` or recorded audio.**
2. Run `pip install -r requirements.txt` and `python setup_demo.py` (first-time lab setup only); start `python run.py` on a private interface behind TLS reverse proxy. Use non-production Avaya/test stations only.
3. Set `RECORDINGS_ENABLED=true` to accept uploads from an external recorder. Set `MONITORING_ENABLED=true` *only after* a dedicated reviewer account and certified DMCC observer are in place.
4. Configure an **AES bridge** to send call.queued/call.answered/call.completed events and a verified heartbeat. Map VDN/skill and station/agent IDs in the Aura panel.
5. Upload from a trusted recorder service, after it has applied the legally required caller notice/consent policy and has set the explicit `consent_attested=true` flag:

```bash
curl -X POST 'https://QUEUEPILOT/api/cc/recordings/import?source=aura-aes&call_id=CALL_FROM_CTI&consent_attested=true&duration_seconds=95' \
  -H "X-Recording-Bridge-Key: $RECORDING_BRIDGE_KEY" \
  -H 'Content-Type: audio/wav' --data-binary @recording.wav
```

6. A supervisor signed in as **admin** requests monitoring for an active answered call. A **different reviewer** logs in to approve (never one-click silent observation). The monitor bridge polls `POST /api/cc/monitor/bridge/claim` with `X-Monitor-Bridge-Key`; it issues a release-specific DMCC Service Observing command and sends the verified result to `POST /api/cc/monitor/bridge/sessions/{id}/result`. Only after `active` may the reviewer request browser audio and press Listen. Monitor PCM uplink uses `POST /api/cc/monitor/bridge/sessions/{id}/audio` with `Content-Type: application/octet-stream`, frames <=64,000 bytes and `X-Monitor-Bridge-Key`.
7. A completed CTI call revokes playback and requests an observer stop. A bridge crash / `uncertain` state needs a manual PBX check. The bridge must impose an independent absolute monitoring duration and stop observing if the API is unavailable.

### Recorder delivery constraints

- Matching CTI events must already exist; incoming `source` and `call_id` must match the call's unique source and external identifier.
- Recording import explicitly rejects uploads without consent/notification attestation; this **does not verify actual consent**.
- Max recording file is 32 MiB per upload. Large multi-hour WAV files require an object-store upload scheme in a future release. File handling checks only basic file signatures (not antivirus/codec sandboxing).
- Files are stored outside the Git repository (`data/recordings` by default) with restricted POSIX file permissions, but are **not encrypted at rest** by QueuePilot. Provision server-side disk encryption, backups and access logging before real customer audio. Do not put your application on the public Internet.
- Protected audio endpoint: `GET /api/cc/recordings/{id}/audio` using the supervisor bearer token. Playback/download actions are audited. The dashboard uses authenticated fetch to create a temporary in-browser blob URL.

### Browser listening limits

- The relay accepts PCM from a trusted on-premises media adapter. It never joins calls or captures RTP on its own.
- Test implementation is **memory-local to a single API worker**. It is NOT production scale and provides no packet sequencing, jitter buffer, Opus/WebRTC encryption/transport beyond TLS/WSS, congestion control, distributed session state, per-supervisor granular permissions, or failover.
- Browser audio starts only after the reviewer intentionally clicks Listen and receives a short-lived one-time ticket. This ticket should never be logged, stored, transmitted over HTTP or shared.
- Before production, replace with a dedicated audited WebRTC media service using TLS/SRTP, policy-based authorization, segmented networks, per-session agent/call scoping, and 24-hour alerting.

## IVR drafting

Design a graph with one `start` node and types: `announcement`, `menu`, `queue`, `transfer`, `hangup`. Every node must be reachable, menus map DTMF keys to target node IDs, and queue/transfer endpoints must already exist in QueuePilot's approved mappings. The UI provides a sample.

`POST /api/cc/ivr` creates a draft. `PUT /api/cc/ivr/{id}` revises drafts. `POST /api/cc/ivr/{id}/propose-deployment` freezes a reviewed draft as **proposed** only. There is no live vector/SMS/Experience Portal deployment action.

## Reports and exports

All are initially **event-derived** (not CMS certified):

| Report | Endpoint `GET /api/cc/reports/NAME` |
|---|---|
| Call detail | `calls` |
| Queue summary | `queues` |
| Agent performance | `agents` |
| 30-minute traffic | `intervals` |
| Queue SLA | `sla` |
| Recording inventory | `recordings` |
| Live-monitoring audit | `monitoring` |
| IVR versions | `ivr` |
| Control and playback audit | `audit` |
| CTI event log | `events` |

`/api/cc/reports/NAME.csv` exports UTF-8 BOM CSV (Excel-compatible); `/api/cc/reports/all.zip` bundles all ten CSVs. Optional `start`/`end` must be ISO 8601 with UTC offset, default 30-day period, up to 366 days. Per report 50,000 rows maximum for bounded operational export. Large data sets need pagination or background batch export in future. CSV cells beginning in potential Excel-formula characters are escaped.

## Operational gap list before production

- **Highest priority:** installed AES/CM/CMS version discovery; validate DMCC and TSAPI licensing; lab PoC for DMCC media capture and playback; verify Service Observing entitlement and approved/legal signaling.
- Implement native recording client or adapt existing enterprise recording platform with SSO-signed media URLs and explicit scoping, transcription/redaction if approved.
- Implement real on-premises DMCC supervisor monitor adapter, duplex quality, stop-on-failure, RTP/PCM transcoding, and authenticated WebRTC bridge.
- Implement CMS historical extraction and reconcile live metrics, including skill-level abandoned threshold, redirects, transfers, AUX/ACW, occupancy, time zones, interval reporting, agent shift adherence, and CDRs.
- IVR compile/review/deploy workflow with vector/SMS or VoiceXML as supported, complete with staging, rollback and two-person change approval.
- Production security: corporate IdP SSO/MFA/RBAC, row-level entitlements, immutable audit logs, network isolation, DLP, key rotation, file encryption, recording retention/erasure/holds, backups and disaster recovery, penetration testing, change-control, and legal notices.
