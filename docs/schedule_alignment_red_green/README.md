# Schedule alignment red/green verification

This work fixes the coupled cycle/marker false lock found in artifact
`be64aa4b22f9436c8ff25547a3589b98` and makes alignment quality observable.

## Failure reproduced

The former greedy search retained one best coarse `(cycle, marker)` pair and
refined only a ±2 ms marker neighbourhood around it. A small coarse-cycle error
accumulated across 25 frames, so the correct joint candidate was never tested.

| Result | Cycle | Marker | Combined score | Explained fraction |
|---|---:|---:|---:|---:|
| Legacy false lock | 384.6 ms | 47.0 ms | 0.797035408 | 0.798437416 |
| Correct alignment | 384.6 ms | about 149 ms | 0.999999648 | 0.999999949 |

The 80 MB dual-RX IQ artifact is represented in the test suite by a 142 KB
hashed reduction containing only coherent transfer bins and their validity
mask. The fixture retains source hashes and the exact reduction provenance.

## Search policies

- `exhaustive_fine` evaluates the complete fine cycle/marker Cartesian grid and
  is the offline correctness oracle.
- `global_refined` evaluates every fine cycle on a global marker grid, then
  refines several distinct basins.
- `transition_seeded` uses the independently decoded unique dwell schedule and
  searches only its quantified uncertainty neighbourhood. Strict decoding is
  required.

All modes use the same candidate evaluator. On devpi they converged to the same
real-capture plateau:

| Mode | Candidates | Runtime | Cycle | Marker | Score |
|---|---:|---:|---:|---:|---:|
| Exhaustive fine | 79,130 | 713.85 s | 384.6 ms | 148.600 ms | 0.999999648 |
| Global refined | 8,075 | 78.03 s | 384.6 ms | 148.600 ms | 0.999999648 |
| Transition seeded | 231 | 1.91 s | 384.6 ms | 148.688 ms | 0.999999648 |

The transition decoder estimated 149.688 ms. Its 1 ms difference from the
phase-fit plateau is within coherent-bin quantization. Transition-seeded search
is therefore the production default; the other modes are explicit cross-checks.

## Reported quality

Each result now preserves:

- explained and residual fractions;
- residual and null energy;
- coherent and cycle-deviation energy;
- detection ratio and strength;
- even/odd agreement and cycle coherence;
- combined score and selected-bin count;
- evaluated/valid candidate counts and search resolution;
- a runner-up from a distinct evaluated basin and score margin;
- agreement with independently decoded transition timing.

Reference-transfer reanalysis writes
`fast20-reference-transfer-v2.json` by default so historical analysis is not
overwritten. `--promote-canonical` publishes the same document to the legacy
filename only when its quality gate passes.

## Test strategy

The test suite includes:

- the captured false-lock regression;
- exact noiseless tone component checks near numerical unity;
- exhaustive-grid coverage and optimized/oracle equivalence;
- deterministic noise degradation and no-signal rejection;
- transition timing, jitter, phase-wrap and compatibility tests;
- JSON schema, weak-fit and decoder-disagreement fail-closed tests;
- end-to-end reference-transfer phasor recovery.

Machine-readable benchmark evidence is in
[`data/false-lock-verification.json`](data/false-lock-verification.json).

