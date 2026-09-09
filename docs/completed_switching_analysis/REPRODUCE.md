# Reproducing the completed offline analysis

Run from the repository root with the existing `.venv` and retained bulk-storage
captures available. Every command below analyzes files only: none opens a radio
for control, transmits, changes a selector state, or flashes firmware.

The original acquisition records, fixed-policy phase analyses, and completed
A-rate rolling report remain inputs. The frozen recipe files must retain their
recorded hashes; the renderer checks this. Replaying the B report can take
approximately 15–20 minutes on this host. Host timing results vary with load.

```bash
export PYTHONPATH=src OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
campaign_data=/srv/bulk/samteway/lab-data/tracking-comprehensive-20260908-v1
report_data=docs/completed_switching_analysis/data

.venv/bin/python scripts/analyze_reference_timing.py \
  --block "$campaign_data/block-20260908T231415311061Z.json" --rolling

.venv/bin/python scripts/analyze_comprehensive_bearings.py \
  --block "$campaign_data/block-20260908T231415311061Z.json"

.venv/bin/python scripts/audit_switching_evidence.py \
  --campaign-root "$campaign_data" --output "$report_data/acquisition-audit.json"

.venv/bin/python scripts/analyze_muted_switching.py \
  --campaign-root "$campaign_data" \
  --output "$campaign_data/offline-analysis-20260909/source-muted-spectrum.json"

.venv/bin/python scripts/analyze_affine_transfer.py \
  --timing-report "$campaign_data/block-20260908T225855806955Z-reference-timing.json" \
  --dwell-us 25 --round 1 \
  --output "$campaign_data/offline-analysis-20260909/intercept-25us-r1-finalized.json"

.venv/bin/python scripts/profile_timing_replay.py \
  --campaign-root "$campaign_data" --output "$report_data/runtime-profile.json"

.venv/bin/python scripts/render_completed_switching_analysis.py \
  --campaign-root "$campaign_data"

.venv/bin/pytest -q tests/test_campaign_analysis.py tests/test_completed_switching_report.py
```

The two pre-existing harmonic-consensus experiments are retained as exploratory
inputs with their original hashes and failed-window accounting. They are not
dependencies of the frozen timing algorithm or selected as replacements for it.

The earlier DC-intercept artifact referenced a still-running parent timing
report. The new explicit `--output` path preserves that original artifact and
binds the recomputation to the finalized parent. Never substitute a current
parent hash into an old artifact without recomputing its result.

The renderer can regenerate figures and machine-readable conclusions. The
human-written README must also be reviewed if inputs or decisions change;
it is not silently regenerated. Artifact regression tests intentionally check
the documented capture counts and accepted conditions for this specific dataset.
