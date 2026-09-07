#!/usr/bin/env bash
set -Eeuo pipefail

resume=false
if [[ ${1:-} == "--resume" ]]; then
  resume=true
  shift
fi
if [[ $# -ne 1 ]]; then
  echo "usage: $0 [--resume] CAMPAIGN_DIRECTORY" >&2
  exit 2
fi

campaign=$1
expected_prefix=/home/mouse9911/.local/state/smateway/lab-runs/network-192.168.1.15/repeatability-10mhz/
if [[ "$campaign" != "$expected_prefix"* || "$campaign" == "$expected_prefix" ]]; then
  echo "campaign directory is outside the pinned repeatability root" >&2
  exit 2
fi
mkdir -p "$campaign/runs" "$campaign/logs"

if [[ "$resume" == true ]]; then
  completed_shards=$(CAMPAIGN_PATH="$campaign" PYTHONPATH=src .venv/bin/python - <<'PY'
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

campaign = Path(os.environ["CAMPAIGN_PATH"])
plan_path = campaign / "plan.json"
manifest_path = campaign / "shards.ndjson"
if not plan_path.is_file() or not manifest_path.is_file():
    raise SystemExit("resume requires the existing plan and shard manifest")
plan = json.loads(plan_path.read_text(encoding="utf-8"))
rows = [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines()]
expected = list(range(1, len(rows) + 1))
if [row.get("global_shard") for row in rows] != expected:
    raise SystemExit("admitted shard manifest is not one contiguous prefix")
runner_sha = hashlib.sha256(Path("scripts/run_pinned_static_screen.py").read_bytes()).hexdigest()
validator_sha = hashlib.sha256(Path("scripts/validate_pinned_static_shard.py").read_bytes()).hexdigest()
if runner_sha != plan.get("capture_runner_sha256"):
    raise SystemExit("capture runner changed since campaign creation")
if validator_sha != plan.get("validator_sha256"):
    raise SystemExit("validator changed since campaign creation")
orchestrator_sha = hashlib.sha256(
    Path("scripts/run_overnight_10mhz_repeatability.sh").read_bytes()
).hexdigest()
event = {
    "schema": 1,
    "resumed_utc": datetime.now(UTC).isoformat(),
    "completed_shards_before_resume": len(rows),
    "runner_sha256": runner_sha,
    "validator_sha256": validator_sha,
    "orchestrator_sha256": orchestrator_sha,
}
with (campaign / "resumptions.ndjson").open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(event, sort_keys=True) + "\n")
    handle.flush()
    os.fsync(handle.fileno())
print(len(rows))
PY
  )
  echo "overnight_campaign_resume=$campaign completed_shards=$completed_shards/152"
else
  completed_shards=0
  CAMPAIGN_PATH="$campaign" PYTHONPATH=src .venv/bin/python - <<'PY'
import hashlib
import json
import os
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path

campaign = Path(os.environ["CAMPAIGN_PATH"])
runner = Path("scripts/run_pinned_static_screen.py")
validator = Path("scripts/validate_pinned_static_shard.py")
plan = {
    "schema": 1,
    "kind": "pinned-external-10mhz-repeatability",
    "campaign_id": campaign.name,
    "created_utc": datetime.now(UTC).isoformat(),
    "receiver_uri": "ip:192.168.1.15",
    "receiver_serial": "104000b29905000e17000800065934759d",
    "source_uri": "ip:192.168.1.173",
    "source_serial": "104473b80a16000de6ff2000f8a6beca79",
    "selector_board_id": "stm32c011-4c0055000950313950363920",
    "stlink_serial": "002D003A3335511035383531",
    "fixture_change": "none; user confirmed unchanged and ready",
    "sweep_directions": ["ascending", "descending", "ascending", "descending"],
    "first_hz": 2_100_000_000,
    "last_hz": 5_800_000_000,
    "frequency_step_hz": 10_000_000,
    "frequencies_per_sweep": 371,
    "states": ["ALL_OFF", *(f"ANT{index}" for index in range(1, 9))],
    "sweep_count": 4,
    "expected_capture_count": 13_356,
    "shards_per_sweep": 38,
    "expected_shard_count": 152,
    "shard_frequency_count": 10,
    "sample_rate_hz": 2_000_000,
    "bandwidth_hz": 1_600_000,
    "sample_count": 262_144,
    "tone_offset_hz": 100_000,
    "rx_gain_db": 60,
    "tx_gain_db": -40.0,
    "dds_scale": 0.25,
    "capture_git_head": subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip(),
    "capture_runner_sha256": hashlib.sha256(runner.read_bytes()).hexdigest(),
    "validator_sha256": hashlib.sha256(validator.read_bytes()).hexdigest(),
    "orchestrator_sha256": hashlib.sha256(
        Path("scripts/run_overnight_10mhz_repeatability.sh").read_bytes()
    ).hexdigest(),
    "host": {"node": platform.node(), "platform": platform.platform()},
}
path = campaign / "plan.json"
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(plan, handle, indent=2, sort_keys=True)
    handle.write("\n")
manifest = campaign / "shards.ndjson"
fd = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.close(fd)
print(f"overnight_campaign={campaign}", flush=True)
print(f"runner_sha256={plan['capture_runner_sha256']}", flush=True)
print(f"validator_sha256={plan['validator_sha256']}", flush=True)
print(f"orchestrator_sha256={plan['orchestrator_sha256']}", flush=True)
PY
fi

cleanup() {
  cleanup_status=$?
  trap - EXIT INT TERM
  set +e
  CAMPAIGN_PATH="$campaign" CLEANUP_STATUS="$cleanup_status" PYTHONPATH=src \
    .venv/bin/python - <<'PY'
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import adi
from pluto_plus.hardware.iio import _mute_transmit

from scripts.run_pinned_static_screen import (
    RADIO_SERIAL,
    RADIO_URI,
    SOURCE_SERIAL,
    SOURCE_URI,
    _radio_facts,
    _readback_mute,
)
from smateway.bench import BenchManifest, OpenOcdBench
from smateway.profile import load_profile

result = {
    "checked_utc": datetime.now(UTC).isoformat(),
    "orchestrator_exit_status": int(os.environ["CLEANUP_STATUS"]),
    "passed": False,
}
try:
    receiver = adi.ad9361(uri=RADIO_URI)
    source = adi.ad9361(uri=SOURCE_URI)
    try:
        result["receiver_identity"] = _radio_facts(
            receiver, expected_uri=RADIO_URI, expected_serial=RADIO_SERIAL
        )
        result["source_identity"] = _radio_facts(
            source, expected_uri=SOURCE_URI, expected_serial=SOURCE_SERIAL
        )
        _mute_transmit(receiver)
        result["receiver_mute"] = _readback_mute(receiver)
        _mute_transmit(source)
        result["source_mute"] = _readback_mute(source)
    finally:
        receiver.rx_destroy_buffer()
        source.rx_destroy_buffer()
    profile = load_profile(Path("profiles/fast20-v1/control_profile.json"))
    manifest = BenchManifest.load(
        Path("build/STM32C011F4P6/bench/pluto_bench.manifest.json")
    )
    controller = OpenOcdBench(manifest, Path("openocd/stlink-v3-stm32c011.cfg"))
    selector = controller.request(profile.all_off_code, 0).as_dict()
    result["selector"] = selector
    result["passed"] = (
        selector["applied_code"] == profile.all_off_code
        and selector["remaining_lease_ms"] == 0
        and selector["lease_active"] is False
        and selector["guard_active"] is False
        and selector["invalid_command"] is False
    )
except BaseException as error:
    result["cleanup_error"] = {"type": type(error).__name__, "message": str(error)}
path = Path(os.environ["CAMPAIGN_PATH"]) / "final-safety.json"
path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print("overnight_final_safety=" + json.dumps(result, sort_keys=True), flush=True)
if not result["passed"]:
    raise SystemExit(1)
PY
  cleanup_result=$?
  if ((cleanup_status != 0)); then
    exit "$cleanup_status"
  fi
  exit "$cleanup_result"
}
trap cleanup EXIT INT TERM

states=(ALL_OFF ANT1 ANT2 ANT3 ANT4 ANT5 ANT6 ANT7 ANT8)
directions=(ascending descending ascending descending)
global_shard=0

for sweep_index in 1 2 3 4; do
  direction=${directions[$((sweep_index - 1))]}
  echo "overnight_sweep_start=$sweep_index/4 direction=$direction utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  for shard_index in $(seq 1 38); do
    global_shard=$((global_shard + 1))
    if ((global_shard <= completed_shards)); then
      continue
    fi
    offset=$(((shard_index - 1) * 10))
    remaining=$((371 - offset))
    count=10
    if ((remaining < count)); then count=$remaining; fi
    frequencies=()
    for position in $(seq 0 $((count - 1))); do
      lattice_index=$((offset + position))
      if [[ "$direction" == ascending ]]; then
        frequency=$((2100000000 + lattice_index * 10000000))
      else
        frequency=$((5800000000 - lattice_index * 10000000))
      fi
      frequencies+=("$frequency")
    done
    first_frequency=${frequencies[0]}
    last_frequency=${frequencies[$((count - 1))]}
    if ((first_frequency < last_frequency)); then
      minimum_frequency=$first_frequency
      maximum_frequency=$last_frequency
    else
      minimum_frequency=$last_frequency
      maximum_frequency=$first_frequency
    fi
    attempt=1
    while :; do
      log_path="$campaign/logs/sweep-$(printf '%02d' "$sweep_index")-shard-$(printf '%02d' "$shard_index")-attempt-$(printf '%02d' "$attempt").log"
      if [[ ! -e "$log_path" ]]; then break; fi
      attempt=$((attempt + 1))
    done
    args=(external --repeats 1 --tx-gain-db -40 --output-root "$campaign/runs")
    for frequency in "${frequencies[@]}"; do
      args+=(--frequency-hz "$frequency")
    done
    for state in "${states[@]}"; do
      args+=(--state "$state")
    done
    echo "overnight_shard_start=$global_shard/152 sweep=$sweep_index direction=$direction shard=$shard_index/38 attempt=$attempt frequencies=$first_frequency..$last_frequency captures=$((count * 9)) utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    set +e
    PYTHONPATH=src .venv/bin/python scripts/run_pinned_static_screen.py "${args[@]}" \
      >"$log_path" 2>&1
    runner_status=$?
    set -e
    run_dir=$(awk -F= '/^run_dir=/{value=$2} END{print value}' "$log_path")
    if ((runner_status != 0)) || [[ -z "$run_dir" ]]; then
      echo "overnight_shard_failed=$global_shard sweep=$sweep_index shard=$shard_index attempt=$attempt runner_status=$runner_status run_dir=$run_dir log=$log_path"
      exit 1
    fi
    validation=$(
      PYTHONPATH=src .venv/bin/python scripts/validate_pinned_static_shard.py \
        "$run_dir/run.json" \
        --first-hz "$minimum_frequency" \
        --last-hz "$maximum_frequency" \
        --step-hz 10000000 \
        --direction "$direction"
    )
    CAMPAIGN_PATH="$campaign" VALIDATION_JSON="$validation" SWEEP_INDEX="$sweep_index" \
      DIRECTION="$direction" SHARD_INDEX="$shard_index" GLOBAL_SHARD="$global_shard" \
      ATTEMPT="$attempt" \
      PYTHONPATH=src .venv/bin/python - <<'PY'
import json
import os
from datetime import UTC, datetime
from pathlib import Path

row = {
    "global_shard": int(os.environ["GLOBAL_SHARD"]),
    "sweep": int(os.environ["SWEEP_INDEX"]),
    "direction": os.environ["DIRECTION"],
    "shard": int(os.environ["SHARD_INDEX"]),
    "attempt": int(os.environ["ATTEMPT"]),
    "validated_utc": datetime.now(UTC).isoformat(),
    "validation": json.loads(os.environ["VALIDATION_JSON"]),
}
path = Path(os.environ["CAMPAIGN_PATH"]) / "shards.ndjson"
with path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(row, sort_keys=True) + "\n")
    handle.flush()
    os.fsync(handle.fileno())
PY
    total_frequencies=$(((sweep_index - 1) * 371 + offset + count))
    total_captures=$((total_frequencies * 9))
    echo "overnight_shard_validated=$global_shard/152 sweep=$sweep_index direction=$direction shard=$shard_index/38 total_frequencies=$total_frequencies/1484 total_captures=$total_captures/13356 utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  done
  echo "overnight_sweep_complete=$sweep_index/4 direction=$direction utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
done

echo "overnight_campaign_complete=4_sweeps shards=152 frequencies=1484 captures=13356 utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
