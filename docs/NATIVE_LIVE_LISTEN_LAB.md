# QueuePilot v1.1 — Native RTP Live Listening (LAB ONLY)

## Purpose and ownership

QueuePilot owns **both SIP call legs and the RTP media anchor**. Avaya Aura delivers inbound SIP calls; no Avaya AES, DMCC, TSAPI, Asterisk, FreeSWITCH, external recorder or monitoring PBX is used. This milestone attaches an observation-only tap to a connected QueuePilot call and sends a 16kHz mono PCM16 stream to the supervisor dashboard. It **does not** inject audio into the call (no whisper/barge), and it does not modify the call's single stereo WAV recording.

**Status:** isolated local integration tests, not approved for live caller monitoring or production. Real Avaya SIP trunk and Avaya IP-phone interoperability, encrypted media/TLS, enterprise identity, distributed session state, session failover, recording notice and legal compliance require further development.

## Control and audio flow

1. An admin selects an **active answered native call** and requests monitoring for supervision/quality/training.
2. A **different reviewer account** approves (`APPROVE LIVE LISTEN`). Monitoring is disabled by default. Approval creates a queue item, not an immediate media tap.
3. The explicitly enabled `--local-monitor` worker inside the QueuePilot native SIP runtime (not a separate PBX) authenticates to the loopback-only management API with `MONITOR_ENGINE_KEY`, claims the command and verifies that the *same call ID and agent extension* have a bridged native RTP session.
4. Only then does it attach `LiveAudioMixer` to **already validated RTP packets**. The audio tap is read-only; ordinary relay to the caller/agent continues unchanged.
5. The mixer decodes G.711 µ-law or A-law from both legs, uses bounded per-leg queues, produces 20ms mono PCM frames and resamples from 8kHz to 16kHz by simple sample duplication. It posts at most 100ms (3,200 bytes) per request over local authenticated HTTP to the Dashboard API.
6. The reviewer clicks *Listen in browser*. A one-use ticket valid for 20 seconds starts a WebSocket stream. Each frame is rechecked against active approval and active call state. Stopping the request (or ending the call) revokes the tap and the browser stream.
7. All requests, approvals, claims, playback tickets, stops and results are audited. A failed API or revoked state stops the tap **fail closed**, never keeps listening offline.

```text
Avaya SIP handoff → QueuePilot native SIP/RTP bridge → registered SIP phone
                                      │
                               approved tap (no injection)
                                      │
                                LiveAudioMixer
                                      │
                        authenticated local PCM POST
                                      │
                       QueuePilot FastAPI monitoring relay
                                      │
                         one-use reviewer WebSocket ticket
                                      │
                          Supervisor browser playback
```

## Run on an isolated machine

Run the dashboard first (see README). Create a QueuePilot queue/agent whose database ID matches the SIP JSON. For a real test, set **separate non-default secrets**, an independent reviewer account, and enable monitoring in the **local** `.env`:

```dotenv
MONITORING_ENABLED=true
MONITOR_ENGINE_KEY=<unique random 32+ byte secret>
APP_REVIEWER_USER=reviewer
APP_REVIEWER_PASSWORD=<different strong password>
```

The dashboard binds to loopback during lab tests. Do not expose it on an untrusted network. In the private, ignored `telephony/native_switch.local.json`, configure the SIP peers, extensions, DID and agent IDs as documented for the lab. Start:

```bash
python -m telephony.native_switch \
  --config telephony/native_switch.local.json \
  --lab --local-events --local-monitor \
  --monitor-origin http://127.0.0.1:8000
```

`--local-monitor` **requires `--local-events`** for event correlation. The monitor origin must be an HTTP loopback IP (no external host/credentials, no redirect). The monitor key must match the server `.env` and must not be placed in the public SIP JSON or Git. A separate `--local-monitor` flag prevents accidental activation merely by enabling dashboard configuration. No monitoring occurs until independent approval is granted.

Monitoring and recording are separate: `RECORDINGS_ENABLED=false` does **not** block monitoring; `MONITORING_ENABLED=false` disables it. If recording is enabled separately with a lawful policy, each conversation still creates **one stereo WAV** on close. Listening itself does not create audio files.

## Release limitations and security

- G.711/UDP/IPv4 and lab topology only. This code neither supplies SIP TLS/SRTP nor resolves multi-host failover. PCM to the dashboard is **local HTTP only**; browser usage is for localhost test rigs, not an Internet service.
- Audio mixes both parties to mono; it uses simple 8kHz→16kHz resampling. No jitter buffer/clock-drift recovery, packet-loss concealment, AGC, volume management, detailed quality analytics or acoustic echo cancellation.
- When a call ends or a stop request is received, the tap is detached. Browser WebSocket verifies session state before sending frames and periodically while idle. Session recovery following abrupt process/network loss is not guaranteed; fail-closed handling and manual review remain required.
- Monitoring approvals are governed by the existing basic admin/reviewer identities. A real deployment requires SSO, granular RBAC, privacy notices, retention controls, audit retention, supervisor consent policies and rigorous penetration/interoperability/load testing.
- No microphone/audio injection: **Whisper, Barge, Hold, Transfer and Conference remain unimplemented**. Do not report these as production features.
- This implementation **never bypasses phone registration or H.323 firmware restrictions**. Physical Avaya phones must be independently SIP-compatible, configured and interoperability-tested.

## Verify

```bash
python -m pytest -q
python -m compileall -q app telephony tests
node --check app/static/app.js
```

Tests cover authorization, separate approval, active-call/agent binding, G.711 decode/mix, spoofed RTP rejection, restricted loopback origin, mono PCM delivery, revocation/stop acknowledgement, call termination, and continued **single-WAV** recording. These are local synthetic media tests, not actual customer media or a security certification.
