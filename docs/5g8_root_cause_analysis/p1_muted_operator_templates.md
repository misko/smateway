# P1 muted-control operator templates

`p1_muted_fixture_v1.template.json` and
`p1_muted_setup_attestation_v1.template.json` are drafts, not evidence. Copy them to local
Raspberry Pi storage and replace every `REPLACE_...` token before invoking the P1 runner.

Use one completed fixture manifest, with identical bytes and SHA-256, for all five P1 runs. Its
component and connection IDs describe the unchanged physical fixture shared by `r01` through
`r05`. Do not put a P1 run ID or run-specific photograph in that shared file.

Create a separate completed setup attestation for every P1 run. Each copy must have a unique
`attestation_id`, the exact current `run_id`, and a run-specific setup photograph or diagram. It
must bind:

- the SHA-256 of the shared completed P1 fixture manifest;
- the SHA-256 of the sealed Fast20 selector evidence used before P0; and
- exactly five P0 manifest SHA-256 values, ordered identically to the five repeated
  `--p0-manifest` arguments (`r01` through `r05`).

The selector evidence itself is supplied separately with `--selector-evidence`; the setup file
contains its SHA-256, not a hand-written timing claim. P1 independently reopens the five P0 raw
artifacts to prove timing. A setup attestation must never be reused for a different P1 run, while
the shared fixture manifest must not change within the cohort.

Both fixture and setup parsing fail closed on a `REPLACE_...` token at any nesting depth, including
placeholder keys and placeholders embedded inside longer path strings.
