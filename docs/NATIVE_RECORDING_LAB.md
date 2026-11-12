# QueuePilot v0.8 — Native RTP recording archive (LAB only)

## Scope and limits

Avaya delivers calls over an authorized SIP trunk, but the **QueuePilot-owned** SIP
runtime is the media anchor and captures G.711 RTP locally. The implementation
can archive two **separate unsynchronized mono WAV tracks** (customer/trunk and
agent/phone) into the QueuePilot management library after a call finishes.
This is not a mixed conversation recording, real-time monitoring, or a compliant
production recorder. No Asterisk, FreeSWITCH, AES, or DMCC is used.

## Security gates: default OFF

1. On the management process, set `RECORDINGS_ENABLED=true` and configure a
   protected `RECORDING_DIR` in local `.env` only.
2. On the native SIP process, set `record_calls=true`,
   `recording_notice_confirmed=true`, and a local `record_directory` in
   `telephony/native_switch.local.json`. The capture and library directories
   **must differ**. The flag is an *operator acknowledgment* that a lawful,
   appropriately notified and authorized recording policy exists; QueuePilot
   does not itself play consent notices or determine legal permission.
3. Use `--lab --local-events`, the **same** `APP_DB_PATH`, and matching QueuePilot
   queue and agent IDs on both processes. Without local event integration,
   diagnostics remain only local WAV files (not indexed to the library).
4. Configure strict local filesystem access: the archived files are mode 0600
   and the destination folder mode 0700. Do not expose raw media folders over
   a web/static server. Browser playback requires an authenticated supervisor.

The runtime refuses to start with requested recording but without explicit
policy acknowledgement, and refuses to enable recording-library integration if
the management `RECORDINGS_ENABLED` flag is disabled.

## Native process lifecycle

```
Avaya test SIP INVITE -> NativeSwitch -> agent INVITE / 200 / ACK
                                   -> QueuePilot RTP relay G.711
                                   -> per-direction WAV capture (OFF by default)
BYE -> close WAV -> write call.completed -> archive each leg -> recordings table
                                                             -> /api/cc/recordings
                                                             -> CSV/JSON report
                                                             -> authenticated audio
```

- `telephony/rtp_media.py`: manages RTP and local mono PCM16 8 kHz WAV legs.
- `telephony/native_switch.py`: owns the call and invokes the archive after
  closing media and publishing a terminal call event.
- `telephony/local_events.py`: optional, strictly local event and archive hook.
- `telephony/recording_archive.py`: validates each capture location and WAV
  properties, copies to a uniquely named protected file, hashes its bytes,
  deduplicates by `(source, call ID, hash)`, and audits the index action.
- The two media directions have distinct sources so even equal-content silence
  tracks do not unintentionally deduplicate each other.
- Original local capture files are retained to avoid data loss if indexing
  fails. Storage lifecycle, encryption and automated retention are pending.

## Operational limitations

- **Lab only:** UDP/IPv4/G.711 subset without SIP transaction completeness,
  SRTP/TLS, secure RTP identity, HA, durable event journal, RTCP and jitter
  correction. Avaya phone interoperability has not been demonstrated.
- Each track has its own packet timeline; the recorder **does not** guarantee
  stereo, synchronization, silence-filling, transcript or complete audio when
  packets are lost. A one-way or no-media call may have one or zero recordings.
- Capture is limited to 30 minutes per leg; truncated tracks are not
  indexed in the recording library. Archive accepts at most 32 MiB per WAV file. This is **not** an approved retention, compliance or lossless design.
- A local DB or filesystem failure is logged but cannot be retried durably yet;
  inspect `native_recording_archive_failed` and source captures after a failure.
- Consent, employee monitoring policies, RBAC/SSO, tenant isolation, encryption,
  retention/deletion and tamper-evident audit must be completed before deployment.

## Verification

```
python -m pytest -q
python -m compileall -q app telephony tests
node --check app/static/app.js
```

Tests include a simulated SIP trunk and registered test phone, two-way RTP,
call termination, source-to-library archival, distinct media-leg identity,
authenticated browser playback, deduplication, disabled/absent-consent checks,
and invalid audio/path refusal. **These are local tests only.**
