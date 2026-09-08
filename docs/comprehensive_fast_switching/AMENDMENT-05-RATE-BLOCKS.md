# Rate-block implementation binding — 2026-09-08

This implements the existing A/B/D protocol, without changing acceptance thresholds.
It is recorded before the first fresh B/D switched block in this campaign.

Each B (5 MS/s, 1.6 MHz bandwidth) or D (5 MS/s, 4 MHz bandwidth) block acquires
separate two-second static references for every port at **both A and the tested
configuration**, before and after the block. Power weights and visibility remain
frozen from the independent A-before references. Phase/gain closure uses the
tested configuration's own references. After/before drift is evaluated with the
same frozen A weights and all six port errors retained. Missing or failed
references cannot be replaced or counted as a pass.

Three independently restarted four-second captures per requested dwell are
randomized within rounds. Each round also includes separate A/200 us and A/1000 us
controls. Exact full selector-image restoration precedes after references and
also runs on switched-capture failure or interruption. Source power, the
serial-pinned radios, current fixture and guards are unchanged. Acquisition
headroom/continuity failures stop that block and remain recorded failures.

B is admitted on the already recorded two independent 30-second continuous
captures in each band. Its earlier dropout remains visible. D has the same
5 MS/s transport load but a different receive filter: its own static references
and sample/clipping checks are required, not inferred from B. C/E (10 MS/s)
remain unqualified and are rejected by this runner.

The 2.475 GHz A block has a complete before/after bracket: maximum relative phase
drift 0.970 degrees and maximum gain drift 0.593 dB, passing the frozen closure
limits. All three 1 ms dwells pass six-port phase/gain closure. The legacy bearing
ambiguity gate still fails; this is not a validated bearing deployment setting.
The 200 us port-label/timing discrepancy is being investigated separately; no
decoder adjustment or threshold change is included in this rate-block amendment.
