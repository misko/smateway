# Data dictionary

## Run manifest

`manifest.json` is the acquisition authority.

| Field | Meaning |
|---|---|
| `configuration.center_frequencies_hz` | Reviewed centre-frequency set, hertz |
| `configuration.condition_order` | Adjacent TX pair order within a round |
| `configuration.round_order_policy` | Deterministic reversal/rotation policy |
| `plan` | Complete planned condition list persisted before capture |
| `attempts` | Immutable execution records and artifact identities |
| `summary` | Planned, completed, passed, rejected, and failed counts |
| `status` | `complete` only after all conditions and final mute pass |

## Per-artifact phase analysis

Each `fast20-relative-phase.json` verifies one immutable dual-RX artifact.

| Field | Meaning |
|---|---|
| `artifact.sha256` | Persisted IQ artifact digest |
| `capture.stream_id` | One fresh metadata generation identity |
| `capture.first/last_*sequence` | Buffer and FPGA sample-counter bounds |
| `pilot.estimated_offset_hz` | Coherently refined baseband pilot frequency |
| `quality_gate.passed` | Capture-wide and all-state phase-quality result |
| `phase.states[].complex_delta` | Leakage-cancelled robust state phasor |
| `phase.states[].phase_deg` | Raw within-capture phase, degrees |
| `phase.states[].phase_relative_to_ant1_deg` | Wrapped ANT1 reference, degrees |
| `phase.states[].approximate_phase_standard_error_deg` | Analyzer noise estimate, degrees |

## Aggregate/direct analysis

`analysis-multifrequency-direct-nominal-2m.json` combines adjacent TX pairs.

| Field | Meaning |
|---|---|
| `experiment.continuity` | Cross-artifact ABI, block, sample, and gap totals |
| `localization.frequency_profile_rows` | Exactly one circular aggregate per frequency |
| `circular_mean_double_relative_phase_deg` | TX2−TX1 phase, then ANT1 referenced |
| `circular_repeat_standard_deviation_deg` | Across-round circular scatter |
| `aggregate_analyzer_standard_error_deg` | Propagated artifact analyzer error |
| `combined_phase_standard_deviation_deg` | Repeat/analyzer error plus systematic floor |
| `map_residual_diagnostics` | Per-frequency direct-model diagnostic |
| `posterior.map_residuals` | Joint direct-model residual matrix and RMS |
| `posterior.effective_sample_size` | Importance-sampling reliability diagnostic |

## Anchored analysis

`analysis-anchored-slope-sys10-exclude2458-2m.json` is the declared primary result.

| Field | Meaning |
|---|---|
| `analysis_configuration.tx1_anchor_position_mm` | Fixed TX1 board-centred coordinate |
| `excluded_center_frequencies_hz` | Transparent post-hoc/sensitivity exclusions |
| `used_center_frequencies_hz` | Profiles entering this likelihood |
| `localization.posterior.map` | Highest-density TX2 point, direction, and radius |
| `localization.posterior.tx2` | Means and radial credible intervals |
| `map_residuals.nuisance_intercept_deg` | Marginalized fixed phase for ANT2–ANT8 |
| `map_residuals.frequency_weighted_rms_deg` | Fit diagnostic for each retained frequency |
| `map_residuals.antenna_weighted_rms_deg` | Fit diagnostic for each non-reference antenna |
| `output_particles` | Bounded weighted subset for reproducible visualization |

All phase quantities are wrapped degrees unless explicitly labeled otherwise. Positions and
distances are millimetres. Centre and carrier frequencies are hertz.
