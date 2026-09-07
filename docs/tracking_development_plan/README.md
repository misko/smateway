# From calibrated selector to a fast radio direction tracker

- **Plan date:** 2026-09-03
- **ISM profiles considered:** 433.05–434.79 MHz in ITU Region 1,
  902–928 MHz in ITU Region 2, 2.400–2.500 GHz, and 5.725–5.875 GHz
- **Currently released PCB-LUT span:** 0.500–6.000 GHz; the 433 MHz
  profile is designed but intentionally blocked until the LUT is extended
- **Frequency policy:** sequential calibration/validation centres spaced by
  50 MHz where the band is wide enough; never extend a grid outside a band
- **Receiver/selector Pluto:** serial `104000b29905000e17000800065934759d`
- **Two-channel source Pluto:** serial `104473b80a16000de6ff2000f8a6beca79`
- **Selector board:** `stm32c011-4c0055000950313950363920`
- **Recommended first array:** six-element `c6-v2`
- **Status:** continuous TX1 and coherent-pilot TX2 estimation pass across the
  installed 5.8 GHz profile; the empirical OTA manifold remains to be calibrated

## Executive decision

Develop the direction finder in three deliberate steps:

1. Use **TX1 as the truth-source instrument**. Its splitter branch illuminates
   RX1 continuously while its antenna illuminates the switched RX2 array. This
   makes `RX2/RX1` phase valid even when captures start at unrelated times and
   lets us qualify the installed array, solver, and confidence metrics without
   also solving a clock-tracking problem.
2. Use **TX2 as a blind laboratory target**. TX2 does not need a known or
   absolute phase. The implemented test uses a frequency-separated TX1 pilot
   from the same dual-TX Pluto; the shared source clock and LO cancel in a
   cross-frequency correlation. A true unknown field emitter instead needs to
   be heard simultaneously by the fixed RX1 reference antenna.
3. Repeat the frozen-manifold blind validation on every 50 MHz-lattice centre
   that fits inside a supported ISM allocation. Keep calibration, antennas,
   array geometry, jurisdiction, and quality limits explicitly band-profiled;
   do not pretend one compact 5.8 GHz array has the same resolution at 433 or
   915 MHz.
4. After both paths recover withheld bearings, replace slow host-controlled
   switching with a deterministic selector schedule on the FPGA sample
   timeline. Current raw replay supports a conservative 200 ms universal dwell
   and a 10 ms TX2 dwell; shorten adaptively only after link-budget and blind
   angle qualification.

The first release should still close at 5.8 GHz because that is where the PCB
holdout evidence is strongest. The runtime and data model must nevertheless be
multi-band from the beginning. Promote 2.4 GHz next, then 915 MHz, and only then
433 MHz after extending the PCB LUT and building larger low-band apertures. A small system
that reports a bearing plus an honest validity score is more useful than a broad
solver that emits angles when the signal, timing, or calibration is not
identifiable.

The completed ten-run implementation and hardware campaign, including all
figures, per-frequency results, dwell replay, and the band-promotion plan, is in
the [two-source tracking verification report](../tracking_verification_campaign/README.md).

```text
                         DEVELOPMENT MODE

 source Pluto TX1 ── 2-way splitter ── attenuator ──> receiver RX1
             │              coherent reference             │
             └── antenna ── free space ──> C6 antennas     │ same sample clock
                                             │              │
                                      8-way selector ─────> RX2

                    h_i = coherent RX2_i / RX1


                       BLIND-TARGET / DEPLOYMENT MODE

 unknown emitter TX2 ── free space ──┬──> fixed RX1 reference antenna
                                     └──> C6 antennas -> selector -> RX2

       The emitter's absolute phase is common and cancels in RX2_i / RX1.
```

## ISM scope and the 50 MHz rule

The term “ISM band” is regulatory and regional, not a synonym for every common
unlicensed radio channel. ITU Radio Regulations No. 5.150 includes 433.05–434.79
MHz in Region 1, 902–928 MHz in Region 2, and the worldwide 2.400–2.500 and
5.725–5.875 GHz allocations. The United States table in 47 CFR 18.301 lists
915 MHz plus or minus 13 MHz, 2450 MHz plus or minus 50 MHz, and 5800 MHz plus
or minus 75 MHz; it does not add the Region-1-only 433 MHz profile. See the
[ITU Radio Regulations, 2024 edition](https://www.itu.int/hub/publication/r-reg-rr-2024/)
and [47 CFR 18.301](https://www.ecfr.gov/current/title-47/chapter-I/subchapter-A/part-18/subpart-C/section-18.301).

An ISM designation does not by itself authorize arbitrary communications or
power. Every active OTA run must use a country-specific authorization profile,
appropriate power and occupied bandwidth, or a shielded RF environment. The
engineering grid below defines calibration intent; the runtime regulatory
profile decides which TX conditions may actually be enabled. Passive reception
is a separate policy decision.

In this plan, “50 MHz spread” means **centre frequencies separated by 50 MHz**,
captured sequentially. It does not mean a single 50 MHz-wide waveform. Use this
canonical lattice:

| ISM allocation | Complete 50 MHz-lattice centres | Blind frequency holdout | Hardware/calibration note |
| --- | --- | --- | --- |
| 433.05–434.79 MHz, Region 1 | **433.920 MHz** | 433.500 and 434.350 MHz auxiliary closure | The band is only 1.74 MHz wide and lies below the released PCB LUT. The runner must reject bearing output until direct-injection calibration is extended below 500 MHz |
| 902–928 MHz, Region 2 | **915 MHz** | 905 and 925 MHz auxiliary closure | The band is only 26 MHz wide, so a second point 50 MHz away cannot remain in-band. The existing PCHIP value at 915 MHz needs a new low-band interstitial holdout |
| 2.400–2.500 GHz | **2.425, 2.475 GHz** | 2.450 GHz | Both primary centres are exact 12.5 MHz PCB-LUT knots; the holdout tests the OTA manifold between them |
| 5.725–5.875 GHz | **5.750, 5.800, 5.850 GHz** | 5.775 and 5.825 GHz | All are exact PCB-LUT knots and lie inside the independently validated 5–6 GHz interpolation region |

The edge frequencies are not used as OTA centres because the occupied signal
and filter transition need guard from the band edge. The lattice is deliberately
offset 25 MHz from the edge in the two wide bands. The 433 and 915 MHz auxiliary
points are closer closure tests, not a claim of 50 MHz spacing; forcing a
second 50 MHz point into either narrow allocation is mathematically impossible.

This exhausts the ISM allocations plausibly reachable by this radio architecture.
For completeness, 6.78, 13.56, 27.12, and 40.68 MHz lie below the candidate
radio range, while 24.125, 61.25, 122.5, and 245 GHz lie above it. They are
recorded with explicit exclusion reasons in the machine-readable plan rather
than silently omitted. Common 868 MHz SRD operation is not an ITU ISM allocation
and therefore needs its own jurisdiction profile instead of being mislabeled.

To claim support across the complete permitted portion of a band, add two
jurisdiction-generated **edge anchors** at `lower edge + required guard` and
`upper edge - required guard`. They are extra boundary anchors, not part of the
50 MHz lattice. Train the manifold at those anchors and reserve at least one
frequency between each edge anchor and its nearest regular centre as a blind
holdout. Until these pass, the supported frequency interval ends at the outer
regular centres rather than silently extrapolating to the allocation edges.

The same plan is available as a machine-readable artifact in
[`data/ism-frequency-plan.json`](data/ism-frequency-plan.json). Acquisition
software should consume a jurisdiction-approved derivative of this file rather
than constructing frequencies with an unconstrained arithmetic loop.

The PCB LUT covers 0.5–6.0 GHz. The 433 MHz profile is therefore a planned,
fail-closed extension—not a currently calibrated result. It requires a new
direct-injection sweep, interpolation holdouts, suitable filters/antennas, and
radio verification at 433.92 MHz before any OTA manifold is fitted.

On 2026-09-03, with both radios' transmit paths muted, receiver serial
`104000b29905000e17000800065934759d` accepted all 14 primary and holdout LOs in
the four-profile plan. Readback error was 0–2 Hz, within the declared 5 Hz
synthesizer tolerance. This is only an LO-capability result; it does not promote
the 433 MHz calibration or prove any OTA path. The complete observation table is
in [`data/receiver-ism-lo-acceptance-20260903.json`](data/receiver-ism-lo-acceptance-20260903.json).

If “50 MHz spread” instead means one simultaneous 50 MHz-wide signal, treat
that as a later wideband mode. The already tested 25 MS/s receive path cannot
represent a 50 MHz-wide complex baseband without aliasing. That mode needs a
higher-rate local capture/channelizer or several coherently related stepped
sub-bands; it must not be inferred from the sequential centre-frequency plan.

## What the evidence establishes

The conclusions below deliberately distinguish released measurements from
proposed engineering gates.

| Layer | Evidence | Current conclusion |
| --- | --- | --- |
| Radio capability | Both relevant Rev.C Plutos enumerate as AD9361/2R2T after the environment fix and accept a 5.8 GHz LO | Dual RX and dual TX are available at 5.8 GHz; discover the source by serial because its DHCP address moved from `.173` to `.179` |
| Safe bounded control | Static screens verified every selector acknowledgement and exact post-capture/final TX mute | Suitable for supervised experiments; retain fail-muted cleanup in the new streamer |
| PCB calibration | 25 direct-injection runs, 4,528 observations and 18.99 GB raw IQ, with independent raw replay | Use the released 0.5–6.0 GHz complex LUT, not a scalar delay model |
| PCB interpolation | Independent 5–6 GHz interstitial holdout | 1.12° spatial RMS, 2.49° absolute p95, 5.09° maximum; 0.141 dB magnitude RMS |
| 5.8 GHz repeatability | Five-repeat direct-injection qualifier | 0.059–0.137° phase SD and 0.005–0.014 dB magnitude SD |
| 5.8 GHz selector matrix | Eight driven-port by nine-state qualifier | Condition number 1.180; weakest wrong-state margins are ANT3 20.93 dB and ANT6 21.71 dB |
| Frequency model | Dense broadband calibration and held-out tests | A delay-only fit leaves roughly 16–21° RMS on ANT2–ANT7; frequency-selective ripple is real and repeatable |
| Centered HexRay | Fifteen held-out 2.4 GHz artifacts at five exact centers | The unchanged array can be normalized extremely repeatably, but one centered near-field vector is not an angular manifold |
| Earlier localization | Continuous switched captures at 2.4 GHz | TX2 was conditionally placed in a 19–35° sector, but the direct model had 53.9° RMS and range was prior dominated |
| 2026-09-03 two-source screen | 92 safe 5.8 GHz captures with the source on TX1 and TX2 | TX1 has a valid conducted RX1 reference. TX2 is plainly visible on RX2 near ANT8, but its intended tone is absent on RX1, so the static `RX2/RX1` TX2 phase is invalid |
| 2026-09-03 continuous two-source campaign | Ten dual-RX runs at 5750/5775/5800/5825/5850 MHz, 173.8 million samples | All sample timelines and cleanups passed; TX2 coherent-pilot bearing remains 181.0–185.5° and valid-frequency fusion gives 183.25° |

The PCB result is strong enough that electronics interpolation should contribute
only a small fraction of the eventual angle error. The dominant unqualified
terms are now final cables, antenna phase centres, mutual coupling, mounting,
multipath, and selector-time phase evolution.

![Measured PCB gain and phase LUT](../pcb_direct_injection_calibration/png/fig08_relative_phase_correction_heatmap.png)

![Independent PCB interpolation holdout](../pcb_direct_injection_calibration/png/fig12_holdout_residual_heatmaps.png)

### Initial static two-source screen

The latest setup was:

```text
TX1 -> 2-way splitter -> RX1 conducted reference
                    \-> TX1 antenna, about 30 cm from the array

TX2 ------------------> TX2 antenna, about 20 cm behind ANT8

C6/hex array -> selector common -> RX2
```

These screens are local preliminary evidence rather than a released calibration
artifact. Their immutable run records are under
`/srv/bulk/samteway/state/lab-runs/network-192.168.1.15/ota-two-source-bringup`:

| Condition | Run ID | Captures | Final state |
| --- | --- | ---: | --- |
| Both sources muted | `20260903T163015.812319Z` | 2 | Muted, `ALL_OFF` |
| TX1, -40 dB | `20260903T163123.118958Z` | 27 | Both radios muted, `ALL_OFF` |
| TX2, -40 dB | `20260903T163236.414514Z` | 27 | Both radios muted, `ALL_OFF` |
| TX1, -35 dB | `20260903T163421.588619Z` | 18 | Both radios muted, `ALL_OFF` |
| TX2, -35 dB | `20260903T163513.949003Z` | 18 | Both radios muted, `ALL_OFF` |

All 92 observations completed without a failed cleanup; the largest raw ADC
component was 222 counts, so none approached the campaign clipping limit.

At `-35 dB` source gain, intended-tone RX2 amplitude relative to the local
`ALL_OFF` floor was:

| Port | TX1 dB over floor | TX2 dB over floor |
| --- | ---: | ---: |
| ANT1 | +6.61 | +11.67 |
| ANT2 | +6.75 | -5.01 |
| ANT3 | -0.43 | +0.91 |
| ANT4 | -0.77 | -0.63 |
| ANT5 | +0.26 | +9.57 |
| ANT6 | -0.23 | +0.29 |
| ANT7 | +1.33 | +9.59 |
| ANT8 | +2.54 | **+15.80** |

TX2 being strongest at ANT8 agrees qualitatively with its placement. This is a
path-health observation, not yet a bearing: the source is near field, the room
is reflective, and the installed manifold has not been measured. TX1's weak
ANT4 response despite its stated placement is a particularly useful warning
that an ideal free-space model must not be trusted before cable and OTA closure.

The source and receiver were offset by about 600 Hz at the nominal 100 kHz
pilot. The legacy coarse search selected an unrelated approximately 187.36 kHz
spur in the TX2 files because RX1 did not contain the intended TX2 pilot. The
intended approximately 99.4 kHz TX2 signal is present on RX2. This is an
estimator/reference failure, not a TX2 hardware failure.

The follow-on continuous campaign fixes that estimator failure with a -100 kHz
TX1 conducted pilot and +100 kHz TX2 target from the same dual-channel source.
The fitted tone separation is stable to 0.004 Hz across the five RF centres,
minimum coherent-estimator SNR is 23.2–38.4 dB for TX2, and forward/reverse
repeat phase is at most 2.33°. See the
[full verification report](../tracking_verification_campaign/README.md).

## Why TX2 absolute phase is unnecessary

For one narrowband emitter, write the two observations during selector dwell
`i` as

\[
x_1[n] = r\,s[n]e^{j\theta[n]} + v_1[n],
\qquad
x_{2,i}[n] = b_i\,a_i(p,f)\,s[n]e^{j\theta[n]} + v_{2,i}[n].
\]

Here `r` is the reference path, `b_i` is the board/cable path, `a_i` is the
spatial response, and `theta[n]` contains the emitter's unknown absolute phase
and source/receiver clock drift. When RX1 and RX2 observe the same emitter at
the same time,

\[
\hat h_i=
\frac{\sum_n w[n]x_{2,i}[n]x_1[n]^*}
     {\sum_n w[n]|x_1[n]|^2+\epsilon}
\approx \frac{b_i a_i(p,f)}{r}.
\]

The source waveform and common phase cancel. Apply the board and cable
corrections, then remove the remaining snapshot-common complex scalar:

\[
z_i=C_{\mathrm{board},i}(f)C_{\mathrm{cable},i}(f)\hat h_i,
\qquad
\tilde z=\frac{z}{\|z\|_2}e^{-j\arg z_{\mathrm{ref}}}.
\]

Only the relative complex vector `tilde z` carries bearing. Absolute TX phase,
absolute RX phase, and one common calibration phase are gauge freedoms.

If RX1 cannot hear the target, an RX2-only switched measurement is still
possible, but the common phase must be evaluated at a common epoch:

\[
\phi_i(t_i)=\phi_0+2\pi\Delta f t_i+\phi_{\mathrm{path},i}
             +\phi_{\mathrm{space},i}.
\]

The static runner loses the required relationship between the `t_i`. A
continuous sample counter, repeated anchor port, or simultaneous pilot is what
removes \(2\pi\Delta f t_i\); knowledge of \(\phi_0\) is not required.

## Array configuration

### Recommended C6 first article

Use the six strongest PCB paths and put electrically matched pairs across each
diameter. This is the geometry identity `c6-v2`; it is not compatible with the
old `hexcal-v1` assumption that ANT1 through ANT6 are clockwise.

| Bearing clockwise from forward | PCB port | Ideal position at 6 GHz `(x right, y forward)` mm | Opposite |
| ---: | --- | ---: | --- |
| 0° | ANT1 | `(0.000, +24.983)` | ANT8 |
| 60° | ANT2 | `(+21.636, +12.491)` | ANT7 |
| 120° | ANT4 | `(+21.636, -12.491)` | ANT5 |
| 180° | ANT8 | `(0.000, -24.983)` | ANT1 |
| 240° | ANT7 | `(-21.636, -12.491)` | ANT2 |
| 300° | ANT5 | `(-21.636, +12.491)` | ANT4 |

Omit ANT3 and ANT6 initially. At 5.8 GHz they are about 6–9 dB weaker than the
other selected paths and have the two lowest isolation margins. A digital gain
correction cannot restore the SNR already lost in a weak analog path.

Use the temporal order

```text
forward: ANT1, ANT8, ANT2, ANT7, ANT4, ANT5
reverse: ANT5, ANT4, ANT7, ANT2, ANT8, ANT1
```

so every diametric baseline is sampled close in time. Alternate forward and
reverse scans to expose and cancel first-order motion bias.

![Recommended C6 and C8 geometry and scan orders](../pcb_direct_injection_calibration/png/fig15_recommended_c6_c8_layout_and_port_map.png)

### C8 extension

After C6 passes blind tests, add ANT3 and ANT6 to form UCA8. Keep the measured
pairs on diameters: ANT1/ANT8, ANT2/ANT7, ANT3/ANT6, and ANT4/ANT5. Use the
pair-first order `ANT1, ANT8, ANT2, ANT7, ANT3, ANT6, ANT4, ANT5`, then reverse
it. Whiten by measured per-port noise so ANT3/ANT6 cannot dominate merely
because their inverse gain corrections are large. Compare C8 and C6 on exactly
the same captures; retain C8 only if blind-angle error or ambiguity improves.

The C6 diameter is 49.97 mm and C8 diameter is 65.28 mm when designed to avoid
spatial aliasing through 6.0 GHz. That compact aperture will have weak angular
resolution at low frequencies even though the PCB LUT extends to 0.5 GHz.

### Band-scaled array profiles

Direction sensitivity scales with aperture measured in wavelengths. Preserve
the PCB port pairing and software interfaces, but use a band-appropriate antenna
set and preferably a band-scaled mechanical aperture:

| Profile | Highest design frequency | Recommended C6 radius | Maximum alias-safe C6 radius | Recommended C8 radius | Antenna requirement |
| --- | ---: | ---: | ---: | ---: | --- |
| `ism433-c6-v1` | 434.79 MHz | about 310 mm | 344.75 mm | about 405 mm for C8 | Matched 433 MHz elements and cables; new PCB calibration below 500 MHz |
| `ism915-c6-v1` | 928 MHz | about 145 mm | 161.5 mm | about 190 mm for C8 | Matched 902–928 MHz elements and cables |
| `ism2450-c6-v1` | 2.500 GHz | about 54 mm | 60.0 mm | about 70 mm for C8 | Matched 2.4 GHz elements and cables |
| `ism5800-c6-v1` | 6.000 GHz design limit | **24.983 mm** | 24.983 mm | **32.641 mm for C8** | Matched 5.7–5.9 GHz elements and cables |

The recommended 433 MHz, 915 MHz, and 2.4 GHz radii retain roughly 10% mechanical margin
below the C6 half-wavelength adjacent-spacing limit. Their exact coordinates
must be generated and surveyed before calibration. If one physical array must
cover all four candidate bands, the 6 GHz spacing limit controls and the existing
approximately 25 mm C6 radius is safe—but its diameter is only about 0.15
wavelength at 915 MHz and 0.07 wavelength at 433 MHz, so its low-band likelihood
will be broad and noise-sensitive. A multi-band antenna does not solve the
aperture problem.

Give every combination of band, antenna set, cable set, and coordinates a
different geometry/manifold ID. The PCB LUT may be shared after closure, but an
OTA manifold may not be transferred between these profiles.

## Calibration stack

Treat calibration as composable, versioned layers with explicit reference
planes:

| Order | Layer | How it is measured | When it becomes invalid |
| ---: | --- | --- | --- |
| 1 | Temporal phase reference | Simultaneous RX2/RX1 ratio, or qualified continuous phase tracker | Reference antenna/path, clock, or continuity contract changes |
| 2 | PCB selector path | Released 12.5 MHz PCHIP complex LUT | Board replacement, repair, or failed closure check |
| 3 | Final cable set | VNA S21 or direct injection at installed cable tips | Cable, port assignment, torque, bend, or routing changes |
| 4 | Surveyed geometry | Actual antenna phase-centre `x/y/z`, polarization, and array yaw | Mechanics or antenna pose changes |
| 5 | Installed OTA manifold | Known-angle complex measurements of the completed array | Antenna, enclosure, environment class, or mounting changes materially |
| 6 | Noise/leakage model | `ALL_OFF`, terminated noise, and optional complex selector matrix | Gain, bandwidth, frequency band, or RF environment changes |

Never add separately wrapped phase tables. Store continuous unwrapped phase or
complex coefficients and compose them by complex multiplication. Do not apply
the board LUT on top of an end-to-end manifold that already includes the board
unless that manifold was explicitly defined after board correction.

At runtime, choose calibration by `(board_uid, band_profile, centre_frequency,
cable_set_uid, antenna_set_uid, geometry_uid, manifold_uid)`. A nearby
frequency is not sufficient if any identity differs.

![Calibration reference planes](../pcb_direct_injection_calibration/png/fig01_campaign_setup_and_reference_planes.png)

## Development campaign

### Phase 0 — freeze the experiment

Before fitting another angle:

- Confirm the physical clockwise port map and assign geometry ID `c6-v2`.
- Label and strain-relieve every final antenna cable. Record connector torque.
- Survey antenna phase-centre coordinates, array height, TX height, range, and
  yaw. Target 0.25 mm repeatability; 1 mm is about 7.2° at 6 GHz.
- Discover each Pluto by immutable serial and record the current IP only as an
  endpoint. Refuse a serial mismatch.
- Warm the radio and board for 30 minutes, then take one control observation.
- Close the first experiment at exactly 5.8 GHz, then run the declared ISM
  frequency lattice. Do not substitute an arbitrary broadband sweep for
  band-specific angular holdouts.

**Exit gate:** a machine-readable fixture manifest completely identifies the
board, radios, cables, antennas, geometry, LUT, gains, bandwidth, firmware, and
software revision.

### Phase 1 — make TX1 recover known bearings

TX1 is the controlled truth source because the splitter gives RX1 a strong
simultaneous copy of the exact OTA signal.

1. First keep the current slow static selector. At each state, estimate
   `h_i = RX2_i/RX1`, apply the PCB/cable correction, and retain amplitude,
   phase, SNR, coherence, clipping, and `ALL_OFF` contrast.
2. Put TX1 at the array height and a practical far-field distance, preferably
   1–2 m in the least reflective available space. The theoretical `2D^2/lambda`
   bound for this small array is shorter, but it is not a guarantee against the
   antennas, bench, and room.
3. Measure initial sanity bearings `0, 90, 180, 270°` at 5.8 GHz, then training
   bearings every 10° around the circle. Reserve the interleaved 5° bearings as
   blind holdouts.
4. At every bearing take at least three forward/reverse scan pairs, plus
   `ALL_OFF`. Repeat a subset after a reconnect and after thermal/reboot cycles.
5. Compare two solvers without tuning on the holdouts: surveyed ideal geometry
   and empirical matched manifold.
6. When 5.8 GHz closes, repeat the angular campaign at 5.750 and 5.850 GHz in a
   randomized frequency order. Test 5.775 and 5.825 GHz without using them to
   fit the frequency interpolator.
7. Repeat the same procedure with the 2.4 GHz band profile at 2.425 and 2.475
   GHz, holding out 2.450 GHz. Qualify the 915 MHz array separately at 915 MHz
   and use 905/925 MHz only as within-band frequency closure tests.
8. Only after extending and independently validating the PCB LUT below 500 MHz,
   qualify the Region-1 433 MHz array at 433.920 MHz and use 433.500/434.350 MHz
   as narrow-band closure tests. This profile is disabled outside an applicable
   jurisdiction or shielded setup.

Start with the normalized, noise-whitened matched-manifold score

\[
P(\theta)=
\frac{|a(\theta)^H R_n^{-1}z|^2}
     {a(\theta)^H R_n^{-1}a(\theta)}.
\]

This test answers whether the board-corrected installed array actually has an
angular signature. It also reveals port mapping or polarity errors immediately.

**Proposed first-pass gates:**

| Quantity | Gate |
| --- | ---: |
| Metadata continuity / selector identity | 100% valid, zero splices or guessed states |
| RX1 same-emitter coherence | >= 0.95 per admitted dwell |
| Repeat relative phase SD | <= 2° per port at 5.8 GHz |
| Selected/`ALL_OFF` contrast | >= 20 dB per used port |
| Empirical-manifold blind median error | <= 5° |
| Empirical-manifold blind p95 error | <= 10° |
| Blind frequency interpolation | No more than 2° p95 degradation versus an exact trained centre |
| Invalid/ambiguous holdouts | Reported invalid, never forced to an angle |

These are proposed engineering targets, not achieved bearing results.

### Phase 2 — use TX2 as a blind source

Once TX1 angle and frequency holdouts pass for a band profile, freeze that
manifold. Move TX2 to withheld positions and do not give its bearing or exact
frequency holdout to the fitting code.

There are three useful TX2 modes:

| Mode | Hardware | Purpose | Generalizes to an unknown field emitter? |
| --- | --- | --- | --- |
| A. Same-emitter RX1 reference | Put a fixed RX1 antenna near the array centre so RX1 and RX2 both hear TX2 | Cleanest end-to-end direction-finding test | **Yes; recommended deployment architecture** |
| B. Repeated RX2 anchor | Keep TX2 continuous and revisit a strong port such as ANT8 between other states | Proves absolute TX phase is unnecessary and tests reference-free tracking | Yes, for one sufficiently stable, continuously observable emitter |
| C. Conducted TX1 pilot | Keep TX1 on RX1 and emit TX2 at a distinct offset from the same dual-TX Pluto; demodulate both on one continuous sample timeline | Excellent development diagnostic for phase/timestamp logic | No; the unknown emitter will not share TX1's LO |

Mode A reuses exactly the TX1 estimator and is the long-term choice. Keep the
reference antenna near the centre to minimize direction-dependent reference
delay, but include its measured position and pattern in the manifold.

For Mode B, use a schedule such as

```text
ANT8, ANT1, ANT8, ANT2, ANT8, ANT4,
ANT8, ANT5, ANT8, ANT7, ANT8
```

and interpolate the unwrapped ANT8 common phase to each intervening dwell.
This costs scan speed and can be biased by target motion, but it is a clean
identifiability experiment.

For Mode C, TX1 and TX2 use distinct baseband offsets. Estimate each tone in the
same sample windows with the absolute FPGA sample index. The shared TX LO and
shared RX LO phase then cancel between the demodulated TX2/RX2 and TX1/RX1
phasors; only one fixed TX-channel phase remains, and that common scalar cannot
steer the bearing. It is essential that DDS generation and acquisition stay on:
restarting either tone recreates the failure in the current static screen.

Mode C is now implemented as direct cross-frequency correlation. The 2026-09-03
hardware campaign passed at all five 5.8 GHz primary/holdout frequencies and
recovered the stated TX2 direction. It validates phase/timestamp logic but,
exactly as the table notes, does not replace a same-emitter deployment
reference.

Test TX2 first at the same far-field range used for the TX1 manifold. Run the
5.750/5.800/5.850 GHz profile in randomized order, then repeat the frozen test
at the 5.775/5.825 GHz frequency holdouts. Promote 2.4 GHz and 915 MHz only with
their matching array, antenna, cable, and manifold identities. The present 20
cm placement is a separate near-field problem. After far-field bearing passes,
fit near-field position using

\[
a_i(p,f)=A_i(p,f)\exp\{-jk[\|p-r_i\|-\|p-r_0\|]\},
\]

and report both bearing and range only when the posterior is not
wavelength-aliased. A single-frequency CW source is normally good for bearing
but poor for unique range.

For a signal observed at more than one ISM centre, normalize and solve each
frequency independently, then combine calibrated likelihoods:

\[
\log P_{\mathrm{joint}}(\theta)
=\sum_f \alpha_f\log\left[P_f(\theta)+\epsilon\right],
\]

where `alpha_f` is based on admitted SNR, coherence, manifold residual, and
calibration uncertainty. Do not add raw phases from 915 MHz, 2.4 GHz, and 5.8
GHz: every retune has an arbitrary common phase and may use a different antenna
and geometry profile. Likelihood fusion gains ambiguity rejection without
requiring absolute cross-band phase. Also emit the per-frequency bearings so a
bad band cannot hide inside the joint score.

**Exit gate:** TX2 blind bearing meets the same predeclared median/p95 targets
as TX1 without refitting the withheld angle or frequency. Repeat after moving
both source and array so room multipath cannot masquerade as geometry.

### Phase 3 — prove tracking, not just static localization

Move TX2 slowly through surveyed bearings while collecting a continuous stream.
The estimator should emit, for every scan:

- raw bearing likelihood over 0–360°;
- peak bearing and circular peak width;
- peak-to-second-peak or peak-to-sidelobe ratio;
- complex manifold residual;
- per-port SNR/coherence and ports used;
- continuity, selector, LUT, geometry, and manifold identities; and
- a validity reason when no bearing is emitted.

Track within one tuned centre first. A sequential multi-frequency tracker may
retune among the band's 50 MHz centres on a slower supervisory cadence—for
example, maintain the main 5.8 GHz track continuously and take periodic 5.75
and 5.85 GHz confirmation bursts. Retune latency, LO settling, and the loss of
observations during a hop must be measured; they are not selector dwell time.
Do not hop among 915 MHz, 2.4 GHz, and 5.8 GHz while claiming one uninterrupted
fast track unless the source is known to persist across the entire cycle.

Feed valid measurements to a circular alpha-beta or Kalman tracker. The tracker
may smooth a valid noisy angle; it must not turn an invalid RF observation into
a plausible trajectory. Tune dynamics only on training tracks and evaluate on
withheld motion patterns, speeds, powers, and room positions.

### Phase 4 — optimize switching speed

The final acquisition must be one uninterrupted dual-RX stream with selector
state boundaries expressed on the FPGA sample timeline. Host command times are
not accurate enough. In descending order of confidence, use:

1. selector state/transition metadata sampled into the FPGA stream;
2. a hardware marker from the selector MCU captured by the Pluto FPGA;
3. a prequalified autonomous schedule plus FPGA sample counter and periodic
   unambiguous markers;
4. RF-energy edge inference only as a diagnostic fallback.

The existing 200 us active / 20 us guard HexRay profile proves that high-rate
experimentation is plausible, but its short guard is an experimental waiver,
not a released switching guarantee. The new `c6-v2` profile needs its own edge,
settling, state-identity, and illegal-code qualification.

At 2 MS/s, useful starting points are:

| Active dwell | Samples/dwell | Ideal coherent sample gain | C6 theoretical scans/s with 20 us guards | C8 theoretical scans/s |
| ---: | ---: | ---: | ---: | ---: |
| 100 us | 200 | 23.0 dB | 1,389 | 1,042 |
| **200 us** | **400** | **26.0 dB** | **758** | **568** |
| 500 us | 1,000 | 30.0 dB | 321 | 240 |
| 1,000 us | 2,000 | 33.0 dB | 163 | 123 |

The scan rates are arithmetic ceilings before periodic markers, buffering, and
rejected transitions. With 10–15% schedule overhead, 200 us suggests a planning
rate around 650 C6 or 500 C8 scans/s. These remain arithmetic ceilings. Replay
of the new raw campaign shows that the current link budget needs about 200 ms
per state for a conservative universal setting; TX2 reaches the joint phase/SNR
target at 10 ms. Improve link budget and qualify descending dwell lengths before
attempting the microsecond-scale table.

An FFT bin width does not set the minimum dwell for a known pilot. Use a complex
correlator, Goertzel estimator, or PLL at the refined tone. For other signals:

| Signal | Per-dwell observable | Important limitation |
| --- | --- | --- |
| Known CW/pilot | Complex matched filter at refined frequency | Most sensitive and fastest development mode |
| Unknown narrowband carrier | Detect frequency on RX1, then matched-filter both channels | Requires the carrier to remain coherent across the scan |
| Continuous modulated signal | Windowed RX2/RX1 cross-correlation or cross-spectrum | RX1 must receive adequate common signal bandwidth |
| Wideband waveform | Cross-spectrum with robust phase slope/earliest-path gating | More compute, but can reduce CW range and multipath ambiguity |
| Bursty emitter | RX1 burst detector plus only coincident RX2 dwells | A single switched chain can miss ports during a short burst |
| Multiple simultaneous emitters | Separate by frequency/waveform before forming each steering vector | A switched array is not a simultaneous multi-source covariance array |

Treat 50–100 Hz user-facing output as a later target, not a current capability.
At present TX2 supports about 8.3 forward/reverse C6 pairs per second at its
10 ms replay-qualified dwell, while the conservative 200 ms universal dwell
supports about 0.42 paired scans/s before overhead. Adaptive integration and a
better link budget are required to move higher.

Qualify dwell independently for every ISM profile. Equal received power does
not imply equal dwell: antenna efficiency, environmental noise, channel
occupancy, fractional bandwidth, and electrical aperture differ sharply among
915 MHz, 2.4 GHz, and 5.8 GHz. The 200 us result may be valid for one band and
invalid for another.

## Minimal software architecture

The present repository contains excellent campaign and forensic tooling, but
the online path should be intentionally smaller. Build a new runtime package
with five narrow responsibilities:

```text
stream     -> continuous dual-RX samples, FPGA sequence, selector intervals
channel    -> signal detection, frequency tracking, per-dwell complex h_i
calibrate  -> identity checks, board/cable LUT, noise/leakage correction
solve      -> ideal or empirical manifold likelihood and quality metrics
track      -> circular motion filter, validity propagation, output protocol
```

Suggested boundaries under `src/smateway/tracking/`:

| Module | Owns | Must not own |
| --- | --- | --- |
| `stream.py` | Ring buffers, exact sample indices, gap detection | RF fitting or plotting |
| `schedule.py` | Geometry ID, physical port map, temporal sequence, state intervals | Inferring a state after a failed marker |
| `channel.py` | Tone/search tracking and `RX2/RX1` estimates | Board or antenna calibration |
| `calibration.py` | Immutable coefficient lookup, PCHIP, identity/range checks | Campaign fitting |
| `manifold.py` | Surveyed coordinates and empirical steering-table interpolation | Live sample acquisition |
| `bearing.py` | Noise-whitened likelihood, ambiguity and residual | Temporal smoothing |
| `tracker.py` | Circular state estimator and measurement rejection | Repairing invalid bearings |
| `cli.py` | `record`, `replay`, `track`, and `qualify` commands | Hidden hardware defaults |

The exact same estimator must run live and during replay. Raw data storage can
be optional during normal tracking, but every qualification run should retain
immutable IQ, identities, coefficient hashes, state/sample intervals, and
solver output. Campaign fitting and PNG generation remain offline.

## Acceptance matrix

| Milestone | Experiment | Required decision |
| --- | --- | --- |
| M1 electronics closure | TX1 at one surveyed bearing, slow static selector | Every C6 port produces stable board-corrected `RX2/RX1`; explain ANT4 or stop |
| M2 angular observability | TX1 at cardinal and then 10° training bearings | Empirical manifold is smooth and distinct around 360° |
| M3 blind localization | Interleaved 5° TX1 holdouts | Meet predeclared median/p95 bearing gates without fitting holdouts |
| M4 5.8 GHz frequency closure | Train 5.750/5.800/5.850; withhold 5.775/5.825 GHz | Frequency interpolation meets the gate without fitting holdouts |
| M5 independent target | TX2 at withheld far-field bearings and frequencies | Same frozen manifold locates TX2; absolute TX2 phase unused |
| M6 2.4 GHz profile | Band-scaled array at 2.425/2.475 GHz; withhold 2.450 GHz | Independent band profile meets the same declared angle policy |
| M7 915 MHz profile | Larger array at 915 MHz; 905/925 MHz closure | Quantify low-band resolution and decide whether the physical size is worthwhile |
| M8 433 MHz Region-1 profile | Extend PCB LUT below 500 MHz, then larger array at 433.920 MHz; withhold 433.500/434.350 MHz | No output before RF-chain and interpolation closure; quantify practical resolution |
| M9 near-field model | TX2 on a surveyed range/bearing grid | Bearing closes; range emitted only where aliases are resolved |
| M10 motion | Continuous TX2 trajectories | No phase slips; confidence falls before gross angle failure |
| M11 dwell reduction | 200 -> 100 -> 50 -> 20 -> 10 ms, then microseconds only after link improvement | Choose the shortest dwell whose blind p95 and invalid rate remain acceptable |
| M12 C8 comparison | Add ANT3/ANT6, same blind corpus | C8 retained only if it improves error/ambiguity after noise weighting |
| M13 robustness | Power, frequency, reboot, temperature, reconnect, room/range holdouts | Define calibration lifetime and operating envelope |

## Immediate next experiment

The highest-value next action is **not another broadband sweep**. It is a
surveyed TX1 angular sanity test at 5.8 GHz:

1. Confirm the six physical ports follow `c6-v2`.
2. Put TX1 at array height, preferably 1–2 m away, on the ANT1/0° radial line.
3. Keep the TX1 splitter-to-RX1 reference connected.
4. Capture `ALL_OFF` and the six C6 states for five repeats, forward and reverse.
5. Apply the board LUT and inspect corrected phase, amplitude, coherence, and
   ideal-geometry residual before attempting a bearing.
6. Repeat at 90°, 180°, and 270°.

If ANT4 remains at the leakage floor, pause localization and swap only its
antenna/cable with a strong path. If the weakness follows the cable/antenna, fix
the installed feed. If it stays at ANT4 despite the direct-injection PCB pass,
inspect the current port mapping and connection. This one fork will prevent us
from teaching the manifold around a wiring fault.

After the four TX1 positions pass, build the 10°/5° train/holdout manifold and
then run TX2 as the frozen-model validation source. Close 5.750/5.800/5.850 GHz
and their two frequency holdouts before moving to the band-scaled 2.4 GHz and
915 MHz fixtures. Treat the Region-1 433 MHz profile as a separate extension:
first extend the PCB LUT and verify the RF chain, then qualify its much larger
array at 433.920 MHz.

## Risks and explicit non-goals for the first release

- The board LUT calibrates the PCB reference plane, not moved deployment
  cables or antennas.
- A fixed delay per port is not an acceptable replacement for the LUT.
- Excellent repeated phase does not prove the free-space geometry model; the
  earlier localization experiment demonstrated exactly that failure mode.
- A 20 cm TX2 range is likely near field and strongly exposed to room coupling.
- Indoor multipath may be stable enough to look calibratable while failing
  after either endpoint moves. Blind room/range holdouts are mandatory.
- Narrowband phase gives excellent bearing sensitivity but wavelength-periodic
  range. Do not report centimetre-level range from a single CW tone.
- The first release will target one sufficiently persistent emitter. Short
  bursts and multiple simultaneous emitters require additional observability.
- ANT3/ANT6 remain conditional until their loss and isolation are repeated.
- The 50 MHz plan is a sequential centre-frequency lattice, not proof of a 50
  MHz instantaneous acquisition bandwidth.
- Every eligible centre is included: 433.920 MHz; 915 MHz; 2.425/2.475 GHz; and
  5.750/5.800/5.850 GHz. The narrower bands necessarily have only one regular
  lattice centre.
- Regulatory permission, allowed occupied bandwidth, and power are
  jurisdiction-specific; a calibration frequency is not automatically an
  authorized OTA transmit condition.

## Related released evidence

- [Machine-readable ISM frequency plan](data/ism-frequency-plan.json)
- [Two-source tracking verification campaign](../tracking_verification_campaign/README.md)
- [Muted receiver ISM LO-acceptance evidence](data/receiver-ism-lo-acceptance-20260903.json)
- [PCB direct-injection calibration and runtime LUT](../pcb_direct_injection_calibration/README.md)
- [Current calibration and direction-finding status](../current_calibration_and_df_status/README.md)
- [Earlier phase-localization experiment](../localization/phase-localization-experiment-report-20260825.md)
- [Dense 1 MHz frequency campaign](../dense_1mhz_campaign/README.md)
- [Broadband three-sweep campaign](../broadband_external_fixture_campaign/README.md)
- [5.8 GHz corrected-fixture campaign](../5g8_external_fixture_campaign/README.md)

## Bottom line

We do not need TX2's absolute phase. We need a calibrated relative array vector
whose elements refer to the same source epoch. TX1 gives us that vector today
through the conducted RX1 reference. The coherent-pilot TX2 implementation now
proves the continuous timing and second-source laboratory path; its five-centre
bearing remains directionally correct. Next use TX1 to measure the installed
angular manifold, freeze it, and use TX2 for blind angle validation. Qualify this first
on the 5.750/5.800/5.850 GHz lattice, then with separate 2.4 GHz and 915 MHz
array profiles, and finally the calibration-gated 433 MHz Region-1 profile.
Once that passes, deterministic sample-timestamped switching—
not another undirected full-band sweep—is the path to a fast, robust tracker.
