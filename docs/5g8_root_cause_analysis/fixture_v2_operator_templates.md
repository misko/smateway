# Fixture-v2 operator templates

These templates are draft inputs for `scripts/generate_5g8_fixture_manifest.py`; they are not
capture evidence until every `REPLACE_...` value has been replaced, the graph has been physically
checked, and the generator has written a new immutable manifest.

| Step | Runner stage | Fixture draft | Per-run setup attestation | Selector flash binding |
|---|---|---|---|---|
| A | `direct_rx2_termination` | `fixture_manifest_v2.stage-a.template.json` | Generated from the immutable fixture | Must be `null` |
| B | `rx2_cable_terminated` | `fixture_manifest_v2.stage-b.template.json` | Generated from the immutable fixture | Must be `null` |
| C | `powered_selector_all_inputs_terminated` | `fixture_manifest_v2.stage-c.template.json` | Generated from the immutable fixture | Exact sealed bench-image path, SHA-256, and flash run ID |
| E | `full_conducted_fixture` | `fixture_manifest_v2.stage-e.template.json` | Generated from the immutable fixture | Exact sealed bench-image path, SHA-256, and flash run ID |

Use one campaign ID, comparable-fixture-group ID, board ID, Pluto serial, and `shared_fixture`
identity through the entire A → B → C → E chain. The repeated placeholder names identify assets
that must remain physically identical between stages. In particular:

- A → B preserves the TX1 stimulus load and the RX2 termination.
- B → C preserves the TX1 stimulus branch and the RX2 cable identity up to its moved far endpoint.
- C → E preserves the selector, its bench-power identities, and the complete RX2-to-selector-common
  connection.
- A → B → C → E preserves the explicit RX2 attenuator state. An absent attenuator is represented
  by `state: "absent"` with null asset/orientation/connection fields. A present attenuator is a
  fully identified, rated, oriented component and Pluto-side graph edge; it is never inferred from
  a cable-loss value.

All supplied assets are explicitly `uncharacterized`. That permits screening captures but not a
causal-attribution claim. Change an asset to `characterized` only when its evidence file, file
SHA-256, S-parameter SHA-256, and 5.8-GHz return loss are all available.

The numeric rating, attenuation, impedance, selector-voltage, and selector-current-limit fields
are also placeholders. Replace each one only with a value transcribed from the exact physical
asset's label, its manufacturer documentation, a bench-supply setting/readback, or its linked
characterization evidence. The templates intentionally contain no example numeric defaults: an
unchanged example could otherwise be mistaken for an operator observation. The generator rejects
every unresolved `REPLACE_...` token recursively, including tokens nested inside component,
connection, and characterization objects.

## Optional RX2 attenuator

Every checked-in fixture draft deliberately starts with
`REPLACE_RX2_ATTENUATOR_STATE_PRESENT_OR_ABSENT`; neither presence nor absence is a template
default. Inspect the live RX2 chain and choose exactly one representation.

If no attenuator is installed, replace only the state with `absent` and retain the three JSON
nulls:

```json
{
  "state": "absent",
  "asset": null,
  "orientation": null,
  "pluto_connection": null
}
```

If an attenuator is installed, replace the entire object with the structure below. Every
`REPLACE_...` value remains mandatory, and `fixture_side_port_role` / `pluto_side_port_role` must
assign the two roles `input` and `output` exactly once:

```json
{
  "state": "present",
  "asset": {
    "id": "REPLACE_RX2_ATTENUATOR_ID",
    "rated_min_frequency_hz": "REPLACE_OPERATOR_OBSERVED_RATED_MIN_FREQUENCY_HZ",
    "rated_max_frequency_hz": "REPLACE_OPERATOR_OBSERVED_RATED_MAX_FREQUENCY_HZ",
    "maximum_input_power_dbm": "REPLACE_OPERATOR_OBSERVED_MAXIMUM_INPUT_POWER_DBM",
    "port_map": {"input": "REPLACE_PHYSICAL_INPUT_PORT_ID", "output": "REPLACE_PHYSICAL_OUTPUT_PORT_ID"},
    "characterization": {
      "status": "uncharacterized",
      "evidence_path": null,
      "evidence_sha256": null,
      "s_parameter_sha256": null,
      "return_loss_db_at_5g8": null
    },
    "attenuation_db": "REPLACE_OPERATOR_OBSERVED_RX2_ATTENUATION_DB"
  },
  "orientation": {
    "fixture_side_port_role": "REPLACE_INPUT_OR_OUTPUT",
    "pluto_side_port_role": "REPLACE_OPPOSITE_INPUT_OR_OUTPUT"
  },
  "pluto_connection": {
    "id": "REPLACE_PLUTO_RX2_TO_ATTENUATOR_CONNECTION_ID",
    "from": {"component_id": "REPLACE_PLUTO_FIXTURE_ID", "port_id": "RX2"},
    "to": {"component_id": "REPLACE_RX2_ATTENUATOR_ID", "port_id": "REPLACE_PLUTO_SIDE_PHYSICAL_PORT_ID"},
    "interconnect": {
      "id": "REPLACE_PLUTO_RX2_TO_ATTENUATOR_INTERCONNECT_ID",
      "kind": "direct_adapter",
      "rated_min_frequency_hz": "REPLACE_OPERATOR_OBSERVED_RATED_MIN_FREQUENCY_HZ",
      "rated_max_frequency_hz": "REPLACE_OPERATOR_OBSERVED_RATED_MAX_FREQUENCY_HZ",
      "maximum_input_power_dbm": "REPLACE_OPERATOR_OBSERVED_MAXIMUM_INPUT_POWER_DBM",
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

For the present case, also change the source endpoint of the stage-specific RX2 edge from Pluto
`RX2` to the attenuator's fixture-side physical port. The generator validates both endpoints and
includes the attenuator component, its Pluto connection, orientation, and stage edge in every
inventory and shared-fixture hash. Stage A then terminates the frozen receiver chain at the
attenuator's fixture-facing plane; it must not be described as a bare-Pluto measurement.

## Draft and manifest sequence

1. Copy the relevant fixture template into a run-specific directory on local Raspberry Pi storage.
   Never edit the repository template in place. The checked-in setup templates are structural
   references only; do not copy values or inventories from them into a run.
2. Replace every placeholder. Component, interconnect, connection, reference-plane, supply, and
   port IDs must describe the labels physically observed by the operator. Numeric values must be
   observed or documented values for those same exact IDs; do not substitute a generic example.
3. Leave `prior_stage_binding` as JSON `null` in **every** draft. For B, C, and E, supply the exact
   immediately prior immutable plan with `--prior-plan`; the generator derives and verifies the
   binding. A must not receive `--prior-plan`.
4. Validate first, then create a new immutable output. The generator will not overwrite an existing
   manifest.

Example validation shape:

```bash
./.venv/bin/python scripts/generate_5g8_fixture_manifest.py \
  --draft /absolute/local/path/stage-c.draft.json \
  --stage powered_selector_all_inputs_terminated \
  --board-id EXACT_BOARD_ID \
  --serial EXACT_PLUTO_SERIAL \
  --prior-plan /absolute/local/path/stage-b/plan.json \
  --validate-only
```

Repeat with `--output /absolute/local/path/stage-c.fixture.json` instead of `--validate-only` only
after the physical graph is reviewed.

## Per-run setup attestation

Create a fresh, run-bound setup draft from the exact generated fixture manifest. Do not manually
calculate or copy the fixture hash, shared-fixture hash, stage-delta hash, or either sorted identity
inventory. The generator reopens the fixture without symlinks, requires local Raspberry Pi
storage, revalidates its normalized graph and prior-stage chain, requires canonical generator
bytes, and derives those values itself:

```bash
./.venv/bin/python scripts/generate_5g8_fixture_manifest.py \
  --setup-from-fixture /absolute/local/path/stage-c.fixture.json \
  --setup-run-id EXACT_UNIQUE_RUN_ID \
  --setup-draft-output /absolute/local/path/EXACT_UNIQUE_RUN_ID.setup-draft.json \
  --stage powered_selector_all_inputs_terminated \
  --board-id EXACT_BOARD_ID \
  --serial EXACT_PLUTO_SERIAL
```

The setup-draft output is private (`0600`), create-only, and never overwritten. It contains derived,
exact values for:

- `run_id`, campaign, comparable-fixture group, topology stage;
- the actual file SHA-256 of the generated fixture manifest;
- the canonical SHA-256 of its normalized `shared_fixture` and `stage_delta`; and
- the exact sorted component/interconnect and connection inventories derived from the graph.

After physically checking the run setup and creating its photo or diagram, replace only the
remaining observation placeholders:

- one unique setup-attestation ID;
- one timezone-qualified observation timestamp;
- the setup photo/diagram path and its SHA-256; and
- for C/E only, the exact sealed selector-flash evidence path, SHA-256, and flash run ID.

Do not change any generator-derived field. The capture runner independently re-hashes the fixture,
setup attestation, setup photo/diagram, selector evidence, and recomputes the graph inventories
before it can freeze or execute a plan.

For A and B, `selector_flash_evidence` must remain `null`, and selector flash CLI arguments are
forbidden. For C and E, its path, SHA-256, and flash run ID must exactly match the sealed selector
bench-image evidence passed to the capture command. A setup attestation from one run or stage must
never be reused for another.
