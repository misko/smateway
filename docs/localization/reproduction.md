# Reproduction

Run commands from the `smateway` repository with the pinned `pluto-plus-utils` environment.
Raw capture commands enable RF and require the exact reviewed hardware. Analysis and report
commands are offline.

## 1. Acquisition

Keep the board, receive array, transmit antennas, and nearby objects fixed for the entire run.
Power and preflight the selector independently. Verify both transmitters are muted before
starting.

```bash
PYTHONPATH=src /home/pi/pluto-plus-utils/.venv/bin/python \
  scripts/run_fast20_phase_distribution.py --rounds 3 \
  --run-id multifrequency-phase-RUN_ID \
  --center-frequency-hz 2400000000 \
  --center-frequency-hz 2409000000 \
  --center-frequency-hz 2423000000 \
  --center-frequency-hz 2440000000 \
  --center-frequency-hz 2458000000 \
  --center-frequency-hz 2472000000 \
  --center-frequency-hz 2483000000
```

The runner persists the complete condition order before capture, resumes only when the stored
plan matches exactly, validates each immutable artifact, requires an independent post-condition
mute, and finishes with a separate mute/readback.

## 2. Immutable artifact reanalysis

An existing CI16 artifact can be reprocessed without transmitting:

```bash
PYTHONPATH=src /home/pi/pluto-plus-utils/.venv/bin/python \
  scripts/reanalyze_fast20_phase_artifact.py ARTIFACT_ID
```

This verifies the data hash and metadata ledger before recalculating the pilot, selector
alignment, complex state phasors, quality gates, and pairwise phases.

## 3. Aggregate/direct analysis

```bash
PYTHONPATH=src /home/pi/pluto-plus-utils/.venv/bin/python \
  scripts/analyze_dualband_phase_distribution.py \
  --manifest MANIFEST.json \
  --output analysis-multifrequency-direct-nominal-2m.json \
  --sample-count 2000000 \
  --systematic-floor-2g4-deg 25
```

The historical `analysis_kind` contains the word `dualband` for schema compatibility; the
actual frequency list is manifest-derived and this run contains seven 2.4 GHz profiles.

## 4. Anchored frequency-slope analysis

All-frequency diagnostic:

```bash
PYTHONPATH=src /home/pi/pluto-plus-utils/.venv/bin/python \
  scripts/analyze_anchored_frequency_slope.py \
  --analysis analysis-multifrequency-direct-nominal-2m.json \
  --output analysis-anchored-slope-sys10-2m.json \
  --tx1-anchor-x-mm -26.503035 \
  --tx1-anchor-y-mm 315.670945 \
  --sample-count 2000000 \
  --systematic-phase-std-deg 10
```

Primary sensitivity-declared result:

```bash
PYTHONPATH=src /home/pi/pluto-plus-utils/.venv/bin/python \
  scripts/analyze_anchored_frequency_slope.py \
  --analysis analysis-multifrequency-direct-nominal-2m.json \
  --output analysis-anchored-slope-sys10-exclude2458-2m.json \
  --tx1-anchor-x-mm -26.503035 \
  --tx1-anchor-y-mm 315.670945 \
  --sample-count 2000000 \
  --systematic-phase-std-deg 10 \
  --exclude-center-frequency-hz 2458000000
```

For LOFO, repeat the primary command with one additional
`--exclude-center-frequency-hz` for each retained frequency. Each output records the source,
excluded, and used frequency sets.

## 5. Compact snapshot and PNG report

Install the optional report dependencies, then derive a compact source-hashed snapshot and all
figures from the retained run directory:

```bash
uv sync --extra report
uv run --extra report python scripts/render_localization_report.py \
  --run-directory \
  ~/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/phase-distributions/multifrequency-phase-20260825-b \
  --refresh-snapshot
```

Regenerate from the committed compact snapshot without raw captures or full particle files:

```bash
uv run --extra report python scripts/render_localization_report.py
```

Verify that the committed PNGs and figure manifest are byte-for-byte reproducible:

```bash
uv run --extra report python scripts/render_localization_report.py --check
```

The renderer uses fixed styling, dimensions, DPI, and metadata. `data/figures-manifest.json`
records each output SHA-256 plus the full source-document hashes used to create the snapshot.

## 6. Expected validation

Before committing a report update:

```bash
make test
uv run ruff check .
uv run mypy src
git diff --check
```

Do not commit raw CI16/SigMF captures or the complete Monte Carlo particle documents. Preserve
them in the per-board state directory and retain their SHA-256 identities in the snapshot.
