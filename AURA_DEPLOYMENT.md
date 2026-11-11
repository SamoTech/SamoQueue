# QueuePilot v0.2 — Avaya Aura / Communication Manager Implementation Guide

**Status, 11 November 2026:** Working web app, database, mapped Aura event API, command approval/queue, audit trail, routing proposals, and configurable AES WTI worker are implemented and locally tested. **No real AES/CM connection has been authenticated or tested.** Live WTI endpoints, success fields, and event callback schemas MUST be taken from the API specification of the customer's installed AES release; the sample configuration has intentionally nonworking placeholders. Do not enable control in production without a lab commissioning run.

## 1. Architecture

```
Avaya Communication Manager / Call Center Elite
  |  CTI link (ADJ-IP), configured AES TLink / WTI / TSAPI
Avaya Aura Application Enablement Services (AES)
  |  TLS + vendor WTI REST/WebSocket or licensed TSAPI SDK
AES bridge (under bridge/)
  |  a) POST normalized event /api/aura/events (event key)
  |  b) POST verified session heartbeat /api/integrations/heartbeat (event key)
  |  c) CLAIM/ACK approved actions /api/aura/bridge/* (separate command key)
QueuePilot FastAPI + SQL database
  |  Supervisor dashboard / management UI

CMS -> future reconciled real-time/historical reporting adapter
SMS SOAP -> future authorized VDN/vector/skill provisioning adapter
```

**Authoritative responsibilities:** Avaya CM performs real call routing, ACD, IVR, and vector processing. QueuePilot stores desired local mappings and proposals. It cannot make a VDN, vector, or skill change inside CM without a separately authorized AES SMS or administrative adapter.

## 2. Site prerequisites — collect without sharing passwords

| Item | Required information or administrator action |
| --- | --- |
| CM | Exact release/build; ACD / EAS / Elite features; whether CM is Main/Access vs Feature Server |
| AES | Version/build and FQDN of AES nodes; WTI support on installed release; high availability requirements |
| CM-to-AES | Licensed and enabled Computer Telephony Adjunct Links; configured ADJ-IP CTI link; active switch/TLink |
| Credentials | Dedicated low-privilege AES integration account with permitted monitor/call-control operations; certificate trust |
| Licensing | Sufficient TSAPI Basic/Simultaneous Users for monitored devices and ACD splits; DMCC licenses if media/device access is later needed |
| Inventory | VDN -> vector -> skill mapping; hunt-group numbers; stations/extensions -> agent login IDs; valid transfer destinations |
| History | Whether Avaya CMS is licensed; supervisor access and agreed definitions for abandoned/short abandoned/SLA |
| Networking | DNS, time sync, TLS, firewalls as required by official documentation for installed versions, dedicated VPN/private segments |
| Change control | Nonproduction AES/CM lab, test agents/extensions, maintenance window, rollback and telephony administrator approval |

Avaya WTI is available starting in AES 10.1.2; feature/API coverage varies by release. Earlier AES releases may require a TSAPI/JTAPI/DMCC SDK bridge instead. A single version string is not enough to infer compatibility or licensing. See official references below.

## 3. Avaya-side preparation (performed by authorized PBX administrator)

1. Verify the CM/AES and licensing versions; check that AES supports the chosen integration API and monitored station/device types.
2. Verify or administer the CM-to-AES CTI link with the correct ADJ-IP settings. Follow the Avaya *Administering AE Services* instructions; **do not change production links based solely on this guide**. A working TLink must be shown in AES monitoring.
3. Enable and authorize the appropriate AES WTI or TSAPI application identity. Use least privilege and a certificate chain trusted by the bridge host.
4. Inventory VDNs, vectors, measured skills/hunt groups, station extensions, agent login IDs. Configure monitoring only on explicitly authorized resources.
5. Obtain the **exact WTI REST and WebSocket API reference** for the installed AES version from Avaya DevConnect / official SDK downloads. WTI session creation, bearer refresh, monitors and callbacks must follow that reference.
6. For historical Avaya-grade reporting, plan a dedicated CMS integration with Avaya CMS permissions; do not treat live call event reconstruction as a replacement for CMS.

### Official references

- AES WTI overview, features, and license requirements: https://documentation.avaya.com/en-us/home/bundle/application-enablement-services/aesoverviewandspec_r10-2-x/web-telephony-interface-%28wti%29-service.html
- AES SDK/API downloads (WTI REST and WebSocket OpenAPI, TSAPI, SMS): https://www.avaya.com/en/developer-support/sdks/
- CM-to-AES CTI link prerequisites: https://documentation.avaya.com/en-us/home/bundle/application-enablement-services/administeringaeservices_r10-2-x/administering-communication-manager-for-applicatio/administering-a-cti-link-for-tsapi%2C-jtapi%2C-dmcc-wi.html
- CM vectors/VDNs and routing precautions: https://documentation.avaya.com/en-us/home/bundle/communication-manager/administeringcommunicationmanager_r10-3-x/vectors-and-vdns.html
- AES System Management Service (SMS): https://documentation.avaya.com/en-us/home/bundle/application-enablement-services/aesoverviewandspec_r10-2-x/application-developers/web-services-programmers.html
- Avaya CMS report setup and permissions: https://documentation.avaya.com/en-us/home/bundle/cms/avayacmssupervisorreports_r21-2-x/overview-of-cms-supervisor-reports/performing-prerequisite-system-administration-task.html

## 4. Run QueuePilot safely in demo mode

```bash
cd avaya-queue-platform
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python setup_demo.py  # creates .env, prints randomized initial admin password once
python run.py
```

Open `http://127.0.0.1:8000` **locally only**. Do not expose an HTTP service on the network. For integration use a trusted HTTPS reverse proxy and network ACLs; configure `QUEUEPILOT_URL` using its `https://` URL. `AURA_CONTROL_ENABLED=false` is the default. Demo mode can be disabled separately.

In **Avaya Aura control**, first create the local queues/agents using the Configuration and Agent management pages. Then use **Map** to associate each local queue with the real CM VDN/skill (and optionally vector) and each local agent with the actual station and agent login ID. VDN and skill numbers must be unique, and if both are supplied in a single event they must resolve to one queue.

## 5. Normalized event contract: received from an authorized AES adapter

**This is our own intermediate JSON model, not the raw Avaya WTI callback format.** The WTI callback parser is site/version-specific and must be added after the real callback schema is obtained. `bridge/forward_normalized_event.py` can forward newline-delimited canonical JSON after correct vendor parsing.

```http
POST /api/aura/events
X-Integration-Key: <event key>
Content-Type: application/json
```

```json
{
  "event_id": "cti-mon-17-unique-1804",
  "event_type": "call.queued",
  "occurred_at": "2026-11-11T12:00:00+02:00",
  "call_id": "vendor-call-id-123",
  "vdn": "65001",
  "skill_number": "87",
  "caller": "+201011111234"
}
```

Allowed `event_type`: `call.queued`, `call.answered`, `call.abandoned`, `call.completed`, `agent.status`. The bridge must correlate actual AES connection events to this lifecycle. **Do not equate a generic `CallCleared` or `ConnectionCleared` callback to `call.abandoned` without checking call state.** The API rejects unknown mappings, timestamps without offsets and inconsistent identifiers; caller numbers are masked on ingestion and source event IDs are deduplicated.

For agent status:

```json
{
  "event_id": "agent-state-unique-27",
  "event_type": "agent.status",
  "occurred_at": "2026-11-11T12:02:00+02:00",
  "agent_station": "4101",
  "agent_login": "2024",
  "agent_status": "available"
}
```

Avaya advanced agent-state feed support and licensing may vary; do not assume AUX/ACW is available through every release and basic monitor. Prefer actual Avaya agent-state events over guessing from the call lifecycle.

## 6. Command workflow (implemented)

1. Supervisor creates `POST /api/aura/commands` (or uses the Avaya Aura page). The action is **only a request**, status `pending_approval`.
2. With `AURA_CONTROL_ENABLED=true`, supervisor explicitly approves: `POST /api/aura/commands/{id}/approve` with `{"confirmation":"APPROVE AVAYA ACTION"}`. Status becomes `queued` — **not yet executed**.
3. A healthy AES bridge must send `POST /api/integrations/heartbeat` with `{"source":"aura-aes", "connected":true}` only after checking actual AES-to-CM CTI/TLink status.
4. Privileged bridge sends `POST /api/aura/bridge/claim` with `X-Aura-Command-Key: <distinct command bridge key>`; receives at most one approved command and marks it `leased`.
5. Bridge calls a configured, reviewed AES WTI operation and acknowledges with `POST /api/aura/bridge/commands/{id}/result`, using `succeeded`, `failed`, or `uncertain` and a short code. `succeeded` means **bridge-reported confirmation**, not independent PBX verification.
6. No automatic command replay. Approved commands expire after 120 seconds if unclaimed; claimed commands without an outcome are marked **uncertain** after 180 seconds when cleanup next runs. Reconcile call state with CM before any manual retry.

Supported command request types: `call.make`, `call.answer`, `call.hold`, `call.retrieve`, `call.transfer`, `call.drop`, `agent.ready`, `agent.aux`. **These are API-level capabilities.** Each action becomes executable only when its version-specific AES driver mapping is correctly installed. The example WTI template contains only illustrative make/hold definitions, with nonfunctional placeholders.

Call originate/transfer destination must be 2–8 digits **and** already mapped as an agent station or VDN in QueuePilot. This is a deliberately restrictive allowlist to reduce toll fraud. The assigned source station must be mapped as well.

Separate secrets:

- `AVAYA_INGEST_KEY`: event ingestion and connector heartbeat only.
- `AURA_COMMAND_BRIDGE_KEY`: claim and acknowledge PBX command only; must differ from event key.
- `APP_SECRET` and `APP_ADMIN_PASSWORD`: web supervisor session and login.
- `AES_WTI_BEARER_TOKEN`: obtain from a valid WTI session through the installed release's documented authentication mechanism; keep only in the bridge runtime secret manager, never in QueuePilot's DB.

The demo setup generates all required QueuePilot secrets. Set `AURA_CONTROL_ENABLED=false` until a real lab has passed and separately controlled approval procedures are in place.

## 7. Install and configure the external WTI worker

The included `bridge/wti_driver.py` is a **working HTTPS transport and confirmation engine**, not an Avaya endpoint-specification replacement. `bridge/wti_actions.template.json` is deliberately NOT executable against a live PBX. Obtain Avaya's official OpenAPI JSON for the exact AES release, then configure only permitted actions with verified paths, JSON request structures and response-success selectors, and verify the TLink probe against actual AES CTI state. The transport rejects off-host paths, non-HTTPS base, missing token, unmatched success responses and insecure TLS bypass.

```bash
export QUEUEPILOT_URL="https://queuepilot.internal.example"
export AVAYA_INGEST_KEY="<event ingest secret>"
export AURA_COMMAND_BRIDGE_KEY="<different command secret>"
export AES_WTI_URL="https://aes.internal.example"
export AES_WTI_BEARER_TOKEN="<valid AES WTI session token>"
export AES_CA_BUNDLE="/path/to/enterprise-root-ca.pem"  # optional when OS trust is enough
export AES_WTI_ACTIONS_FILE="/private/path/reviewed-wti-actions.json"
python -m bridge.worker
```

If WTI session renewal is required, the installed-release-specific login/refresh mechanism must be integrated; the worker does **not** refresh expired tokens yet. Use a supervised service and never put production secrets into a repository or logs.

Call-control results can be **uncertain** after an AES timeout or 5xx even if the command took effect. Operator reconciliation is mandatory; no duplicate dial attempts. The WTI driver can emit `succeeded` only after a configured JSON response matches a documented confirmation field; for operations that only confirm *acceptance*, treat success as acceptance and independently verify completion from CTI events before marking business outcomes.

## 8. Routing changes and CMS reports

The `POST /api/aura/routing-plans` and UI form records a structured proposal for VDN destination, vector steps, skill membership, or hours of operation, including the business reason. **There is no automatic SAT/SMS submission and no PBX configuration change.** Next stage: implement a licensed SMS SOAP change adapter with retrieved before-state, validated template, separate two-person approval, backup, nonproduction validation, post-change verification and rollback.

Current SLA and call-history reports reconstruct only normalized CTI events, and will not reproduce all Avaya CMS definitions (short abandons, interval boundaries, requeued calls, conferences, call accounting or transfers). CMS read-only reporting and reconciliation is a separate next stage.

## 9. Commissioning acceptance checks

- [ ] CM/AES versions and system features validated with Avaya administrator.
- [ ] CTI link / TLink tested and monitored; licenses sufficient for total stations and skills.
- [ ] Queue/agent mappings imported and double-checked against live CM administration.
- [ ] Monitor callbacks normalized with real AES samples for queue, answer, transfer, abandon, disconnect, login, logout, ready, AUX, ACW.
- [ ] Test event ID stability, out-of-order/dropped events, reconnection and deduplication.
- [ ] Validate only expected, approved stations/VDNs can be call-control destinations.
- [ ] WTI transport driver operation payload/response fields generated from actual release API.
- [ ] Hold/retrieve/transfer tests against **test agents only**, verify effect on PBX.
- [ ] Test timeout after AES receives request: no automatic retry.
- [ ] Validate TLS, IP allowlists, distinct keys, secret rotation, VPN and access logs.
- [ ] Commission independent CMS reporting comparison and retained backup / rollback plan.
- [ ] Replace MVP SQLite and single-admin access with production RDBMS, SSO/RBAC, auditing, HA before enterprise deployment.

## 10. What is not finished

- No installed-release Avaya WTI endpoint schema, valid WTI login/session/refresh implementation, active monitor callbacks, or actual PBX authentication in the current package.
- No direct TSAPI Java/C++ driver; necessary if WTI release coverage is insufficient.
- No automatic routing/agent-skill administration (SMS/SAT), no IVR designer or recording/media handling.
- No CMS historical import or reconciled SLA semantics.
- No enterprise RBAC/SSO, distributed processing, HA or hardened production database.

**Do not call this a production-ready, PBX-connected contact center yet.** This release is the testable integration control plane plus the configurable connection worker.
