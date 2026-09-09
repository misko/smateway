#!/usr/bin/env python3
"""Hash-checked RX2 spectra for matched source-on/off switching diagnostics."""

import argparse
import json
from pathlib import Path

import numpy as np

from smateway.rate_timing import load, sha256


def summarize_iq(raw, fs):
    """Average four nonoverlapping Hann spectra; retain the native sample rate."""
    vectors = []
    for item in raw:
        path = Path(item["path"])
        if sha256(path) != item["sha256"]:
            raise ValueError("raw hash differs")
        vectors.append(np.memmap(path, dtype=np.complex64, mode="r"))
    if len(vectors) != 2 or len(vectors[0]) != len(vectors[1]) or len(vectors[0]) != 4 * fs:
        raise ValueError("four-second paired IQ required")
    window = np.hanning(fs)
    scale = fs * np.sum(window**2)
    psd, power = np.zeros(16001), np.zeros(2)
    for start in range(0, 4 * fs, fs):
        for channel, vector in enumerate(vectors):
            values = np.asarray(vector[start : start + fs], dtype=np.complex128)
            power[channel] += float(np.mean(abs(values) ** 2)) / 4
            if channel == 1:
                values -= values.mean()
                psd += abs(np.fft.fft(values * window)[:16001]) ** 2 / scale / 4
    return power.tolist(), psd.tolist()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    off = args.campaign_root / "switched-muted-20260908T231105869478Z.json"
    on = args.campaign_root / "block-20260908T222235133339Z.json"
    rows = []
    for path, enabled in ((on, True), (off, False)):
        manifest = load(path)
        for item in manifest["captures"]:
            if item.get("control") or item["dwell_us"] not in (25, 200, 1000):
                continue
            run_path = Path(item["run_json"])
            if sha256(run_path) != item["sha256"]:
                raise ValueError("capture hash differs")
            run = load(run_path)
            cfg = run["configuration"]
            if (
                run["status"] != "passed"
                or cfg["frequency_hz"] != 5811000000
                or cfg["name"] != "A"
                or cfg["sample_rate_hz"] != 2000000
                or cfg["bandwidth_hz"] != 1600000
                or cfg["receiver_gain_db"] != 60
            ):
                raise ValueError("matched passed A / 5811 MHz record required")
            if not enabled and (
                cfg.get("source_rf_enabled") is not False or cfg["mode"] != "fast-ambient"
            ):
                raise ValueError("explicit source-muted record required")
            if not run["safety"]["final_source_mute"]["passed"]:
                raise ValueError("source cleanup not verified")
            if not enabled:
                for key in (
                    "initial_source_mute",
                    "source_after_capture_mute",
                    "final_source_mute",
                ):
                    readback = run["safety"][key]
                    if (
                        not readback["passed"]
                        or readback["tx_gain_db"] != [-80.0, -80.0]
                        or readback["dds_scales"] != [0.0] * 8
                    ):
                        raise ValueError("both source channels must have verified mute readbacks")
            print(f"[source-{'on' if enabled else 'off'}] {run_path.parent.name}", flush=True)
            power, psd = summarize_iq(run["capture"]["raw"], cfg["sample_rate_hz"])
            rows.append(
                {
                    "run_json": str(run_path),
                    "sha256": sha256(run_path),
                    "manifest": str(path),
                    "manifest_sha256": sha256(path),
                    "source_enabled": enabled,
                    "round": item["round"],
                    "dwell_us": item["dwell_us"],
                    "configuration": cfg,
                    "power_counts2": power,
                    "rx2_psd_counts2_per_hz": psd,
                    "clipped_samples": run["capture"]["clipped_samples"],
                    "final_source_mute": True,
                    "source_mute_readbacks": run["safety"] if not enabled else None,
                }
            )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "schema": 1,
                "status": "analyzed",
                "source_sha256": sha256(Path(__file__)),
                "method": "Native-rate RX2 complex IQ; per-second DC removal; 1 s Hann; "
                "four disjoint PSD averages; positive frequencies, no doubling or extra decimation",
                "scope": "Noncontemporaneous source-on/off diagnostic; no causal attribution",
                "frequency_hz": list(range(16001)),
                "rows": rows,
            },
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )
    print(f"spectrum_evidence={args.output}")


if __name__ == "__main__":
    main()
