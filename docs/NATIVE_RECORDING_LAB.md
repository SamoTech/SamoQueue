# QueuePilot v0.9 — ONE WAV recording per call (LAB only)

## What is actually implemented

The QueuePilot-owned RTP relay captures both legs into **ONE stereo WAV**
(PCM16, 8000 Hz). Channel 0/left is the caller (inbound SIP trunk), and
channel 1/right is the agent (registered IP phone). A shared monotonic
arrival-time origin plus per-leg RTP timestamps approximate their alignment.
Silence is inserted for RTP gaps; a bounded 500 ms late-packet window
permits modest packet reordering. There is no outside recorder, Asterisk,
FreeSWITCH, AES, or DMCC.

```text
Avaya test SIP -> QueuePilot NativeSwitch -> registered SIP phone
                 QueuePilot RTP anchor
                 ├─ caller frames -> LEFT channel
                 └─ agent frames  -> RIGHT channel
                   one call-id-conversation.wav
BYE -> close final WAV -> call.completed -> archive one row per call
      -> protected recording library -> authenticated playback / reports
```

The recorder enforces a 30-minute lab maximum, rejects unsupported codecs,
keeps in-memory packet reordering bounded, and never indexes incomplete
captures. On a successful archived DB transaction the temporary capture is
removed, leaving only one WAV in the library. On failure the source is
retained to permit manual recovery. Old v0.8 two-leg recordings are NOT
automatically deleted; those are historical data subject to retention rules.

## Explicit opt-in; OFF by default

1. Set `RECORDINGS_ENABLED=true` and a private `RECORDING_DIR` in `.env` for
   the management service. No real passwords in Git.
2. Set `record_calls=true`, `recording_notice_confirmed=true`, and a private
   `record_directory` in `telephony/native_switch.local.json`.
   The capture and archive directories must differ.
3. Run the lab native engine with `--lab --local-events` and matching queue
   and agent database IDs. Both processes must use the same `APP_DB_PATH`.
4. Recording files are mode 0600; library directory 0700, never static served.
   Browsing/playback requires supervisor login and is audit logged.

The operator policy flag is NOT evidence that callers received notice or
consented. A real consent announcement/approval workflow is NOT implemented.

## Limits / security

- **Lab-only UDP/IPv4 G.711**; no TLS, SRTP, RTCP or accurate long-duration
  clock drift correction; no tested Avaya hardware interoperability.
- Timeline approximation uses arrival of the first RTP packet per direction
  plus RTP timestamps. It is not sample-accurate across distinct clocks.
- Audio is ONE stereo file, not a mono mix; normal stereo playback hears both.
- No live listen, whisper, barge, production IVR/DTMF or retention automation.
- Files are not yet encrypted at rest by QueuePilot and there is no HA.
- Max 30 minutes; max archive size 64 MiB. General upload API has a separate
  32 MiB limit and is not the native WAV archive pipeline.
- If the DB/indexing fails, a source file can remain. Alerting/recovery
  and compliance workflows are still needed before any real deployment.

## Verification

```bash
python -m pytest -q
python -m compileall -q app telephony tests
node --check app/static/app.js
```

Tests cover SIP REGISTER/INVITE/RTP/BYE to one WAV, left/right channel
content, RTP gaps/late arrivals, timestamp wrap, truncation, one indexed
row, access controls, duplicate rejection, authenticated playback and
post-commit removal of the temporary source file.
