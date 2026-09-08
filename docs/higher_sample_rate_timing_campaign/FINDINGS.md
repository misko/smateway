# Higher sample rate: findings and operating decision

Date: 2026-09-08 UTC. Primary dataset: `tracking-rate-timing-20260908-v2`.

**Do not release a faster tracking profile from this experiment.** Continuous
2 and 5 MS/s acquisition worked, but neither higher sample rate nor wider RX
bandwidth produced an independently qualified shorter dwell in the unchanged
5.8 GHz OTA fixture. The fresh 200 µs control also failed its held-out checks.
That is a failed qualification, not a measured physical speed limit of the PCB.

The [measurement report](README.md) contains the nine PNG figures, individual
run tables, frozen references and validation evidence. It uses only this new
campaign: 36 switching captures, six bracketing controls and 36 static references.
The earlier `v1` pilot is retained separately and is not pooled into these results.

## 1. What the hardware actually did

| Configuration | RX settings | Observed acquisition | Operating conclusion |
| :--- | :--- | :--- | :--- |
| A | 2 MS/s, 1.6 MHz BW | 30.000 RF seconds in 29.983 wall seconds; continuous counters | Available for the experiment |
| B | 5 MS/s, 1.6 MHz BW | 30.000 RF seconds in 29.995 wall seconds; continuous counters | Available for the experiment |
| C | 10 MS/s, 1.6 MHz BW | Rate readback accepted; only 1.750 RF seconds accepted before a sample gap | Not continuous-qualified |
| D | 5 MS/s, 4 MHz BW | All saved static/switched captures admitted at their requested settings | Available; wider bandwidth did not qualify shorter dwell |
| E | 10 MS/s, 8 MHz BW | Excluded by the common 10 MS/s transport gate | Not independently RF-tested |

The C rejection records **250,000 missing samples** in the next block. Its overflow
flag is false: the concrete evidence is a counter/sample gap, not an asserted
hardware overflow flag. Accepted data arrived at approximately 5.882 MS/s before
failure. This locates a limitation in the current continuous acquisition path;
it does not isolate network, firmware, device buffering or host processing as the
sole cause. Discontinuous IQ was excluded, not silently stitched together.

Blocks were capped at 250k samples per channel following pilot transport checks.
Consequently 10 MS/s used 25 ms blocks, while 2/5 MS/s used 50 ms blocks. Total
requested RF observation remained unchanged. This campaign did not qualify a
10 MS/s RAM-ring/burst mode or modify radio firmware.

### Readbacks and fixed conditions

Receiver serial: `104000b29905000e17000800065934759d`, `192.168.1.15`.
Source serial: `104473b80a16000de6ff2000f8a6beca79`, `192.168.1.179`.
The identities were checked before control; no other radio was substituted.
Board: `stm32c011-4c0055000950313950363920`.

| RX output rate | BBPLL | ADC | R2 | R1 | RF | FIR readback |
| ---: | ---: | ---: | ---: | ---: | ---: | :--- |
| 2 MS/s | 1024 MHz | 64 MHz | 32 MHz | 16 MHz | 8 MHz | RX 128 taps, decimation 4, enabled |
| 5 MS/s | 1280 MHz | 160 MHz | 80 MHz | 40 MHz | 20 MHz | RX 128 taps, decimation 4, enabled |
| 10 MS/s | 1280 MHz | 320 MHz | 160 MHz | 80 MHz | 40 MHz | RX 128 taps, decimation 4, enabled |

These are complete receiver-configuration comparisons, not pure oversampling of
an unchanged digital filter. The exposed FIR readback is a tap-count/decimation
summary, not a recovered coefficient vector or measured impulse response.

TX1 stayed at 2 MS/s, 1.6 MHz TX bandwidth, -35 dB hardware gain and DDS scale
0.25; its nominal 100 kHz tone read back as 100,007 Hz. TX2 DDS scales remained
zero. Both RX gains were manual 60 dB. The LO centre was 5.800 GHz throughout.
The existing conducted RX1 reference and OTA C6-to-RX2 arrangement was unchanged.

## 2. Higher sample rate did not remove the dwell penalty

The following is the **common comparison recipe**, native-refined timing with
5 µs trimmed from both ends. Values are the first tested RF integration budget
meeting ≤10° weighted phase RMS. They do not include independent-closure failure
or live delivery latency; a number in this table is not a qualified operating mode.

| Dwell | A: 2 MS/s, 1.6 MHz | B: 5 MS/s, 1.6 MHz | D: 5 MS/s, 4 MHz |
| ---: | :--- | :--- | :--- |
| 25 µs | No RMS pass in decoded repeats; one decode failure | 200.4 ms in one repeat; two decode failures | No RMS pass in decoded repeats; one decode failure |
| 50 µs | 200.3 / 400.6 ms / no RMS pass | 100.4 / 100.5 ms / no RMS pass | No RMS pass in all three repeats |
| 100 µs | 100.4 / 50.2 / 50.2 ms | 50.2 / 50.2 / 50.2 ms | 100.4 / 200.9 / 200.0 ms |
| 200 µs | 20.9 / 50.8 / 50.8 ms | 50.8 / 50.8 / 20.9 ms | 50.8 / 50.8 / 50.8 ms |

"No RMS pass" means none at the tested budgets through approximately 400 ms,
not proof that longer integration can never help. The budgets are discrete and
rounded to complete measured cycles. Differences of a few microseconds from
clock/cycle rounding are not meaningful performance wins.

At the approximately 20 ms budget, 200 µs dwell gave about **9.4–13.1° RMS**
across these configurations/repeats; 100 µs gave about **12.2–20.6°**. The data do
not show a robust advantage from widening 5 MS/s RX bandwidth to 4 MHz.

Fixed overhead explains part of the penalty. For the common trim recipe:

| Dwell | Nominal C6 cycle | Nominal scans/s | Usable time per port per cycle | Per-port duty fraction |
| ---: | ---: | ---: | ---: | ---: |
| 25 µs | 450 µs | 2222 | 15 µs | 3.33% |
| 50 µs | 600 µs | 1667 | 40 µs | 6.67% |
| 100 µs | 900 µs | 1111 | 90 µs | 10.00% |
| 200 µs | 1500 µs | 667 | 190 µs | 12.67% |

Each cycle retains six 20 µs guards and a 180 µs marker. More physical scans do
not automatically provide more useful signal integration. The matched-active-time
figure partly brings the curves together, but short-dwell outliers and failures
remain. Overhead alone is therefore not a complete explanation.

## 3. Independent references expose the important weak-port problem

ANT1's initial transfer magnitude was about 0.000504, versus 0.004573 for ANT2:
approximately **19.2 dB weaker** in this fixture. Its 10 ms static phase RMS was
32.2° at A, 44.0° at B and 30.4° at D. The other five ports were roughly 3–7°.

ANT1 narrowly passed the predeclared -20 dB relative-amplitude observability
threshold in the independent A reference. Its weight and observable status were
then frozen across all configurations. We did **not** drop it after seeing failures.
The small power weight lets aggregate phase RMS look reasonable while the
maximum-per-port phase/gain gate still exposes a bad arm.

In the 200 µs controls, ANT1's measured gain differed from the initial A static
reference by as much as -5.7 dB for the common comparison recipe. The other five
ports' phase errors were generally much smaller. This demonstrates a weak and
variable reference-relative path, but does not by itself distinguish a propagation
null, antenna/cable/connector issue, receiver response, leakage, switching effects
or reference uncertainty. The before/after static figures further constrain this;
they do not remove the need for a controlled fixture.

The final static drift check passed at A and D, but failed at B: ANT1 changed by
**+1.78 dB and +10.55°** after common-phase removal. A had a maximum 0.76 dB/
7.25° drift; D had 0.68 dB/7.36°. This additional independent failure reinforces
the decision not to promote a higher-rate profile.

The training-only selector retained A/200 µs with native refinement and zero
leading discard (5 µs trailing trim). It failed **both** independent holdouts:

| Holdout | RMS integration budget | Weighted phase bias | Maximum port phase bias | Maximum gain error | Result |
| ---: | ---: | ---: | ---: | ---: | :--- |
| 2 | 50.82 ms | 0.38° | 1.07° | 2.14 dB | Fail |
| 3 | 50.83 ms | 2.88° | 35.48° | 1.58 dB | Fail |

We therefore did not promote the zero-discard recipe. A small whole-vector RMS
is not sufficient evidence for a stable calibrated six-port array.

## 4. What was implemented and what remains gated

Implemented and exercised: serial-pinned captures, separate RX rate/bandwidth,
fixed TX settings, equal-duration observations, metadata continuity rejection,
bounded-memory IQ analysis, native-rate timing refinement, independent settled
references, frozen weights/mask, independent holdouts, exact selector flash
readback/restore, and regenerable PNG/CSV reporting with a raw-hash audit.

The four experimental firmware schedules were built and flashed on the named
board. They were not left running as a production deployment: the original full
16 KiB bench image was restored byte-for-byte, with SHA-256
`4eddf1e07cda11b78800a80a2935790f554913e6d2b70e024e46c68c263c888f`.
The final live mailbox check confirmed lease-free ALL_OFF, and both radios'
DDS scales were read back as zero with TX gains at -80 dB.

No shorter-dwell configuration passed the expansion gate. The new TX2 sentinel
campaign and new 1 MHz frequency map were therefore **not run**. Causal tracking,
initial lock/relock latency, aligned GPIO timing, ±0.5/±1 µs sensitivity sweeps,
and filtered-decimation replay remain follow-up work, not measured claims here.
Native refinement searches ±5 µs; one of the 38 decoded captures reached a search
boundary. RF-inferred boundaries cannot establish intrinsic PCB switching time.

The first pilot uncovered an exact-tag capture-adoption bug: a control name could
match a training capture prefix. It was stopped, the original firmware restored,
the matching fixed and regression-tested, and this entire primary campaign
restarted with new independent references and captures. Pilot measurements were
not relabeled as independent validation.

## 5. The next useful experiment

**Use a strong, stable conducted fixture before another dense OTA frequency sweep.**

1. With TX muted, reconnect the previously verified attenuated splitter fixture
   feeding RX1 and the six active PCB ports. Confirm the exact wiring and available
   attenuation first; do not connect the current OTA source directly at an assumed
   safe level. Keep unused branches appropriately terminated.
2. At 5.800 GHz, set a safe level using ADC headroom and repeat settled ANT1/ANT2
   measurements. Require a stable, comfortably observable ANT1 reference before
   beginning the timing ladder. Equal splitter phases are unnecessary: each
   configuration gets its own measured static reference vector.
3. Compare A and B at 200/100/50/25 µs with fixed guard/marker, independent
   controls and held-out repeats. Add wider bandwidth only if those comparisons
   support it. This separates the OTA weak-path problem from switched-path timing.
4. If the conducted result passes, return to the installed array, investigate the
   ANT1 antenna/cable/position with controlled swaps, and acquire the empirical
   OTA manifold. If conducted short dwells still fail, measure synchronized GPIO
   and RF transients before changing guard timing or blaming the PCB switch.
5. Address the 10 MS/s acquisition bottleneck separately. A burst buffer, alternate
   transport path or firmware change needs its own continuity/dead-time benchmark;
   none should be hidden inside a receiver-bandwidth comparison.

This is an experiment order, not a request to loosen the phase/gain gates. The
existing PCB calibration and earlier frequency map remain historical evidence;
this failed fresh qualification does not establish a new deployed accuracy guarantee.

## 6. Reproduce and verify

Offline report regeneration, with all admitted raw samples rehashed:

```bash
PYTHONPATH=src .venv/bin/python scripts/render_rate_timing_report.py \
  --campaign-root /srv/bulk/samteway/lab-data/tracking-rate-timing-20260908-v2 \
  --output docs/higher_sample_rate_timing_campaign --verify-raw
```

The acquisition entry point is `scripts/run_rate_timing_campaign.py --help`.
Actual RF/flash stages require explicit acknowledgement flags and the pinned
hardware/fixture; report regeneration never enables RF.

All **78** admitted raw captures were rehashed: **14,592,000,000 bytes**, with
one consistent captured-source contract. Hash-matching copies of the executed
capture/analysis sources are retained under the raw campaign's `captured-source/`.
Post-campaign hardening additionally rejects inconsistent counter spans and
ensures a source-serial mismatch cannot cause cleanup writes to an unrelated
radio; it also checks the frequency/configuration identity of the frozen A
weight references. These changes are regression-tested separately from the
primary dataset.

The final muted smoke capture encountered an IIO **`[Errno 61] No data available`**
after 11 accepted blocks at 2 MS/s. It failed closed and retained its evidence.
Three fresh 30-second checks then passed in the order **A, B, A** (2, 5, 2 MS/s),
with 60M, 150M and 60M samples per channel respectively. This establishes
successful restarted acquisition, not an error-free long-running reliability
guarantee. No automatic retry replaced a phase holdout or erased the failure.
The separate [verification record](data/verification.json) binds all four checks
and final read-only radio/selector status; none is pooled into the phase results.

All **52 targeted tests** covering timing, capture identity, reports,
firmware profiles and bench control passed. The broader
suite was also attempted: **1245 passed, 437 failed, 1 skipped**. Representative
legacy failures reproduce independently of the new tests: missing `/home/pi`
and incompatible temporary-filesystem admission, frozen Python 3.11.2 artifact
contracts on this Python 3.11.16 host, and an existing floating-point boundary
assertion (`0.8000000000000007` versus an inclusive `0.8` limit). The full suite
is not green; the remaining legacy failures were not all individually triaged.
