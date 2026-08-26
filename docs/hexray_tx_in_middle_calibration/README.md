# HexRay TX-in-middle complex calibration design

## Status and evidence boundary

This document defines the experiment for a six-element circular receive array connected to
`ANT1` through `ANT6`, with Pluto `TX1` at its centre. It is a **pre-execution design**. The
numbers labelled “expected” are geometry or released-PCB calculations, not measured calibration
results.

No logic analyzer was physically connected when this revision was prepared. Section 8 therefore
defines a preferred independent GPIO-observation path and a restricted low-power RF timing
fallback. The fallback does not independently observe GPIO code or active-port identity.

The user-confirmed top-view convention is:

- the receive phase centres lie on a `51 mm` diameter circle;
- `ANT1` points forward;
- `ANT2` through `ANT6` proceed clockwise;
- TX1 is at the circle centre; and
- ANT7 and ANT8 are not selected by this experiment.

Before RF acquisition, physically confirm that `51 mm` describes the RF phase-centre circle,
not only a mechanical outline. Record all antenna and TX phase-centre heights, polarization,
cable identities and the exact calibration reference plane.

![HexRay geometry and RF scale](png/fig01_geometry_and_wavelengths.png)

## 1. Objective

The experiment has four distinct objectives:

1. Qualify a high-rate, break-before-make selector frame with a short, measurable `ALL_OFF`
   interval between every receive state.
2. Measure one complex, null-subtracted response for every receive element while the centred
   transmitter and complete assembly remain fixed.
3. Estimate repeatability, symmetry, spatial modes and frequency dependence without treating
   repeated samples or antennas as independent geometry.
4. Produce a versioned end-to-end calibration with an explicit reference plane, uncertainty,
   held-out validation and fail-closed safety record.

The centered run is not a complete direction-of-arrival calibration. It excites one near-field
manifold vector. Directional operation still requires surveyed off-centre or angular calibration
positions.

## 2. Geometry and expected RF regime

Use an array-centred coordinate system in millimetres: `+x` points right and `+y` points
forward when viewed from above. With radius `r = 25.5 mm`, the nominal receive coordinates are:

| Port | Angle | Nominal `(x, y)` mm | Opposite element |
|---|---:|---:|---|
| ANT1 | +90° | `(0.000, +25.500)` | ANT4 |
| ANT2 | +30° | `(+22.084, +12.750)` | ANT5 |
| ANT3 | -30° | `(+22.084, -12.750)` | ANT6 |
| ANT4 | -90° | `(0.000, -25.500)` | ANT1 |
| ANT5 | -150° | `(-22.084, -12.750)` | ANT2 |
| ANT6 | +150° | `(-22.084, +12.750)` | ANT3 |

These are design coordinates. The execution record must contain surveyed coordinates and
uncertainty before using geometric residuals as a pass/fail metric.

| Quantity | 2.400 GHz | Exact experimental 5.800 GHz |
|---|---:|---:|
| Free-space wavelength | 124.914 mm | 51.688 mm |
| Radius / adjacent chord | 0.204 λ | 0.493 λ |
| Circle diameter | 0.408 λ | 0.987 λ |
| Free-space phase per millimetre | 2.882° | 6.965° |
| 51 mm-aperture reactive boundary | 20.2 mm | 31.4 mm |
| 51 mm-aperture Fraunhofer boundary | 41.6 mm | 100.6 mm |
| Centre-to-element interpretation | Fresnel heuristic | Reactive-near-field heuristic |

The standard aperture boundaries are only scale indicators for this compact antenna system.
If the receive elements are the earlier `95 mm` whips, their own maximum dimension places the
25.5 mm separation deeper in the near field at both bands.

Near-field operation does not itself destroy C6 symmetry. With a perfectly centred,
rotationally symmetric source, equal phase-centre heights, identical polarization and a C6
environment, all six OTA complex responses should be equal. Near-field coupling, pattern
distortion and small placement errors make those conditions demanding.

At 5.8 GHz, the adjacent chord is only `0.344 mm` below half a wavelength. A `1 mm` source
displacement along an opposite-element axis produces approximately `5.76°` opposite-pair phase
at 2.4 GHz and `13.93°` at 5.8 GHz. Exact 5.800 GHz is therefore geometry-relevant but has very
little phase-centre-error margin. It remains an explicitly experimental operating point outside
the official AD9363 range; qualify the implementation at 2.4 GHz first.

## 3. Expected raw electrical response

The selector PCB was deliberately not length matched. Released v0.2.1 copper lengths give the
following diagnostic prior, using the released CPWG effective permittivity
`epsilon_eff = 3.13660852672`:

| Port | RF copper mm | Relative delay vs ANT1 | PCB-only lag at 2.4 GHz | At 5.8 GHz |
|---|---:|---:|---:|---:|
| ANT1 | 22.194973 | 0.00 ps | 0.00° | 0.00° |
| ANT2 | 34.930782 | 75.24 ps | 65.01° | 157.10° |
| ANT3 | 31.500992 | 54.98 ps | 47.50° | 114.79° |
| ANT4 | 36.557345 | 84.85 ps | 73.31° | 177.16° |
| ANT5 | 36.557345 | 84.85 ps | 73.31° | 177.16° |
| ANT6 | 31.500992 | 54.98 ps | 47.50° | 114.79° |

The common J2 route is `14.503822 mm` and cancels from relative comparisons. These numbers do
not include switch, launch or cable delay and must never be subtracted as though they were a
measured calibration.

Two exact-copper pairs are useful diagnostics: ANT3/ANT6 and ANT4/ANT5. The physical opposite
pairs are instead ANT1/ANT4, ANT2/ANT5 and ANT3/ANT6. These two pair families answer different
questions and must not be conflated.

## 4. High-rate frame

The implemented `hexcal-v1` frame is:

```text
180 us  ALL_OFF marker body
 20 us  ALL_OFF guard before ANT1  } observable marker = 200 us
200 us  ANT1
 20 us  ALL_OFF guard
200 us  ANT2
 20 us  ALL_OFF guard
200 us  ANT3
 20 us  ALL_OFF guard
200 us  ANT4
 20 us  ALL_OFF guard
200 us  ANT5
 20 us  ALL_OFF guard
200 us  ANT6
---------------------------------
1500 us total
```

This produces `666.7` complete array scans/s, `4,000` active dwells/s and `8,000` RF edges/s.
Aggregate active duty is `80%`, or `13.33%` per antenna. Each 20 µs guard is `14.3` times the
PE42482 maximum `1.4 µs` settling time.

Analysis discards `5 µs` at both edges of every interval. It therefore retains `190 µs` from
each active dwell and `10 µs` from each ordinary null. At the existing 100 kHz pilot these are
approximately 19 and one pilot periods, respectively.

![High-rate selector timing and capture order](png/fig02_high_rate_timing_and_capture_plan.png)

The short `ALL_OFF` interval is a temporal null slot, not a spatial beamforming null and not an
assumption of zero received power. It measures leakage and supplies a visible boundary. The
decoder must reject the frame if null contrast, the long marker, any guard or any state edge is
missing. Center symmetry can make adjacent active states indistinguishable, so state identity
must come from the long marker, fixed order and intervening null slots—not from a guessed active
amplitude.

The predecessor Fast20 image polls a 1 ms SysTick and stores millisecond durations. This profile
requires a genuine integer-microsecond timer implementation, atomic GPIO writes, and separate
qualification. Merely scaling the old duration constants is not acceptable.

## 5. Capture matrix

Use a bounded 100 kHz-offset TX1 pilot, manually fixed receive gain, metadata ABI 2 and a
1 MS/s continuous capture. One artifact lasts exactly one second. The six center frequencies
are:

```text
2.400, 2.423, 2.440, 2.458, 2.483, 5.800 GHz
```

The emitted carrier is the centre plus the refined DDS offset. The exact 5.800 GHz centre
requires the repository's explicit experimental opt-in.

Do not predeclare `20 dB` or another convenient RX gain. Before the calibration matrix, run a
bounded low-power gain qualification starting at the lowest conservative gain supported by the
hardware. Increase manual gain only as needed for every port to pass pilot SNR and
selected-to-null contrast while retaining all clipping and headroom gates. Persist the selected
gain before admitting calibration artifacts. Never change it within an artifact; reuse the same
predeclared value for all three repeats of a frequency/condition. Prefer one common gain for the
entire matrix when it passes every condition. AGC is forbidden.

Use three independent rounds:

| Round | Centre-frequency order GHz | Purpose |
|---:|---|---|
| 1 | 2.400, 2.423, 2.440, 2.458, 2.483, 5.800 | forward baseline |
| 2 | 5.800, 2.483, 2.458, 2.440, 2.423, 2.400 | reverse time ordering |
| 3 | 2.440, 2.458, 2.483, 5.800, 2.400, 2.423 | rotated held-out order |

The plan contains 18 immutable artifacts. A one-second capture contains about 666 frames; after
arbitrary start/end phase, require at least 600 complete frames. At 1 MS/s this gives about
126,700 edge-trimmed active samples per antenna per artifact.

Do not selectively recapture a merely unfavourable antenna or frequency. A transport or safety
failure may retry the unchanged plan item, but the failed attempt and reason remain in the
manifest.

## 6. Observable and gauge ambiguity

For antenna `i`, frequency `f`, frame `k`, let the matched-filter phasors be `S_i` in the
selected interval and `N_before`, `N_after` in the adjacent `ALL_OFF` intervals. Interpolate the
local complex null to the active-dwell centre and form:

```text
Z_i(f,k) = S_i(f,k) - interp(N_before, N_after).
```

If RX1 is deliberately configured as a stable coherent reference, form `RX2/RX1` before null
subtraction. The execution report must state whether RX1 is terminated, receives OTA, or uses a
conductive reference; those modes are not interchangeable.

The centered response factorizes conceptually as:

```text
H_i(f) = C_i(f) * A_i(f)
```

where:

- `C_i` is cable, connector, PCB route and switch response; and
- `A_i` is the element, mutual coupling and electromagnetic environment.

A single centered OTA capture identifies only `H_i`. For any nonzero complex `q_i`, replacing
`C_i` with `q_i C_i` and `A_i` with `A_i/q_i` leaves every measurement unchanged. This is a
gauge ambiguity, not something more repeats can solve.

![Signal chain and identifiability](png/fig03_signal_chain_and_identifiability.png)

The defensible center-run artifact is therefore an end-to-end, in-situ complex manifold. To
establish separate reference planes:

1. Inject a characterized splitter/through standard at the six selector-board SMA planes.
2. Repeat at the six array feed ends to include external cables.
3. Divide the centered OTA response by the appropriate through response.
4. Rotate characterized splitter outputs or cyclically permute physical elements among ports.
5. Fit a complex port/element two-factor model and reject separability if its held-out residual
   exceeds the declared gate.

Mutual coupling is generally direction-dependent. Even a successful factorization at the
centre does not replace an angular OTA manifold.

## 7. Complex normalization and symmetry metrics

For each artifact, robustly aggregate admitted frame phasors per antenna. Remove a single common
amplitude and phase gauge using the geometric-mean amplitude and circular phase centre. Store
the original complex values, normalized values, uncertainty and gauge choice.

For normalized antenna phasors `h_n`, use the six-point circular spatial transform:

```text
M_m = (1/6) * sum(n=0..5) h_n * exp(-j * 2*pi*m*n/6).
```

An ideal centered C6 response has only common mode `M_0`. Non-common modes measure asymmetry;
they do not by themselves identify whether its origin is centering, polarization, cable phase,
switch response, mutual coupling or the room.

Also retain:

- amplitude peak-to-peak spread;
- circular phase resultant and RMS;
- physical opposite-pair residuals `(ANT1,ANT4)`, `(ANT2,ANT5)`, `(ANT3,ANT6)`;
- exact-PCB-length pair residuals `(ANT3,ANT6)` and `(ANT4,ANT5)`;
- even/odd frame agreement and per-port cycle coherence;
- forward/reverse/rotated round differences; and
- phase and amplitude versus exact emitted carrier frequency.

![Expected raw route phasors and calibrated acceptance](png/fig04_expected_phasors_and_acceptance.png)

Calibration can force its own training data to look perfectly symmetric. All symmetry gates
must therefore be evaluated on held-out data. Use leave-one-round-out validation at every
frequency. Within the 2.4 GHz band, also perform leave-one-frequency-out interpolation. Treat
5.8 GHz as a separate band; do not interpolate across the 3.3 GHz gap.

## 8. Predeclared acceptance gates

### Firmware and timing

| Gate | Acceptance |
|---|---:|
| Active order | exactly ANT1 through ANT6 |
| ANT7/ANT8 selections | zero |
| Illegal GPIO codes | zero |
| Every selected-to-selected transition | passes through ALL_OFF |
| Marker, guard and dwell duration error | within ±5% |
| Minimum observed ordinary guard | 18 µs |
| Analysis edge trim | at least 5 µs each side |
| Boot, reset, watchdog and detected clock fault | remain/return ALL_OFF |

### Timing and GPIO evidence paths

Complete and record one of these paths before admitting calibration data. The strict code,
order, illegal-state and timing gates above do not change between paths; only the strength and
label of the evidence changes.

**Path A — preferred independent GPIO qualification.** With RF muted, connect a logic analyzer
to the selector GPIO and a suitable timing reference. Independently observe boot and reset
`ALL_OFF`, the exact `ALL_OFF`/ANT1-through-ANT6 codes, active order, zero illegal codes,
break-before-make transitions, 180 µs marker body, 20 µs guards, 200 µs dwells and 1.500 ms
cycle. Firmware self-report alone is not independent timing or GPIO evidence.

**Path B — fallback when no logic analyzer is connected.** Before the smoke capture, pass the
host state-machine, generated-profile and ELF checks; bind the exact `hexcal-v1` profile and
firmware hashes; flash only the intended target; and prove that the flashed/readback image hash
matches. Then use the smallest bounded TX stimulus and a continuous low-power RF capture to
observe selected/null edges. If continuity, contrast and edge-fit gates pass, those edges may
independently qualify only marker, guard, dwell and cycle timing.

Under Path B, GPIO code identity, active-port identity/order and absence of illegal GPIO codes
remain **source-derived plus flashed/readback-hash-backed; not independently GPIO-observed**.
A centered equal-amplitude source cannot prove which antenna code produced an active dwell.
Reports must retain that evidence label and must not describe RF-only timing as full hardware
GPIO qualification.

### Artifact integrity and RF admission

| Gate | Acceptance |
|---|---:|
| Complete frames per artifact | at least 600 |
| Buffer-sequence gaps | zero |
| FPGA first-sample-sequence gaps | zero |
| Overflow/failure flags | zero |
| Persisted artifact identity and SHA-256 | exact match |
| Clipped samples | zero |
| Near-full-scale sample fraction | at most `1e-4` |
| Pilot SNR, every port | at least 20 dB |
| Weakest selected-to-null contrast | at least 20 dB |
| Post-condition and independent final TX mute/readback | pass |

### Repeatability and held-out calibration

| Gate | Acceptance |
|---|---:|
| Within-artifact cycle coherence | at least 0.995 |
| Within-artifact cycle phase RMS | at most 6° |
| Between-round relative-phase spread | at most 5° |
| Between-round amplitude spread | at most 0.5 dB |
| Held-out corrected amplitude span | at most 1.0 dB |
| Held-out corrected circular phase RMS | at most 5° |
| Held-out corrected phase resultant | at least 0.995 |
| Held-out opposite-pair phase mismatch | at most 5° |
| Largest non-common mode | target ≤ -20 dBc; minimum ≤ -15 dBc |
| Held-out coefficient residual | at most 5° and 0.5 dB |
| Two-factor permutation residual | at most 5° RMS and 0.5 dB RMS |

The board release separately calls for calibrated VNA insertion loss, return loss and isolation.
This OTA experiment cannot turn a relative complex response into those missing absolute
S-parameter results.

## 9. Safety, failure handling and data admission

- Bind every live command to the exact Pluto serial and board identity.
- Keep both transmitters muted until the board input ceiling, fixed TX attenuation, DDS scale,
  centre frequency and load bound pass.
- Start with the smallest bounded TX stimulus. Increase it only while every receiver retains
  ADC headroom. The selector board operating ceiling remains `0 dBm` at its RF mating plane.
- Begin gain qualification at the lowest conservative manual hardware gain. Increase it only
  enough to pass the declared SNR/contrast gates with headroom, record it, and then lock it as
  specified in Section 5. AGC would create an uncontrolled time-varying coefficient.
- Mute and read back the exact radio after every condition and once independently at the end.
- Cooperative cleanup is not an external RF interlock. Unattended operation still requires an
  independent cutoff if USB loss, host loss or `SIGKILL` must be covered.

### `ENODATA` quarantine and retry

If `buffer.refill()` raises Linux `ENODATA`:

1. Stop the condition and accept no artifact identity.
2. Do not splice partial IQ into any prior or subsequent artifact.
3. Mute both TX channels and verify the exact-radio readback.
4. Preserve the failed attempt, error, plan index and cleanup evidence.
5. Retry the unchanged plan item under a new attempt/artifact identity.
6. Admit the retry only if all ordinary continuity, headroom, timing, analysis and mute gates
   pass.

`ENODATA` means the host did not receive the next streaming buffer. It is not evidence that no
RF was present.

## 10. Implementation, deployment and rollback gates

The implementation should remain split into independently reviewable components:

1. A generated microsecond profile containing the authoritative GPIO codes and schedule.
2. A host-testable pure state machine with exact transition and duration tests.
3. An STM32 timer/GPIO image that boots fail-closed and uses atomic writes.
4. A hardware-free matched-filter analyzer using integer sample indices and the profile hash.
5. A bounded runner that persists its complete three-round plan before RF is enabled.
6. A report snapshot that contains artifact hashes, firmware/profile identities, measurements,
   rejection reasons and calibrated coefficients.

Before flashing, pass host tests, static checks, ELF policy checks and a timing-model test. Then:

1. Record the current qualified Fast20 image and safe-hold rollback identities.
2. Flash only the explicitly selected board through the existing SWD recovery workflow.
3. Read back and verify the programmed image.
4. Prefer Path A: with TX muted, use a logic analyzer to verify boot `ALL_OFF`, all six codes,
   every 20 µs guard, the 180 µs marker body, 200 µs active dwells and 1.500 ms cycle.
5. If no logic analyzer is connected, use Path B: retain source/test code evidence and verified
   flashed/readback hashes, then qualify marker/guard/dwell/cycle timing only with a bounded
   low-power RF edge capture. Mark code identity/order/illegal-state claims as not independently
   GPIO-observed.
6. Exercise reset, watchdog and clock-fault recovery under the strongest available observation
   and prove each returns to `ALL_OFF`; record whether that evidence is direct GPIO, RF-level or
   source/test plus image-readback evidence.
7. Select and persist the manual RX gain under the bounded policy in Section 5 before executing
   the matrix.

Any firmware identity, GPIO, duration, recovery, serial, continuity, headroom or mute failure
blocks the next gate. Rollback means returning to `safe_hold` for fault isolation or the prior
qualified Fast20 image for the established experiment; it does not mean ignoring a failed
high-rate qualification.

## 11. Expected findings and interpretation policy

Expected, but not guaranteed:

- Raw phases should not be equal because the PCB route prior alone spans about `73°` at
  2.4 GHz and `177°` at 5.8 GHz.
- ANT3/ANT6 and ANT4/ANT5 may expose external/switch asymmetry because their PCB lengths match.
- A well-centred, corrected response should concentrate energy in spatial mode `M0`.
- 5.8 GHz should be much more sensitive to sub-millimetre centering and antenna phase-centre
  error than 2.4 GHz.
- Averaging thousands of repeated edges may reveal an empirical settling transient; if so, use
  it to increase edge trim in a new versioned profile rather than relaxing admission after the
  fact.

Interpret failures by layer:

- **Marker/null contrast failure:** state identity is not observable; reject the artifact.
- **Low cycle coherence:** noise, drift, timing or transient contamination; do not calibrate.
- **High repeat coherence but poor C6 symmetry:** deterministic geometry, polarization,
  coupling or environmental asymmetry; retain as a fingerprint, not a symmetry pass.
- **Forward/reverse difference:** state-history or time-after-marker bias.
- **Through pass, OTA fail:** element/coupling/environment dominates.
- **Permutation-factorization fail:** port and element effects are not separable under the
  proposed product model.

No gate may be lowered after seeing the data without retaining the original failed result and
labelling the new analysis exploratory.

## 12. Reproduction and provenance

The compact, source-hashed design snapshot is
[`data/design-snapshot.json`](data/design-snapshot.json). It records the confirmed geometry,
calculated RF scales, released route priors, schedule, plan, equations, gates, safety policy and
SHA-256 identities of the local source documents used for this design.

Generate the four PNGs:

```bash
uv run --extra report python scripts/render_hexray_center_calibration_design.py
```

Verify byte-for-byte reproduction and the figure manifest:

```bash
uv run --extra report python scripts/render_hexray_center_calibration_design.py --check
```

Run the focused renderer tests:

```bash
uv run --extra report pytest -q tests/test_render_hexray_center_calibration_design.py
```

[`data/figures-manifest.json`](data/figures-manifest.json) records the renderer and snapshot
hashes, Matplotlib version, PNG byte sizes, dimensions and hashes. Generated PNGs are never
manually edited.
