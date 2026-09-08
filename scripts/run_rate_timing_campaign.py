#!/usr/bin/env python3
"""Staged, resumable sample-rate campaign with exact selector restoration."""

from __future__ import annotations

# ruff: noqa: E402, I001
import argparse
import os
import random
import re
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = ROOT / ".venv"
if __name__ == "__main__" and (
    Path(sys.prefix).resolve() != PREFIX.resolve()
    or os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep)[0] != str(PREFIX / "lib")
    or str(ROOT / "src") not in sys.path
):
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = str(PREFIX / "lib")
    env["PYTHONPATH"] = str(ROOT / "src")
    os.execve(
        str(PREFIX / "bin/python"),
        [str(PREFIX / "bin/python"), str(Path(__file__).resolve()), *sys.argv[1:]],
        env,
    )

from capture_fast_tracking_timing import _write_json_atomic
from flash_tracking_c6_firmware import _mute
from run_fast_tracking_timing_campaign import _flash, _lock, _restore
from smateway.rate_timing import (
    CONFIGURATIONS,
    PORTS,
    RECEIVER_SERIAL,
    SOURCE_SERIAL,
    analyze_rate_capture,
    closure,
    complex_value,
    frozen_reference,
    load,
    sha256,
)


def execute(command: list[str], timeout_s: float = 180) -> str:
    process = subprocess.Popen(
        command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    try:
        output, _ = process.communicate(timeout=timeout_s)
    except BaseException:
        process.terminate()
        try:
            process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        raise
    if process.returncode:
        raise RuntimeError(f"capture child failed ({process.returncode}):\n{output}")
    return output


def select_candidate(rows: list[dict]) -> dict | None:
    """Choose on round 1 only. Validation data never changes the selected variant."""
    paths = [str(Path(r["run_json"]).resolve()) for r in rows]
    if len(paths) != len(set(paths)):
        raise ValueError("a capture was reused across independent campaign conditions")
    candidates = []
    baseline_latencies = []
    for row in rows:
        if row["round"] != 1 or row.get("control"):
            continue
        result = load(Path(row["analysis_json"]))
        for v in result["variants"]:
            if not v["metrics"]["passed"]:
                continue
            latency = v["metrics"]["first_phase_pass_ms"]
            item = {
                "configuration": row["configuration"],
                "dwell_us": row["dwell_us"],
                "method": v["method"],
                "leading_discard_us": v["leading_discard_us"],
                "training_latency_ms": latency,
            }
            candidates.append(item)
            if row["configuration"] == "A" and row["dwell_us"] == 200:
                baseline_latencies.append(latency)
    if not candidates or not baseline_latencies:
        return None
    limit = min(baseline_latencies)
    eligible = [c for c in candidates if c["training_latency_ms"] <= limit]
    return min(
        eligible,
        key=lambda c: (
            c["dwell_us"],
            c["training_latency_ms"],
            CONFIGURATIONS[c["configuration"]].sample_rate_hz,
            c["configuration"],
            c["leading_discard_us"],
        ),
    )


def evaluate_candidate(candidate: dict | None, rows: list[dict]) -> dict:
    if candidate is None:
        return {"passed": False, "reason": "no independently qualified training candidate/control"}
    paths = [str(Path(r["run_json"]).resolve()) for r in rows]
    if len(paths) != len(set(paths)):
        return {"passed": False, "reason": "capture reused across validation conditions"}
    validation = []
    for row in rows:
        if (
            row["round"] not in (2, 3)
            or row.get("control")
            or row["configuration"] != candidate["configuration"]
            or row["dwell_us"] != candidate["dwell_us"]
        ):
            continue
        result = load(Path(row["analysis_json"]))
        variant = next(
            (
                v
                for v in result["variants"]
                if v["method"] == candidate["method"]
                and v["leading_discard_us"] == candidate["leading_discard_us"]
            ),
            None,
        )
        validation.append(
            {
                "round": row["round"],
                "passed": bool(variant and variant["metrics"]["passed"]),
                "metrics": variant["metrics"] if variant else None,
                "analysis_error": result.get("error") if variant is None else None,
                "run_json": row["run_json"],
            }
        )
    return {
        "passed": len(validation) == 2
        and {v["round"] for v in validation} == {2, 3}
        and all(v["passed"] for v in validation),
        "candidate": candidate,
        "validation": validation,
    }


class Campaign:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.path = self.root / "campaign.json"
        self.record = (
            load(self.path)
            if self.path.exists()
            else {
                "schema": 1,
                "campaign_id": self.root.name,
                "status": "prepared",
                "created_at": datetime.now(UTC).isoformat(),
                "preflight": [],
                "references": [],
                "screen": [],
                "flashes": [],
                "restores": [],
                "failures": [],
                "selection": None,
                "reference_drift": None,
            }
        )
        self.current_dwell = None
        self.flash = None
        self.first_flash = None
        self.save()

    def save(self):
        self.record["updated_at"] = datetime.now(UTC).isoformat()
        _write_json_atomic(self.path, self.record)

    def capture(self, tag, configuration, mode, duration_s, *, port=None):
        # Adopt an already completed exact-tag capture only after checking its configuration.
        existing = sorted((self.root / "captures").glob(f"{tag}-*/run.json"))
        for path in reversed(existing):
            if re.fullmatch(re.escape(tag) + r"-\d{8}T\d{6}\.\d{6}Z", path.parent.name) is None:
                continue
            run = load(path)
            cfg = run["configuration"]
            if (
                run["status"] == "passed"
                and cfg["name"] == configuration
                and cfg["mode"] == mode
                and cfg["duration_s"] == duration_s
                and cfg["port"] == port
                and (mode != "fast" or cfg["dwell_us"] == self.current_dwell)
            ):
                return path
        command = [
            sys.executable,
            str(ROOT / "scripts/capture_rate_timing.py"),
            "--configuration",
            configuration,
            "--mode",
            mode,
            "--duration-s",
            str(duration_s),
            "--output-root",
            str(self.root),
            "--tag",
            tag,
        ]
        if mode != "muted":
            command.append("--acknowledge-ota-authorization")
        if port:
            command.extend(("--port", port))
        if mode == "fast":
            command.extend(
                (
                    "--profile",
                    str(
                        ROOT
                        / f"profiles/tracking-c6-{self.current_dwell}us-v1/control_profile.json"
                    ),
                    "--flash-evidence",
                    str(self.flash),
                )
            )
        print(f"[capture] {tag} {configuration} {mode} {duration_s}s", flush=True)
        started = time.monotonic()
        output = execute(command)
        match = re.search(r"^run_dir=(/.+)$", output, re.MULTILINE)
        if match is None:
            raise RuntimeError("capture did not return run evidence")
        path = Path(match.group(1)) / "run.json"
        if load(path)["status"] != "passed":
            raise RuntimeError(f"capture evidence failed: {path}")
        print(f"[captured] {tag} wall={time.monotonic() - started:.1f}s", flush=True)
        return path

    def preflight(self):
        rejected = self.record.setdefault("unavailable_configurations", {})
        for name in ("A", "B", "C"):
            if any(x["configuration"] == name for x in self.record["preflight"]):
                continue
            if name in rejected:
                continue
            failed = [
                p
                for p in sorted((self.root / "captures").glob(f"preflight-{name}-*/run.json"))
                if load(p)["status"] == "failed"
            ]
            if len(failed) >= 2:
                reason = {
                    "reason": "repeated muted acquisition failure",
                    "evidence": [
                        {"path": str(p), "sha256": sha256(p), "error": load(p).get("error")}
                        for p in failed
                    ],
                }
                for cfg_name, cfg in CONFIGURATIONS.items():
                    if cfg.sample_rate_hz == CONFIGURATIONS[name].sample_rate_hz:
                        rejected[cfg_name] = reason
                self.save()
                print(f"[unavailable] {name}: {reason['reason']}", flush=True)
                continue
            try:
                path = self.capture(f"preflight-{name}", name, "muted", 30)
            except RuntimeError as error:
                for cfg_name, cfg in CONFIGURATIONS.items():
                    if cfg.sample_rate_hz == CONFIGURATIONS[name].sample_rate_hz:
                        rejected[cfg_name] = {"reason": str(error)}
                self.save()
                continue
            run = load(path)
            self.record["preflight"].append(
                {
                    "configuration": name,
                    "run_json": str(path),
                    "run_sha256": sha256(path),
                    "capture": run["capture"],
                }
            )
            self.save()
        if "A" in rejected:
            raise RuntimeError("baseline acquisition is unavailable")

    def available_configurations(self):
        rejected = self.record.get("unavailable_configurations", {})
        return [name for name in CONFIGURATIONS if name not in rejected]

    def references(self, position):
        for name in self.available_configurations():
            for port in PORTS:
                if any(
                    x["configuration"] == name and x["port"] == port and x["position"] == position
                    for x in self.record["references"]
                ):
                    continue
                path = self.capture(f"ref-{position}-{name}-{port}", name, "static", 2, port=port)
                run = load(path)
                self.record["references"].append(
                    {
                        "position": position,
                        "configuration": name,
                        "port": port,
                        "run_json": str(path),
                        "run_sha256": sha256(path),
                        "reference": run["reference"],
                    }
                )
                self.save()
                r = run["reference"]
                print(
                    f"[reference] {name} {port} |h|={abs(complex_value(r['transfer'])):.4g} "
                    f"phase_rms_10ms={r['phase_rms_10ms_deg']:.2f}deg",
                    flush=True,
                )
        refs = self.reference_paths("A", position)
        frozen_reference({p: load(path)["reference"] for p, path in refs.items()})

    def reference_paths(self, name, position="before"):
        return {
            x["port"]: Path(x["run_json"])
            for x in self.record["references"]
            if x["position"] == position and x["configuration"] == name
        }

    def set_dwell(self, dwell):
        if dwell == self.current_dwell:
            return
        print(f"[flash] {dwell}us", flush=True)
        self.flash = _flash(self.root, dwell)
        if self.first_flash is None:
            self.first_flash = self.flash
        self.current_dwell = dwell
        self.record["flashes"].append(
            {"dwell_us": dwell, "path": str(self.flash), "sha256": sha256(self.flash)}
        )
        self.save()

    def screen_capture(self, round_number, name, dwell, control=False):
        if any(
            x["round"] == round_number
            and x["configuration"] == name
            and x["dwell_us"] == dwell
            and x.get("control") == control
            for x in self.record["screen"]
        ):
            return
        self.set_dwell(dwell)
        tag = f"screen-r{round_number}-{name}-{dwell}us" + ("-control" if control else "")
        path = self.capture(tag, name, "fast", 4)
        started = time.monotonic()
        print(f"[analyze] {tag}", flush=True)
        try:
            analysis = analyze_rate_capture(
                path, self.reference_paths(name), self.reference_paths("A")
            )
        except ValueError as error:
            if not any(
                word in str(error)
                for word in ("autonomous", "RF envelope", "marker", "periodicity", "frames")
            ):
                raise
            analysis = {
                "schema": 1,
                "run_json": str(path),
                "status": "analysis-failed",
                "error": str(error),
                "variants": [],
            }
        analysis_path = self.root / "analysis" / (path.parent.name + ".json")
        analysis_path.parent.mkdir(exist_ok=True)
        _write_json_atomic(analysis_path, analysis)
        self.record["screen"].append(
            {
                "round": round_number,
                "configuration": name,
                "dwell_us": dwell,
                "control": control,
                "run_json": str(path),
                "analysis_json": str(analysis_path),
                "analysis_sha256": sha256(analysis_path),
            }
        )
        if round_number == 1:
            self.record["selection"] = select_candidate(self.record["screen"])
        self.save()
        passes = sum(v["metrics"]["passed"] for v in analysis["variants"])
        print(
            f"[analyzed] {tag} {passes}/{len(analysis['variants'])} variants pass "
            f"wall={time.monotonic() - started:.1f}s",
            flush=True,
        )

    def restore(self):
        if self.first_flash:
            print("[restore] original bench image", flush=True)
            path = _restore(self.root, self.first_flash)
            self.record["restores"].append({"path": str(path), "sha256": sha256(path)})
            self.first_flash = self.flash = self.current_dwell = None
            self.save()

    def screen(self):
        rng = random.Random(580020260908)
        for round_number, dwells in enumerate(
            ((200, 100, 50, 25), (25, 50, 100, 200), (100, 25, 200, 50)), 1
        ):
            self.screen_capture(round_number, "A", 200, control=True)
            for dwell in dwells:
                names = self.available_configurations()
                rng.shuffle(names)
                for name in names:
                    self.screen_capture(round_number, name, dwell)
            # Separate round-end capture cannot be confused with the round-start control.
            self.screen_capture(round_number + 10, "A", 200, control=True)
        self.record["screen_validation"] = evaluate_candidate(
            self.record["selection"], self.record["screen"]
        )
        self.save()

    def reference_drift(self):
        result = {}
        weights = frozen_reference(
            {p: load(path)["reference"] for p, path in self.reference_paths("A").items()}
        )
        for name in self.available_configurations():
            before = [
                complex_value(load(self.reference_paths(name)[p])["reference"]["transfer"])
                for p in PORTS
            ]
            after = [
                complex_value(load(self.reference_paths(name, "after")[p])["reference"]["transfer"])
                for p in PORTS
            ]
            result[name] = closure(after, before, weights["weights"], weights["observable"])
        self.record["reference_drift"] = result
        if not all(value["passed"] for value in result.values()):
            self.record["screen_validation"]["passed"] = False
            self.record["screen_validation"]["reference_drift_failure"] = True
        self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--stage", choices=("preflight", "references", "screen"), default="screen")
    parser.add_argument("--acknowledge-ota-authorization", action="store_true")
    parser.add_argument("--acknowledge-selector-flash", action="store_true")
    args = parser.parse_args()
    if args.stage != "preflight" and not args.acknowledge_ota_authorization:
        raise SystemExit("RF stage requires acknowledgement")
    if args.stage == "screen" and not args.acknowledge_selector_flash:
        raise SystemExit("screen requires selector-flash acknowledgement")
    args.output_root.mkdir(parents=True, exist_ok=True)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _s, _f: (_ for _ in ()).throw(KeyboardInterrupt()))
    with _lock(args.output_root / ".campaign.lock"):
        campaign = Campaign(args.output_root)
        campaign.record["status"] = "running"
        campaign.save()
        try:
            campaign.preflight()
            if args.stage in ("references", "screen"):
                campaign.references("before")
            if args.stage == "screen":
                campaign.screen()
                campaign.restore()
                campaign.references("after")
                campaign.reference_drift()
            campaign.record["status"] = "completed-" + args.stage
        except BaseException as error:
            campaign.record["status"] = "failed"
            campaign.record["failures"].append(
                {
                    "time": datetime.now(UTC).isoformat(),
                    "type": type(error).__name__,
                    "message": str(error),
                }
            )
            raise
        finally:
            try:
                campaign.restore()
                campaign.record["final_mutes"] = [
                    _mute("ip:192.168.1.15", RECEIVER_SERIAL),
                    _mute("ip:192.168.1.179", SOURCE_SERIAL),
                ]
            except BaseException as error:
                campaign.record["status"] = "cleanup-failed"
                campaign.record["cleanup_error"] = str(error)
                raise
            finally:
                campaign.save()
    print(f"campaign_json={campaign.path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
