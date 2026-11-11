# QueuePilot — Independent Native SIP Call Center (v0.5 foundation)

> **Corrected requirement (11 Nov 2026): no Asterisk, FreeSWITCH or independent telephony middleware.** Avaya Aura **only hands off incoming calls via SIP**. QueuePilot itself will own IP phone calls, IVR, agent ACD, RTP bridging, recordings, supervisor listening, reports, and administrative controls. See the **[native SIP architecture and compatibility requirements](docs/NATIVE_SIP_CALLCENTER_ARCHITECTURE.md)**.
>
> **Implementation status:** The standalone ACD rules, trusted SIP ingress routing policy, registered/provisioned phone location policy and isolated **lab-only** SIP OPTIONS probe are tested. The real SIP B2BUA, phone registrar/authentication and RTP media switch are **NOT YET IMPLEMENTED**. **Do not route live calls to this build.** A SIP-enabled IP phone is required; a phone that is H.323-only or locked to Avaya registration cannot be called independently without changing its endpoint setup.

## Native SIP foundation

- `telephony/acd.py`: queue/agent scheduler, skills, priorities and offer/ACW timing.
- `telephony/native_sip.py`: validates Avaya peer and DID mapping, authorized LAN phone contacts and SIP dial *intent* (not live origination).
- `telephony/sip_probe.py`: local UDP OPTIONS compatibility probe on 127.0.0.1:15060 (explicitly rejects INVITEs; lab only).
- `tests/test_native_sip.py`: signaling ingress/phone/ACD authorization and safety tests.
- No Asterisk or FreeSWITCH runtime is part of the architecture. Historical v0.2/v0.3 Avaya control code below is legacy and not required for the target system.

---

# QueuePilot v0.2 — Avaya Aura Operations Platform

A runnable prototype of a call-center supervisor platform with **queue statistics, agent directory, polling, call events, SLA reporting, secured integration ingestion, Avaya Aura resource mapping, guarded command approval, routing proposals, audit trails and CSV exports**.

**Integration status:** The Avaya Aura control plane and configurable WTI HTTPS worker are implemented and tested in isolation. **No customer PBX connection has been made or verified.** The included WTI action template is deliberately nonoperational until filled with the official API definitions for the installed AES release. Read [`AURA_DEPLOYMENT.md`](AURA_DEPLOYMENT.md) before integration.

## Run locally

Requires Python 3.11+.

```bash
cd avaya-queue-platform
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python setup_demo.py
python run.py
```

The setup script generates unique credentials and prints the initial **admin password once**. Keep it secure. Open **http://127.0.0.1:8000**, sign in as `admin` and click **Load demo data**.

For manual production-oriented configuration, copy `.env.example` to `.env`, replace every secret placeholder (admin password, app signing secret, event ingestion key and separate command bridge key) with different strong secrets and change `APP_DEMO_MODE=false`.

The `Simulate event` buttons then generate new/answered/abandoned/completed calls; refresh occurs every 4 seconds. Avaya call-control actions remain disabled by default (`AURA_CONTROL_ENABLED=false`).

## Capabilities

- Six-view operations console with queue health, agent state, call activity, reporting, Aura control, and configuration
- API-key-authenticated event ingestion (`X-Integration-Key`), timestamp validation, deduplication and out-of-order protections
- Configurable queues: names, skill labels, SLA seconds, targets, enable/disable
- Agents: local directory, extension, status and team assignment; authenticated integration events update agent state
- Aura VDN/skill -> local queue and station/login -> local agent mapping
- Supervisor call-control requests with mandatory approval, internal-destination allowlist, separate privileged bridge key, expiry/no replay and audit
- Standalone configurable WTI HTTPS bridge with fail-closed response validation and TLink-state probing
- Routing change proposals (deliberately **not** applied automatically to CM)
- Call lifecycle: queued → answered → completed, or queued → abandoned
- Masking of caller numbers at ingestion, CSV exports, SQLite WAL persistence
- Basic local supervisor login and demo mode
- API documentation at `/docs`; health check at `/api/health`

## Docker

```bash
docker build -t queuepilot .
docker run --rm -p 127.0.0.1:8000:8000 \
  --env-file .env -v "$PWD/data:/srv/app/data" queuepilot
```

If `APP_DB_PATH` is relative, it is resolved relative to the project root. The directory mounted above is writable by container UID 10001; ensure permissions are correct on Linux.

## Architecture

```
Avaya Aura AES WTI / TSAPI / DMCC   ┐
Avaya IP Office DevLink3            ├──> licensed/vendor-specific CTI bridge ── HTTPS + API key
Avaya Experience Platform APIs      ┘                                      │
                                                                        ▼
                                                             /api/integrations/events
                                                                        │
                                                                  FastAPI + SQLite
                                                                        │
                                                              Supervisor web console
```

`Avaya` remains responsible for actual telephony, queue selection, IVR, hold audio and routing. QueuePilot provides **monitoring + call-control request workflows**; the control gateway requires a separately configured AES WTI/TSAPI driver. It does not replace the PBX/ACD or IVR.

## Production hardening before connecting to a real PBX

1. Set `APP_DEMO_MODE=false`, unique strong secrets, and run behind a TLS reverse proxy with network allowlists and rate limiting.
2. Use per-connector authentication/key rotation, mTLS where available, and log redaction; restrict API/docs to a private management network.
3. Replace MVP admin login with enterprise SSO and role-based access (supervisor/administrator/auditor), auditing every edit.
4. Use a supported production DB (e.g. PostgreSQL), migrations, retention policy, backups, monitoring, HA, and an event replay queue; the included SQLite prototype is single-node.
5. Obtain exact Avaya product, release, SDK, CTI license, endpoint, user privilege, VDN/skill/agent mapping and event semantics. Implement session reconnects, heartbeats, event replay, and media/PII controls; **do not automatically replay uncertain telephony commands**.
6. Align SLA definitions, timezones, call-transfer semantics, and abandon treatment with Avaya CMS/your organization's reports.
7. Test out-of-order events, missing lifecycle events, large loads, emergency call handling policies, and failure recovery in a **nonproduction Avaya tenant** first.

## Limitations

- Dashboard polls every 4 seconds (not a vendor-guaranteed real-time feed).
- The local status/queue configuration remains **local only**. A call-control request can be sent through the *configurable external bridge* only if AES access is commissioned, per-operation responses are validated and `AURA_CONTROL_ENABLED=true`. No actual customer PBX operation has been performed.
- The example WTI JSON template is **not** a production action mapping. Routes and response selectors must come from actual AES API documentation.
- VDN/vector/skill route changes are advisory only; CMS/SMS adapters are not implemented.
- Basic SLA definition: answered within configured seconds / (answered + abandoned) for calls arriving during the current local day. No short-abandon exclusions or vendor-specific transfer accounting.
- Traffic chart uses UTC calendar dates, whereas today's metrics use `APP_TIMEZONE`.
- `connected` is true only when an authenticated bridge reports an active CTI monitor session via `/api/integrations/heartbeat` within the past 30 seconds. This is **bridge-reported** health, not an independently verified PBX session.
- Requires correct upstream call IDs, event ordering, and unique event IDs per source.

## Tests

```bash
pytest -q
```

For **Aura/Communication Manager**, start with `AURA_DEPLOYMENT.md`. `AVAYA_INTEGRATION.md` contains general vendor-neutral ingestion notes.

The test suite includes bridge transport, mapping, approvals, idempotency, unauthorized claims and routing advisory checks.

## QueuePilot v0.3 — media, supervision, IVR drafts and reports

The **Contact Center** upgrade adds a recording library with authenticated upload/playback; a dedicated reviewer and audited monitoring workflow; a secure WebSocket PCM relay for an *external authorized* Avaya DMCC media bridge; validated IVR graph drafts and non-executing deployment proposals; and CSV/JSON/ZIP exports across ten operational report categories.

**No real-time recording, DMCC media capture, browser live listening, or IVR deployment operates against Avaya without a licensed and configured external bridge**. These are integration points, not an Avaya-native recording engine. Read [`AURA_MEDIA_DEPLOYMENT.md`](AURA_MEDIA_DEPLOYMENT.md) before enabling any audio or monitoring feature. All audio-related flags default to disabled.

### v0.3 testing

```bash
python -m pytest -q
node --check app/static/app.js
```
