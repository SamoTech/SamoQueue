# QueuePilot v1.3 — Local ACD control and event outbox (LAB ONLY)

## Purpose

Avaya may hand off an inbound SIP call, but QueuePilot remains the only call-controller. This release connects the QueuePilot Dashboard's local SQLite state to the embedded ACD and SIP runtime, **without Asterisk, FreeSWITCH, Avaya AES, or a separate broker**.

## Setup

1. Create Dashboard queues and agents, with unique matching SIP extension values.
2. Configure `telephony/native_switch.local.json` so each DID route targets a valid Dashboard queue ID, each SIP agent ID maps to the same Dashboard agent and extension, and skills align.
3. Run the Dashboard and SIP processes on the same **isolated lab host** with the same `APP_DB_PATH` and SQLite file. Use explicit `--lab --local-events`; without `--local-events` synchronization and journaling do not run.
4. Start the SIP process with `python -m telephony.native_switch --config telephony/native_switch.local.json --lab --local-events`.
5. Test SIP REGISTER and use Dashboard Agent Edit: Available / Away / Offline. `Busy` and `ACW` are runtime-only.
6. Inspect `/api/dashboard` for `native_managed`, `desired_state` and `control_state`; a command may remain `deferred` throughout an active call and ACW. Real agent presence is `offline` until phone registration succeeds.
7. Disable a Dashboard queue and confirm new inbound INVITEs are rejected while already-connected conversations are not interrupted. For IVR, disabled destinations are treated as invalid options; disabled fallback destinations cause the IVR to end instead of routing.

## Data model

- `native_agent_bindings`: ephemeral local SIP-to-agent mapping; rebuilt on each writer startup. Pending command history is preserved separately.
- `native_agent_commands`: durable per-agent desired state with monotonic revision, applied revision, and `pending/deferred/applied` acknowledgement. Superseded commands do not overwrite newer revisions.
- `native_event_outbox`: ordered event journal with stable event ID, retry count, and delivery acknowledgement. No caller number or audio is stored. Retry ingestion is idempotent via the existing `events(source,event_id)` uniqueness constraint.

## Failure / recovery

- If the manager service or SQLite ingest temporarily fails **after the outbox insert**, the event stays pending. `LocalEventWriter.drain()` retries on new events and every 10 seconds during normal lab operation. Restarting the SIP process does not erase the backlog.
- Outbox insertion itself can fail, including disk full or SQLite corruption; there is no independent storage or recovery in this lab. A process crash before recording an event may lose that event.
- Existing in-flight SIP dialogs and RTP cannot be reconstructed from the database. Pending admin requests may be applied after restart if a compatible agent mapping is restored.
- A disabled queue only prevents *new* admission. Admin skill changes affect *new* calls; existing calls retain their admitted skill requirements.
- This single-process SIP/SQLite approach deliberately makes no promise of guaranteed delivery under all failures, concurrent multi-node control, database failover, SIP RFC 3261 compliance, Avaya handset interoperability, or recording compliance.
- Keep a backup before upgrading; old call records/recordings and legacy historical tables are left untouched.

## Validation

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m compileall -q app telephony tests
node --check app/static/app.js
```

Tests cover: authenticated dashboard requests, deferred commands during ringing/talking/ACW, no false availability before registration, queue disable and skill propagation, persisted retry after SQLite ingest failure, duplicate-event reconciliation after an ambiguous ACK, and existing single WAV/live monitor/IVR regressions.

## v1.4 — One-shot supervisor media commands

The `native_call_controls` table keeps a single pending hold/resume command
per live SIP call. Admins request it through the authenticated dashboard and
`LocalEventWriter.sync_call_controls()` verifies the bridged call under the
native SIP lock. `applied` / `rejected` are durable acknowledgements with
audit entries; no external telephony middleware is used. A stale command is
rejected after a restart rather than replayed against a reused Call-ID.

This is local RTP muting plus caller comfort silence, **not RFC 3264 SIP hold
or PBX transfer**. See [NATIVE_HOLD_LAB.md](NATIVE_HOLD_LAB.md).
