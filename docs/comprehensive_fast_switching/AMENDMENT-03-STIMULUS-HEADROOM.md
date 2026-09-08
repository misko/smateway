# Protocol v1 amendment 03 — source-enabled receive headroom

The first bounded TX1/ANT1 2.450 GHz capture at 50 dB receive gain failed the
full-scale test on RX2 (peak 2048 counts; RX1 peak 401). The independent muted
checks at that gain remain transport/headroom evidence only, not a qualification
under stimulus. Preserve run `stimulus-2450000000-A-g50-ANT1-20260908T162926.932557Z`.
Exact final source muting passed.

Before selecting calibration references, screen receive gain downward at
40, 30, 20, 10 and 0 dB, keeping TX settings unchanged. At each candidate, take
bounded two-second static captures at all six current C6 ports. Stop that candidate
on acquisition failure, clipping or peak component magnitude at least 1600.
The highest tested candidate with two independent complete six-port rounds passing
those headroom checks may be frozen for the lower-band screening block. All
captures from rejected gain candidates remain headroom diagnostics and are not
substituted for held-out phase/bearing validation.

Apply the same procedure independently at 5.800 GHz, starting at its historical
60 dB and descending in 10 dB steps only if its source-enabled checks fail. Fresh
frequencies/configurations still require their own headroom check; gain blocks
and their references must never be mixed. If no gain passes, stop that RF block.
This amendment decreases receive gain only: it introduces no RF power increase,
physical change, relaxed headroom threshold or geometry-qualified bearing claim.
