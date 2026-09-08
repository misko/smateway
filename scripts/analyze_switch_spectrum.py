#!/usr/bin/env python3
"""Read-only broad timing-spectrum check; does not loosen decoder acceptance."""

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from smateway.fast_tracking import _parabolic_frequency, _weighted_median
from smateway.rate_timing import coarse_product, load, sha256


def spectrum(one, two, fs):
    product = coarse_product(one, two, fs)
    # Match the decoder's 1 MHz grid and five-microsecond smoothing exactly.
    # Do not downsample with a boxcar: out-of-band products can alias into the
    # harmonic search windows and manufacture misleading clock sidebands.
    product = np.convolve(product, np.ones(5) / 5, mode="same")
    product = product - product.mean()
    size = 1 << (4 * len(product) - 1).bit_length()
    fft = np.fft.fft(product, n=size)
    frequency = np.fft.fftfreq(size, 1 / 1000000)
    keep = (frequency >= 0) & (frequency < 16000)
    return frequency[keep], abs(fft[keep])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--block", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-output", type=Path)
    args = parser.parse_args()
    block = load(args.block)
    selected = {}
    for row in block["captures"]:
        if not row["control"] and row["status"] == "passed":
            selected.setdefault(row["dwell_us"], row)
    fig, axes = plt.subplots(len(selected), 1, figsize=(13, 2.7 * len(selected)), squeeze=False)
    records = []
    for ax, (dwell, row) in zip(axes[:, 0], sorted(selected.items()), strict=True):
        path = Path(row["run_json"])
        if sha256(path) != row["sha256"]:
            raise ValueError("record hash differs")
        run = load(path)
        raw = run["capture"]["raw"]
        if any(sha256(Path(r["path"])) != r["sha256"] for r in raw):
            raise ValueError("raw hash differs")
        one, two = [np.memmap(r["path"], dtype=np.complex64, mode="r") for r in raw]
        frequency, amplitude = spectrum(one, two, run["configuration"]["sample_rate_hz"])
        nominal = 1e6 / (6 * (dwell + 20) + 180)
        peaks = []
        for harmonic in range(1, 7):
            indices = np.flatnonzero(
                (frequency >= harmonic * nominal / 1.05) & (frequency <= harmonic * nominal / 0.95)
            )
            best = indices[np.argmax(amplitude[indices])]
            refined = _parabolic_frequency(amplitude, best, frequency[1] - frequency[0])
            peaks.append(
                {
                    "harmonic": harmonic,
                    "peak_hz": float(refined),
                    "inferred_fundamental_hz": float(refined / harmonic),
                    "relative_weight": float(amplitude[best] ** 2 / np.max(amplitude) ** 2),
                }
            )
        local = (
            np.flatnonzero((amplitude[1:-1] > amplitude[:-2]) & (amplitude[1:-1] > amplitude[2:]))
            + 1
        )
        order = local[np.argsort(amplitude[local])[::-1]]
        strongest = []
        for index in order:
            value = float(frequency[index])
            if all(abs(value - old) > 5 for old in strongest):
                strongest.append(value)
            if len(strongest) == 8:
                break
        estimates = np.array([p["inferred_fundamental_hz"] for p in peaks])
        weights = np.array([p["relative_weight"] for p in peaks])
        legacy = _weighted_median(estimates, weights)
        counts = [int(np.sum(abs(estimates - f) <= 0.005 * f)) for f in estimates]
        records.append(
            {
                "run_json": str(path),
                "sha256": sha256(path),
                "dwell_us": dwell,
                "nominal_fundamental_hz": nominal,
                "restricted_harmonic_peaks": peaks,
                "strongest_separated_peaks_hz": strongest,
                "legacy_weighted_median_hz": float(legacy),
                "legacy_consistent_harmonic_count": int(
                    np.sum(abs(estimates - legacy) <= 0.005 * legacy)
                ),
                "consensus_count_by_candidate": counts,
            }
        )
        ax.plot(frequency, 20 * np.log10(np.maximum(amplitude / amplitude.max(), 1e-8)), lw=0.6)
        for harmonic in range(1, 7):
            ax.axvline(nominal * harmonic, color="orange", alpha=0.5, linestyle=":")
        ax.set(
            xlim=(0, min(16000, nominal * 7)),
            ylim=(-85, 3),
            ylabel="Relative spectrum (dB)",
            title=f"{dwell} us; expected {nominal:.2f} Hz; strongest peaks "
            + ", ".join(f"{f:.1f}" for f in strongest[:4])
            + " Hz",
        )
        ax.grid(alpha=0.2)
    axes[-1, 0].set_xlabel("RX2 x conjugate(RX1) modulation frequency (Hz)")
    fig.suptitle(
        f"{block['frequency_hz'] / 1e6:g} MHz / {block['configuration']}: "
        "broad spectrum diagnostic; orange = nominal harmonics"
    )
    fig.tight_layout()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, dpi=160)
    (args.data_output or args.output.with_suffix(".json")).write_text(
        json.dumps(
            {
                "schema": 1,
                "block": str(args.block),
                "block_sha256": sha256(args.block),
                "source_sha256": sha256(Path(__file__)),
                "rows": records,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
