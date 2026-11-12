# QueuePilot v0.7 — Independent SIP Contact Center (LAB)

QueuePilot is being built as an **independent contact-center platform**, not an Avaya management console. **Avaya Aura hands off inbound calls over SIP; all queue decisions, agent SIP sessions, call media, IVR, recording and supervision are intended to be owned by QueuePilot.** There is no Asterisk, FreeSWITCH, AES, WTI, TSAPI, or DMCC component in this repository.

> **Release status: restricted lab / development. NOT production-ready.** A simulated SIP trunk and softphone have been tested; Avaya Aura and actual Avaya IP handset interoperability have not been verified. Many requested call-center features are still only management scaffolding or planned. Do **not** route customer calls to this release.

## What actually works today

- Native UDP/IPv4 SIP REGISTER with SHA-256 Digest and time-limited nonces (lab subset).
- Allowlisted inbound SIP INVITE / DID routing; an independent in-memory ACD chooses a registered agent according to skills and longest idle time.
- Agent outbound INVITE, basic SIP responses/ACK/BYE/CANCEL, anchored two-way G.711 RTP in an isolated lab.
- Optional local per-leg diagnostic WAV capture, **disabled by default**.
- Separate management service with queue/agent directory, call history, dashboard, basic SLA, recording library, audited supervisor approval workflow, IVR **drafts**, and ten CSV/JSON/ZIP report datasets.
- **v0.7:** the optional `--local-events` adapter writes native SIP call lifecycle events straight into QueuePilot's SQLite database (no PBX CTI bridge). The dashboard displays the source's **reported** health. The adapter validates its queue and agent IDs against the dashboard database before starting.
- Existing legacy PBX integration endpoints, bridge workers, UI, setup keys, and obsolete documentation were removed.

## Run management dashboard locally

Requires Python 3.11+.

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python setup_demo.py               # once: writes .env with unique demo secrets
python run.py
```

Open `http://127.0.0.1:8000` on the test machine. The setup script prints the generated administrator password once. `.env` and all runtime data must remain uncommitted.

The standalone native SIP lab starts separately (same project, **no external telephony system**):

```bash
cp telephony/native_switch.example.json telephony/native_switch.local.json
# Replace every REPLACE_ placeholder with unique secrets.
python -m telephony.native_switch --config telephony/native_switch.local.json --lab
```

**Optional reporting integration (lab only):** Create dashboard queues and agents first, replace the route `queue` and agent IDs in the local SIP JSON with their actual QueuePilot database IDs, then launch with `--lab --local-events`. Both processes must use the same `APP_DB_PATH`. The adapter writes directly to SQLite and records a local heartbeat. It is single-node and synchronous: **not a durable message bus, crash-recovery journal, or production-scale event stream**. If this ID validation fails, the SIP process refuses to start with local persistence enabled. Without the option, the dashboard does **not** get native SIP call events automatically.

## API surface

- `POST /api/login`, `GET /api/dashboard`, `/api/queues`, `/api/agents` — dashboard management.
- `POST /api/engine/events` and `/api/engine/heartbeat` — internal native-telephony event API; `X-Engine-Key` and exact `source=queuepilot-native` are mandatory. No Aura or external CTI endpoint exists.
- `/api/cc/recordings`, `/api/cc/monitor`, `/api/cc/ivr`, `/api/cc/reports` — domain services. Native RTP recording import/streaming and IVR execution are **not integrated end-to-end**. Recording and monitoring flags are off by default.

## Missing features and release blockers

**Not completed:** complete RFC 3261 SIP dialogs/transactions, retransmissions, TLS/SRTP/RTCP, DTMF IVR prompt playback, hold/transfer/conference, production recordings, RTP mixing and live listening/whisper/barge, durable ACD/registration state, multi-node HA, comprehensive queue/agent metrics, disaster recovery, session recovery, production identity and compliance workflows. Real Avaya SIP trunk and Avaya phone firmware compatibility must be validated before use.

The `app/` dashboard and `telephony/` runtime currently run as **two QueuePilot-owned processes**. That does not make them intermediary PBXs. `--local-events` shares a lab SQLite DB; it is not suitable for production concurrency/failover.

## Security and preservation

- Bound SIP networking to loopback in the supplied lab example. Do not expose UDP SIP/RTP, `/docs`, or internal engine APIs to public networks.
- Use a private voice VLAN, strong per-phone credentials, legal recording notices/consent, secure audio storage, audit logs and supervised monitoring approvals.
- Existing database tables created by the obsolete AES/Aura integration are **preserved, not used**. This cleanup does not delete historical data or recordings. The old audit table is copied idempotently to the generic `audit_log`. Back up the database before upgrading any existing installation.
- `.env.example` contains placeholders only. Real phone secrets go in the ignored `telephony/native_switch.local.json`.

See [architecture and gap analysis](docs/NATIVE_SIP_CALLCENTER_ARCHITECTURE.md) and [the cleanup audit](docs/CODEBASE_CLEANUP_AUDIT.md). Run checks with:

```bash
python -m pytest -q
python -m compileall -q app telephony tests
node --check app/static/app.js
```
