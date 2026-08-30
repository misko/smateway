# P2 input-drive-off operator workflow

P2 is a run-bound, two-load control. It is not the normal fully conducted fixture: the exact
TX1 stimulus branch and the exact 8-way input are disconnected from one another and each receives
its own identified, 5.8-GHz-rated 50-ohm load. The 8-way outputs, selector, selector-common/RX2
chain, protected RX1 reference branch, and terminated TX2 remain physically unchanged from the
accepted P0 evidence.

Use these checked-in structures:

- `p2_input_off_fixture_v2.template.json` — complete port-level fixture draft;
- `p2_input_off_setup_attestation_v1.template.json` — structural reference for the derived
  run-bound setup draft; and
- `scripts/generate_5g8_input_off_setup.py` — the required inventory/hash derivation tool.

Every `REPLACE_...` value is deliberately invalid evidence. The fixture and setup validators scan
recursively and fail closed before planning, radio access, or capture. Numeric ratings are never
examples: transcribe them from the exact asset label, manufacturer data, or linked
characterization evidence.

## 1. Normalize the five accepted P0 observations

For each accepted P0 run, normalize its exact 5.8-GHz Rotation-0 analysis and manifest. Use the
P0 run ID recorded in that manifest and a new create-only output on local Raspberry Pi storage:

```bash
./.venv/bin/python scripts/analyze_5g8_input_off_cohort.py \
  --normalize-p0 \
  --run-id REPLACE_EXACT_P0_RUN_ID \
  --legacy-analysis /absolute/local/path/to/p0/reference-transfer-analysis.json \
  --legacy-manifest /absolute/local/path/to/p0/manifest.json \
  --output /absolute/local/path/to/p0/normalized-p0-observation.json
```

Repeat for the five source-distinct P0 runs. Do not copy one output five times. The P2 planner
requires exactly five distinct run IDs, artifact IDs, stream IDs, and raw hashes, all at the same
Fast20 profile hash and final frozen Smateway source commit.

Each output is a canonical create-only, read-only P0 evidence envelope—not a loose summary. It
binds and later recursively reopens the exact legacy manifest, its embedded `/plan`, the raw IQ,
SigMF metadata, reference-transfer analysis, and the clean normalizer source revision. Do not
edit, rename over, or make the envelope writable. Both P2 planning and cohort comparison re-hash
all of those sources, and every P2 plan must contain the exact same five envelope bindings in the
same caller-supplied order. A former bare normalized-observation JSON is not admissible; regenerate
it with the frozen normalizer.

## 2. Complete one fixture-v2 draft per P2 run

Copy `p2_input_off_fixture_v2.template.json` to a run-specific local directory. Replace every
placeholder, including the unique P2 run ID, exact board/Pluto identities, all component and
interconnect IDs/ratings/port labels, the accepted P0 topology-evidence binding, exact sealed
Fast20 evidence, and exact Fast20 profile file binding.

The RX2 attenuator is never inferred. If none is physically installed, use:

```json
{
  "state": "absent",
  "component": null,
  "pluto_connection": null
}
```

If one is installed, replace the whole `rx2_attenuator` object with a fully rated component and
its physical connection. `orientation` is `input_toward_fixture` when its labelled input faces the
selector/common cable, or `output_toward_fixture` for the reverse orientation:

```json
{
  "state": "present",
  "component": {
    "id": "REPLACE_RX2_ATTENUATOR_ID",
    "kind": "fixed_attenuator",
    "manufacturer": "REPLACE_MANUFACTURER",
    "model": "REPLACE_MODEL",
    "ports": {"input": "REPLACE_PHYSICAL_INPUT_ID", "output": "REPLACE_PHYSICAL_OUTPUT_ID"},
    "rated_min_frequency_hz": "REPLACE_OBSERVED_MIN_HZ",
    "rated_max_frequency_hz": "REPLACE_OBSERVED_MAX_HZ",
    "maximum_input_power_dbm": "REPLACE_OBSERVED_MAX_DBM",
    "characterization": {
      "status": "uncharacterized",
      "evidence_path": null,
      "evidence_sha256": null,
      "s_parameter_sha256": null,
      "return_loss_db_at_5g8": null
    },
    "attenuation_db": "REPLACE_OBSERVED_ATTENUATION_DB",
    "orientation": "REPLACE_input_toward_fixture_OR_output_toward_fixture"
  },
  "pluto_connection": {
    "id": "REPLACE_RX2_ATTENUATOR_TO_PLUTO_CONNECTION_ID",
    "from": {"component_role": "rx2_attenuator", "port_role": "REPLACE_PLUTO_SIDE_INPUT_OR_OUTPUT"},
    "to": {"component_role": "pluto", "port_role": "rx2"},
    "interconnect": {
      "id": "REPLACE_RX2_ATTENUATOR_TO_PLUTO_INTERCONNECT_ID",
      "kind": "direct_adapter",
      "rated_min_frequency_hz": "REPLACE_OBSERVED_MIN_HZ",
      "rated_max_frequency_hz": "REPLACE_OBSERVED_MAX_HZ",
      "maximum_input_power_dbm": "REPLACE_OBSERVED_MAX_DBM",
      "characterization": {
        "status": "uncharacterized",
        "evidence_path": null,
        "evidence_sha256": null,
        "s_parameter_sha256": null,
        "return_loss_db_at_5g8": null
      }
    }
  }
}
```

For the present case, change `connections.selector_common_to_rx2.to` to the attenuator port facing
the fixture. The opposite labelled port must be the source of `pluto_connection`. The validator
checks this orientation and includes the optional component and connection in the fixed-graph
hash and observed inventories. The state must be identical to accepted P0; adding/removing or
reversing the attenuator is not the P0→P2 variable.

## 3. Derive, inspect, and complete the run setup

After the final fixture JSON is complete, derive the exact setup draft. Never hand-copy the
fixture hash or sorted inventories from the checked-in structural setup template:

```bash
./.venv/bin/python scripts/generate_5g8_input_off_setup.py \
  --fixture-manifest /absolute/local/path/to/p2-r01.fixture.json \
  --run-id REPLACE_UNIQUE_P2_RUN_ID \
  --board-id stm32c011-4c0055000950313950363920 \
  --serial 104000b29905000e17000800065934759d \
  --output /absolute/local/path/to/p2-r01.setup-draft.json
```

Open that new `0600` file. Add a unique attestation ID, timezone-qualified timestamp, operator ID,
and exact setup-photo path/hash/size. Physically inspect each derived component and connection ID,
then change every confirmation to JSON `true`. Rename or copy the completed bytes to the final
setup-attestation path without changing any generator-derived value. The capture runner reopens
and verifies all bound files.

## 4. Plan, execute, and aggregate

Use the exact commands in the P2 section of `5p8_ghz_debug_plan.md`. Plan and execute use the same
run ID, fixture, setup, current USB URI, Fast20 profile, and ordered five P0 observations. Repeat
with five unique P2 run IDs and five unique setup photographs/attestations. Do not retry a failed
run ID: its execution tombstone permanently burns it.
