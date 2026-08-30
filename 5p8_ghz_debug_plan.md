# 5.8 GHz calibration debug and physical-attribution plan

| Field | Value |
|---|---|
| Campaign ID | `5p8-debug-r1` |
| Plan status | **ENGINEERING CHECKPOINT PUBLISHED; HOST HARDENING, FULL REGRESSION, AND HARDWARE CAPTURE PENDING** |
| Date | 2026-08-29 |
| Pre-tooling Smateway baseline | `df3438f22b10996fc0674715af28cca2a1dbcf1f` |
| Tooling source freeze | Clean pushed commit containing this plan; exact commit is sealed into every immutable run plan |
| Selector board | `stm32c011-4c0055000950313950363920` |
| Pluto serial | `104000b29905000e17000800065934759d` |
| Current USB URI | `usb:1.3.5` — resolve again after every USB reconnect |
| Calibration disposition | Exact 5.8 GHz remains rejected; no coefficients may be deployed |
| Durable report | [`docs/5g8_root_cause_analysis/README.md`](docs/5g8_root_cause_analysis/README.md) |

This file is the single execution plan and checklist for isolating the coherent 5.8 GHz
`ALL_OFF` term. It replaces neither the immutable per-run `plan.json` files nor the run-specific
fixture/setup attestations. Every RF capture must still be admitted by its runner's exact
contract.

## 1. Objective and completion definition

Localize the 5.8 GHz coherent baseline to the narrowest supported physical boundary, confirm the
leading mechanism with an independent intervention, apply the least-invasive supported fix, and
repeat the qualification measurement.

The campaign is complete only when:

1. the untouched fixture, muted control, input-drive-off control, and physical A/B/C/D/E stages
   have accepted, source-bound artifacts;
2. the first material boundary has a frequency sweep and a controlled one-variable
   perturbation;
3. the leading mechanism is independently confirmed or the final disposition explicitly remains
   unresolved;
4. any supported fix is retested at the implicated boundary and in the simultaneous fixture; if
   no fix is supported, X instead records a predeclared controlled falsifier and the disposition
   remains unresolved;
5. `C_raw` and `C_path` are both reported, with deployment prohibited unless each is at least
   20 dB;
6. any accepted physical fix is followed by fresh timing qualification and a new calibration
   matrix; and
7. normalized data, analysis, figures, hashes, limitations, and the hypothesis disposition are
   committed and pushed to `main`.

No soldering, PCB cutting, connector replacement, or other irreversible modification is
authorized by this plan. Such work requires a separate, evidence-backed user decision.

## 2. Starting facts

The historical/expected full conducted fixture is shown below. It is not asserted to be the
present physical state until the Section 5 photograph, port map, and operator checklist are
complete.

```text
Pluto TX1
   |
   +-- matched 2-way -- attenuated branch ----------------> Pluto RX1 reference
   |
   +-- 2-8 GHz 8-way -- F1..F8 --> selector ANT1..ANT8
                                             |
                                      selector common
                                             |
                                          Pluto RX2

Pluto TX2: digitally muted and physically 50-ohm terminated
```

Existing evidence establishes:

- RX1 is a strong, stable, unclipped conducted reference.
- RX2 selected-path coherent detection SNR is high.
- RX2 sees repeatable `ALL_OFF` leakage at 5.8 GHz:
  `|H_off| = 0.059257`, or `-24.545 dB` relative to RX1.
- Median desired-path transfer is `0.119179`, so aggregate physical desired/leakage contrast is
  only `6.069 dB`; zero of 160 path observations passes 20 dB.
- RX1 is directly connected to the 2-way splitter. RX2 is indirectly connected to the 8-way
  network through the selector. There is no accepted truly splitter-disconnected RX capture.
- Feed permutations, TX gain tracking, the TX2-antenna removal control, model rejection, and the
  selector-history guard replay narrow the hypotheses but do not localize the physical path.

The unresolved observable is:

```text
H_off = H_Pluto/local + H_RX2-cable/common + H_selector/common-launch + H_input-fixture
```

## 3. Passive preflight snapshot

The following was established read-only on 2026-08-29 before this plan was authored:

- Pluto USB identity is `usb:1.3.5`, serial
  `104000b29905000e17000800065934759d`.
- Firmware reports `v0.40-plutoplus-spf-tandem-agc-v7` and metadata ABI 2.
- The required native library is `/usr/local/lib/libiio.so.0.25`, version `0.25/c26258b`, SHA-256
  `d0a18bddcb54d182262acb2a9e31a88c81618cb43789320b8381c149777bef89`, with the
  kernel-buffer-count symbol present.
- Both TX gains read `-80 dB`; all eight DDS channels read disabled/scale zero at the instant of
  inspection.
- The selector responds over SWD, proving target logic power and the SWD path.
- At the instant of this passive snapshot, the target contained the Fast20 image, not the reviewed
  static bench image. Its read flash prefix matched
  `build/STM32C011F4P6/fast20/pluto_fast20.bin`, SHA-256
  `aeaed9d2f892d2a59add1aba2a7477e349b750c99f81610632286d04d91326ac`.
- That prefix match is point-in-time image evidence; it does not prove that the target is still
  powered, still contains that image, or is actively following the Fast20 timing schedule.
- The reviewed static mailbox is absent; the instantaneous GPIO latch is not proof of a physical
  RF state. Static Stage C/D/E execution is forbidden until the reviewed bench image is
  deliberately flashed, read back exactly, and reports lease-free `ALL_OFF`.
- Bench-supply voltage/current telemetry is unavailable to software and must be recorded by the
  operator.
- No capture or OpenOCD process was active; approximately 220 GiB of local storage was free.
- The 130 existing exact-5.8-GHz raw captures remain present and integrity-checked.
- The two historical Stage-A plans have zero attempts but are obsolete and are not execution
  authority.
- An offline optimizer-seed sensitivity control reran the same admitted closed-loop calibration
  cohort with seeds `0`, `1`, `2`, `3`, `7`, `42`, `20260827`, `0x5A8`, `0xC0FFEE`, and
  `0xFFFFFFFF`. All ten produced the same 5.8-GHz point result to numerical precision:
  `0.158351929 dB` amplitude RMS, `0.812145757°` phase RMS, and `-8.764165473 dB` worst
  selected-to-`ALL_OFF` contrast. Maximum gain-coefficient change was `0 dB`; maximum phase-
  coefficient change was `5.1e-7°`. This rules out random optimizer initialization as the fix;
  bootstrap seeds affect interval resampling, not the measured IQ or fitted point estimate.

## 4. Questions frozen before capture

1. Does a material 5.8 GHz component remain when both transmitters and every DDS are exactly
   muted?
2. Does the coherent baseline require RF drive into the 8-way/selector inputs?
3. Does it remain when RX2 is terminated at the frozen receiver-chain boundary (the Pluto RX2
   connector when no RX2 attenuator is installed, or the attenuator fixture-side plane when one
   is present)?
4. What complex increment is added by the fixed RX2 cable?
5. What increment is added by the powered selector common launch, board, supply, and control
   harness when every input is independently terminated?
6. Is leakage structured by driven input and selected state, or broadly poor across the matrix?
7. Do independently measured, correctly excited input terms predict the simultaneous fixture?
8. At which boundary does the sharp frequency-selective 5.8 GHz rise first appear, and does a
   controlled cable/load perturbation move or damp it?

## 5. Mandatory physical inventory and setup evidence

Do not execute P0 or any later RF stage until the current topology has one clear labeled setup
photograph and the operator confirms the items that software cannot observe. Before Stage A,
create fixture-v2 and run-specific setup-attestation files from:

- [`docs/5g8_root_cause_analysis/fixture_manifest_v2.stage-a.template.json`](docs/5g8_root_cause_analysis/fixture_manifest_v2.stage-a.template.json)
- [`docs/5g8_root_cause_analysis/setup_attestation_v1.stage-a.template.json`](docs/5g8_root_cause_analysis/setup_attestation_v1.stage-a.template.json)

P1 uses its separate untouched-fixture contract and operator guide:

- [`docs/5g8_root_cause_analysis/p1_muted_fixture_v1.template.json`](docs/5g8_root_cause_analysis/p1_muted_fixture_v1.template.json)
- [`docs/5g8_root_cause_analysis/p1_muted_setup_attestation_v1.template.json`](docs/5g8_root_cause_analysis/p1_muted_setup_attestation_v1.template.json)
- [`docs/5g8_root_cause_analysis/p1_muted_operator_templates.md`](docs/5g8_root_cause_analysis/p1_muted_operator_templates.md)

P2 uses its own two-load, run-bound fixture/setup contract and derivation guide:

- [`docs/5g8_root_cause_analysis/p2_input_off_fixture_v2.template.json`](docs/5g8_root_cause_analysis/p2_input_off_fixture_v2.template.json)
- [`docs/5g8_root_cause_analysis/p2_input_off_setup_attestation_v1.template.json`](docs/5g8_root_cause_analysis/p2_input_off_setup_attestation_v1.template.json)
- [`docs/5g8_root_cause_analysis/p2_input_off_operator_templates.md`](docs/5g8_root_cause_analysis/p2_input_off_operator_templates.md)

Record all of the following. `Uncharacterized` is allowed for screening, with null S-parameter
and return-loss evidence, but identity, port map, frequency range, and power rating are mandatory.

- [ ] Current setup photograph showing labels, cable routing, cable contact, and every RF port.
- [ ] 2-way splitter make/model/ID, frequency and power ratings, and exact port map.
- [ ] 8-way splitter make/model/ID, frequency and power ratings, input ID, output IDs, and exact
      `F1..F8 -> ANT1..ANT8` Rotation-0 map.
- [ ] RX1 attenuator make/model/ID, actual attenuation, frequency/power ratings, and orientation.
- [ ] Explicit RX2 attenuator state: `absent`, or exact make/model/ID, frequency/power ratings,
      attenuation, labelled-port orientation, Pluto-side connection, and fixture-side endpoint
      when present. Never infer presence from measured loss.
- [ ] Separate IDs and ratings for the TX1 stimulus load, RX2 load, TX2 load, and all eight
      selector-input loads.
- [ ] IDs/ratings/endpoints for every cable and adapter, especially the fixed selector-common to
      RX2 cable and the direct one-hot feed cable.
- [ ] Pluto TX1/TX2/RX1/RX2 physical port IDs and distinct reference-plane IDs.
- [ ] Selector board ID/revision, J2/common reference plane, bench-supply output ID, set voltage,
      current limit, displayed current, positive lead, power-ground, and control-ground IDs.
- [ ] Characterization evidence or explicit `uncharacterized` status for every passive.
- [ ] Confirmation that no component moved between the setup photograph and capture.

## 6. Universal safety and acquisition contract

### RF and physical safety

- No antennas are present in P0/P1/P2 or A through E.
- Power the target through exactly one of J12 bench 5 V or J1 USB-C. Never power it from a Pi
  5-V/3V3 rail or J11.1, and never energize J1 and J12 together.
- Join Pi/bench/target ground before SWCLK, SWDIO, or NRST. J11.1 is target VTref only and remains
  disconnected from Pi power.
- Before any selector flash, confirm NRST continuity, record the supply set voltage/current limit
  and displayed current, and require J11.1 in the `3.26–3.34 V` interval with no unexpected heat.
- Start OpenOCD only while target power and J11.1 are valid. Stop OpenOCD before target power-off.
- Both transmitters must be digitally muted and read back before every RF touch.
- TX2 remains at `-80 dB`, every TX2 DDS scale exactly zero, and TX2 physically terminated except
  in a separately planned port-pair test.
- Never hot-rewire. Mute first, stop capture/control processes, and remove the appropriate power
  before touching an RF connection.
- Every unused RF port receives an identified 5.8-GHz-rated 50-ohm load.
- RX1 protection/attenuation is never removed or bypassed.
- Start at the weakest declared TX condition and never exceed the established `-10 dB` ceiling.
- An unpowered selector is not a valid `ALL_OFF` termination.
- A GPIO latch alone is not selector-state evidence; static stages require mailbox and GPIO
  readback from the reviewed bench image.
- Final exact-radio mute and, where applicable, selector `ALL_OFF` readback are mandatory after
  every condition and every error path.

### Hardened A/B/C/E acquisition settings

Unless a new immutable plan states otherwise:

| Parameter | Value |
|---|---:|
| Center frequency | 5,800,000,000 Hz |
| Requested pilot offset | approximately +100 kHz |
| Sample rate | 1 MS/s |
| RF bandwidth | 800 kHz |
| Duration per condition | 0.3 s |
| Kernel buffers | 8 |
| RX gain | 60 dB manual |
| DDS scale | 0.125 |
| TX hardware gains | -35, -30, -25, -20, -15, -10 dB |
| Attribution repeats | five fresh, source-distinct streams at -20 dB |
| Conditions per A/B/C/E stage | 10 |

### Hardened-result admission gates

- Exact source, dependency, native-library, device, fixture, plan, and runtime identities match.
- Metadata ABI 2, monotonic FPGA sample sequence, exact sample count, and one fresh stream per
  condition.
- Zero clipped samples and conservative ADC headroom.
- RX1 reference-tone SNR at least 20 dB.
- Detected attribution phasor: coherence at least 0.995 and phase RMS no greater than 6 degrees.
- A nondetection retains a phase-free noise-derived upper bound; it is never replaced by a zero
  complex phasor.
- `ENODATA`, continuity, clipping, headroom, identity, selector, or mute failure accepts no raw
  artifact. Captures are never spliced.
- Failed run IDs are burned and never retried.
- Raw IQ is stored only on the Raspberry Pi local filesystem, never on Pluto storage.

P0 is deliberately a legacy screening cohort and follows the separate Section 8 P0 admission
contract. Every authoritative P1/P2/A/B/C/D/E/F/X and post-fix result must satisfy the hardened
contract above, including exact native-libiio and dependency attestations.

## 7. Required tooling gates before authoritative RF capture

This section freezes the required behavior for every campaign runner and analyzer. The initial
implementation is present; all items remain subject to the final semantic release audit and full
regression gate before any RF execution.

Complete, test, commit, push, and freeze **T1 through T8 before P0 or authoritative Stage A**.
P0 is not allowed from a dirty or pre-freeze tree. P1/P2 may run after their tooling is frozen,
but the same final
campaign revision must be used for every authoritative A/B/C/D/E/F/X comparison. No tooling
source commit may change after A; if it does, reacquire A/B/C/E under the final revision.

The P2, T6, and T7 runners share one source-fixed root-owned reserve/burn authority. Its design,
fixed paths, provisioning command, read-only verification, and fail-closed recovery rules are in
[`shared_global_ledger_authority.md`](docs/5g8_root_cause_analysis/shared_global_ledger_authority.md).
The implementation and offline tests are complete. Host provisioning is deliberately still
pending and is a mandatory operator gate before any authoritative run; implementation work did
not modify `/var`, `/etc`, or `/usr/local`.

### T1 — true TX-muted dual-RX control runner

- [x] Add a receive-only runner that proves both TX gains `-80 dB`, every DDS raw/scale value
      zero, and captures exactly one 10-second dual-RX CI16 stream at 1 MS/s with ABI-2 continuity
      per invocation before exact final mute. The P1 cohort is exactly five unique run IDs and five
      source-distinct streams total, not five streams per run.
- [x] Do not fake a muted control with a very weak active DDS tone.
- [x] Analyze absolute PSD/tone counts independently in RX1 and RX2. Without an RX1 pilot,
      `RX2/RX1` transfer phase is undefined and must not be reported.
- [x] Separately label/exclude expected DC/LO, image, and filter-edge regions. Only a material spur
      in the former active-pilot analysis window directly blocks attribution; another spur blocks
      only if it overlaps an admitted analysis window or materially raises that window's floor.
- [x] Bind the untouched fixture identity, recursively sealed Fast20 live-image evidence, and the
      accepted post-power-cycle P0 RF schedule proof. Flash/reset evidence never substitutes for
      autonomous dwell-timing evidence.
- [x] Add red/green tests for nonzero DDS, wrong serial/URI, discontinuity, clipping, partial
      output, and final-mute failure.

### T2 — P2 input-drive-off control

- [x] Add an explicit fixture-v2-bound topology for a terminated TX1 stimulus branch and a
      separately terminated 8-way input while the 8-way outputs, selector, RX2 cable, and RX1
      chain remain fixed.
- [x] Never reuse the legacy `--confirm-fully-conducted` label for P2; that would falsely attest
      the normal full fixture.
- [x] Record both new loads/reference planes and prove no other connection moved.
- [x] Match P0 exactly at 1 MS/s, 800-kHz RF bandwidth, 10-second Fast20 schedule, RX gain 40 dB,
      TX gain -20 dB, DDS scale 0.25, pilot estimator, and the same central `ALL_OFF` windows.
- [x] Apply the P2 `±1 dB` RX1 stability criterion to the 95% interval of the P2-minus-P0
      reference-amplitude difference, not just the point estimate.
- [x] Reserve and burn the board/run namespace through the shared root-owned ledger before
      frozen-evidence or hardware access; bind the exact privileged receipts into accepted output.
- [ ] Provision and read-only verify the reviewed shared ledger authority on devpi after the
      final source commit is frozen and before executing P2, T6, or T7.

### T3 — Stage-D repeat-count parity

- [x] Promote `run_5g8_one_hot_path_ladder.py` from three to five attribution repeats per matrix
      cell.
- [x] Define this as a six-gain ladder containing one `-20 dB` capture plus four additional
      `-20 dB` captures, for exactly five source-distinct `-20 dB` streams.
- [x] Expected direct D1 size becomes 90 captures per row, 720 total, approximately 1.73 GB.
- [x] Add tests proving exactly five source-distinct streams and rejecting 3/4/6 or duplicates.

### T4 — closure-qualified excitation and covariance

Direct one-hot D1 drives a selector input without the 8-way splitter. E drives each input through
a different splitter arm/cable. Therefore the unweighted expression

```text
H_C + sum_i(H_D1,i - H_C)
```

is not a valid E prediction.

Freeze and implement one of these methods before D2 RF execution:

1. **Preferred arm-preserving method:** drive the 8-way input, connect only the exact E arm/cable
   `Fi -> ANTi`, and independently terminate the other seven splitter outputs and seven selector
   inputs. Capture a fresh source-disjoint all-inputs-terminated reference `C_i` for each row.
2. **Weighted method:** independently measure each complex excitation weight `w_i` at the board
   input reference plane and predict

   ```text
   H_E,pred = H_C + sum_i w_i (H_D1,i - H_C,i)
   ```

The arm-preserving topology is not automatically an exact reproduction of E excitation: splitter
output waves can change when the other seven outputs see loads instead of selector inputs. Treat
it as a diagnostic unless splitter multiport behavior is characterized. For the weighted method,
record uncertainty and covariance for every complex `w_i` and the exact `C_i` topology.

One shared C capture cannot be silently reused in eight supposedly independent differences.
Either use dedicated five-repeat `C_i` references or implement and test a joint
covariance-aware complex bootstrap. The HMC253 automation module is not transparent evidence at
5.8 GHz unless its complete complex network is characterized there.

**Implemented choice:** the preferred arm-preserving method, with exactly five source-disjoint
`C_i` and five source-disjoint `D2_i` observations for each of ANT1–ANT8 (80 total captures).
The result remains explicitly diagnostic-only until splitter multiport behavior is characterized.

### T5 — source-bound campaign analyzers

- [x] Add a fixture-v2 generator/validator for A/B/C/E. It must derive B/C/E
      `prior_stage_binding` from the immediately prior immutable plan, provide reviewed examples
      for all four stages, and reject stale/non-adjacent hashes or graph changes outside the
      declared stage delta.
- [x] Add a two-phase `flash_and_attest_selector.py` workflow. Phase 1 builds/verifies/programs and
      exits `awaiting_power_cycle`; phase 2 consumes a run-bound operator power/current/J11.1
      attestation, performs an exact non-overwriting BIN-extent readback, resumes the MCU, proves
      mailbox `ALL_OFF`, and seals logs/hashes/status in a canonical evidence manifest.
- [x] Make C/E and D setup/plan evidence bind the exact selector-flash evidence-manifest path and
      SHA-256. A local build manifest alone is insufficient proof of the live target image.
- [x] Red/green-test build/program/readback/cmp/resume/mailbox failures, pre-existing evidence
      paths, symlinks, partial logs, invalid operator attestation, and a target left halted.
- [x] Add CLIs that verify and aggregate A/B/C/E, eight D1 rows, D2/weights, and fine-sweep raw
      identities into normalized JSON.
- [x] Verify every local manifest, plan, raw data, metadata, condition record, stream ID, and
      SHA-256 before analysis.
- [x] Reject reused upstream artifacts unless the selected covariance model explicitly accounts
      for them.
- [x] For weighted and arm-preserving closure, propagate the full joint uncertainty/covariance of
      global `H_C`, every `w_i` (when used), D1/D2, every dedicated `C_i`, and E through the final
      complex bootstrap. Freeze every `C_i` topology and upstream hash in the immutable plan.
      Global `H_C` must be source-disjoint from all dedicated `C_i` evidence unless a tested joint
      covariance model explicitly represents the reuse.
- Final attribution/closure/frequency figures are a post-capture deliverable. They are generated
  only from accepted committed normalized data and are not claimed complete before RF data exist.
- [x] Add the hardened post-fix selected-state analyzer that reports `ALL_OFF`, ANT1–ANT8,
      `C_raw`, and `C_path` with simultaneous confidence intervals.

### T6 — protected TX/RX port-pair matrix

- [x] Add the conditional four-cell TX1/TX2 by RX1/RX2 runner with explicit per-cell inactive-TX
      termination/mute, direct test-receiver termination, safely attenuated reference chain,
      preflight headroom, final mute, and reference-plane identities.
- [x] Never remove or bypass RX1 protection. If a receiver role is exchanged, use a second
      identified 5.8-GHz-rated attenuation chain rather than moving the established protection.
- [x] De-embed or normalize receiver/reference-chain gain before comparing cells; never interpret
      raw channel amplitudes as a port-pair result.
- [x] Add red/green tests for an unprotected input, wrong attenuator, active inactive-TX, clipping,
      missing termination, and non-disjoint repeats.

### T7 — bidirectional fine-frequency runner

- [x] Add a hardened, policy-reviewed runner for `5.60–5.95 GHz`, including the experimental
      operation warning above the AD9363 qualified range, exact LO/DDS readback, source binding,
      failure tombstones, and local-RPi-only raw storage.
- [x] Coarse mode uses 10-MHz points in ascending and descending order, five independent captures
      per point, and an interleaved 5.800-GHz anchor after every five non-anchor frequencies.
- [x] Fine mode uses 1-MHz points over a predeclared `peak ±10 MHz` interval in both directions,
      five independent captures per point, with the same interleaved anchor rule.
- [x] Select refinements deterministically from coarse data: refine the largest local maximum and
      smallest local minimum of `|H_off|` whose multiplicity-corrected simultaneous 95% interval
      differs from both adjacent coarse points; ties go to the lower frequency. If neither
      qualifies, refine only `5.800 GHz ±10 MHz`.
- [x] Write and hash a new immutable fine-sweep plan after coarse selection and before fine RF.
      Analyze ascending/descending strata separately; pool only if the simultaneous direction-
      difference test passes.
- [x] Freeze a storage/time estimate and require at least twice that much free local capacity
      before planning; no size claim is accepted until the final condition count is generated.
- [x] Add tests for endpoints, direction, duplicate/missing points, anchor cadence, out-of-policy
      frequencies, LO/readback mismatch, and interrupted sweeps.

### T8 — intervention and selected-state re-entry qualification

- [x] Add a generic, fixture-v2-bound comparison contract that identifies exactly one reversible
      changed component/property and binds a fresh same-revision baseline to the intervention.
- [x] Add a hardened full-simultaneous-fixture runner for static `ALL_OFF` and ANT1–ANT8 states;
      it must not reuse the direct-one-hot topology label.
- [x] Extend the two-phase selector flash attestor to the reviewed Fast20 ELF/BIN/profile and add
      `run_5g8_selected_state_qualification.py` timing/matrix modes. Every artifact must bind the
      exact static-bench or Fast20 live-image evidence appropriate to that capture.
- [x] After a supported fix, require two source-distinct timing-qualification captures and a fresh
      ANT1–ANT8 complex matrix before any 5.8-GHz coefficient release.
- [x] Each timing run must independently pass ABI-2 continuity, exact sample count, state-boundary
      alignment, guard/dwell-duration, clipped-sample, and final-mute/`ALL_OFF` gates. The fresh
      matrix must include every state, have no reused stream IDs, pass the Section 6 quality gates,
      and meet pre-fix repeatability limits (`≤0.2 dB` amplitude and `≤2°` phase simultaneous
      95% intervals).
- [x] Final simultaneous lower 95% confidence bounds must satisfy both `C_raw ≥20 dB` and
      `C_path ≥20 dB`; release for the stated one-degree objective additionally requires the
      `C_path ≥35.1629 dB` lower bound.
- [x] Add red/green tests for multi-variable interventions, stale baselines, selected-state
      readback failure, incomplete state sets, reused streams, and failure to return to `ALL_OFF`.

The initial T1–T8 implementation passed the full offline repository suite (`1108 passed`), and a
later pre-final-migration cut passed `1356`. Subsequent semantic audits identified fail-closed
contract gaps and drove the shared-ledger and runner hardening documented in
[`2026-08-30-campaign-tooling-status-and-handoff.md`](docs/5g8_root_cause_analysis/2026-08-30-campaign-tooling-status-and-handoff.md).
At that checkpoint the latest combined focused gate passed `316`; a fresh whole-repository run was
stopped at the operator's request after `305` passes and is incomplete. The current tooling must
therefore complete a fresh full regression and final semantic review before it is called frozen.
No RF transmission, capture, selector programming, OpenOCD mutation, power change, or physical
rewire was performed while implementing or testing this tooling.

## 8. Execution stages and decision points

Campaign order is: publish this plan; implement/freeze T1–T8 without RF or hardware changes;
inventory the fixture; exact-match Fast20 attestation and power cycle; P0; P1; P2; A; B; reviewed bench flash/readback; C; E; conditional M; D1;
D2; F; X; and, if a fix is supported, Q. This order preserves the untouched Fast20 fixture long
enough for the controls and avoids disturbing the A→B→C→E comparison with the port matrix.

Before P0, the two-phase attestor must read back the current complete BIN extent and refuse to
program unless every byte already equals the reviewed Fast20 BIN. Only then may it reprogram the
identical bytes, complete the required five-second power cycle, and seal the live-image evidence.
P0 is then the post-cycle autonomous-schedule proof used by P1/P2. A mismatch stops the campaign
while preserving the original image; it is not permission to replace it. P0 must be accepted
before any cable is moved, and Fast20 must not be replaced by the bench image until P0/P1/P2 and
accepted A/B are complete.

| Stage | Physical topology and capture | Purpose and decision |
|---|---|---|
| P0 | Untouched current Rotation-0 simultaneous fixture. Five independent 5.7/5.8 GHz Fast20 runs and five 2.4 GHz controls. | Freeze a contemporaneous pre-move baseline. Screening only, not authoritative E. |
| P1 | Same untouched fixture and Fast20 image, with both TXs and every DDS exactly muted; five fresh 10-s dual-RX streams. | Measure absolute receiver noise, spurs, and external interference. No transfer phasor without a pilot. Requires T1; approximately 0.4 GB. |
| P2 | TX1 stimulus branch terminated and 8-way input separately terminated; all downstream cables remain fixed. Five 5.8 GHz repeats plus gain-safe active controls. | Quick screen: if the baseline collapses, driven inputs are required. Requires T2 and is not A/B/C attribution. |
| A | TX1 reference remains; stimulus branch terminated. Terminate the frozen RX2 receiver chain at its fixture-facing boundary: directly at Pluto RX2 only when the optional RX2 attenuator is explicitly absent, or at that attenuator's fixture-facing port when explicitly present. RX2 cable and selector RF absent; selector supply off and control/ground harness disconnected. | Measures the fixed Pluto/optional-attenuator/internal or connector-local boundary contribution. |
| B | Move the exact A RX2 load to the far end of the fixed RX2 cable; selector remains RF-disconnected, unpowered, and control-disconnected. | `H_B - H_A` estimates cable/common-mode pickup. |
| C | Fixed cable to powered selector common; all eight selector inputs get independent loads; reviewed static bench proves lease-free `ALL_OFF`. | `H_C - H_B` estimates selector common-launch/board/power/control pickup. |
| E | Restore the exact 8-way splitter, arms/cables/map, selector, supply, and common cable; bind prior C and capture five attribution repeats plus gain ladder. | Authoritative simultaneous-feed result and complex closure target. |
| M | Conditional after contemporaneous A and E only if A meets the frozen material trigger. Test the protected TX1/TX2 by RX1/RX2 port-pair matrix without disturbing A→B→C→E. | Finds channel-pair dependence and a possible lower-leak workaround. Historical OTA TX2 data do not answer this. Requires T6. |
| D1 | Direct one-hot board matrix: drive one input, terminate seven, and measure `ALL_OFF`, intended state, and seven wrong states at a six-gain ladder plus four additional -20 dB repeats. | Board through/isolation matrix and defective-port localization. Requires T3. |
| D2 | Closure-qualified arm-preserving or independently weighted input experiments with source-disjoint references. | Produces uncertainty-bounded terms capable of testing E closure. Requires T4/T5. |
| F | Sweep the first material boundary and E from 5.60 to 5.95 GHz at 10 MHz in both directions with interleaved anchors; refine predeclared peaks/notches at 1 MHz. | Locates frequency-selective behavior and tests hysteresis/drift. Requires T7. |
| X | Apply one evidence-supported reversible intervention and repeat the implicated boundary plus E, or perform a predeclared controlled falsifier if no fix is yet supported. | Independently confirms/falsifies the mechanism and quantifies any fix. Requires T8. |
| Q | If X supports a fix, first run simultaneous-fixture static-bench `ALL_OFF` plus ANT1–ANT8; then attest a reviewed Fast20 restore, run two fresh Fast20 timing captures, and acquire a new Fast20 calibration matrix. | Reports final `C_raw`/`C_path`; does not authorize deployment unless all exit gates pass. |

### Pre-P0 exact-match Fast20 live-image checkpoint

Execute this checkpoint after the fixture inventory and immediately before P0. Generate and
complete the run-bound pre-program form, then invoke phase 1 with the additional
`--require-current-image-match` gate:

```bash
install -d -m 700 /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash/operator-inputs
./.venv/bin/python scripts/flash_and_attest_selector.py \
  --campaign-id 5p8-debug-r1 \
  --run-id 5p8-debug-r1-fast20-precontrol-r01-20260829 \
  --board-id stm32c011-4c0055000950313950363920 \
  --image-role fast20 \
  --write-pre-program-attestation-template /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash/operator-inputs/5p8-debug-r1-fast20-precontrol-r01-20260829.pre-program-attestation.json
```

**STOP:** open that create-only template, replace every operator field with the observed supply,
lead, ground, J11.1, current, heat, and timing facts, and save it. Do not run phase 1 until the
completed form is reviewed. Re-resolve the exact Pluto serial/USB URI, create a fresh immutable
TX1/TX2/DDS mute readback, and then run phase 1. The flash command rejects a writable, stale,
wrong-serial, wrong-URI, nonzero-gain, or nonzero-DDS evidence file before any OpenOCD command:

```bash
/home/pi/pluto-plus-utils/.venv/bin/python scripts/attest_selector_flash_pluto_mute.py \
  --checkpoint phase1_pre_openocd \
  --serial 104000b29905000e17000800065934759d \
  --uri REPLACE_CURRENT_USB_URI \
  --output /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash/operator-inputs/5p8-debug-r1-fast20-precontrol-r01-20260829.phase1-pluto-mute.json
```

Then run:

```bash
./.venv/bin/python scripts/flash_and_attest_selector.py \
  --campaign-id 5p8-debug-r1 \
  --run-id 5p8-debug-r1-fast20-precontrol-r01-20260829 \
  --board-id stm32c011-4c0055000950313950363920 \
  --image-role fast20 \
  --elf build/STM32C011F4P6/fast20/pluto_fast20.elf \
  --bin build/STM32C011F4P6/fast20/pluto_fast20.bin \
  --profile profiles/fast20-v1/control_profile.json \
  --openocd-config openocd/rpi4-swd.cfg \
  --evidence-root /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash \
  --pre-program-attestation /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash/operator-inputs/5p8-debug-r1-fast20-precontrol-r01-20260829.pre-program-attestation.json \
  --pluto-serial 104000b29905000e17000800065934759d \
  --pluto-uri REPLACE_CURRENT_USB_URI \
  --pluto-mute-evidence /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash/operator-inputs/5p8-debug-r1-fast20-precontrol-r01-20260829.phase1-pluto-mute.json \
  --require-current-image-match \
  --prepare-and-program
```

The attestor performs a complete pre-program target readback and returns the target to reset-run.
If that readback differs from the reviewed BIN, it refuses the program command. On an exact match,
follow this physical sequence exactly:

1. Mute both Pluto transmit gains and all eight DDS channels exactly.
2. Stop OpenOCD and verify that no OpenOCD process remains before target power-off.
3. Remove selector-board power for at least five measured seconds.
4. Restore its single allowed power source.
5. Remeasure supply set voltage/current limit/displayed current, J11.1 voltage, and heat.
6. Only then complete and review the generated editable power-cycle draft; do not pre-fill
   observed values.

Seal that draft with the hardware-inert command below. It validates the observations against the
exact phase-1 hash and creates the canonical read-only `power-cycle-attestation.json` consumed by
phase 2; phase 2 rejects the editable draft directly.

```bash
./.venv/bin/python scripts/flash_and_attest_selector.py \
  --campaign-id 5p8-debug-r1 \
  --run-id 5p8-debug-r1-fast20-precontrol-r01-20260829 \
  --board-id stm32c011-4c0055000950313950363920 \
  --image-role fast20 \
  --evidence-root /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash \
  --power-cycle-draft /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash/5p8-debug-r1-fast20-precontrol-r01-20260829/power-cycle-attestation.template.json \
  --seal-power-cycle-attestation
```

Create a new phase-2 mute/readback after sealing so its five-minute freshness window
cannot be consumed by the manual cycle. Then run the explicit phase-2 command; phase 2 deliberately
omits `--pre-program-attestation`:

```bash
/home/pi/pluto-plus-utils/.venv/bin/python scripts/attest_selector_flash_pluto_mute.py \
  --checkpoint phase2_pre_openocd \
  --serial 104000b29905000e17000800065934759d \
  --uri REPLACE_CURRENT_USB_URI \
  --output /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash/operator-inputs/5p8-debug-r1-fast20-precontrol-r01-20260829.phase2-pluto-mute.json
```

```bash
./.venv/bin/python scripts/flash_and_attest_selector.py \
  --campaign-id 5p8-debug-r1 \
  --run-id 5p8-debug-r1-fast20-precontrol-r01-20260829 \
  --board-id stm32c011-4c0055000950313950363920 \
  --image-role fast20 \
  --elf build/STM32C011F4P6/fast20/pluto_fast20.elf \
  --bin build/STM32C011F4P6/fast20/pluto_fast20.bin \
  --profile profiles/fast20-v1/control_profile.json \
  --openocd-config openocd/rpi4-swd.cfg \
  --evidence-root /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash \
  --power-cycle-attestation /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash/5p8-debug-r1-fast20-precontrol-r01-20260829/power-cycle-attestation.json \
  --pluto-serial 104000b29905000e17000800065934759d \
  --pluto-uri REPLACE_CURRENT_USB_URI \
  --pluto-mute-evidence /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash/operator-inputs/5p8-debug-r1-fast20-precontrol-r01-20260829.phase2-pluto-mute.json \
  --verify-after-power-cycle
```

The legacy P0 CLI cannot directly bind selector-flash evidence. P1 therefore forms a composite,
recursively verified proof: the sealed selector evidence must predate all five P0 manifests and
SigMF artifacts, and each P0 raw stream must independently prove the Fast20 schedule. The P2
fixture `fast20_control.live_image_evidence` binds the same sealed evidence. Flash attestation
proves exact image bytes, UID, and reset-run; only P0/P2 RF data prove autonomous dwell timing.

### P0 screening command template

The exact-match checkpoint above must be complete before any command in this section. The passive
snapshot found Fast20, which is exactly what the legacy current-fixture screen expects. Use five
unique run IDs `r01` through `r05` and do not flash the bench image before accepted B.

Immediately before each plan/execute pair, run `iio_info -s`, identify the one USB context whose
serial is `104000b29905000e17000800065934759d`, record its current `usb:` URI, and substitute it for
`REPLACE_CURRENT_USB_URI` below. Stop on zero or multiple serial matches. Scripts are not
executable files; invoke them through the repository virtual environment.

```bash
./.venv/bin/python scripts/run_closed_loop_frequency_sweep.py \
  --run-id 5p8-debug-r1-p0-paired-r01-20260829 \
  --board-id stm32c011-4c0055000950313950363920 \
  --serial 104000b29905000e17000800065934759d \
  --uri REPLACE_CURRENT_USB_URI \
  --receiver-gain-db 40 \
  --frequency-min-hz 5700000000 \
  --frequency-max-hz 5800000000 \
  --prepare-only

./.venv/bin/python scripts/run_closed_loop_frequency_sweep.py \
  --run-id 5p8-debug-r1-p0-paired-r01-20260829 \
  --board-id stm32c011-4c0055000950313950363920 \
  --serial 104000b29905000e17000800065934759d \
  --uri REPLACE_CURRENT_USB_URI \
  --receiver-gain-db 40 \
  --frequency-min-hz 5700000000 \
  --frequency-max-hz 5800000000 \
  --execute-stage rotation0 \
  --confirm-fully-conducted \
  --confirm-mapping rotation0
```

Stopping after Rotation 0 with manifest status `awaiting_rotation1` is intentional. These legacy
captures are labeled screening evidence because they lack fixture-v2, prior-C, native-attestation,
and failed-run-tombstone contracts. Their separate admission contract is: exact board/Pluto/run
identity; the runner's built-in plan, continuity, clipping, selector-alignment, and final-mute
checks all pass; all five run IDs are source-distinct; and no file is accepted after an interrupted
run. P0 does not claim the hardened native/fixture contract. Five paired runs consume
approximately 0.8 GB. Five required 2.4 GHz controls use run IDs
`5p8-debug-r1-p0-2p4-r01-20260829` through `r05`, with both frequency limits set to
`2400000000`, and consume approximately 0.4 GB.

The P0 cohort is reproducible only when all five 5.8-GHz `ALL_OFF` estimates have detected-pilot
SNR at least 20 dB, amplitude coefficient of variation no more than 10%, and circular phase
standard deviation no more than 10 degrees. Failure preserves the cohort but stops topology
attribution for a capture-system investigation.

### P1 muted-control command template

Complete the shared P1 fixture and one unique setup attestation per run using the P1 operator guide
linked in Section 5. The ordered five `--p0-manifest` arguments and their hashes in every setup
attestation must be `r01` through `r05`. The selector evidence is the sealed pre-P0 checkpoint
result. For `r01`:

```bash
P1_RUN_ID=5p8-debug-r1-p1-muted-r01-20260829
P1_FIXTURE=/absolute/local/path/p1-muted-fixture.json
P1_SETUP=/absolute/local/path/5p8-debug-r1-p1-muted-r01-20260829.setup.json
P1_SELECTOR=/home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash/5p8-debug-r1-fast20-precontrol-r01-20260829/selector-flash-evidence.json
P0_R01=/home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/closed-loop-frequency-sweeps/5p8-debug-r1-p0-paired-r01-20260829/manifest.json
P0_R02=/home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/closed-loop-frequency-sweeps/5p8-debug-r1-p0-paired-r02-20260829/manifest.json
P0_R03=/home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/closed-loop-frequency-sweeps/5p8-debug-r1-p0-paired-r03-20260829/manifest.json
P0_R04=/home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/closed-loop-frequency-sweeps/5p8-debug-r1-p0-paired-r04-20260829/manifest.json
P0_R05=/home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/closed-loop-frequency-sweeps/5p8-debug-r1-p0-paired-r05-20260829/manifest.json

./.venv/bin/python scripts/run_5g8_muted_control.py \
  --run-id "$P1_RUN_ID" \
  --board-id stm32c011-4c0055000950313950363920 \
  --serial 104000b29905000e17000800065934759d \
  --uri REPLACE_CURRENT_USB_URI \
  --fixture-manifest "$P1_FIXTURE" \
  --setup-attestation "$P1_SETUP" \
  --selector-evidence "$P1_SELECTOR" \
  --p0-manifest "$P0_R01" \
  --p0-manifest "$P0_R02" \
  --p0-manifest "$P0_R03" \
  --p0-manifest "$P0_R04" \
  --p0-manifest "$P0_R05" \
  --plan-only
```

After reviewing the immutable plan, execute the same run with:

```bash
P1_RUN_ID=5p8-debug-r1-p1-muted-r01-20260829
P1_FIXTURE=/absolute/local/path/p1-muted-fixture.json
P1_SETUP=/absolute/local/path/5p8-debug-r1-p1-muted-r01-20260829.setup.json
P1_SELECTOR=/home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash/5p8-debug-r1-fast20-precontrol-r01-20260829/selector-flash-evidence.json
P0_R01=/home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/closed-loop-frequency-sweeps/5p8-debug-r1-p0-paired-r01-20260829/manifest.json
P0_R02=/home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/closed-loop-frequency-sweeps/5p8-debug-r1-p0-paired-r02-20260829/manifest.json
P0_R03=/home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/closed-loop-frequency-sweeps/5p8-debug-r1-p0-paired-r03-20260829/manifest.json
P0_R04=/home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/closed-loop-frequency-sweeps/5p8-debug-r1-p0-paired-r04-20260829/manifest.json
P0_R05=/home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/closed-loop-frequency-sweeps/5p8-debug-r1-p0-paired-r05-20260829/manifest.json

./.venv/bin/python scripts/run_5g8_muted_control.py \
  --run-id "$P1_RUN_ID" \
  --board-id stm32c011-4c0055000950313950363920 \
  --serial 104000b29905000e17000800065934759d \
  --uri REPLACE_CURRENT_USB_URI \
  --fixture-manifest "$P1_FIXTURE" \
  --setup-attestation "$P1_SETUP" \
  --selector-evidence "$P1_SELECTOR" \
  --p0-manifest "$P0_R01" \
  --p0-manifest "$P0_R02" \
  --p0-manifest "$P0_R03" \
  --p0-manifest "$P0_R04" \
  --p0-manifest "$P0_R05" \
  --confirm-no-antennas \
  --confirm-tx1-untouched \
  --confirm-tx2-terminated \
  --confirm-rx1-protected \
  --confirm-no-movement \
  --confirm-sealed-fast20-unchanged \
  --confirm-topology-token UNTOUCHED_ROTATION0_FULL_CONDUCTED_FIXTURE \
  --execute
```

Repeat with unique `r02`–`r05` run IDs and setup files. Then aggregate exactly those five complete
manifests:

```bash
./.venv/bin/python scripts/analyze_5g8_muted_control_cohort.py \
  --manifest /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5g8-muted-control/5p8-debug-r1-p1-muted-r01-20260829/manifest.json \
  --manifest /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5g8-muted-control/5p8-debug-r1-p1-muted-r02-20260829/manifest.json \
  --manifest /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5g8-muted-control/5p8-debug-r1-p1-muted-r03-20260829/manifest.json \
  --manifest /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5g8-muted-control/5p8-debug-r1-p1-muted-r04-20260829/manifest.json \
  --manifest /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5g8-muted-control/5p8-debug-r1-p1-muted-r05-20260829/manifest.json \
  --output /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/analysis/p1-muted-cohort.json
```

### P2 input-drive-off command template

Do not rewire for P2 until P0 and P1 are accepted. Follow the checked-in
[`p2_input_off_operator_templates.md`](docs/5g8_root_cause_analysis/p2_input_off_operator_templates.md)
guide and copy the complete P2 fixture template into a run-specific local directory. P2 is five
new run IDs, five run-bound fixture files, five setup attestations/photos, and five source-distinct
streams. The exact same five normalized P0 observations are passed in the same order to every P2
plan and execution.

Normalize the five accepted P0 5.8-GHz observations once, using their exact run IDs and their
distinct legacy analysis/manifest pairs. Each command publishes a create-only read-only envelope
that recursively binds the manifest's embedded plan, manifest, raw IQ, SigMF metadata,
reference-transfer analysis, and clean normalizer source; a loose observation JSON is rejected:

```bash
./.venv/bin/python scripts/analyze_5g8_input_off_cohort.py --normalize-p0 \
  --run-id REPLACE_EXACT_P0_RUN_ID_01 \
  --legacy-analysis /absolute/local/path/p0-r01/reference-transfer-analysis.json \
  --legacy-manifest /absolute/local/path/p0-r01/manifest.json \
  --output /absolute/local/path/p0-r01/normalized-p0-observation.json
./.venv/bin/python scripts/analyze_5g8_input_off_cohort.py --normalize-p0 \
  --run-id REPLACE_EXACT_P0_RUN_ID_02 \
  --legacy-analysis /absolute/local/path/p0-r02/reference-transfer-analysis.json \
  --legacy-manifest /absolute/local/path/p0-r02/manifest.json \
  --output /absolute/local/path/p0-r02/normalized-p0-observation.json
./.venv/bin/python scripts/analyze_5g8_input_off_cohort.py --normalize-p0 \
  --run-id REPLACE_EXACT_P0_RUN_ID_03 \
  --legacy-analysis /absolute/local/path/p0-r03/reference-transfer-analysis.json \
  --legacy-manifest /absolute/local/path/p0-r03/manifest.json \
  --output /absolute/local/path/p0-r03/normalized-p0-observation.json
./.venv/bin/python scripts/analyze_5g8_input_off_cohort.py --normalize-p0 \
  --run-id REPLACE_EXACT_P0_RUN_ID_04 \
  --legacy-analysis /absolute/local/path/p0-r04/reference-transfer-analysis.json \
  --legacy-manifest /absolute/local/path/p0-r04/manifest.json \
  --output /absolute/local/path/p0-r04/normalized-p0-observation.json
./.venv/bin/python scripts/analyze_5g8_input_off_cohort.py --normalize-p0 \
  --run-id REPLACE_EXACT_P0_RUN_ID_05 \
  --legacy-analysis /absolute/local/path/p0-r05/reference-transfer-analysis.json \
  --legacy-manifest /absolute/local/path/p0-r05/manifest.json \
  --output /absolute/local/path/p0-r05/normalized-p0-observation.json
```

Complete `p2-r01.fixture.json`, including an explicit `present` or `absent` RX2 attenuator state,
then derive its setup draft rather than hand-copying hashes or inventories:

```bash
./.venv/bin/python scripts/generate_5g8_input_off_setup.py \
  --fixture-manifest /absolute/local/path/p2-r01.fixture.json \
  --run-id 5p8-debug-r1-p2-input-off-r01-20260830 \
  --board-id stm32c011-4c0055000950313950363920 \
  --serial 104000b29905000e17000800065934759d \
  --output /absolute/local/path/p2-r01.setup-draft.json
```

Replace every remaining setup observation placeholder, attach the setup photo, verify the derived
inventories, and change every physical confirmation to JSON `true`. Then freeze the plan:

```bash
./.venv/bin/python scripts/run_5g8_input_off_control.py \
  --run-id 5p8-debug-r1-p2-input-off-r01-20260830 \
  --board-id stm32c011-4c0055000950313950363920 \
  --serial 104000b29905000e17000800065934759d \
  --uri REPLACE_CURRENT_USB_URI \
  --fixture-manifest /absolute/local/path/p2-r01.fixture.json \
  --setup-attestation /absolute/local/path/p2-r01.setup.json \
  --profile profiles/fast20-v1/control_profile.json \
  --p0-observation /absolute/local/path/p0-r01/normalized-p0-observation.json \
  --p0-observation /absolute/local/path/p0-r02/normalized-p0-observation.json \
  --p0-observation /absolute/local/path/p0-r03/normalized-p0-observation.json \
  --p0-observation /absolute/local/path/p0-r04/normalized-p0-observation.json \
  --p0-observation /absolute/local/path/p0-r05/normalized-p0-observation.json \
  --state-root /home/pi/.local/state/smateway \
  --plan-only
```

After reviewing the immutable plan and rechecking the untouched downstream graph, execute with
the identical arguments and these exact action/confirmation flags:

```bash
./.venv/bin/python scripts/run_5g8_input_off_control.py \
  --run-id 5p8-debug-r1-p2-input-off-r01-20260830 \
  --board-id stm32c011-4c0055000950313950363920 \
  --serial 104000b29905000e17000800065934759d \
  --uri REPLACE_CURRENT_USB_URI \
  --fixture-manifest /absolute/local/path/p2-r01.fixture.json \
  --setup-attestation /absolute/local/path/p2-r01.setup.json \
  --profile profiles/fast20-v1/control_profile.json \
  --p0-observation /absolute/local/path/p0-r01/normalized-p0-observation.json \
  --p0-observation /absolute/local/path/p0-r02/normalized-p0-observation.json \
  --p0-observation /absolute/local/path/p0-r03/normalized-p0-observation.json \
  --p0-observation /absolute/local/path/p0-r04/normalized-p0-observation.json \
  --p0-observation /absolute/local/path/p0-r05/normalized-p0-observation.json \
  --state-root /home/pi/.local/state/smateway \
  --confirm-no-antennas \
  --confirm-two-distinct-terminations \
  --confirm-downstream-unchanged \
  --confirm-rx1-protected-reference \
  --confirm-tx2-terminated-muted \
  --confirm-fast20-live \
  --confirm-no-movement \
  --confirm-topology-token TX1_STIMULUS_AND_8WAY_INPUT_SEPARATELY_TERMINATED \
  --execute
```

Repeat with unique `r02`–`r05` run IDs, fixture files, setup attestations, and photos. Aggregate
the exact five P0 observations and the exact five accepted P2 observation/manifest pairs:

```bash
./.venv/bin/python scripts/analyze_5g8_input_off_cohort.py --compare \
  --p0-observation /absolute/local/path/p0-r01/normalized-p0-observation.json \
  --p0-observation /absolute/local/path/p0-r02/normalized-p0-observation.json \
  --p0-observation /absolute/local/path/p0-r03/normalized-p0-observation.json \
  --p0-observation /absolute/local/path/p0-r04/normalized-p0-observation.json \
  --p0-observation /absolute/local/path/p0-r05/normalized-p0-observation.json \
  --p2-observation /absolute/local/path/p2-r01/5g8-input-off-observation.json \
  --p2-manifest /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5g8-input-off-control/5p8-debug-r1-p2-input-off-r01-20260830/manifest.json \
  --p2-observation /absolute/local/path/p2-r02/5g8-input-off-observation.json \
  --p2-manifest /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5g8-input-off-control/5p8-debug-r1-p2-input-off-r02-20260830/manifest.json \
  --p2-observation /absolute/local/path/p2-r03/5g8-input-off-observation.json \
  --p2-manifest /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5g8-input-off-control/5p8-debug-r1-p2-input-off-r03-20260830/manifest.json \
  --p2-observation /absolute/local/path/p2-r04/5g8-input-off-observation.json \
  --p2-manifest /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5g8-input-off-control/5p8-debug-r1-p2-input-off-r04-20260830/manifest.json \
  --p2-observation /absolute/local/path/p2-r05/5g8-input-off-observation.json \
  --p2-manifest /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5g8-input-off-control/5p8-debug-r1-p2-input-off-r05-20260830/manifest.json \
  --bootstrap-replicates 32768 \
  --seed 94904358 \
  --output /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/analysis/p2-input-off-cohort.json
```

### A/B/C/E hardened runner template

Every stage uses one fresh run ID, a final fixture-v2 manifest, and a unique run-bound setup
attestation. Plan-only is run from a clean committed source revision and is followed by execute
with the same files and identities.

```bash
./.venv/bin/python scripts/run_5g8_leakage_ladder.py \
  --run-id REPLACE_UNIQUE_RUN_ID \
  --board-id stm32c011-4c0055000950313950363920 \
  --serial 104000b29905000e17000800065934759d \
  --uri REPLACE_CURRENT_USB_URI \
  --stage REPLACE_STAGE \
  --fixture-manifest /absolute/path/to/final-fixture.json \
  --setup-attestation /absolute/path/to/run-specific-setup.json \
  --plan-only
```

For C and E, insert these six arguments immediately **before** the terminal `--plan-only` or
`--execute` action flag in the corresponding command:

```bash
  --bench-manifest build/STM32C011F4P6/bench/pluto_bench.manifest.json \
  --openocd-config openocd/rpi4-swd.cfg \
  --profile profiles/fast20-v1/control_profile.json \
  --selector-flash-evidence /absolute/path/to/selector-flash-evidence.json \
  --selector-flash-evidence-sha256 REPLACE_LOWERCASE_64_HEX_SHA256 \
  --selector-flash-run-id 5p8-debug-r1-bench-flash-r01-20260829
```

The final three values bind the exact sealed live image. For C/E, the run-specific setup
attestation and explicit CLI arguments carry the same evidence path, SHA-256, and flash run ID;
the runner derives that binding into the immutable plan. The fixture manifest describes physical
hardware only and does **not** claim selector-flash evidence. Copy the values from successful
phase-2 JSON output; never infer or hand-edit the digest.

Execution replaces `--plan-only` with `--execute` and adds every common confirmation plus the
exact stage enum/token:

```bash
  --confirm-no-antennas \
  --confirm-tx1-matched-conducted \
  --confirm-tx2-terminated-muted \
  --confirm-rx1-conducted-reference \
  --confirm-no-movement \
  --confirm-stage REPLACE_STAGE \
  --confirm-topology-token REPLACE_EXACT_TOKEN
```

C and E execution additionally requires:

```bash
  --confirm-selector-static-all-off
```

Tokens:

| Stage | Enum | Exact topology token |
|---|---|---|
| A | `direct_rx2_termination` | `DIRECT_RX2_50OHM_AT_PLUTO` |
| B | `rx2_cable_terminated` | `RX2_CABLE_FAR_END_50OHM` |
| C | `powered_selector_all_inputs_terminated` | `POWERED_SELECTOR_COMMON_TO_RX2_ALL_8_INPUTS_50OHM` |
| E | `full_conducted_fixture` | `FULL_CONDUCTED_TX1_2WAY_RX1_AND_8WAY_SELECTOR_RX2` |

B binds the exact A plan through its fixture-v2 `prior_stage_binding`; C binds B; E binds C. The
fixture generator/analyzer must reject a missing, stale, non-adjacent, or hash-mismatched binding.
C/E use:

```text
build/STM32C011F4P6/bench/pluto_bench.manifest.json
openocd/rpi4-swd.cfg
profiles/fast20-v1/control_profile.json
```

The runner does not flash the reviewed bench image. Flashing is a separate deliberate checkpoint
only after accepted B—therefore after P0/P1/P2/A/B—and before C/D/E. Run it only after the
operator power/SWD gates in Section 6 pass, with OpenOCD stopped before any target power-off.

T5 must provide the fail-closed two-phase wrapper used below. Phase 1 creates a unique evidence
directory, runs `make bench`, regenerates/verifies the manifest and ELF, records hashes, programs
with verify, and exits only in `awaiting_power_cycle`. It must trap errors and record whether the
target was left running or halted; later stages may not consume a failed/incomplete directory.

First generate the editable, run-bound pre-program operator form. This command has no target or RF
access. Complete every observed value and confirmation in that file immediately before phase 1;
leaving any template value in place makes phase 1 fail closed.

```bash
set -euo pipefail
FLASH_EVIDENCE_ROOT=/home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash
PRE_PROGRAM_ATTESTATION="$FLASH_EVIDENCE_ROOT/operator-inputs/5p8-debug-r1-bench-flash-r01-20260829.pre-program-attestation.json"
install -d -m 700 "$FLASH_EVIDENCE_ROOT/operator-inputs"
./.venv/bin/python scripts/flash_and_attest_selector.py \
  --campaign-id 5p8-debug-r1 \
  --run-id 5p8-debug-r1-bench-flash-r01-20260829 \
  --board-id stm32c011-4c0055000950313950363920 \
  --image-role bench \
  --write-pre-program-attestation-template "$PRE_PROGRAM_ATTESTATION"
```

```bash
set -euo pipefail
/home/pi/pluto-plus-utils/.venv/bin/python scripts/attest_selector_flash_pluto_mute.py \
  --checkpoint phase1_pre_openocd \
  --serial 104000b29905000e17000800065934759d \
  --uri REPLACE_CURRENT_USB_URI \
  --output /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash/operator-inputs/5p8-debug-r1-bench-flash-r01-20260829.phase1-pluto-mute.json
./.venv/bin/python scripts/flash_and_attest_selector.py \
  --campaign-id 5p8-debug-r1 \
  --run-id 5p8-debug-r1-bench-flash-r01-20260829 \
  --board-id stm32c011-4c0055000950313950363920 \
  --image-role bench \
  --elf build/STM32C011F4P6/bench/pluto_bench.elf \
  --bin build/STM32C011F4P6/bench/pluto_bench.bin \
  --build-manifest build/STM32C011F4P6/bench/pluto_bench.manifest.json \
  --openocd-config openocd/rpi4-swd.cfg \
  --evidence-root /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash \
  --pre-program-attestation /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash/operator-inputs/5p8-debug-r1-bench-flash-r01-20260829.pre-program-attestation.json \
  --pluto-serial 104000b29905000e17000800065934759d \
  --pluto-uri REPLACE_CURRENT_USB_URI \
  --pluto-mute-evidence /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash/operator-inputs/5p8-debug-r1-bench-flash-r01-20260829.phase1-pluto-mute.json \
  --prepare-and-program
```

Both Pluto transmitters remain exactly muted. Stop OpenOCD and verify no process remains, power the
target off for at least five measured seconds, power it back on, remeasure supply
voltage/current-limit/displayed-current, J11.1 voltage and heat, and only then complete the unique
run directory's editable `power-cycle-attestation.template.json`; never claim software measured
these values. Validate and seal it into the read-only phase-2 authority before creating a fresh
mute readback:

```bash
./.venv/bin/python scripts/flash_and_attest_selector.py \
  --campaign-id 5p8-debug-r1 \
  --run-id 5p8-debug-r1-bench-flash-r01-20260829 \
  --board-id stm32c011-4c0055000950313950363920 \
  --image-role bench \
  --evidence-root /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash \
  --power-cycle-draft /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash/5p8-debug-r1-bench-flash-r01-20260829/power-cycle-attestation.template.json \
  --seal-power-cycle-attestation
```

Finally create a new phase-2 exact-mute readback immediately before phase 2:

```bash
/home/pi/pluto-plus-utils/.venv/bin/python scripts/attest_selector_flash_pluto_mute.py \
  --checkpoint phase2_pre_openocd \
  --serial 104000b29905000e17000800065934759d \
  --uri REPLACE_CURRENT_USB_URI \
  --output /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash/operator-inputs/5p8-debug-r1-bench-flash-r01-20260829.phase2-pluto-mute.json
```

```bash
set -euo pipefail
./.venv/bin/python scripts/flash_and_attest_selector.py \
  --campaign-id 5p8-debug-r1 \
  --run-id 5p8-debug-r1-bench-flash-r01-20260829 \
  --board-id stm32c011-4c0055000950313950363920 \
  --image-role bench \
  --elf build/STM32C011F4P6/bench/pluto_bench.elf \
  --bin build/STM32C011F4P6/bench/pluto_bench.bin \
  --build-manifest build/STM32C011F4P6/bench/pluto_bench.manifest.json \
  --openocd-config openocd/rpi4-swd.cfg \
  --evidence-root /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash \
  --power-cycle-attestation /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash/5p8-debug-r1-bench-flash-r01-20260829/power-cycle-attestation.json \
  --pluto-serial 104000b29905000e17000800065934759d \
  --pluto-uri REPLACE_CURRENT_USB_URI \
  --pluto-mute-evidence /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash/operator-inputs/5p8-debug-r1-bench-flash-r01-20260829.phase2-pluto-mute.json \
  --verify-after-power-cycle
```

Phase 2 must refuse a pre-existing readback/final manifest, dump and compare the complete BIN
extent, require every ELF/BIN/build-manifest-or-profile/OpenOCD-config hash to equal its exact
phase-1 frozen hash, hash every input/log/output, explicitly `reset run`, wait for startup, prove
mailbox and GPIO-latch `ALL_OFF`, and seal a canonical `selector-flash-evidence.json`. Its absolute
path and SHA-256 are mandatory in C/E/D run setup evidence and CLI arguments and are derived into
each immutable plan; they are not fixture-manifest fields. Do not hard-code pre-build bench hashes
into this plan.

### Post-fix Q firmware re-entry checkpoint

The source-bound device/intervention producers and exact Q `prepare → capture → analyze` commands
are frozen in
[`docs/5g8_root_cause_analysis/t8_selected_state_workflow.md`](docs/5g8_root_cause_analysis/t8_selected_state_workflow.md).
Its installed-after and diagnostic-transition drafts are:

- [`docs/5g8_root_cause_analysis/t8_installed_after_state.template.json`](docs/5g8_root_cause_analysis/t8_installed_after_state.template.json)
- [`docs/5g8_root_cause_analysis/t8_fixture_state_transition.template.json`](docs/5g8_root_cause_analysis/t8_fixture_state_transition.template.json)

If X supports a fix, finish the implicated-boundary/E repeat and the static-bench `ALL_OFF` plus
ANT1–ANT8 matrix first. Then restore Fast20 through the same safety-gated two-phase attestor; a
bench mailbox capture cannot substitute for autonomous dwell timing.

Generate and complete a new Fast20-specific pre-program form; the bench form is deliberately not
reusable across image roles or run IDs:

```bash
set -euo pipefail
FLASH_EVIDENCE_ROOT=/home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash
PRE_PROGRAM_ATTESTATION="$FLASH_EVIDENCE_ROOT/operator-inputs/5p8-debug-r1-fast20-restore-r01-20260829.pre-program-attestation.json"
install -d -m 700 "$FLASH_EVIDENCE_ROOT/operator-inputs"
./.venv/bin/python scripts/flash_and_attest_selector.py \
  --campaign-id 5p8-debug-r1 \
  --run-id 5p8-debug-r1-fast20-restore-r01-20260829 \
  --board-id stm32c011-4c0055000950313950363920 \
  --image-role fast20 \
  --write-pre-program-attestation-template "$PRE_PROGRAM_ATTESTATION"
```

```bash
set -euo pipefail
/home/pi/pluto-plus-utils/.venv/bin/python scripts/attest_selector_flash_pluto_mute.py \
  --checkpoint phase1_pre_openocd \
  --serial 104000b29905000e17000800065934759d \
  --uri REPLACE_CURRENT_USB_URI \
  --output /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash/operator-inputs/5p8-debug-r1-fast20-restore-r01-20260829.phase1-pluto-mute.json
./.venv/bin/python scripts/flash_and_attest_selector.py \
  --campaign-id 5p8-debug-r1 \
  --run-id 5p8-debug-r1-fast20-restore-r01-20260829 \
  --board-id stm32c011-4c0055000950313950363920 \
  --image-role fast20 \
  --elf build/STM32C011F4P6/fast20/pluto_fast20.elf \
  --bin build/STM32C011F4P6/fast20/pluto_fast20.bin \
  --profile profiles/fast20-v1/control_profile.json \
  --openocd-config openocd/rpi4-swd.cfg \
  --evidence-root /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash \
  --pre-program-attestation /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash/operator-inputs/5p8-debug-r1-fast20-restore-r01-20260829.pre-program-attestation.json \
  --pluto-serial 104000b29905000e17000800065934759d \
  --pluto-uri REPLACE_CURRENT_USB_URI \
  --pluto-mute-evidence /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash/operator-inputs/5p8-debug-r1-fast20-restore-r01-20260829.phase1-pluto-mute.json \
  --prepare-and-program
```

Keep both Pluto TX gains at `-80 dB` and all eight DDS channels at raw/scale zero. Stop OpenOCD
and verify no process remains, remove selector-board power for at least five measured seconds,
restore the single allowed power source, and remeasure supply voltage/current limit/displayed
current, J11.1 voltage, and heat. Only then complete and review the generated editable power-cycle
draft, then seal it into the canonical read-only phase-2 authority:

```bash
./.venv/bin/python scripts/flash_and_attest_selector.py \
  --campaign-id 5p8-debug-r1 \
  --run-id 5p8-debug-r1-fast20-restore-r01-20260829 \
  --board-id stm32c011-4c0055000950313950363920 \
  --image-role fast20 \
  --evidence-root /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash \
  --power-cycle-draft /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash/5p8-debug-r1-fast20-restore-r01-20260829/power-cycle-attestation.template.json \
  --seal-power-cycle-attestation
```

Create a new exact-mute readback immediately before phase 2; the phase-2 flash command
intentionally omits the phase-1-only `--pre-program-attestation` argument:

```bash
/home/pi/pluto-plus-utils/.venv/bin/python scripts/attest_selector_flash_pluto_mute.py \
  --checkpoint phase2_pre_openocd \
  --serial 104000b29905000e17000800065934759d \
  --uri REPLACE_CURRENT_USB_URI \
  --output /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash/operator-inputs/5p8-debug-r1-fast20-restore-r01-20260829.phase2-pluto-mute.json
```

```bash
./.venv/bin/python scripts/flash_and_attest_selector.py \
  --campaign-id 5p8-debug-r1 \
  --run-id 5p8-debug-r1-fast20-restore-r01-20260829 \
  --board-id stm32c011-4c0055000950313950363920 \
  --image-role fast20 \
  --elf build/STM32C011F4P6/fast20/pluto_fast20.elf \
  --bin build/STM32C011F4P6/fast20/pluto_fast20.bin \
  --profile profiles/fast20-v1/control_profile.json \
  --openocd-config openocd/rpi4-swd.cfg \
  --evidence-root /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash \
  --power-cycle-attestation /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash/5p8-debug-r1-fast20-restore-r01-20260829/power-cycle-attestation.json \
  --pluto-serial 104000b29905000e17000800065934759d \
  --pluto-uri REPLACE_CURRENT_USB_URI \
  --pluto-mute-evidence /home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/selector-flash/operator-inputs/5p8-debug-r1-fast20-restore-r01-20260829.phase2-pluto-mute.json \
  --verify-after-power-cycle
```

Phase 2 must compare the complete Fast20 BIN extent, `reset run`, and prove the expected startup
image identity before sealing evidence.

Under that exact live Fast20 evidence, run one T8 timing plan. It captures exactly two independent
radio streams with derived IDs `<timing-run-id>-capture-01` and
`<timing-run-id>-capture-02`. Then run one new matrix plan that binds the single qualified timing
result containing both streams. The Fast20 evidence-manifest path/hash, fixture-v2 manifest,
run-specific setup attestation, Pluto/native/dependency identities, and all raw hashes are bound to
each result.

## 9. Analysis contract

Perform complex arithmetic before magnitude or phase reporting:

```text
H          = RX2 pilot phasor / RX1 pilot phasor
H_path     = H_selected - H_off
C_raw      = 20 log10(|H_selected| / |H_off|)
C_path     = 20 log10(|H_path| / |H_off|)
```

For independent rewired stages:

- do not pair repeat indices;
- use independent two-sample complex bootstrap intervals;
- preserve phase-free upper bounds for nondetections;
- use simultaneous 95% amplitude, phase, and normalized-vector residual gates;
- report coherent increments and counterfactual vector removal, not percentages of incoherent
  power; and
- require upstream artifact identities to be disjoint unless a tested joint covariance model is
  explicitly used.

Frozen decision thresholds:

| Gate | Value |
|---|---:|
| Operational-low transfer ratio | `0.011918` |
| Metrology-low transfer ratio | `0.002080` |
| Conditioned excess discriminator | `0.028764` |
| Full complex equivalence | `+/-0.2 dB`, `+/-2 degrees`, residual no more than `4.23%` |
| Operational state observability | `C_raw >= 20 dB` |
| Operational physical contrast | `C_path >= 20 dB` |
| One-degree coherent phase goal | `C_path >= 35.1629 dB` |

Additional predeclared branch gates:

- **P1 muted spur:** bind `f_p` to P0's actual positive pilot readback and define the attribution
  window as `f_p ±2 kHz`. Classify, but exclude from this blocking test, DC/LO (`|f| ≤5 kHz`), the
  conjugate image (`-f_p ±2 kHz`), and filter edges (`|f| ≥350 kHz`). Using T1's frozen
  Welch/robust-noise estimator, stop for a capture-system/interference investigation only if a
  narrowband feature inside `f_p ±2 kHz` is at least 10 dB over local noise in at least four of
  five streams on either RX, with peaks agreeing within two analyzer bins; or if another spur
  raises the robust target-window floor by at least 3 dB relative to both 10-kHz control windows
  centered at `f_p ±15 kHz`. Report all other admitted-passband features as diagnostics without
  treating ordinary DC/image/edge structure as a topology failure.
- **P2 collapse:** compare matched 5.8-GHz gain/LO/DDS conditions using an independent complex
  bootstrap. Call the input drive *required* only when the 95% upper confidence bound on
  `|H_P2|/|H_P0|` is at most `0.31623` (at least 10 dB reduction) and RX1 reference amplitude
  remains within `±1 dB`. Call it *not collapsed* only when the 95% lower bound is at least
  `0.70795` (less than 3 dB reduction). Anything between is inconclusive and cannot choose a
  physical branch.
- **Conditional M trigger:** evaluate only after A and E exist. Run M when the 95% lower bound on
  `|H_A|` exceeds the operational-low ratio `0.011918`; otherwise skip M unless A is complex-
  equivalent to E under the full equivalence gate.
- **First material boundary:** for adjacent A/B/C/E stages, an increment is material only when its
  simultaneous 95% amplitude interval excludes zero and the counterfactual removal changes
  `|H_E|` by at least 3 dB. A phase-only difference is reported but does not by itself select an
  intervention.

### Interpretation and first response

| Observation | Supported location | First reversible intervention |
|---|---|---|
| P1 retains a repeatable target spur | External signal, LO/device spur, or capture artifact | Retuned and shielded muted controls; no RF attribution yet |
| P2 collapses relative to P0 | Driven 8-way/selector-input path required | Prioritize D1/D2 and E closure |
| A is complex-equivalent to E | Pluto/internal or connector-local path dominates | Port-pair matrix; external source/second radio; no board rework |
| B-A is material | RX2 cable/common mode | Separate/reverse/substitute cable; inspect connector and routing |
| C-B is material | Selector common launch, board, power, or control harness | Controlled shielding/ground/harness intervention; inspect J2/U1/ground |
| One/few D1 cells are poor | Port cable/load/launch/solder defect | Substitute one item at a time; inspect implicated port |
| D1 is broadly poor after low A/B/C | Selector or assembly/ground architecture | Evidence-gated inspection/rework; consider higher-isolation switch |
| Correctly excited D2 closes to E | Coherent sum of input leakages | Avoid simultaneous-feed calibration or improve per-arm isolation |
| D2 fails closure | Splitter interaction, mismatch, or common bypass | Alternate splitter/load/cable; controlled length/pad perturbation |
| Feature moves/damps under one perturbation | Mismatch/standing wave contribution | Improve terminations/matching/routing and repeat |

Bandpass filtering cannot remove leakage at the same frequency as the desired tone. An RX2
attenuator normally reduces desired and leakage together and is not a cure for a channel that is
already unclipped. Additional averaging cannot remove a coherent stable bias. A single path-length
correction is inadmissible because the retained response rejects a single-delay/rank-one model.
Passing 20 dB is only the minimum switching-observability gate. It does not meet the 35.1629-dB
one-degree goal and does not make 5.8-GHz AD9363 operation production-qualified; all results above
the device's qualified range remain explicitly experimental.

## 10. Artifact locations and run IDs

Use unique, never-reused IDs:

```text
5p8-debug-r1-p0-paired-r01-20260829 ... r05
5p8-debug-r1-p0-2p4-r01-20260829 ... r05
5p8-debug-r1-p1-muted-r01-20260829 ... r05
5p8-debug-r1-p2-input-off-r01-20260829 ... r05
5p8-debug-r1-fast20-precontrol-r01-20260829
5p8-debug-r1-a-direct-20260829
5p8-debug-r1-b-cable-20260829
5p8-debug-r1-bench-flash-r01-20260829
5p8-debug-r1-c-selector-20260829
5p8-debug-r1-d1-ant1-20260829 ... ant8
5p8-debug-r1-d2-ci-ant1-r01-20260829 ... ci-ant8-r05
5p8-debug-r1-d2-d2i-ant1-r01-20260829 ... d2i-ant8-r05
5p8-debug-r1-e-full-20260829
5p8-debug-r1-m-tx1-rx1-r01-20260829 ... m-tx2-rx2-r05
5p8-debug-r1-f-<boundary>-coarse-20260829
5p8-debug-r1-f-<boundary>-fine-<center1>[-<center2>]-20260829
5p8-debug-r1-x-<boundary>-baseline-20260829
5p8-debug-r1-x-<boundary>-<intervention>-20260829
5p8-debug-r1-x-e-baseline-20260829
5p8-debug-r1-x-e-<intervention>-20260829
5p8-debug-r1-q-static-r01-20260829
  derived captures: ...-capture-01 through ...-capture-09
5p8-debug-r1-fast20-restore-r01-20260829
5p8-debug-r1-q-timing-r01-20260829
  derived captures: ...-capture-01 and ...-capture-02
5p8-debug-r1-q-matrix-r01-20260829
  derived captures: ...-capture-01 through ...-capture-05
```

The D2 patterns above expand to one unique run ID for every role (`c_i` or `d2_i`), arm
(ANT1–ANT8), and repeat (`r01`–`r05`): 80 captures total. The M pattern expands to four cells
(`TX1_RX1`, `TX1_RX2`, `TX2_RX1`, `TX2_RX2`) by five repeats: 20 condition runs total. A coarse
F run contains exactly 430 conditions. A fine F run contains 250 conditions for one selected
center or 500 for two selected centers. X always binds distinct fresh same-revision baseline and
intervention IDs at the implicated boundary and at E; if E is itself the implicated boundary,
only one baseline/intervention pair is used. Q uses one static run, one timing plan containing two
independent derived capture IDs, and one fresh matrix run that binds that timing result.

Exact existing-runner locations are:

```text
~/.local/state/smateway/boards/<board>/closed-loop-frequency-sweeps/<run-id>/
~/.local/state/smateway/boards/<board>/5g8-leakage-ladder/<run-id>/
~/.local/state/smateway/boards/<board>/5g8-one-hot-path-ladder/<run-id>/
~/.local/state/smateway/boards/<board>/pluto-usb-captures/<artifact-id>/
~/.local/state/smateway/boards/<board>/pluto-usb-captures/leakage-runs/<run-id>/
~/.local/state/smateway/boards/<board>/pluto-usb-captures/one-hot-runs/<run-id>/
~/.local/state/smateway/boards/<board>/5g8-one-hot-path-ladder/.run-state/<run-id>/
~/.local/state/smateway/boards/<board>/5g8-muted-control/<run-id>/
~/.local/state/smateway/boards/<board>/5g8-input-off-control/<run-id>/
~/.local/state/smateway/boards/<board>/5g8-arm-preserving-d2/<run-id>/
~/.local/state/smateway/boards/<board>/5g8-port-pair-matrix/<run-id>/
~/.local/state/smateway/boards/<board>/5g8-fine-frequency/<run-id>/
~/.local/state/smateway/boards/<board>/5g8-selected-state/<campaign>/<mode>/<run-id>/
```

The existing general hardened runner stores about 24 MB per stage (about 96 MB for A/B/C/E); the
one-hot size after T3 is about 1.73 GB for all eight D1 rows. P0's five 5.7/5.8 runs are about
0.8 GB and its five 2.4-GHz controls about 0.4 GB. P1 is budgeted conservatively at 0.4 GB. T7
generates the exact condition count and byte estimate in its immutable plan and requires twice
that amount free on the Raspberry Pi local filesystem before capture.

Every hardened accepted result binds the clean Smateway commit, `pluto-plus-utils` source, exact native
libiio identity, Pluto identity/URI, firmware, selector control image/profile, fixture and setup
evidence, RF readbacks, stream identity, continuity/headroom/quality evidence, raw data, metadata,
condition records, plans, manifests, and hashes.

Commit normalized derived JSON and PNGs, not raw IQ. Planned durable outputs:

```text
docs/5g8_root_cause_analysis/data/topology-ladder-plan.json
docs/5g8_root_cause_analysis/data/topology-ladder-results.json
docs/5g8_root_cause_analysis/data/one-hot-matrix-results.json
docs/5g8_root_cause_analysis/data/weighted-closure-results.json
docs/5g8_root_cause_analysis/data/fine-frequency-results.json
docs/5g8_root_cause_analysis/data/hypothesis-disposition.json
docs/5g8_root_cause_analysis/data/figures-manifest.json
docs/5g8_root_cause_analysis/png/fig08_physical_stage_attribution_and_disposition.png
```

## 11. Execution ledger

| Step | State | Evidence / blocker |
|---|---|---|
| Plan authored and reviewed | Checkpoint published | Full status and restart point recorded in the 2026-08-30 tooling handoff report |
| Passive device/native/storage preflight | Complete | Read-only snapshot in Section 3; re-resolve URI before capture |
| T1–T8 campaign tooling | Implemented; not RF-released | Latest focused gate `316 passed`; full run stopped after `305 passed`; host authority and final semantic/full regression gates remain |
| Fixture inventory and current setup photograph | Blocked on operator | Section 5 |
| P0 untouched-fixture screen | Pending | Requires frozen tooling revision, inventory/photo, and explicit physical confirmation; Fast20 retained |
| P1 muted control | Pending | Requires T1 and untouched fixture |
| P2 input-drive-off screen | Pending | Requires T2 and physical rewire |
| Stage A | Pending | Requires Stage-A fixture/setup artifacts and rewire |
| Stage B | Pending | Requires accepted A and one controlled cable change |
| Reviewed static bench flash/readback | Pending | Only after accepted B; required before C/D/E |
| Stage C | Pending | Requires accepted B, supply/ground evidence, eight loads, bench `ALL_OFF` |
| Stage E simultaneous fixture | Pending | Capture requires accepted C; closure analysis waits for D2 |
| Conditional M port matrix | Pending | Evaluate only after A/E; requires T6 and protected chains |
| Stage D1/D2 | Pending | Eight manual rows each; arm-preserving/weight contract required for D2 |
| Stage F fine sweep | Pending | Run at first material boundary and E |
| Stage X intervention and confirmation | Pending | Requires evidence-backed reversible change |
| Stage Q selected-state re-entry | Pending | Required after a supported fix; two timing captures plus fresh matrix |
| Final report and calibration disposition | Pending | 5.8 GHz stays rejected until all exit gates pass |

## 12. Stop and rewrite conditions

Stop without accepting new data and revise this file with a new clean commit/run ID if:

- any component identity, rating, reference plane, or physical port map is unknown;
- the live setup differs from the photograph or manifest;
- P0 has not been accepted before a cable is moved, or Fast20 is replaced before accepted B;
- the source/dependency tree is dirty or any software/native identity fails;
- the USB URI or Pluto serial differs;
- P1 reveals a material muted spur at the target;
- a stage straddles a frozen decision threshold and needs a fresh independent cohort;
- a component is substituted, disconnected, nudged, or moved unexpectedly;
- D excitation weights/source-disjoint references are unavailable;
- closure would reuse evidence without explicit covariance treatment;
- a proposed intervention changes more than one physical variable;
- selector mailbox/GPIO, RF readback, continuity, headroom, or final mute fails; or
- power is lost, USB disconnects, `ENODATA` occurs, or any capture process is interrupted.

The correct status after any such event is **paused with the last accepted artifact preserved**,
not an improvised retry.
