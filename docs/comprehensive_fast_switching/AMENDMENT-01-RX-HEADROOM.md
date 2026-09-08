# Protocol v1 amendment 01 — receive headroom

Recorded 2026-09-08 after the first fresh muted acquisition and before any new
OTA capture. The original protocol remains unchanged.

The initial 2.450 GHz A/60 dB muted capture had continuous counters for 60 million
samples per channel, but RX1 reached 2048 component counts and 75 samples met
the full-scale test. RX2 peaked at 346 counts with zero full-scale samples.
Evidence: `tracking-comprehensive-20260908-v1/captures/` run
`preflight-2450000000-A-20260908T154819.481609Z`.
That condition remains failed; this does not establish the interfering signal's cause.

Authorize a **receive-only** gain ladder of 50, 40 and 30 dB at 2.450 GHz,
30 seconds per setting, stopping descent once two independent checks at a gain
have no full-scale samples. Use the highest tested gain with both zero clips
and peak component magnitude below 1600 counts for candidate headroom. Confirm
again under the actual RF stimulus before freezing it across A/B/D within a band.
If source-enabled headroom fails, create another explicitly labeled gain block;
do not combine gains or references or silently lower the gain mid-capture.

Keep 5.8 GHz at its historical 60 dB unless its own headroom check fails. Gain
selection is per-band; it does not alter historical reproduction or quality gates.
Record requested/readback gains and per-block clipping/peak statistics. No new
transmitter authorization, power increase, antenna change or threshold relaxation
is introduced by this amendment.
