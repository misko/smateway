# Comprehensive fast-switching campaign — protocol v1

Frozen before fresh capture on 2026-09-08. Protocol ID:
`comprehensive-fast-switching-20260908-v1`. Starting repository commit: `34729ba`.
The machine-readable contract is [protocol-v1.json](data/protocol-v1.json).
Operational readiness is separate evidence, never inferred from an old profile.

## Questions and independent result classes

Reproduce the September 3 bearing examples and September 8 independent-reference
failure, then measure 2.4/5.8 GHz coverage. Report separately: acquisition integrity;
historical self-referenced phase RMS; independent reference closure; model-accepted
bearing yield/repeatability; surveyed angle accuracy (only with measured truth);
and causal replay versus actual live delivery. No single phase gate substitutes
for a bearing result. Historical successes and failures are both reproduction targets.

Hardware is restricted to receiver serial `104000b29905000e17000800065934759d`,
source serial `104473b80a16000de6ff2000f8a6beca79`, and selector UID
`stm32c011-4c0055000950313950363920`. Resolve IPs by serial, retain identity/readback,
and restore the original full selector image exactly. No radio firmware upgrade.

## Readiness and frequency coverage

The user must freshly confirm wiring/antenna positions and band suitability before
OTA. Jurisdiction or an appropriate contained-test authorization must be recorded.
Do not silently adopt the old `current_fixture_ready` flags. The installed geometry
is measured/confirmed separately: the old 5.8 GHz reference assumed 24.9827 mm radius,
whereas the *proposed* 2.4 GHz profile assumes 54 mm. A frequency change is not a
physical array rebuild. If unchanged, use the same physical coordinates in both
bands and report the smaller electrical aperture at 2.4 GHz.

Engineering intent covers every integer-MHz centre in 2400–2500 and 5725–5875 MHz,
including explicit excluded/blocked rows at boundaries. A provisional 1 MHz edge
margin leaves 2401–2499 and 5726–5874 MHz before jurisdiction/occupied-signal checks.
This margin is not an emissions certification or permission to transmit. In the US,
[47 CFR 15.249](https://www.ecfr.gov/current/title-47/chapter-I/subchapter-A/part-15/subpart-C/subject-group-ECFR2f2e5828339709e/section-15.249)
has different lower-band limits (2400–2483.5 MHz), field-strength and out-of-band
requirements; ISM allocation alone is insufficient. Regulatory source checked
2026-09-08; eCFR displayed currency through 2026-09-03.

Muting and receive-only checks may proceed before OTA readiness. The two bands
advance independently: a failed 5.8 GHz improvement does not cancel a feasible
2.4 GHz baseline. Keep measured, failed, unobservable, policy-excluded and
physically-blocked grid points distinct; never label interpolation as measurement.

## Stages

0. Rehash and reprocess the exact historical raw records, preserving their
   original algorithms, geometry, LUT, 5 µs trim and grouping policies. Reproduce
   5.811 GHz TX1/200 µs/16 cycles; 5.750 GHz TX2/200 µs/2 cycles;
   5.775 GHz TX2/100 µs/8 cycles; and the negative 5.800 GHz TX1 case.
   Separately replay the September 8 A/200 µs training and both holdouts.
1. Fresh muted continuity at 2 and 5 MS/s: two independently restarted 30-second
   captures at 2.450 and 5.800 GHz. Admit 10 MS/s only after two fresh uninterrupted
   30-second captures; preserve failures and continue the lower rates regardless.
2. After readiness, reproduce the named OTA conditions with three *new* independent
   four-second captures each. Keep this reproduction block separate from fresh
   screening/selection and validation. Bracket with independent references.
3. Screen TX1 using A (2 MS/s, 1.6 MHz), B (5 MS/s, 1.6 MHz), D (5 MS/s, 4 MHz),
   adding C/E only if transport qualifies. Dwell ladder: 25/50/100/200/1000 µs.
   Each switched capture contains four seconds of acquired IQ. Use three rounds,
   shuffled configuration order, reversed/permuted dwell order, and separate
   A/200 µs and long-dwell controls. Preserve failures without replacing a holdout.
4. Before/after each source/frequency/configuration block, obtain settled per-port
   reference evidence. TX1 can use separately acquired true static two-second
   port captures because same-emitter phase cancels. TX2 requires a coherent
   continuous reference: separate static acquisitions cannot be assumed to share
   a cross-frequency phase origin. Evaluate a 20 ms-dwell continuous long-reference
   profile, with first/last 1 ms excluded, as a labeled quasi-static reference;
   first check it against true-static TX1. If not established, mark TX2 independent
   closure unavailable rather than inventing port offsets.
5. Each band's selected candidate and A/200 µs baseline proceed to independent
   frequency holdouts and the permitted 1 MHz grid, even if the result is only a
   diagnostic baseline map. Three independent captures per measured grid condition,
   with reference bracketing and interleaved controls. Dense mapping does not
   automatically qualify a frequency that failed independent closure.
6. Causal replay trains timing/frequency on the preceding one-second preamble,
   freezes it and processes unseen windows without future samples. Report the
   startup cost, drift/relock rejection and buffer-availability latency separately.
   Offline scheduling of stored IQ is not live delivery. A live claim additionally
   requires a streaming run with measured read/compute/emit timestamps.

Screening frequencies (subject to readiness and authorization): 2405, 2425, 2450,
2475, 2495 MHz; 5726, 5750, 5775, 5800, 5811, 5825, 5850, 5874 MHz.
Frequency holdouts: 2413, 2438, 2463, 2481 MHz; 5758, 5764, 5773, 5777,
5843, 5847, 5858 MHz. Excluded frequencies remain explicit in the ledger.

## Frozen analysis policies

Historical reproduction retains its original equal-port bearing solver and
self-derived phase weights. New independent analysis freezes power weights and
the -20 dB observable mask from a separate A reference at each source/frequency;
requires at least four observable ports; and always reports all six arms.
No post-hoc ANT1 removal. If reference uncertainty cannot support a threshold,
report an inconclusive reference, not a board failure or a pass.

Independent closure: ≤5° weighted relative phase bias, ≤10° maximum observable
port bias, ≤1 dB maximum observable gain error, removing only a single common
phase rotation. Repeatability: ≤10° weighted phase RMS, at least eight disjoint
groups. Report raw RX1-referenced phase, spatial-gauge phase and gain individually.

Bearing model gates retain score ≥0.5, ambiguity margin ≥1 dB with peaks separated
by 20°, and residual phase RMS ≤45°. The new repeatable-bearing criterion is
≥95% model-valid groups and ≤5° circular bearing RMS with ≥30 groups, independently
in all three captures. Report RMS for all groups and accepted groups separately,
as well as rejection counts, per-port errors and between-capture centre shifts.
These are predeclared engineering criteria, not surveyed accuracy guarantees.

Use common 1/2/5/10/20/50/100/200/400 ms observation budgets rounded to full cycles,
plus the original powers-of-two cycle counts. Compare matched usable per-port
integration. Keep the 20 µs guard and 180 µs marker fixed. Common comparison trim
is 5 µs on each end; training-only trim candidates are 0/1/2/5/10/15/20/30 µs
leading with 5 µs trailing, omitting impossible windows. Freeze a selected recipe
before two holdouts. Selection is separate per band/source and criterion; a
failed validation requires genuinely new held-out captures for any revised recipe.

Investigate ANT1 with repeated true-static ANT1/ANT2 references and long-versus-short
dwell closure at fixed frequency. Cable/antenna swaps and a verified attenuated
conducted splitter fixture require actual operator confirmation and fresh references;
never merge those data with the unchanged OTA fixture. Do not infer a physical
root cause from a phase floor or weak-port power alone.

## Evidence and reporting

New data live in a new bulk-storage campaign root. Bind the protocol, executed
sources, fixture identity, geometry, LUT, raw hashes, exact counters, firmware
readbacks and restoration. Sessions are bounded; hardware identity and stream
continuity fail closed. A failed attempt remains its own record; no silent retry
or control-as-training adoption. Commit the protocol before fresh RF collection;
later changes use an explicit amendment and cannot rewrite old acceptance rules.

Publish one comparison report with provenance-separated reproduction and fresh
results, band/frequency × dwell/window tables, bearing yield/RMS/ambiguity figures,
independent phase/gain matrices, ANT1 diagnostics, causal-versus-retrospective
latency, measured coverage and exclusions. Label every operating row diagnostic,
repeatably validated, or demonstrated live. Report raw storage and acquisition
status honestly; a queued stage is not an RF scan in progress.
