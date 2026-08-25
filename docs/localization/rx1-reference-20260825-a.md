# RX1 reference experiment: rx1-reference-multifrequency-20260825-a

## Outcome

The experiment measured the new antenna on Pluto RX1 coherently, but it did **not** identify
an RX1 position or an accepted range difference. Acquisition integrity passed; the strict
free-space geometry model failed.

This distinction matters. The data are highly repeatable within each antenna/frequency cell,
yet the eight geometry-corrected antenna states disagree by 66–93 degrees RMS. Lowering the
coherence gate produces several almost-equivalent, wavelength-spaced ranges rather than new
location information. The ungated `+102.3 mm` value is therefore a diagnostic artifact and
must not be used as a range estimate.

![RX1 capture integrity](png/fig09_rx1_capture_integrity.png)

## Setup and observable

- The selector-board common port remained on Pluto RX2.
- The added whip antenna was connected to Pluto RX1 and received continuously.
- TX1 and TX2 were enabled one at a time under the bounded phase stimulus.
- Each 10-second artifact recorded RX1 and RX2 simultaneously at 1 MS/s and 20 dB common
  tandem-HOLD gain.
- Three rounds covered seven centres from 2.400 through 2.483 GHz, with adjacent TX1/TX2
  pairs and reversed/rotated ordering.

For frequency `f` and selector state `i`, the analysis forms

```text
T[t,f,i] = RX2 / RX1
Q[f,i]   = T[TX2,f,i] / T[TX1,f,i]
```

The same-state TX ratio cancels a fixed selector/PCB path. After applying the known-array
geometric rotation, an ideal direct-path model predicts the same phase for all eight states:

```text
phase(Q_corrected[f,i]) = k[f] * (d(RX1,TX2) - d(RX1,TX1))
```

The eight states are repeated observations of that one scalar; they are not eight independent
RX1 baselines.

## Capture and safety audit

| Quantity | Result |
|---|---:|
| Planned/finalized conditions | 42 / 42 |
| Execution attempts | 43 |
| Artifact identity and SHA-256 matches | 42 / 42 |
| Metadata buffers | 4,200 |
| Samples per receiver | 420,000,000 |
| Missing samples / sequence gaps | 0 / 0 |
| Clipped / near-full-scale samples | 0 / 0 |
| Metadata ABI | 2 |
| Complete selector cycles per artifact | 25 |
| Latest independent TX mute/readback | passed |

Two of 336 per-state quality judgments rejected, both ANT7/TX1 in round 2: 2.483 GHz had
32.29 degrees phase scatter against a 30-degree limit, and 2.458 GHz had 14.15 dB SNR against
a 15 dB limit. There were no global capture-gate rejections. Both frequencies retained two
fully admitted transmitter pairs, so no favorable-data recapture was selected.

The capture-side RX2-only dwell diagnostic returned code 2 for some artifacts. That code is
not a USB or artifact failure and is not the OTA-reference admission result. The canonical
`fast20-reference-transfer.json` reanalysis supplied the 40 passed and two state-local
rejections above.

## Transient USB refill failure

Attempt 40 stopped in libiio `buffer.refill()` with Linux `ENODATA` (`errno 61`, “No data
available”). This means the host did not receive the next streaming buffer; it does not mean
that no RF signal existed.

The runner failed closed: no artifact from that attempt was accepted, no partial IQ was
spliced into another capture, and the exact radio was muted and read back. Resuming the
unchanged plan retried the same 2.483 GHz TX1 condition as attempt 41. Artifact
`500b5a6bb77d49fa9dfe382e6e1df3d6` then passed continuity, reference-transfer quality, and
post/final mute checks.

## Why the strict localization failed

Paired-repeat coherence across the 56 frequency/state cells was excellent: minimum `0.9405`,
median `0.9986`, and maximum `0.999998`. RX1 cycle coherence was at least approximately
`0.9997`. Reversing TX order and the long retry gap did not destroy phase repeatability.

The failure appears only after asking the eight states to obey the common direct-path geometry:

| Carrier GHz | State coherence | State phase RMS deg |
|---:|---:|---:|
| 2.400100 | 0.4386 | 68.5 |
| 2.409100 | 0.3586 | 84.9 |
| 2.423100 | 0.3117 | 76.6 |
| 2.440100 | 0.2683 | 87.5 |
| 2.458100 | 0.1847 | 92.9 |
| 2.472100 | 0.2952 | 81.3 |
| 2.483100 | 0.4551 | 66.0 |

Every row is below the predeclared `0.50` state-coherence gate. Reversing the geometry sign,
using SMA faces instead of the assumed whip axes, omitting ALL_OFF subtraction, and borrowing
the preceding terminated-RX1 run as an empirical correction do not repair the disagreement.
This rules out a simple pairing bug or random RX phase reset and instead supports deterministic
model mismatch: unsurveyed source geometry, antenna phase-centre/pattern error, mutual coupling,
and frequency-selective indoor multipath.

![Repeatable measurement, rejected model](png/fig10_rx1_coherence_model_gate.png)

## Why no fallback coordinate is reported

With the state gate disabled, the nominal MAP is `+102.3 mm`, but modes near `+225.1`,
`-20.5`, `+347.9`, `-143.3`, and `-266.1 mm` lose less than `0.13` relative log-likelihood.
The nominal 90% interval spans `-271.1` to `+346.9 mm`; leave-one-frequency and
leave-one-state modes range even more widely. A nuisance-intercept frequency-slope check hits
the physical `-354.6 mm` boundary while individual antenna estimates extend through
`+169.7 mm`. These are model-rejection diagnostics, not candidate ranges.

Even perfect measurements from only TX1 and TX2 would have geometric rank one. They constrain

```text
d(RX1,TX2) - d(RX1,TX1) = constant
```

which is a hyperbola in the board plane, not a unique point. No surveyed Pluto pose, RX1
phase-centre coordinate, cable geometry, or calibrated RX1/RX2 differential delay exists in
the retained setup to select a point on that curve.

![Identifiability and next experiment](png/fig11_rx1_identifiability_and_next_experiment.png)

## Next experiment

1. Survey the array, TX, and RX1 phase centres in `x/y/z`, record whip hinge orientation, and
   lock them in a rigid jig away from reflective bench metal.
2. Use at least three non-collinear surveyed TX positions; four are preferred for
   hold-one-anchor-out validation.
3. Measure a much wider RX2/RX1 cross-spectrum, estimate group delay, and gate the earliest
   usable arrival instead of fitting isolated CW phase alone.
4. Perform a splitter/cable through-calibration for RX1 against every selector path, then an
   OTA array-manifold calibration at surveyed source positions.
5. For later uncontrolled narrowband emitters, use calibrated matched-field/fingerprint
   inference against that manifold.

The current run remains valuable as an immutable continuity proof, a repeatability benchmark,
and a measured multipath/array fingerprint. It is deliberately retained as a negative
localization result.

## Reproduction and provenance

The compact snapshot is
[`data/rx1-reference-20260825-a-report-snapshot.json`](data/rx1-reference-20260825-a-report-snapshot.json).
It records SHA-256 identities for the run manifest, strict failure result, ungated diagnostic,
and array geometry. Raw CI16 artifacts remain outside Git in the per-board state directory.

Regenerate the three PNGs from the committed snapshot:

```bash
uv run --extra report python scripts/render_rx1_reference_report.py
```

Verify byte-for-byte reproducibility:

```bash
uv run --extra report python scripts/render_rx1_reference_report.py --check
```
