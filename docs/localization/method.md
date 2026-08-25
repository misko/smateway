# Method

## 1. Coordinate system and physical setup

Coordinates are millimetres in the board plane. The origin is the centre of the 90 x 65 mm
PCB, `+x` points right/east in top view, and `+y` points down/south. Polar direction uses
`x = r cos(theta)` and `y = r sin(theta)`, so zero degrees is right and +90 degrees is down.

The eight receive points are the vertical axes of the identical hinged whip antennas, not the
SMA signal pads. The 30 mm SMA-face-to-axis offset comes from the user-supplied antenna image.
All antennas were treated as vertical with a common, unspecified phase-centre height; the
position solver is explicitly planar (`z = 0`).

| State | Connector | Whip axis `(x, y)` mm | Released PCB RF length mm |
|---|---|---:|---:|
| ANT1 | J3 | `(-15.0, -62.5)` | 22.194973 |
| ANT2 | J4 | `(-30.0, -62.5)` | 34.930782 |
| ANT3 | J5 | `(-75.0, -4.5)` | 31.500992 |
| ANT4 | J6 | `(-75.0, +13.5)` | 36.557345 |
| ANT5 | J7 | `(+75.0, +13.5)` | 36.557345 |
| ANT6 | J8 | `(+75.0, -4.5)` | 31.500992 |
| ANT7 | J9 | `(+30.0, -62.5)` | 34.930819 |
| ANT8 | J10 | `(+15.0, -62.5)` | 22.194973 |

The common J2 route is 14.503822 mm. These are released realized-copper lengths, not a full
electrical-delay calibration: switch phase, launch phase, dielectric uncertainty, antenna
phase centre, mutual coupling, and the room remain uncalibrated.

For the retained transmitter experiment:

- the selector board common port was connected to Pluto RX2;
- Pluto RX1 was terminated in 50 ohms and served as the coherent leakage/reference channel;
- eight identical receive whips were connected to ANT1 through ANT8;
- identical whips were connected to Pluto TX1 and TX2; and
- only one transmitter was enabled in each bounded capture.

The new experiment with an antenna on RX1 is a different measurement model and must not reuse
the retained run's “terminated RX1” interpretation.

## 2. RF and power safety boundary

The selector is powered independently through exactly one approved board input. Raspberry Pi
GPIO power is not connected to the RF board. Board ground, Pi ground, and bench-supply ground
are common only through the reviewed wiring.

Before and after each capture, both Pluto transmitter gains are set to `-80 dB`, TX buffer/scan
sources are disabled, all DDS scales are set to zero, and DDS enable state is read back. A tone
is enabled only after the exact USB serial, firmware/runtime ABI, frequency, bandwidth, common
RX gain, selected TX port, TX attenuation, DDS scale, and load-input bound pass. Cooperative
cleanup mutes before closing the metadata stream. USB loss, process `SIGKILL`, or host power
loss still require an external RF cutoff for unattended operation.

## 3. Selector schedule

The autonomous `fast20-v1` image provides self-identifying state timing. A nominal frame is:

1. `ALL_OFF`: 80 ms marker body plus the contiguous 5 ms pre-ANT1 guard;
2. ANT1 through ANT8 in fixed order with nominal active dwells of
   `20, 23, 26, 30, 34, 39, 44, 50 ms`; and
3. a 5 ms `ALL_OFF` break-before-make guard between adjacent active states.

The nominal cycle is 386 ms. Unique dwell lengths identify state and the marker supplies frame
synchronization; decoding fails closed on missing, extra, ambiguous, or out-of-order intervals.

## 4. Capture plan and continuity proof

The retained multi-frequency run used these centres:

`2.400, 2.409, 2.423, 2.440, 2.458, 2.472, 2.483 GHz`.

At each centre, TX1 and TX2 were acquired as an adjacent pair. Three rounds reversed and
rotated frequency/TX ordering to reveal temporal drift. Each condition captured 100 metadata
buffers of 100,000 dual-RX samples at 1 MS/s: 10 seconds and 10,000,000 samples per artifact.

The tandem-V7 metadata ABI gives one shared timeline for both receive channels. A capture is
accepted only when:

- buffer sequence begins at zero and increments without gaps;
- FPGA first-sample sequence is contiguous across every refill;
- stream ID and metadata ABI are constant;
- overflow and failure flags remain zero;
- the persisted artifact hash verifies;
- the selector schedule yields at least 20 complete cycles;
- every antenna passes SNR, coherence, confidence, and repeatability gates; and
- independent post-condition and final TX mute/readback checks pass.

Host wall-clock timestamps are not used to splice or phase-align IQ. FPGA sample counters and
buffer sequence are authoritative.

![Capture plan and continuity proof](png/fig02_capture_plan_and_continuity.png)

## 5. Complex phase observable

The coherent pilot is refined from its nominal +100 kHz offset. Samples are reduced to complex
phasors on the continuous sample timeline. In the retained setup, the terminated RX1 channel
measures coherent leakage. The RX2/RX1 transfer during `ALL_OFF` is locally interpolated and
removed from selected states before a robust complex phasor is calculated for each antenna.

For transmitter `t`, frequency `f`, and antenna `i`, let `phi[t,f,i]` be the resulting raw
within-capture phase. A paired transmitter observation is

```text
d[f,i] = wrap(phi[TX2,f,i] - phi[TX1,f,i])
```

and the localization profile references ANT1:

```text
D[f,i] = wrap(d[f,i] - d[f,ANT1]).
```

This double difference cancels the receive phase common to the adjacent TX captures and one
unknown common transmitter-pair phase at each frequency. It does not cancel direction-dependent
antenna response, mutual coupling, frequency-dependent switch/launch delay, or multipath.

Three capture pairs are circularly aggregated into exactly one row per unique frequency. Capture
repeats estimate repeat scatter; they are not treated as independent geometry or repeated
posterior likelihood rows.

![Measured phase profiles](png/fig03_phase_profiles_by_frequency.png)

![Repeatability and quality](png/fig04_repeatability_and_quality.png)

Changing only DDS starting phase supplies no new location information. It rotates all receive
states together and is removed by the ANT1 reference or the marginalized common phase. It is
useful only as an estimator-invariance or leakage diagnostic.

## 6. Direct-path diagnostic

The first model jointly sampled both transmitter positions under independent radial priors of
`304.8 +/- 50 mm`. At every frequency it predicted direct free-space path differences and
marginalized one circular common phase. This model produced 53.9 degrees overall weighted RMS
with very low importance-sampling effective sample size. The mismatch is a diagnostic failure,
not a precise alternate position result.

![Direct-model residuals](png/fig05_direct_model_residuals.png)

## 7. Anchored frequency-slope model

The more robust model fixes TX1 at the previously accepted `(-26.5, +315.7) mm` anchor and
infers TX2 from how each non-reference antenna's double-relative phase changes with frequency.
It marginalizes one frequency-independent circular intercept per antenna. Fixed phase offsets
therefore cannot steer position; the likelihood uses the phase slope across the retained
frequencies.

The chosen TX1 point is an experimental anchor, not a persisted laser/tape survey. TX2 inherits
that uncertainty. The radial prior still determines most of the reported range.

![Anchored frequency-slope fit](png/fig06_anchored_phase_slope_fit.png)

## 8. Outlier and sensitivity policy

The 2.458 GHz profile was excluded after inspecting the all-frequency result. Its anchored
profile residual was 77.0 degrees RMS—more than twice every other frequency—and its measured
phase pattern changed discontinuously despite good within-frequency repeatability. This is
consistent with a stable frequency-selective multipath/antenna systematic, not capture noise.

Because the exclusion is post-hoc, both the all-frequency result and the reason for exclusion
remain in the audit. The primary six-frequency fit is followed by leave-one-frequency-out
(LOFO) analyses, plus a conservative systematic-floor run. LOFO modes between 18.8 and 35.4
degrees support a lower-right sector; their spread is part of the result.

![TX2 posterior geometry](png/fig07_tx2_posterior_map.png)

![Frequency and model sensitivity](png/fig08_sensitivity_lofo.png)
