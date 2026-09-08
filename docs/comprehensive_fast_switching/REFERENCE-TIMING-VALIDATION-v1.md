# Frozen laboratory timing-replay recipe v1

Frozen 2026-09-08 after exploratory analysis of the already acquired 2.475 GHz
block, and before the next 2.475 GHz full-dwell block. This is an additional
phase/timing validation path, not a replacement for protocol-v1 bearing gates.

Implementation: `src/smateway/reference_timing.py` and
`scripts/analyze_reference_timing.py` at commit `6f0518d`. Retain executed source
hashes in each analysis. Host compute instrumentation is part of this revision.

## Fixed recipe

- TX1 only, unchanged fixture, existing power and hardware constraints.
- Use the separately acquired before-reference at the tested rate/bandwidth for
  six complex template levels. Keep weights and the observable mask from the
  independent A-before references. No post-hoc port exclusion or per-port fitting.
- Fit timing only to the preceding 1.000 second. Refit every 0.050 second.
- Use the existing RF periodicity estimator, then circularly align a six-plateau
  reference template plus a zero-level ALL_OFF marker. Omit 5 us at each plateau
  edge and exclude guards. One common complex scale is a nuisance parameter for
  alignment only; it does not recalibrate individual port phases or gains.
- Predict only complete cycles in the following 50 ms window. Discard cycles
  crossing a window boundary. Keep the entire 50 ms as the observation budget,
  including those discards; record actual usable per-port samples separately.
- Produce one averaged six-port transfer per 50 ms window. A failed training or
  prediction window stays failed, and is not compressed out of the time grid.
- Apply unchanged phase/gain closure and repeatability criteria. Require all
  three independent main captures and complete passing reference brackets for
  a repeatable phase/timing result. Report interleaved controls independently;
  any control failure prevents a clean block-level validation claim.
- Keep whole-record reference-labeled fitting and one-second-prefix/open-loop
  fitting as separate diagnostic comparators. Neither can replace a failed
  rolling result. No selection using validation suffixes or later captures.

## Claims this does not support

The template is a **laboratory, known-emitter** transfer pattern. It is not a
source-independent marker detector for a moving unknown emitter. New data can
validate this fixture-specific timing method, not general OTA tracking.

The first prediction follows one second of training. A 50 ms RF window is not
end-to-end latency. Record host replay compute times separately; radio buffering,
network delivery, continuous online scheduling and emitted-output timestamps
remain unmeasured. Do not claim real-time throughput from this offline replay.

Phase/gain acceptance still does not qualify bearings. The unchanged nominal
geometry/legacy ambiguity-gate limitation remains visible. No surveyed angle
accuracy is available, and no failed historical row is retroactively promoted.
