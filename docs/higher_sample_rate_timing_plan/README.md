# Higher sample rate, shorter dwell: experiment plan

Date: 2026-09-08 UTC. Status: execution plan; see the
[implemented campaign report](../higher_sample_rate_timing_campaign/README.md)
for measured results, exclusions and the expansion decision. The protocol below
retains the proposed matrix so unperformed stages remain explicit.

Find the shortest useful C6 dwell at 2, 5, and 10 MS/s, and identify whether any
improvement comes from receiver filtering, timing estimation, or phase estimation.
The decision metric is time to a reliable six-port vector, with physical scan rate
and receiver-to-application latency reported separately.

The [previous campaign](../fast_tracking_timing_campaign/README.md) selected
200 µs at 2 MS/s and 1.6 MHz receive bandwidth. Shorter schedules executed, but
25/50/100 µs passed the phase gate at 0/1/6 of seven TX1 sentinel frequencies;
200 µs passed all seven. That is a measured operating point, not a demonstrated
physical limit of the PCB switch.

## 1. The controlled comparison

| ID | RX sample rate | RX bandwidth request | Comparison it enables |
| :--- | ---: | ---: | :--- |
| A | 2 MS/s | 1.6 MHz | Fresh control matching the previous campaign |
| B | 5 MS/s | 1.6 MHz | Higher output rate at the same requested analog bandwidth |
| C | 10 MS/s | 1.6 MHz | Higher output rate at the same requested analog bandwidth |
| D | 5 MS/s | 4 MHz | Wider receiver response, compared with B |
| E | 10 MS/s | 8 MHz | Wider receiver response, compared with C |

These are requested configurations, not already verified readbacks. Record the
actual RX clock chain, bandwidth, FIR enable/coefficients and decimation settings.
Changing sample rate can change digital filters even when analog bandwidth stays
fixed, so A/B/C measures the complete receiver configuration, not pure ADC
oversampling. ADI documents that analog and digital filter behavior depends on
bandwidth, sample clocks and decimation in its
[receiver/filter overview](https://wiki.analog.com/resources/eval/user-guides/ad9361).
Use the [ADI filter design documentation](https://analogdevicesinc.github.io/documentation/solutions/reference-designs/fmcomms2/software/filters/filters.html)
if a requested configuration needs a new matched filter profile; do not silently
reuse coefficients designed for another rate.

Keep the source at its established 2 MS/s, 1.6 MHz TX bandwidth, DDS tone settings,
gain and physical position. Only RX settings change in this matrix. Start with
TX1 at 5.800 GHz; introduce TX2 after a candidate survives TX1 validation.

| Fixed item | Campaign setting |
| :--- | :--- |
| Receiver | Serial `104000b29905000e17000800065934759d`, last recorded `192.168.1.15` |
| Source | Serial `104473b80a16000de6ff2000f8a6beca79`, last recorded `192.168.1.179` |
| Board | `stm32c011-4c0055000950313950363920` |
| C6 clockwise order | ANT1, ANT2, ANT4, ANT8, ANT7, ANT5 |
| Dwell candidates | 25, 50, 100, 200 µs |
| Transition guard / marker | Existing experimental 20 µs / 180 µs profiles |
| RX / TX hardware gain | Existing manual RX 60 dB / TX -35 dB baseline, subject to clipping check |
| Capture | Continuous dual RX, 4 seconds of valid acquired samples per switching run |
| Hardware scope | Resolve the recorded serials through PPU; never substitute another radio |
| Initial fixture | Existing TX1 conducted RX1 reference and OTA C6-to-RX2 fixture |

During execution, rediscover addresses and record the actual wiring before using
these historical settings. Keep antenna/cable placement, receiver gain and all
guard/marker timings fixed across a comparison block. Preserve the existing
source-mute and exact selector-backup/restore procedure. The short transition
guard retains its existing experimental status.

More samples alone do not guarantee better phase precision at fixed signal power,
observation duration and effective bandwidth. The experiments must measure the
benefit, including any change in noise and transient response.

## 2. Make the measurement capable of answering the question

Implement these changes before capture:

| Component | Required change | Verification |
| :--- | :--- | :--- |
| Capture configuration | Separate RX sample rate, RX bandwidth, TX settings and capture duration; derive sample counts from duration | Every rate captures the same 4 seconds; source settings remain fixed |
| Receiver readback | Check requested rate/bandwidth and record clock/filter configuration where exposed | A rejected or unavailable mode remains explicit; no silent fallback |
| Capture blocks | Start with 50 ms blocks: 100k/250k/500k samples per channel at 2/5/10 MS/s | Exact ABI-2 shapes, counters and stream identity |
| Resource use | Process IQ in chunks and use a coarse stream only for initial schedule discovery | High-rate refinement avoids full-capture complex128/FFT memory expansion |
| Runtime timing | Separate initial synchronization/frequency training from causal tracking of later samples | Full-record retrospective fits are labeled offline; lock time is measured separately |
| Timing decoder | Retain the existing decoder as a baseline; refine boundaries locally at native sample resolution with fractional offsets | Synthetic known-edge tests at all three rates, clock offsets, leakage and weak markers |
| TX2 synchronization | Preserve rejection of the approximately 200 kHz pilot separation when changing timing analysis | No false improvement from admitting the other tone |
| Phase estimator | Retain legacy cross-product averaging; evaluate per-dwell normalized correlation on the same IQ as a separate analysis variant | Estimator changes and acquisition-rate changes have separately labeled results |
| Settling analysis | Extend the search across the complete active dwell, using explicit window support and receiver delay | Every reported window lies inside that port's dwell |
| Analysis provenance | Store source/configuration hashes, timing/filter choices, sample masks and reference IDs | Regeneration produces the same derived tables |

The current decoder reduces data to 1 µs bins and smooths over 5 µs. That smoothing
also rejects the TX1/TX2 beat; simply shortening it would change source rejection.
Use native-rate refinement for TX1 first, and a separately tested pilot-separation
method for TX2. Higher sample density is not proof of equally fine timing accuracy.

Use a logic analyzer or scope to verify actual GPIO dwell/guard timing if available.
For independent RF-to-GPIO delay, capture a selector marker on the IQ sample timeline
or establish a measured common trigger mapping. A standalone GPIO trace without
that mapping proves electrical timing only. If no synchronized marker is available,
label the result "RF-inferred timing" and retain its uncertainty; acquisition can
still compare end-to-end quality, but cannot establish intrinsic switch settling.

## 3. Run stages with explicit exit decisions

| Stage | Measurement | Exit decision |
| :--- | :--- | :--- |
| 0 — acquisition | Muted 30-second continuous checks at 2/5/10 MS/s, plus normal 4-second saved captures at A–E | Admit only settings with correct readback, zero sample loss/overflow and a sustainable capture path |
| 1 — fresh reference | At 5.800 GHz, record 2 seconds of settled TX1 IQ for each C6 port at A–E, before and after screening | Establish reference vectors, noise/coherence, frozen port weights and drift bounds |
| 2 — dwell screen | A–E × 25/50/100/200 µs × three independently restarted 4-second captures, TX1 at 5.800 GHz | Select a provisional rate/bandwidth/dwell/discard combination on repeat 1; verify it on repeats 2 and 3 |
| 3 — cause checks | Replay the same IQ with alternative timing/phase analyses; perform targeted fixture/filter checks if failures remain unexplained | Identify supported causes and distinguish them from unresolved hypotheses |
| 4 — frequency validation | Freeze the candidate; compare it with fresh A/200 µs controls at the seven sentinels and seven challenge centres below | Verify TX1 first, then TX2, with three independent captures per condition |
| 5 — dense map | One complete 149-centre, 1 MHz pass per source for the frozen winner, with interleaved controls | Publish frequency-dependent latency, settling and failures; repeat problem neighbourhoods independently |

Stage 2 contains **60 switching captures**, or **4 minutes of acquired RF time**.
Stage 1 adds 60 static captures, or 2 minutes of RF time. Hardware configuration,
source settling, flash/readback, transfer and analysis add wall time. Estimate the
remaining wall time from the first completed block rather than using RF duration
as an ETA.

Order the three screening rounds as complete blocks. Reverse or permute dwell
block order between rounds, randomize A–E within each dwell block with a recorded
seed, and bracket each round with A/200 µs controls. This limits firmware changes
while exposing warm-up and drift. A new candidate selected after inspecting a
validation failure requires fresh validation captures.

The reference captures are independent of the switching IQ. Establish weights
and a common observable-port mask from the fresh A reference, separately for each
source/frequency. Freeze that mask across A–E: do not improve a candidate's score
by quietly dropping a degraded arm. Retain the existing -20 dB relative-amplitude
observability threshold and require at least four observable C6 ports. Report all
six individual ports regardless. A poor reference or unstable fixture is a fixture
limitation and should trigger targeted investigation.

At stages 4–5, acquire the corresponding settled reference for each new
source/frequency/configuration before assessing transfer bias. References must
also be refreshed after any gain, filter or wiring change; their capture cost is
additional to the switching-run counts.

Frequency validation uses:

- Established sentinels: **5726, 5750, 5775, 5800, 5825, 5850, 5874 MHz**.
- Challenge centres: **5758, 5764, 5773, 5777, 5843, 5847, 5858 MHz**. These cover
  earlier TX1/TX2 failures and a region with poor ideal-manifold agreement.
- Dense confirmation: **5726–5874 MHz inclusive, 1 MHz spacing**, 149 centres.

The challenge centres are held out from selecting settings at 5.800 GHz, but are
known historical problem locations, not a random frequency sample. Keep TX2's
cross-frequency-fit threshold at 0.25. Report separate TX1 and TX2 winners if their
needs differ; a shared runtime setting requires both source modes to qualify.

## 4. Measure accuracy as well as repeatability

| Metric | Definition and proposed acceptance |
| :--- | :--- |
| Capture integrity | Zero reported gaps, overflow, stream changes or full-scale samples in admitted IQ; save failure evidence separately |
| Phase repeatability | Frozen-power-weighted RMS ≤10° with at least eight non-overlapping groups per capture; retain legacy self-referenced RMS for historical comparison |
| Independent transfer closure | Compare against the separate settled vector: weighted residual phase bias ≤5°, maximum observable-port bias ≤10° |
| Common phase treatment | Report raw RX1-referenced phase; for spatial closure allow one common phase rotation for the whole six-port vector, with no per-port refitting |
| Gain closure | Target ≤1 dB gain error on observable ports relative to the corresponding settled reference |
| Settling | Earliest age after selection for which phase/gain closure remains inside the stated limits through all later supported windows of the usable dwell |
| Timing uncertainty | Boundary residuals and sensitivity to ±0.5/±1 µs alignment perturbations; use measured marker uncertainty when larger |
| Time to quality | Earliest measured wall duration reaching the phase gate; also report active per-port integration and exact cycles accumulated |
| Delivery latency | Separately measure sample availability, transfer and processing delay; a 2 ms RF integration does not imply 2 ms application output |
| Bearing | Diagnostic likelihood/residual only; this experiment does not supply the missing surveyed OTA manifold |

The closure limits are predeclared engineering targets for this campaign, not
previously demonstrated guarantees. Check reference repeatability first; an
uncertain reference cannot establish a small bias. Use disjoint windows for RMS
groups and report between-capture variability. Adjacent sliding settling windows
are correlated and must not be counted as independent validation trials.

For a runtime latency claim, train synchronization and TX2 frequency correction
on separate acquisition data, then process held-out data without future samples.
Report initial lock time and any relock separately. Offline full-record decoding
can establish an RF integration lower bound, but cannot establish that an
application could have emitted the vector at that earlier time.

Plot phase RMS at common requested integration budgets of 1, 2, 5, 10, 20, 50,
100, 200 and 400 ms, rounding to complete selector cycles and displaying actual
times. A budget with fewer than eight groups is unqualified. Also compare matched
active integration time to separate guard overhead from estimator behavior.

Replay leading discards of 0, 1, 2, 5, 10, 15, 20 and 30 µs with a fixed 5 µs
trailing trim, excluding choices that leave no supported interior samples. Choose
the discard on training data and freeze it for validation. A 30 µs discard is
incompatible with a 25 µs dwell; the previous provisional setting cannot be
carried over automatically.

Use a common 5 µs measurement window for legacy settling comparisons and a separate
native-rate fine trace for timing diagnosis. Account for the window length and
any channelizer/group delay when labeling sample age. A phase-only early crossing
does not qualify a dwell that later drifts or has a gain transient.

## 5. Experiments that distinguish causes

| Observed result | Interpretation supported | Follow-up |
| :--- | :--- | :--- |
| B/C improve over A; D/E add little | RX clock/filter configuration or timing resolution matters | Compare filter readbacks and replay variants |
| D/E improve over B/C with earlier measured settling | Wider receiver response helps short dwells | Verify gain/phase closure and blocker/noise sensitivity |
| Refined timing improves the same raw IQ | Analysis alignment contributed to the failure | Validate boundary accuracy independently and on held-out captures |
| Long/static dwells work but short dwells retain a phase floor | A switching-dependent error remains | Check timing drift, filter memory, leakage and control transitions |
| A controlled conducted fixture succeeds where OTA failed | Link strength, coupling or room propagation contributes | Validate the candidate again in the installed OTA array |
| Short dwells fail even with controlled strong signals and known edges | Receiver/board transient or interference deserves focused measurement | Isolate PCB paths and receiver settings at one frequency |

Replay high-rate IQ both natively and after a documented anti-alias filter and
decimation to lower rates. Account for filter delay and cross-transition support,
and compare matched usable time. This holds the analog capture fixed and tests
the software information requirement; it does not emulate a separate native
2 MS/s receiver configuration.

If a fixture check is needed, use the previously verified attenuated conducted
splitter arrangement to feed RX1 and the PCB ports, record its wiring and gains,
and establish a fresh static reference. The splitter need not have equal phases:
its independently measured static vector is the reference for repeatability.
This is a separate fixture block requiring the physical cable change; the first
OTA screen needs no rewiring. Confirm signal level from ADC/coherence measurements
and the existing board-input limit rather than assuming the OTA TX setting is
appropriate for direct connection.

Do not reduce the 20 µs guard or 180 µs marker during the A–E comparison. Once a
rate/dwell winner is independently confirmed, those fixed overheads can be a
separate experiment with electrical timing verification.

## 6. Choose a useful winner

1. Require capture integrity, independent transfer closure and the phase target on
   fresh validation data. All three repeats must pass at a centre claimed usable.
2. Compare the candidate against fresh A/200 µs controls using the same observable
   ports, integration budgets and fixture. Retain failures explicitly.
3. Prefer the shortest qualified dwell whose worst tested time to quality is no
   worse than the qualified control. Publish scan-rate/latency trade-offs when
   neither setting dominates; retain 200 µs if faster switching is less useful.
4. Among otherwise equivalent choices, prefer the lower sample rate and lower
   transport/compute cost. Report latency ratios and their across-repeat spread;
   do not claim an improvement when the spread overlaps a tie.
5. Distinguish a universally qualified setting from a setting with frequency
   exclusions. Dense failures remain invalid frequencies until new evidence
   resolves them. A single dense pass maps frequency behavior; it does not
   establish repeatability at every 1 MHz centre.

If 25 µs passes with clear timing and closure margin, generate a separate
12.5/20 µs follow-up only after checking firmware timing resolution and repeat the
qualification. Do not infer sub-25 µs operation from a passing 25 µs result.

## 7. Data budget and report

| RX rate | Dual-RX int16 I/Q payload, before overhead | Current dual-complex64 disk rate | Disk per 4-second capture |
| ---: | ---: | ---: | ---: |
| 2 MS/s | 16 MB/s | 32 MB/s | 128 MB |
| 5 MS/s | 40 MB/s | 80 MB/s | 320 MB |
| 10 MS/s | 80 MB/s | 160 MB/s | 640 MB |

These are decimal, calculated payload sizes; measure network and disk rates on
the actual path. A RAM ring can absorb finite stalls, but continuous operation
still needs adequate average drain bandwidth. If a higher rate only works as a
finite buffered acquisition, label it burst-qualified and measure its dead time.

The 60 switching captures require about **24.6 GB** in the current disk format;
the initial before/after static references add **12.3 GB**, before controls and
preflight evidence. Plan roughly **40 GB plus operational margin** for screening.
One dense pass per source adds 298 switching captures: **38.1/95.4/190.7 GB** at
2/5/10 MS/s, before reference captures and repeats. Native int16 storage could
halve these disk sizes if exact sample-value equivalence is verified first.

Store new raw evidence under a new campaign directory beneath
`/srv/bulk/samteway/lab-data/`, preserving earlier campaigns. Check capacity before
expanding stages. Keep raw capture separate from analysis so alternate decoders
and discards reuse the same observations.

Publish a standalone report with these PNGs and companion CSV tables:

1. Phase RMS versus wall integration, faceted by dwell, with A–E overlaid.
2. Phase and gain versus time after selection, per antenna, with boundary uncertainty.
3. Dwell × sample-rate/bandwidth matrices of pass rate and time to ≤10°.
4. Six-port independent phase-bias and repeatability matrices.
5. Same-IQ legacy/refined/decimated analysis comparison.
6. Frequency × dwell × RMS/latency maps for the validated candidates, with failures visible.
7. Physical scan rate versus RF integration latency and measured application latency.
8. Throughput, continuity, buffer occupancy where available and disk cost.

The final operating table should state sample rate, actual filter/bandwidth
configuration, dwell, guard, marker, leading/trailing discard, frequency coverage,
per-port quality, time to quality, delivery latency and remaining exclusions.
Keep TX1 deployment-like and TX2 laboratory results separate and link back to the
existing PCB calibration and unresolved installed-array manifold work.
