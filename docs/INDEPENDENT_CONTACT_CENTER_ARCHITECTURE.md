# QueuePilot independent contact center — architecture history

**11 November 2026 — v0.5 supersedes the v0.4 Asterisk/ARI suggestion.**

The desired product has **NO third-party PBX, Asterisk, FreeSWITCH, or external media server** between Avaya Aura and the agents. QueuePilot itself will implement the SIP call switch (B2BUA), phone registration/location, ACD/IVR, RTP bridging, recording and supervisory monitoring. Avaya serves as a SIP call delivery source only.

The current tested ACD is a decision model, not a connected SIP/RTP engine. For the full design, phone compatibility constraints, implementation status, and exact acceptance tests, see:

**[NATIVE_SIP_CALLCENTER_ARCHITECTURE.md](NATIVE_SIP_CALLCENTER_ARCHITECTURE.md)**

Old v0.4 Asterisk configuration examples were removed because they do not meet this requirement. Older Avaya AES/WTI modules are retained for historical compatibility but are not dependencies of the new architecture.
