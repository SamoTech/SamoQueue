# QueuePilot v1.4 — Native supervisor Hold / Resume (LAB ONLY)

**Status:** Implemented and tested using simulated SIP/RTP on loopback. This is a
**server-side media hold** operation managed entirely by QueuePilot, **not** a
standards-complete SIP hold feature. A physical Avaya phone's Hold button is
not supported yet: handling phone-generated re-INVITE/UPDATE, `a=sendonly` /
`a=inactive`, session refreshes and glare remains future work.

## Ownership and call flow

```
Avaya SIP handoff -> QueuePilot B2BUA -> registered SIP IP phone
                          |       |
                 QueuePilot RTP media anchor
                          |
         One ongoing stereo WAV / approved listening tap
                          |
                native_call_controls (SQLite)
                          ^
             admin-authenticated Dashboard
```

The dashboard requests Hold or Resume for an existing native answered call.
It does **not** claim the media operation succeeded. The request receives HTTP
202 with `state=pending`. `LocalEventWriter.sync_call_controls` runs under the
native switch's existing single-writer lock on each scheduler iteration (~0.2s)
and either acknowledges `applied` or `rejected` with a non-sensitive reason.
The dashboard displays that state. Rejected or stale requests are never
silently re-run against a subsequent session that reuses the SIP Call-ID.

- `POST /api/engine/calls/{dashboard_call_id}/control` body `{"action":"hold"}` or
  `{"action":"resume"}` requires administrator authentication. It uses the
  **internal** dashboard call ID, not the caller's phone number.
- `GET /api/engine/calls/controls` lists up to 100 recent commands for admins.
- Only calls marked answered by `queuepilot-native` are eligible to be queued.
  An engine heartbeat seen within 30 seconds is required but does not guarantee
  that a specific call is alive. The runtime must still validate it is bridged.
- A unique pending command per SIP Call-ID rejects racing requests (409).
- `audit_log` records request, application and rejection; media-side events
  `call.held` and `call.resumed` are local diagnostics, not authoritative CDRs.

## Media behavior

Hold is implemented within `RtpRelay`. It stops forwarding RTP from either
party to the other and stops forwarding held audio to the supervisor observer.
QueuePilot sends **20-ms G.711 comfort silence** packets to the caller on its
existing RTP port. There is no music-on-hold track yet and no attempted RTP
passthrough from Avaya directly to the IP phone. Resume stops the synthetic
source and relays the original packet stream again. Because the lab silence
uses its own SSRC and lacks RTCP/jitter control, some physical SIP phones or
upstream trunks may reject it. **Never assume Avaya compatibility without a
staging network capture and actual device test.**

## The recording rule

The same `StereoCallRecorder` instance stays alive throughout Hold and Resume.
No new recording is created and no split/concatenation takes place.
When the call ends, there is **one WAV** with caller on the left and agent on
the right. Held time is approximately filled with silence in the existing
recorder's shared clock; production-quality timeline synchronization is not
claimed. Capture is still disabled by default and requires prior approved
notice and consent policy.

## Local validation

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q tests/test_native_call_control.py
python -m pytest -q
node --check app/static/app.js
```

Tests check administrator-only command creation, heartbeat and call-state
gates, one-pending-command rule, command acknowledgements, duplicated/stale
commands, two-way RTP mute/resume, G.711 comfort silence, audit records, and a
single resulting stereo WAV.

## Remaining gaps before a real installation

SIP endpoint-initiated hold/re-INVITE, Music on Hold and prompt licensing,
blind/attended transfer, conference, call recovery, RTP SSRC/RTCP handling,
TLS/SRTP, external Avaya trunk and handset interoperability, monitoring audio
buffer flushing, durable distributed state, compliance and load testing.
This v1.4 laboratory build is not safe for production customer calls.
