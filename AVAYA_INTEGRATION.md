# Avaya integration — product-neutral background

**For your selected Avaya Aura / Communication Manager integration, use [`AURA_DEPLOYMENT.md`](AURA_DEPLOYMENT.md) first. It documents the new v0.2 mapped events, command gateway and WTI bridge.**

# Connecting QueuePilot to Avaya

**Current state:** QueuePilot exposes a tested vendor-neutral ingest endpoint, but **no live Avaya CTI connection has been configured or tested**. Each Avaya product exposes different interfaces and has its own authentication and license requirements. The bridge/adapter must be implemented with the official SDK or API for your specific product and release.

## 1. Determine the Avaya platform

| Avaya platform | Recommended integration approach | What you need |
|---|---|---|
| **Avaya Aura Communication Manager + Application Enablement Services (AES)** | AES Web Telephony Interface (WTI) REST/events when supported by your release, otherwise supported TSAPI/DMCC SDK | AES version, Communication Manager release, AES hostname, WTI/CTI feature enabled, service user, security/certificates, licensed devices/monitors, VDNs/skills |
| **Avaya IP Office** | **DevLink3** TCP call events, using official SDK/parser; newer versions have no dependency on the old Windows DevLink DLL | IP Office release, CTI Link Pro licensing as applicable, DevLink3 rights group, credentials and PBX address |
| **Avaya Experience Platform** | Use platform's public API-first contact-center services and authorized event subscriptions / analytics endpoints available in your tenant | Tenant/region, OAuth/service account permissions, queue IDs, available subscription or analytics API endpoints |
| **Avaya Aura Contact Center / Contact Center Select** | Verify Contact Center real-time and Open Queue Web Services SDK and supported release | Contact Center release and specific CCMS/CCS SDK access |

The Avaya integration method depends on the licensed edition and feature set. **Do not connect an untested adapter to production call routing.** The QueuePilot ingestion API itself does not place, reroute, or answer calls.

Official primary references:

- AES WTI overview: https://documentation.avaya.com/en-us/home/bundle/application-enablement-services/aesoverviewandspec_r10-2-x/web-telephony-interface-%28wti%29-service.html
- AES product / TSAPI services: https://support.avaya.com/support/en/products/P0358/avaya-aura-application-enablement-services
- IP Office rights / DevLink3: https://documentation.avaya.com/en-us/home/bundle/ip-office/ipofficewebmanagerr12_3/security-settings/rights-groups/telephony-apis.html
- IP Office DevLink3 support: https://documentation.avaya.com/en-us/home/bundle/ip-office/ipofficedevlink/cti-devlink.html
- Experience Platform APIs: https://github.com/AvayaExperiencePlatform/welcome-to-avaya-experience-platform
- Contact Center SDK listings: https://www.avaya.com/en/developer-support/sdks/

## 2. Bridge pipeline

```
Upstream Avaya PBX / contact-center interface
  -> licensed CTI/event bridge (vendor-specific implementation)
  -> validate, normalize, and map extension / VDN / skill -> local IDs
  -> POST https://<your-private-host>/api/integrations/events
  -> SQLite -> dashboard
```

The bridge should follow these operational requirements:

1. Establish authenticated session with licensed official Avaya SDK/API, validate its certificate, and subscribe to the required device/VDN/skill or queue monitors.
2. Maintain a **stable source name** and **stable event ID** for every upstream event to guarantee idempotency on retries.
3. Resolve `queue_id` to a queue created in QueuePilot; resolve `agent_id` to an agent created in QueuePilot.
4. Convert timestamps to RFC 3339 with timezone, for example `2026-11-11T19:00:00Z`.
5. Deliver the following normalized lifecycle events, acknowledging only 200 responses: `call.queued`, `call.answered`, `call.abandoned`, `call.completed`, `agent.status`.
6. Retry HTTP 429/5xx with bounded exponential backoff, spool data durably during outages, and retain order *per call ID*. The service rejects events older than one year or more than five minutes in the future.
7. Implement connector-level health checks and send a heartbeat every 10–15 seconds to `/api/integrations/heartbeat` (same API-key header), with `connected: true` **only** after checking an active upstream CTI monitor session. The dashboard considers it live for 30 seconds after the latest authenticated heartbeat. Do not treat merely receiving an event as proof of a currently healthy connection.
8. Protect credentials with a secret vault; use HTTPS, network allowlists or mTLS and appropriate segmentation. Never expose AES/CTI services directly to the public Internet.

## 3. Normalized event contract

Send JSON to `POST /api/integrations/events` with header `X-Integration-Key: <AVAYA_INGEST_KEY>`.

**Call entered queue:**

```json
{
  "source": "avaya-aura-aes-01",
  "event_id": "event-unique-7812",
  "type": "call.queued",
  "call_id": "switch-call-551",
  "queue_id": "q_REPLACE_WITH_REAL_ID",
  "caller": "+12025550177",
  "occurred_at": "2026-11-11T19:00:00Z"
}
```

**Call answered:**

```json
{
  "source": "avaya-aura-aes-01",
  "event_id": "event-unique-7813",
  "type": "call.answered",
  "call_id": "switch-call-551",
  "queue_id": "q_REPLACE_WITH_REAL_ID",
  "agent_id": "a_REPLACE_WITH_REAL_ID",
  "occurred_at": "2026-11-11T19:00:14Z"
}
```

**Call ended:** same source, `call_id` and `queue_id`, unique `event_id`, `type: "call.completed"` and a new timestamp.

**Call abandoned:** `type: "call.abandoned"` while still queued.

**Agent state change:**

```json
{
  "source": "avaya-aura-aes-01",
  "event_id": "agent-2101-state-99211",
  "type": "agent.status",
  "agent_id": "a_REPLACE_WITH_REAL_ID",
  "agent_status": "available",
  "occurred_at": "2026-11-11T19:00:00Z"
}
```

Allowed normalized agent states: `available`, `busy`, `acw`, `away`, `offline`.

Responses contain `outcome` values such as `created`, `updated`, `duplicate`, `stale`; invalid queues/agents cause 404, inconsistent queue mapping causes 409. `source + event_id` must be unique. **Caller number is masked on ingestion and raw ANI is not persisted.**

## 4. Connector health heartbeat

Send every 10–15 seconds, but report true **only** when the bridge has verified its Avaya session/monitor is actually live:

```json
{"source":"avaya-aura-aes-01","connected":true,"details":"CTI monitor active"}
```

Endpoint: `POST /api/integrations/heartbeat` with `X-Integration-Key` header. `connected` is bridge-reported and expires after 30 seconds of silence. It is not independently validated against Avaya by the QueuePilot server. If the CTI session fails, report `connected: false` immediately. 

## 5. Test before going live

After logging into QueuePilot and creating a queue, copy its ID from Configuration. Set `QUEUEPILOT_QUEUE_ID` and `AVAYA_INGEST_KEY` in your terminal, then run `python examples/send_event.py`. Verify that a call appears in **Live calls** and queue waiting count increases. To test an event lifecycle, send `call.answered` and `call.completed` with the *same source and call ID* but unique event IDs.

`examples/normalize_event.py` shows how to translate a hypothetical JSON event to the ingestion contract. **Its input keys are not a claimed Avaya wire format.** Implement a real listener using the official SDK for the actual PBX.

## 6. Confirm these details for a production connector

- Product (Aura Communication Manager / AES, IP Office, Experience Platform, or Contact Center Select)
- Exact software release and deployment (on premises or cloud)
- Management and CTI connectivity path (IP/FQDN and port; never share production passwords in chat)
- Whether the requirement is **monitoring**, **queue/call routing control**, **screen pop**, **callback**, or **IVR**
- Approximate number of concurrent calls, skills/queues, agents, and sites
- Available CTI / SDK licenses and test tenant

When known, implement a product-specific connector and verification tests. Until then the UI reports that no live CTI session has been confirmed.
