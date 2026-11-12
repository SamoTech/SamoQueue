# QueuePilot Native IVR — v1.0 laboratory milestone

## Current implementation

The standalone QueuePilot SIP engine can **answer a trusted inbound SIP call before calling any agent**, play a local greeting as G.711 RTP, accept an **RFC 4733 telephone-event end packet**, select a configured queue based on a single DTMF digit, and dispatch the call using its own ACD to a SIP phone registered to QueuePilot. No Asterisk, FreeSWITCH, AES, CMS, or Avaya IVR is involved.

Tested flow:

```text
Simulated trusted SIP trunk -> INVITE with G.711 and telephone-event
QueuePilot -> 100 Trying, 200 OK (SDP answer with telephone-event)
Caller -> ACK
QueuePilot -> WAV-to-G.711 greeting RTP (8 kHz mono)
Caller -> telephone-event DTMF (end flag) e.g. 2
QueuePilot -> validated queue mapping -> ACD -> registered IP phone INVITE
Phone -> 200 OK -> QueuePilot ACK -> anchored RTP between trunk and phone
BYE -> single two-channel WAV for caller LEFT and agent RIGHT (if opted in)
```

**Recorded audio starts when the agent leg connects**, after the IVR selection. There is one stereo conversation WAV, not a per-leg WAV pair. Greeting playback is not recorded in this file. Do not advertise legal consent announcements: no verified end-to-end consent workflow exists yet.

## Configure in an isolated lab

1. Prepare an **actual spoken welcome/IVR message** recorded as 8000 Hz, signed PCM16, mono `.wav`, max **30 seconds**. It must be locally available to the SIP process. QueuePilot does not generate speech or accept MP3/OGG in the native IVR runtime.
2. Copy `telephony/native_switch.example.json` to ignored `telephony/native_switch.local.json`; set all unique SIP credentials, bindings, DID routes, and allowed loopback peers.
3. Add an `ivr_menus` entry to that **local** JSON, for example:

```json
{
  "ivr_menus": [
    {
      "did": "6000",
      "prompt": "prompts/queuepilot-welcome.wav",
      "digits": { "1": "support", "2": "sales" },
      "timeout_seconds": 8,
      "max_attempts": 2,
      "fallback_queue": "support"
    }
  ]
}
```

This is a **fragment**: insert it in the main local JSON object. Paths are relative to the JSON file's directory, or absolute. Each destination queue name must already appear as a `routes[].queue` value; a DID menu must match `routes[].did`. All other attributes and agent skills must be configured as before. The sample checked-in JSON has `"ivr_menus": []` (IVR disabled). The running dashboard's graphical IVR **draft editor is not yet wired to this native runtime**. For now, configure via the private JSON on the lab host.

4. Start with:

```bash
python -m telephony.native_switch --config telephony/native_switch.local.json --lab
```

5. Originate a lab SIP INVITE offering `RTP/AVP` G.711 PT 0 or 8 and a dynamic RFC4733 payload such as `101` with `a=rtpmap:101 telephone-event/8000`. Without the negotiated telephone-event mapping the IVR route **fails closed with 488**. DTMF is accepted only from the approved RTP peer, only once per completed (end-flagged) RTP telephone-event (SSRC, timestamp, event ID), and only when the call is in IVR state. Invalid digits cause a replay up to `max_attempts`; at the limit the configured fallback queue is used or the call is hung up.

## Security and compatibility limitations

- **LAB ONLY**. No tested real Avaya Aura SIP Trunk or physical Avaya phone. Endpoint must be an independently registerable SIP phone, not H.323-only/locked Avaya firmware.
- The current SIP stack supports limited UDP IPv4 and G.711 only: no complete SIP retransmission/dialog machinery, SIP-TLS/SRTP, RTCP, DTMF via SIP INFO, early-media robustness, anti-fraud controls or supervised deployment.
- The prompt is a local PCM WAV, **not** TTS, speech recognition, a drag-and-drop live-flow deployment engine, multi-level menus or holiday/scheduling rules.
- No complete caller notice/consent enforcement, personal-information protection workflow, IVR abandonment metrics in the report DB, encrypted-at-rest recordings or reliable event replay.
- Calls already answered for IVR must **not** receive a second SIP 200 OK when an agent answers. The test suite verifies this via call state and live UDP/RTP bridging.
- The greeting is sent directly from QueuePilot to the authorized trunk RTP address; no phone is called until a valid selection/fallback is made.
- The current system requires production-grade SIP timers, codec and Avaya interop testing, prompt management, hold music, load and security testing before real traffic.

## Verification

```bash
python -m pytest -q
python -m compileall -q app telephony tests
node --check app/static/app.js
```

The suite contains a real local UDP/SIP/RTP test of `INVITE → 200/ACK → prompt packets → RFC4733 digit → agent INVITE/ACK → two-way RTP → BYE → one stereo WAV`, plus malformed prompt, queue allowlist, dynamic telephone-event negotiation, invalid-digit replay/fallback, and missing-DTMF failure cases. Passing local tests is **not** an Avaya interop or production-ready certification.
