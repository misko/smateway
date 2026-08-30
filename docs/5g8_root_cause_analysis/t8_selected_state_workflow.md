# T8 selected-state qualification workflow

T8 has three deliberately separate authorities. A fresh device observation proves which Pluto,
USB context, Pluto firmware, paired-RX ABI, and native libiio runtime are current. A phase-1
intervention plan freezes one fixture-v2 leaf change and the exact X run branch before X transmits. A
phase-2 intervention seal admits the retained X bytes and records whether the supported after-state
is actually installed. Hand-written `accepted: true`, stream IDs, raw hashes, or a restored baseline
cannot substitute for these producers.

## 1. Produce the device identity immediately before each Q plan

This action is read-only. It resolves one USB serial, opens only the libiio context description,
reads bounded sysfs attributes, attests the native runtime, and creates a read-only JSON file.

```bash
set -euo pipefail
PYTHON=./.venv/bin/python
STATE=/home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/t8-inputs
install -d -m 700 "$STATE"
"$PYTHON" scripts/prepare_5g8_selected_state_inputs.py device-identity \
  --serial 104000b29905000e17000800065934759d \
  --uri REPLACE_CURRENT_USB_URI \
  --output "$STATE/REPLACE_UNIQUE_RUN_ID.device-identity.json"
```

The producer is invoked through the repository virtual environment and bootstraps its pinned
hardware runtime itself. It accepts only create-new outputs on the Raspberry Pi local filesystem,
rejects `/media`, `/mnt`, and `/run/media`, and rejects any symlink in an output path. Keep `STATE`
under `/home/pi/.local/state`; do not point it at Pluto storage or removable/network storage.

Prepare must follow within five minutes. Capture independently resolves the exact serial/URI again
and the persisted condition record binds the observed model, firmware version, PHY, ABI, and native
runtime.

## 2. Mandatory X-run prebinding contract

Before any X baseline capture, the general leakage runner must create immutable plans with
`--x-intervention-contract-id`, `--x-run-role`, `--x-implicated-boundary-stage`,
`--x-installed-fixture-manifest`, `--x-capture-fixture-manifest`,
`--x-acquisition-index`, and `--x-freshness-epoch-id`. Its plan binds both the actual A/B/C/E
topology fixture and this exact intervention context:

```json
{
  "x_intervention_prebinding": {
    "schema": 1,
    "binding_kind": "5g8_x_intervention_prebinding_v1",
    "contract_id": "THE_PREDECLARED_CONTRACT_ID",
    "run_role": "boundary_baseline",
    "installed_fixture_revision_sha256": "THE_AFTER_FIXTURE_REVISION"
  }
}
```

The companion `x_intervention_capture_context` derives the implicated stage, consecutive
acquisition index, common freshness epoch, exact capture-state full-E revision, installed-after
full-E revision, and sealed selector identity from source files. If A, B, or C is implicated, use
exactly four distinct roles in this order: `boundary_baseline`, `boundary_intervention`,
`full_fixture_baseline`, `full_fixture_intervention`. If E itself is implicated, use exactly the
two full-fixture roles; never duplicate the E streams under boundary aliases.

The accepted manifest is `5g8_x_intervention_capture_v1`. It binds the immutable plan and actual
topology fixture, retains every raw/metadata/condition file by path/size/SHA-256, and proves ABI-2
continuity, measurement quality, and final mute. A/B truthfully record RF disconnected, bench
power off, and control harness disconnected; C/E instead record live mailbox `ALL_OFF`. The
phase-1 and phase-2 producers reject missing, stale, mismatched, self-asserted, or reused evidence.

Insert this exact X binding tuple before the terminal `--plan-only` or `--execute` flag in each
otherwise complete A/B/C/E general-ladder command. Use the same contract ID, freshness epoch,
installed-after E fixture, and sealed selector tuple across the cohort; change the run role,
capture-state full-E fixture, acquisition index, and actual `--stage`/`--fixture-manifest` as
predeclared:

```bash
  --x-mode \
  --x-intervention-contract-id REPLACE_UNIQUE_CONTRACT_ID \
  --x-run-role REPLACE_BOUNDARY_OR_FULL_FIXTURE_ROLE \
  --x-implicated-boundary-stage REPLACE_A_B_C_OR_E_STAGE_ENUM \
  --x-installed-fixture-manifest REPLACE_INSTALLED_AFTER_FULL_E_FIXTURE_JSON \
  --x-capture-fixture-manifest REPLACE_BEFORE_OR_AFTER_FULL_E_FIXTURE_JSON \
  --x-acquisition-index REPLACE_CONSECUTIVE_INDEX \
  --x-freshness-epoch-id REPLACE_COMMON_FRESHNESS_EPOCH_ID \
  --selector-flash-evidence REPLACE_SEALED_SELECTOR_EVIDENCE_JSON \
  --selector-flash-evidence-sha256 REPLACE_EXACT_SHA256 \
  --selector-flash-run-id REPLACE_EXACT_SELECTOR_FLASH_RUN_ID
```

For A/B, the selector tuple binds the global intervention identity but the actual capture setup
must keep selector RF, power, and control disconnected; do not add bench/profile/OpenOCD arguments.
For C/E, use the normal selector-connected bench/profile/OpenOCD arguments and the same selector
tuple. Plan and execute commands must use byte-identical inputs and X arguments.

## 3. Freeze phase 1 before X

The before and after full-conducted fixture-v2 manifests must have independently validated
production provenance and normalized physical projections (`shared_fixture` plus `stage_delta`)
that differ at exactly the declared leaf below the declared component ID. Derived provenance such
as `prior_stage_binding` paths and hashes may change when the A→B→C→E chain is regenerated, but it
is reopened and validated rather than treated as a physical change. The producer derives the
before/after values and revision hashes from those bytes; the operator cannot type them into the
result.

```bash
"$PYTHON" scripts/prepare_5g8_selected_state_inputs.py intervention-plan \
  --contract-id REPLACE_UNIQUE_CONTRACT_ID \
  --campaign-id 5p8-debug-r1 \
  --board-id stm32c011-4c0055000950313950363920 \
  --before-fixture-manifest REPLACE_BEFORE_FIXTURE_V2_JSON \
  --after-fixture-manifest REPLACE_AFTER_FIXTURE_V2_JSON \
  --component-id REPLACE_COMPONENT_ID \
  --property-path REPLACE_JSON_POINTER_TO_THE_ONE_CHANGED_LEAF \
  --restore-instruction 'REPLACE_EXACT_REVERSAL_INSTRUCTION' \
  --boundary-baseline-plan REPLACE_PLAN_JSON \
  --boundary-intervention-plan REPLACE_PLAN_JSON \
  --full-fixture-baseline-plan REPLACE_PLAN_JSON \
  --full-fixture-intervention-plan REPLACE_PLAN_JSON \
  --output "$STATE/intervention-change-plan.json"
```

The four plan flags above are the A/B/C branch. For an E-implicated intervention, omit both
`--boundary-*-plan` flags and supply only the two full-fixture plans.

## 4. Seal phase 2 after X and support analysis

The support result is analyzer output, not an operator template. It must be
`5g8_intervention_support_result_v1`, bind every exact X manifest hash and its retained
analysis file, pass its simultaneous improvement gate, and contain no rejection reasons. Complete
[the installed-after template](t8_installed_after_state.template.json) only after the supported
fix is physically installed. The A/B/C four-role order necessarily changes fixture state twice:
seal after→before restoration evidence between `boundary_intervention` and
`full_fixture_baseline`, then before→after reapplication evidence between
`full_fixture_baseline` and `full_fixture_intervention`.

```bash
"$PYTHON" scripts/analyze_5g8_intervention_support.py \
  --change-plan "$STATE/intervention-change-plan.json" \
  --boundary-baseline-manifest REPLACE_ACCEPTED_X_MANIFEST \
  --boundary-intervention-manifest REPLACE_ACCEPTED_X_MANIFEST \
  --full-fixture-baseline-manifest REPLACE_ACCEPTED_X_MANIFEST \
  --full-fixture-intervention-manifest REPLACE_ACCEPTED_X_MANIFEST \
  --analysis-output "$STATE/intervention-support-analysis.json" \
  --result-output "$STATE/intervention-support-result.json"

"$PYTHON" scripts/prepare_5g8_selected_state_inputs.py intervention-seal \
  --change-plan "$STATE/intervention-change-plan.json" \
  --boundary-baseline-manifest REPLACE_ACCEPTED_X_MANIFEST \
  --boundary-intervention-manifest REPLACE_ACCEPTED_X_MANIFEST \
  --full-fixture-baseline-manifest REPLACE_ACCEPTED_X_MANIFEST \
  --full-fixture-intervention-manifest REPLACE_ACCEPTED_X_MANIFEST \
  --installation-attestation REPLACE_INSTALLED_AFTER_ATTESTATION \
  --support-result "$STATE/intervention-support-result.json" \
  --restoration-evidence REPLACE_AFTER_TO_BEFORE_TRANSITION_ATTESTATION \
  --reapplication-evidence REPLACE_BEFORE_TO_AFTER_TRANSITION_ATTESTATION \
  --output "$STATE/intervention-evidence.json"
```

The analyzer has no caller-supplied pass flag or adjustable release threshold. It reopens all bound
CI16 IQ/metadata/condition files, independently recomputes the exact tone, uses phase-free upper
bounds for valid RX2 nondetections, and applies the fixed simultaneous 3 dB improvement and ±1 dB
RX1-reference confidence gate. For an E-implicated two-run branch, omit both
`--boundary-*-manifest` flags from both commands.

For an E-implicated two-run branch, the transition flags may both be omitted if no diagnostic
restoration occurred. Whenever present, each must follow the
[fixture transition template](t8_fixture_state_transition.template.json): restoration proves
after→before, reapplication proves before→after, and the final installed-state attestation must be
newer than both. The seal never treats restoration as adoption: Q accepts only the after fixture
revision.

## 5. Q prepare, capture, analyze primitives

Run each mode as three explicit commands. `prepare` and `analyze` are hardware-inert; only
`capture` can transmit/open buffers. One timing plan produces exactly two capture IDs. One matrix
plan binds exactly that one timing result, the static result, and the phase-2 intervention seal.

```bash
set -euo pipefail
PYTHON=./.venv/bin/python
BOARD=stm32c011-4c0055000950313950363920
SERIAL=104000b29905000e17000800065934759d
CAMPAIGN=5p8-debug-r1
RUN_DATE=REPLACE_YYYYMMDD
FIXTURE=REPLACE_INSTALLED_AFTER_FIXTURE_V2_JSON
STATIC_RUN="5p8-debug-r1-q-static-r01-${RUN_DATE}"
TIMING_RUN="5p8-debug-r1-q-timing-r01-${RUN_DATE}"
MATRIX_RUN="5p8-debug-r1-q-matrix-r01-${RUN_DATE}"
SETUP_DIR=/home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/t8-inputs/q-setup-drafts
install -d -m 700 "$SETUP_DIR"

"$PYTHON" scripts/generate_5g8_fixture_manifest.py \
  --setup-from-fixture "$FIXTURE" --stage full_conducted_fixture \
  --board-id "$BOARD" --serial "$SERIAL" --setup-run-id "$STATIC_RUN" \
  --setup-draft-output "$SETUP_DIR/${STATIC_RUN}.json"
```

Stop and complete only the static draft's observation placeholders: unique attestation ID,
timezone-qualified observation time, setup photograph/diagram path and SHA-256, plus the exact
sealed bench selector path/SHA/run ID. Do not alter the derived fixture hash, component inventory,
connection inventory, stage, campaign, or run ID. Make the completed setup file read-only.

### Q1 — sealed bench image: static matrix only

The selector must already be flashed and power-cycle-attested as the exact bench image named by
`BENCH_SELECTOR`. The static phase must finish before Fast20 is programmed; a Fast20 evidence file
cannot authorize this phase.

```bash
set -euo pipefail
PYTHON=./.venv/bin/python
RUNNER=scripts/run_5g8_selected_state_qualification.py
BOARD=stm32c011-4c0055000950313950363920
SERIAL=104000b29905000e17000800065934759d
CAMPAIGN=5p8-debug-r1
RUN_DATE=REPLACE_YYYYMMDD
URI=REPLACE_CURRENT_USB_URI
FIXTURE=REPLACE_INSTALLED_AFTER_FIXTURE_V2_JSON
PROFILE=REPLACE_FAST20_CONTROL_PROFILE_JSON
OPENOCD=REPLACE_OPENOCD_CONFIG
STATE_ROOT=/home/pi/.local/state/smateway
INPUT_STATE="$STATE_ROOT/boards/$BOARD/$CAMPAIGN/t8-inputs"
SETUP_DIR="$INPUT_STATE/q-setup-drafts"
STATIC_RUN="5p8-debug-r1-q-static-r01-${RUN_DATE}"
TIMING_RUN="5p8-debug-r1-q-timing-r01-${RUN_DATE}"
MATRIX_RUN="5p8-debug-r1-q-matrix-r01-${RUN_DATE}"
STATIC_SETUP="$SETUP_DIR/${STATIC_RUN}.json"
TIMING_SETUP="$SETUP_DIR/${TIMING_RUN}.json"
MATRIX_SETUP="$SETUP_DIR/${MATRIX_RUN}.json"
BENCH_SELECTOR=REPLACE_SEALED_BENCH_FLASH_EVIDENCE
BENCH_SELECTOR_SHA=REPLACE_EXACT_BENCH_SHA256
BENCH_SELECTOR_RUN=REPLACE_BENCH_FLASH_RUN_ID

"$PYTHON" scripts/prepare_5g8_selected_state_inputs.py device-identity \
  --serial "$SERIAL" --uri "$URI" --output "$INPUT_STATE/${STATIC_RUN}.device-identity.json"
"$PYTHON" "$RUNNER" static-bench prepare \
  --run-id "$STATIC_RUN" \
  --campaign-id "$CAMPAIGN" --board-id "$BOARD" --serial "$SERIAL" --uri "$URI" \
  --fixture-manifest "$FIXTURE" --setup-attestation "$STATIC_SETUP" \
  --selector-evidence "$BENCH_SELECTOR" --selector-evidence-sha256 "$BENCH_SELECTOR_SHA" \
  --selector-run-id "$BENCH_SELECTOR_RUN" \
  --device-identity "$INPUT_STATE/${STATIC_RUN}.device-identity.json" \
  --profile "$PROFILE" --openocd-config "$OPENOCD" \
  --bench-manifest REPLACE_SEALED_BENCH_BUILD_MANIFEST --state-root "$STATE_ROOT"
STATIC_PLAN="$STATE_ROOT/boards/$BOARD/5g8-selected-state/$CAMPAIGN/static-bench/$STATIC_RUN/plan.json"
"$PYTHON" "$RUNNER" static-bench capture --plan "$STATIC_PLAN"
"$PYTHON" "$RUNNER" static-bench analyze --plan "$STATIC_PLAN"
```

### STOP — exact image transition and new attestation

Do not prepare or capture timing/matrix yet. Verify the static result exists, command an exact Pluto
TX/RX mute, verify the board is electrically `ALL_OFF`, and stop every selector/OpenOCD process.
Program Fast20 through the two-phase flash-and-attest workflow below. Fill each emitted operator
attestation from a new observation; never copy the bench evidence or edit a sealed result. The manual
power cycle is mandatory between the two commands.

```bash
set -euo pipefail
PYTHON=./.venv/bin/python
FLASHER=scripts/flash_and_attest_selector.py
BOARD=stm32c011-4c0055000950313950363920
SERIAL=104000b29905000e17000800065934759d
CAMPAIGN=5p8-debug-r1
URI=REPLACE_CURRENT_USB_URI
PROFILE=profiles/fast20-v1/control_profile.json
OPENOCD=openocd/rpi4-swd.cfg
INPUT_STATE=/home/pi/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/5p8-debug-r1/t8-inputs
FLASH_RUN="5p8-debug-r1-q-fast20-flash-r01-${RUN_DATE}"
FAST20_ELF=REPLACE_EXACT_FAST20_ELF
FAST20_BIN=REPLACE_EXACT_FAST20_BIN
FLASH_ROOT="$INPUT_STATE/selector-flash"
PRE_ATTEST="$INPUT_STATE/${FLASH_RUN}.pre-program-attestation.json"
PHASE1_MUTE="$INPUT_STATE/${FLASH_RUN}.phase1-pluto-mute.json"
PHASE2_MUTE="$INPUT_STATE/${FLASH_RUN}.phase2-pluto-mute.json"
POWER_DRAFT="$FLASH_ROOT/$FLASH_RUN/power-cycle-attestation.template.json"
POWER_ATTEST="$FLASH_ROOT/$FLASH_RUN/power-cycle-attestation.json"
install -d -m 700 "$INPUT_STATE"

"$PYTHON" "$FLASHER" --campaign-id "$CAMPAIGN" --run-id "$FLASH_RUN" \
  --board-id "$BOARD" --image-role fast20 \
  --write-pre-program-attestation-template "$PRE_ATTEST"
# STOP: complete PRE_ATTEST from the current muted/ALL_OFF setup.
/home/pi/pluto-plus-utils/.venv/bin/python scripts/attest_selector_flash_pluto_mute.py \
  --checkpoint phase1_pre_openocd --serial "$SERIAL" --uri "$URI" \
  --output "$PHASE1_MUTE"
"$PYTHON" "$FLASHER" --campaign-id "$CAMPAIGN" --run-id "$FLASH_RUN" \
  --board-id "$BOARD" --image-role fast20 --elf "$FAST20_ELF" --bin "$FAST20_BIN" \
  --profile "$PROFILE" \
  --openocd-config "$OPENOCD" --evidence-root "$FLASH_ROOT" \
  --pre-program-attestation "$PRE_ATTEST" \
  --pluto-serial "$SERIAL" --pluto-uri "$URI" --pluto-mute-evidence "$PHASE1_MUTE" \
  --prepare-and-program

# STOP: with OpenOCD stopped, power-cycle J12 off for >=5 measured seconds, restore power,
# remeasure supply/current/J11.1/heat, and complete the newly emitted editable POWER_DRAFT.
"$PYTHON" "$FLASHER" --campaign-id "$CAMPAIGN" --run-id "$FLASH_RUN" \
  --board-id "$BOARD" --image-role fast20 --evidence-root "$FLASH_ROOT" \
  --power-cycle-draft "$POWER_DRAFT" --seal-power-cycle-attestation
# Create a distinct, fresh exact-mute artifact only now, immediately before phase 2.
/home/pi/pluto-plus-utils/.venv/bin/python scripts/attest_selector_flash_pluto_mute.py \
  --checkpoint phase2_pre_openocd --serial "$SERIAL" --uri "$URI" \
  --output "$PHASE2_MUTE"
"$PYTHON" "$FLASHER" --campaign-id "$CAMPAIGN" --run-id "$FLASH_RUN" \
  --board-id "$BOARD" --image-role fast20 --elf "$FAST20_ELF" --bin "$FAST20_BIN" \
  --profile "$PROFILE" \
  --openocd-config "$OPENOCD" --evidence-root "$FLASH_ROOT" \
  --power-cycle-attestation "$POWER_ATTEST" \
  --pluto-serial "$SERIAL" --pluto-uri "$URI" --pluto-mute-evidence "$PHASE2_MUTE" \
  --verify-after-power-cycle
```

Record the exact sealed Fast20 evidence path and SHA-256 printed by the final command. Re-observe the
installed fixture after the image transition, then generate and complete new timing and matrix setup
drafts naming that same sealed Fast20 tuple. This is a new authority epoch; neither the static device
identity nor its setup attestation may be reused.

### Q2 — sealed Fast20 image: timing, then matrix

```bash
FAST20_SELECTOR=REPLACE_NEW_SEALED_FAST20_FLASH_EVIDENCE
FAST20_SELECTOR_SHA=REPLACE_NEW_EXACT_FAST20_SHA256
FAST20_SELECTOR_RUN="$FLASH_RUN"

"$PYTHON" scripts/generate_5g8_fixture_manifest.py \
  --setup-from-fixture "$FIXTURE" --stage full_conducted_fixture \
  --board-id "$BOARD" --serial "$SERIAL" --setup-run-id "$TIMING_RUN" \
  --setup-draft-output "$SETUP_DIR/${TIMING_RUN}.json"
"$PYTHON" scripts/generate_5g8_fixture_manifest.py \
  --setup-from-fixture "$FIXTURE" --stage full_conducted_fixture \
  --board-id "$BOARD" --serial "$SERIAL" --setup-run-id "$MATRIX_RUN" \
  --setup-draft-output "$SETUP_DIR/${MATRIX_RUN}.json"
# STOP: complete both drafts from new observations and make them read-only.

# Produce a new <=5-minute-old identity after Fast20 attestation and before this prepare.
"$PYTHON" scripts/prepare_5g8_selected_state_inputs.py device-identity \
  --serial "$SERIAL" --uri "$URI" --output "$INPUT_STATE/${TIMING_RUN}.device-identity.json"
"$PYTHON" "$RUNNER" fast20-timing prepare \
  --run-id "$TIMING_RUN" \
  --campaign-id "$CAMPAIGN" --board-id "$BOARD" --serial "$SERIAL" --uri "$URI" \
  --fixture-manifest "$FIXTURE" --setup-attestation "$TIMING_SETUP" \
  --selector-evidence "$FAST20_SELECTOR" --selector-evidence-sha256 "$FAST20_SELECTOR_SHA" \
  --selector-run-id "$FAST20_SELECTOR_RUN" \
  --device-identity "$INPUT_STATE/${TIMING_RUN}.device-identity.json" \
  --profile "$PROFILE" --openocd-config "$OPENOCD" --state-root "$STATE_ROOT"
TIMING_PLAN="$STATE_ROOT/boards/$BOARD/5g8-selected-state/$CAMPAIGN/fast20-timing/$TIMING_RUN/plan.json"
"$PYTHON" "$RUNNER" fast20-timing capture --plan "$TIMING_PLAN"
"$PYTHON" "$RUNNER" fast20-timing analyze --plan "$TIMING_PLAN"

# Produce another fresh device identity before matrix prepare.
"$PYTHON" scripts/prepare_5g8_selected_state_inputs.py device-identity \
  --serial "$SERIAL" --uri "$URI" --output "$INPUT_STATE/${MATRIX_RUN}.device-identity.json"
"$PYTHON" "$RUNNER" fast20-matrix prepare \
  --run-id "$MATRIX_RUN" \
  --campaign-id "$CAMPAIGN" --board-id "$BOARD" --serial "$SERIAL" --uri "$URI" \
  --fixture-manifest "$FIXTURE" --setup-attestation "$MATRIX_SETUP" \
  --selector-evidence "$FAST20_SELECTOR" --selector-evidence-sha256 "$FAST20_SELECTOR_SHA" \
  --selector-run-id "$FAST20_SELECTOR_RUN" \
  --device-identity "$INPUT_STATE/${MATRIX_RUN}.device-identity.json" \
  --profile "$PROFILE" --openocd-config "$OPENOCD" --state-root "$STATE_ROOT" \
  --intervention-contract "$INPUT_STATE/intervention-evidence.json" \
  --static-result "${STATIC_PLAN%/plan.json}/qualification-result.json" \
  --timing-result "${TIMING_PLAN%/plan.json}/qualification-result.json"
MATRIX_PLAN="$STATE_ROOT/boards/$BOARD/5g8-selected-state/$CAMPAIGN/fast20-matrix/$MATRIX_RUN/plan.json"
"$PYTHON" "$RUNNER" fast20-matrix capture --plan "$MATRIX_PLAN"
"$PYTHON" "$RUNNER" fast20-matrix analyze --plan "$MATRIX_PLAN"
```

Use a new device identity file for each prepare command. Never edit a plan, seal, capture set,
tombstone, or result in place; a failed or interrupted run ID is burned.
