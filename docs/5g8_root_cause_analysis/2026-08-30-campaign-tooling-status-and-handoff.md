# 5.8 GHz root-cause campaign: tooling status and handoff

**Date:** 2026-08-30
**Campaign:** `5p8-debug-r1`
**Disposition:** **NO-GO for authoritative RF capture or coefficient deployment**
**Purpose:** record what has been completed, what is known, what remains, and the exact restart point.

## Executive summary

The 5.8 GHz problem has not been explained by optimizer initialization. Ten deliberately different
optimizer seeds produced the same fitted result to numerical precision. Existing conducted data
continues to show a coherent `ALL_OFF` term large enough to invalidate the 5.8 GHz calibration for
deployment. The next scientific task is physical attribution, not further optimizer tuning.

The repository now contains the planned T1–T8 campaign tooling, fixture/setup schemas, operator
guides, source/native/device provenance checks, raw-IQ reanalysis, replay protection, mandatory
cleanup evidence, and an in-progress shared root-owned run ledger for the P2, T6, and T7 runners.
No RF transmission, capture, selector programming, OpenOCD mutation, power change, or physical
rewire was performed while developing and testing this tooling.

At this publication cut:

- the latest combined shared-ledger/P0/P2/T6/T7 focused gate passed **316/316**;
- Ruff checks, task-surface formatting, Python compilation/type checks run during the component
  work, and Git whitespace checks were green at their last completed gates;
- a fresh whole-repository test run was intentionally stopped at the operator's request after
  **305 tests had passed with no observed failure**; it is therefore **not** a completed full-suite
  result;
- an earlier pre-final-migration repository cut passed **1,356/1,356**, but that count must not be
  used as verification of this newer shared-ledger cut;
- the privileged authority is **not installed on devpi** and the read-only verifier correctly
  fails closed;
- the current physical fixture has not passed the mandatory inventory/photograph gate; and
- exact 5.8 GHz calibration coefficients remain rejected.

This commit is a transparent engineering checkpoint, not an RF-release tag.

## Background and motivation

The calibration fixture uses Pluto TX1 as a conducted stimulus, a matched 2-way branch for the RX1
reference, and a 2–8 GHz 8-way distribution network feeding the selector inputs. The selector
common port returns to Pluto RX2. Existing accepted evidence established a strong, stable RX1
reference and high selected-path coherent SNR, but also a repeatable 5.8 GHz `ALL_OFF` response:

- `|H_off| = 0.059257`, or `-24.545 dB` relative to RX1;
- median desired-path transfer `= 0.119179`;
- aggregate desired-to-`ALL_OFF` contrast `= 6.069 dB`; and
- zero of 160 historical path observations met the 20 dB minimum.

That observation is a sum of several possible physical mechanisms:

```text
H_off = H_Pluto/local
      + H_RX2-cable/common
      + H_selector/common-launch
      + H_input-fixture
```

Earlier permutation, TX-gain, TX2-removal, schedule-alignment, model-rank, and selector-history
controls narrowed the hypotheses but did not identify which physical boundary dominates. The new
campaign is designed to isolate those terms without silently changing multiple variables at once.

## Optimizer-seed result

The admitted 5.8 GHz calibration cohort was refit with seeds:

`0, 1, 2, 3, 7, 42, 20260827, 0x5A8, 0xC0FFEE, 0xFFFFFFFF`.

All ten runs produced, to numerical precision:

| Metric | Result |
|---|---:|
| Amplitude RMS | `0.158351929 dB` |
| Phase RMS | `0.812145757°` |
| Worst selected-to-`ALL_OFF` contrast | `-8.764165473 dB` |
| Maximum gain-coefficient difference between seeds | `0 dB` |
| Maximum phase-coefficient difference between seeds | `5.1e-7°` |

Conclusion: optimizer initialization is not the root cause and changing seeds is not a corrective
action. Bootstrap seeds affect resampling intervals, not the measured IQ or point estimate.

## Completed engineering work

### Campaign plan, fixture evidence, and operator contracts

- Authored the exhaustive execution plan in [`5p8_ghz_debug_plan.md`](../../5p8_ghz_debug_plan.md).
- Added fixture-v2 templates for Stages A, B, C, and E.
- Added stage-specific setup attestations, P1 muted-control templates, P2 input-off templates, and
  explicit reference-plane/load/cable identities.
- Documented single-power-source, SWD, mute, termination, RX1-protection, no-hot-rewire, source
  provenance, local-storage, and failure-quarantine rules.
- Preserved the rule that no irreversible PCB or connector modification is authorized by this
  campaign.

### T1 — true muted dual-RX control

- Added one-stream, 10-second, dual-RX muted capture and cohort analysis.
- Requires both TX gains at `-80 dB`, all DDS channels at exact zero, ABI-2 continuity, local raw
  storage, exact post-capture mute, and immutable run-ID reservation/burn evidence.
- Reports absolute RX1/RX2 spectral evidence without inventing transfer phase when no pilot exists.
- Hardened run paths against symlink/cross-device rebinding and finalization races.

### T2 — input-drive-off control and P0 normalization

- Added the separately terminated stimulus/input topology, setup generator, capture runner, and
  cohort analyzer.
- Recomputes P0 `ALL_OFF` phasors, RX1 amplitude, pilot, schedule timing, and alignment from bound
  raw dual-RX IQ, ABI-2 metadata, immutable plan, and the reviewed Fast20 profile.
- Requires exact legacy command/program provenance and artifact-root containment.
- Added FD-relative/no-follow P2 persistence and quarantine of finalized-but-uncommitted artifacts,
  including the asynchronous interruption window.
- Applies the P2-minus-P0 RX1 stability rule to the 95% interval.

### T3 — one-hot repeat parity

- Promoted the direct one-hot ladder to five source-distinct `-20 dB` repeats per cell.
- Defined the six-gain/five-attribution-repeat contract and rejection of wrong/duplicate counts.

### T4 — arm-preserving D2 closure

- Added arm-weight/covariance-aware closure analysis so direct one-hot D1 is not falsely treated as
  equivalent to excitation through the 8-way arms.
- Hardened D2 capture admission, target identity, cleanup, raw provenance, and fail-closed handling
  where historical summaries cannot support authoritative closure.

### T5 — source/fixture/selector binding

- Added source-bound topology analyzers, fixture generators, selector flash/mute attestation, and
  exact source/native/device comparisons.
- Ensures selector-image evidence is appropriate to Fast20 live timing versus reviewed static
  mailbox operation.

### T6 — protected TX/RX port-pair matrix

- Added the four-cell protected port-pair runner/analyzer, two-stream raw-IQ reanalysis, strict
  identity/mute schemas, and complete UTC/monotonic/boot timeline validation.
- Migrated execution authorization to the shared atomic `burn_run` operation.
- Added degraded cleanup and independent emergency-failure evidence for local persistence loss.
- Latest focused T6 runner/analyzer gate: **95/95 passed**.

### T7 — fine-frequency sweep

- Added coarse-to-fine frequency plans with exact same-campaign/topology/reference/source/device
  binding and fresh URI allowance only.
- Added pre-hardware history rejection, immutable reservation/burn evidence, exact campaign safety
  revalidation, raw reanalysis, and source/native checks.
- Migrated burn acquisition to one nonce-bound atomic operation and added zero-live handling for
  denial/response-loss at the authorization boundary.
- Added exact analyzer validation of successful manifest/attempt lineage and emergency receipts.
- Latest focused T7 model/runner gate: **117/117 passed** after the final fixture correction.

### T8 — selected-state qualification and interventions

- Added Fast20 timing qualification, static ANT1–ANT8 matrix qualification, production fixture-v2
  transition evidence, and selected-state/intervention analysis.
- Requires electrical `ALL_OFF`, source/native/device/image bindings, transition order, one-variable
  interventions, restoration/reapplication, and post-fix timing/matrix re-entry.
- Current supported intervention evidence covers the selector current-limit leaf. RX2
  attenuator/cable/load intervention leaves remain intentionally unsupported until implemented and
  reviewed.

### Shared global run ledger

- Added a standard-library helper module, root-only provisioner, unprivileged verifier, operator
  guide, and offline tests.
- Uses fixed production paths, a fixed `smateway-rf` service-account identity, narrow `sudo -n`
  helper invocation, campaign-specific namespaces, exact request/response schemas, no-follow
  dirfd operations, per-ledger locking, create-once slots, hard-link anchors, atomic nonce-bound
  burn, one-byte monotonic guard, and emergency failure receipt.
- Production installation has deliberately not been performed.

## Review and hardening work completed

Multiple independent semantic reviews were run after ordinary unit tests were already green. They
found and drove fixes for issues that line coverage alone did not expose, including:

- restored/moved run-directory replay;
- same-device symlink ancestry and cross-device artifact redirection;
- mutable derived P0 values and analysis-controlled Fast20 timing;
- ambient Python/module/native-library drift;
- missing initial identity/mute admission;
- cleanup gaps before/after execution-marker persistence;
- incomplete safety timelines and fully rebound safety documents;
- moved common-parent ledger replay;
- concurrent guard mutation and partial burn/response-loss states;
- finalized artifact quarantine gaps; and
- analyzer acceptance of incomplete manifest lineage.

The resulting implementation is substantially stronger, but the remaining release gates below
still apply.

## Verification status at publication

| Gate | Result |
|---|---|
| Combined shared-ledger/P0/P2/T6/T7 focused suite | **316 passed** |
| T6 focused runner/analyzer | **95 passed** |
| T7 focused model/runner | **117 passed** |
| Shared-ledger/provisioner focused | **20 passed** |
| Fresh whole-repository run | **Stopped by operator after 305 passed; incomplete** |
| Earlier full-suite result on an older pre-final-migration cut | `1,356 passed` (historical only) |
| Hardware/RF execution during tooling work | None |
| devpi privileged-ledger installation | Not performed |
| Fixture inventory/current labeled photograph | Not accepted |

The interrupted whole-repository run had no observed failure before cancellation, but it must not
be described as a passing full suite.

## Remaining work

### 1. Finish and re-audit the privileged authority boundary

Before any authoritative P2/T6/T7 run:

1. finish the provisioner's pre-write validation of the fixed `smateway-rf` account, locked
   password/non-login shell, groups/capabilities, effective sudo policy, ancestry, devices,
   ownership, and modes;
2. ensure the runner account has no privilege other than the exact helper allowlist;
3. independently review the final helper/provisioner/verifier source and concurrent/crash tests;
4. complete a fresh full repository regression; and
5. make a new clean source-freeze commit if any source changes are required.

The normal interactive `pi` administrator is explicitly outside the runner-process boundary.

### 2. Provision and verify devpi

Only after the final reviewed commit is frozen:

1. provision the fixed service account, helper, sudoers fragment, global root, anchors, and seal as
   root;
2. run the read-only verifier as `smateway-rf`;
3. retain all ledger history permanently; never delete it to reuse a run ID; and
4. record the exact verification evidence in the first immutable campaign plan.

This checkpoint did not modify `/var`, `/etc`, `/usr/local`, users, groups, or sudo policy.

### 3. Complete the physical fixture gate

Provide and seal:

- one current, clearly labeled setup photograph;
- exact port map and reference planes;
- splitter/filter/attenuator/load IDs and ratings;
- every cable/adapter identity;
- bench voltage, current limit, displayed current, ground topology, and target-power source;
- confirmation that no component moved after the photograph; and
- the exact RX2 attenuation/protection state.

### 4. Execute the physical campaign in order

After a clean source freeze, verified ledger, and accepted fixture evidence:

```text
Fast20 exact-match attestation + required power cycle
  -> P0 untouched-fixture schedule/reference proof
  -> P1 true muted control
  -> P2 input-drive-off control
  -> A direct RX2 termination
  -> B fixed RX2 cable
  -> reviewed static image/readback
  -> C powered selector ALL_OFF with terminated inputs
  -> E simultaneous full fixture
  -> conditional M port-pair matrix
  -> D1/D2 closure measurements
  -> F fine-frequency sweeps
  -> X controlled intervention
  -> Q timing + matrix re-entry after a supported fix
```

Every failed run ID remains burned and is never retried. Every accepted capture must be continuous,
source-distinct, locally stored on the Raspberry Pi, recursively hash-bound, and cleanup-qualified.

### 5. Publish scientific outputs

Commit normalized JSON and figures—not raw IQ—for topology results, one-hot matrix, weighted
closure, fine-frequency observations, intervention result, hypothesis disposition, and final
calibration decision. Keep 5.8 GHz rejected unless both `C_raw` and `C_path` have lower confidence
bounds of at least 20 dB. The one-degree objective additionally requires `C_path >= 35.1629 dB`.

## Exact restart point

The next engineering action is **not RF capture**. It is:

1. harden and independently review the final `smateway-rf` provisioner/account-policy checks;
2. run the full offline suite from zero;
3. create a clean source-freeze commit;
4. provision and read-only verify the authority on devpi; and
5. obtain the labeled fixture photograph/inventory before P0.

Until those steps are complete, the correct status is:

> Tooling implemented and focused-offline-tested; privileged host boundary and full regression
> incomplete; physical campaign not started; 5.8 GHz coefficients rejected.
