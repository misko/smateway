"""Rate/bandwidth campaign contracts and independently referenced phase metrics."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

PORTS = ("ANT1", "ANT2", "ANT4", "ANT8", "ANT7", "ANT5")
RECEIVER_SERIAL = "104000b29905000e17000800065934759d"
SOURCE_SERIAL = "104473b80a16000de6ff2000f8a6beca79"


@dataclass(frozen=True)
class RateConfiguration:
    name: str
    sample_rate_hz: int
    bandwidth_hz: int

    def samples(self, duration_s: float) -> int:
        if not math.isfinite(duration_s) or duration_s <= 0:
            raise ValueError("capture duration must be positive and finite")
        count = round(duration_s * self.sample_rate_hz)
        if count < 1:
            raise ValueError("capture duration must contain samples")
        if not math.isclose(count / self.sample_rate_hz, duration_s, abs_tol=1e-10):
            raise ValueError("capture duration does not contain an integer sample count")
        return count

    def frame_plan(self, duration_s: float) -> tuple[int, int]:
        # The ABI-2 receiver rejects the 500k-sample block at 10 MS/s.
        # Bound transport blocks without shortening the RF observation.
        frame = min(self.sample_rate_hz // 20, 250_000)
        count = self.samples(duration_s)
        if count % frame:
            raise ValueError("duration must contain complete transport blocks")
        return frame, count // frame


CONFIGURATIONS = {
    item.name: item
    for item in (
        RateConfiguration("A", 2_000_000, 1_600_000),
        RateConfiguration("B", 5_000_000, 1_600_000),
        RateConfiguration("C", 10_000_000, 1_600_000),
        RateConfiguration("D", 5_000_000, 4_000_000),
        RateConfiguration("E", 10_000_000, 8_000_000),
    )
}


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


def load(path: Path) -> dict[str, Any]:
    result = json.loads(path.read_text())
    if not isinstance(result, dict):
        raise ValueError("expected a JSON object")
    return result


def complex_json(value: complex) -> dict[str, float]:
    return {"real": float(value.real), "imag": float(value.imag)}


def complex_value(value: dict[str, float]) -> complex:
    return complex(value["real"], value["imag"])


def validate_block(block: Any, previous: Any, frame_samples: int) -> None:
    if block.metadata_abi != 2 or block.samples.shape != (2, frame_samples):
        raise ValueError("non-ABI-2 or partial dual-RX block")
    if block.last_sample_sequence_exclusive - block.first_sample_sequence != frame_samples:
        raise ValueError("sample counter span differs from the block length")
    if block.missing_samples_before or block.overflow_observed:
        raise ValueError("sample loss or overflow")
    if previous is None:
        if block.buffer_sequence != 0:
            raise ValueError("first buffer sequence is not zero")
    elif (
        block.stream_id != previous.stream_id
        or block.buffer_sequence != previous.buffer_sequence + 1
        or block.first_sample_sequence != previous.last_sample_sequence_exclusive
    ):
        raise ValueError("discontinuous sample/stream counters")


def reference_summary(rx1: npt.ArrayLike, rx2: npt.ArrayLike, fs: int) -> dict[str, Any]:
    """Independent static TX1 reference, computed in nonoverlapping 10 ms groups."""
    first, second = np.asarray(rx1), np.asarray(rx2)
    if first.ndim != 1 or first.shape != second.shape or first.size < fs // 100:
        raise ValueError("static reference requires paired IQ and at least 10 ms")
    phasors = []
    powers = []
    cross_total = 0j
    p1_total = p2_total = 0.0
    chunk = fs // 100
    for start in range(0, first.size - chunk + 1, chunk):
        one = np.asarray(first[start : start + chunk], dtype=np.complex128)
        two = np.asarray(second[start : start + chunk], dtype=np.complex128)
        cross = complex(np.vdot(one, two))
        p1, p2 = float(np.vdot(one, one).real), float(np.vdot(two, two).real)
        phasors.append(cross / max(p1, np.finfo(float).tiny))
        powers.append(p1 / chunk)
        cross_total += cross
        p1_total += p1
        p2_total += p2
    transfer = cross_total / max(p1_total, np.finfo(float).tiny)
    vector = np.asarray(phasors)
    phase_rms = float(np.sqrt(np.mean(np.angle(vector / (transfer or 1e-300), deg=True) ** 2)))
    coherence = abs(cross_total) / max(math.sqrt(p1_total * p2_total), 1e-300)
    return {
        "transfer": complex_json(transfer),
        "coherence": float(coherence),
        "phase_rms_10ms_deg": phase_rms,
        "groups": len(phasors),
        "group_duration_ms": 10,
        "group_transfers": [complex_json(x) for x in phasors],
        "rx1_power_counts2": float(np.mean(powers)),
        "rx2_power_counts2": p2_total / (len(phasors) * chunk),
    }


def frozen_reference(references: dict[str, dict[str, Any]]) -> dict[str, Any]:
    values = np.asarray([complex_value(references[p]["transfer"]) for p in PORTS])
    power = np.abs(values) ** 2
    if not np.all(np.isfinite(power)) or np.max(power) <= 0:
        raise ValueError("invalid reference powers")
    observable = np.abs(values) >= 0.1 * np.max(np.abs(values))
    if observable.sum() < 4:
        raise ValueError("fewer than four independently observable ports")
    return {
        "ports": list(PORTS),
        "weights": (power / power.sum()).tolist(),
        "observable": observable.tolist(),
        "transfers": [complex_json(x) for x in values],
    }


def closure(
    vector: npt.ArrayLike,
    references: npt.ArrayLike,
    weights: npt.ArrayLike,
    observable: npt.ArrayLike,
) -> dict[str, Any]:
    measured = np.asarray(vector, dtype=np.complex128)
    expected = np.asarray(references, dtype=np.complex128)
    weight, mask = np.asarray(weights, dtype=float), np.asarray(observable, dtype=bool)
    if any(x.shape != (6,) for x in (measured, expected, weight, mask)):
        raise ValueError("closure requires six aligned ports")
    if (
        not np.all(np.isfinite(measured))
        or not np.all(np.isfinite(expected))
        or np.any(np.abs(expected) == 0)
        or not np.all(np.isfinite(weight))
        or np.any(weight < 0)
        or weight.sum() <= 0
        or mask.sum() < 4
    ):
        raise ValueError("invalid closure vector/reference/weights")
    ratio = measured / expected
    raw_phase = np.angle(ratio)
    rotation = np.angle(np.sum(weight * np.exp(1j * raw_phase)))
    phase = np.angle(ratio * np.exp(-1j * rotation), deg=True)
    gains = 20 * np.log10(np.maximum(np.abs(ratio), np.finfo(float).tiny))
    weighted = float(np.sqrt(np.average(phase**2, weights=weight)))
    maximum = float(np.max(np.abs(phase[mask])))
    maximum_gain = float(np.max(np.abs(gains[mask])))
    return {
        "raw_phase_bias_deg": np.rad2deg(raw_phase).tolist(),
        "common_rotation_deg": float(np.rad2deg(rotation)),
        "relative_phase_bias_deg": phase.tolist(),
        "gain_error_db": gains.tolist(),
        "weighted_phase_bias_deg": weighted,
        "maximum_observable_phase_bias_deg": maximum,
        "maximum_observable_gain_error_db": maximum_gain,
        "passed": weighted <= 5 and maximum <= 10 and maximum_gain <= 1,
    }


def configuration_json(name: str) -> dict[str, Any]:
    return asdict(CONFIGURATIONS[name])


def coarse_product(rx1: npt.ArrayLike, rx2: npt.ArrayLike, fs: int) -> np.ndarray:
    """One-microsecond complex means without allocating native full-capture products."""
    first, second = np.asarray(rx1), np.asarray(rx2)
    factor = fs // 1_000_000
    if fs % 1_000_000 or factor < 1 or first.shape != second.shape or first.ndim != 1:
        raise ValueError("coarse product requires paired IQ at integer MS/s")
    size = first.size // factor
    result = np.empty(size, dtype=np.complex64)
    for start in range(0, size, 100_000):
        stop = min(size, start + 100_000)
        one = np.asarray(first[start * factor : stop * factor], dtype=np.complex128)
        two = np.asarray(second[start * factor : stop * factor], dtype=np.complex128)
        result[start:stop] = (two * one.conj()).reshape(-1, factor).mean(axis=1)
    return result


def interval_moments(
    rx1: npt.ArrayLike,
    rx2: npt.ArrayLike,
    starts: npt.ArrayLike,
    stops: npt.ArrayLike,
    *,
    fs: int,
    difference_hz: float = 0,
) -> tuple[np.ndarray, np.ndarray]:
    """Batched exact interval sums with bounded-memory cumulative chunks."""
    first, second = np.asarray(rx1), np.asarray(rx2)
    left, right = np.asarray(starts, dtype=np.int64), np.asarray(stops, dtype=np.int64)
    if (
        first.shape != second.shape
        or first.ndim != 1
        or left.shape != right.shape
        or np.any(left < 0)
        or np.any(right > first.size)
        or np.any(left >= right)
    ):
        raise ValueError("invalid paired IQ or interval boundaries")
    positions, inverse = np.unique(
        np.concatenate((left.ravel(), right.ravel())), return_inverse=True
    )
    cross_at = np.zeros(positions.size, dtype=np.complex128)
    power_at = np.zeros(positions.size, dtype=np.float64)
    cross_total, power_total = 0j, 0.0
    for start in range(0, first.size, 250_000):
        stop = min(first.size, start + 250_000)
        one = np.asarray(first[start:stop], dtype=np.complex128)
        two = np.asarray(second[start:stop], dtype=np.complex128)
        product = two * one.conj()
        if difference_hz:
            product *= np.exp(-2j * np.pi * difference_hz * np.arange(start, stop) / fs)
        cross = np.concatenate(([0j], np.cumsum(product)))
        power = np.concatenate(([0.0], np.cumsum(np.abs(one) ** 2)))
        lo = np.searchsorted(positions, start, side="left")
        hi = np.searchsorted(positions, stop, side="right")
        offsets = positions[lo:hi] - start
        cross_at[lo:hi] = cross_total + cross[offsets]
        power_at[lo:hi] = power_total + power[offsets]
        cross_total += cross[-1]
        power_total += power[-1]
    cross_values, power_values = cross_at[inverse], power_at[inverse]
    size = left.size
    return (
        (cross_values[size:] - cross_values[:size]).reshape(left.shape),
        (power_values[size:] - power_values[:size]).reshape(left.shape),
    )


def native_fold(rx1, rx2, *, fs, cycle_hz, marker_us, cycle_us):
    """Complex transfer over a periodic cycle at approximately native resolution."""
    bins = round(fs * cycle_us / 1e6)
    cross = np.zeros(bins, dtype=np.complex128)
    power = np.zeros(bins)
    counts = np.zeros(bins, dtype=np.int64)
    first, second = np.asarray(rx1), np.asarray(rx2)
    for start in range(0, first.size, 250_000):
        stop = min(first.size, start + 250_000)
        one = np.asarray(first[start:stop], dtype=np.complex128)
        two = np.asarray(second[start:stop], dtype=np.complex128)
        phase = np.mod((np.arange(start, stop) / fs - marker_us / 1e6) * cycle_hz, 1)
        index = np.minimum((phase * bins).astype(np.int64), bins - 1)
        product = two * one.conj()
        cross += np.bincount(index, weights=product.real, minlength=bins)
        cross += 1j * np.bincount(index, weights=product.imag, minlength=bins)
        power += np.bincount(index, weights=np.abs(one) ** 2, minlength=bins)
        counts += np.bincount(index, minlength=bins)
    return cross / np.maximum(power, 1e-300), counts


def refine_offset(fold, *, fs, dwell_us, guard_us, edge_us=5):
    """Local plateau-consistency refinement; remains RF-inferred and retrospective."""
    factor = fs / 1e6
    candidates = np.arange(-round(5 * factor), round(5 * factor) + 1)
    scores = []
    for shift in candidates:
        residual = energy = 0.0
        for port in range(6):
            begin = round((port * (dwell_us + guard_us) + edge_us) * factor) + shift
            end = round((port * (dwell_us + guard_us) + dwell_us - edge_us) * factor) + shift
            segment = fold[np.arange(begin, end) % fold.size]
            residual += float(np.sum(np.abs(segment - segment.mean()) ** 2))
            energy += float(np.sum(np.abs(segment) ** 2))
        scores.append(1 - residual / max(energy, 1e-300))
    best = int(np.argmax(scores))
    return {
        "offset_us": float(candidates[best] / factor),
        "score": float(scores[best]),
        "at_search_boundary": best in (0, len(candidates) - 1),
        "offset_grid_us": (candidates / factor).tolist(),
        "scores": scores,
    }


def integration_metrics(matrix, *, cycle_ms, weights, references, observable):
    matrix = np.asarray(matrix, dtype=np.complex128)
    mean = matrix.mean(axis=0)
    independent = closure(mean, references, weights, observable)
    studies = []
    counts = sorted(
        {1, *[max(1, math.ceil(b / cycle_ms)) for b in (1, 2, 5, 10, 20, 50, 100, 200, 400)]}
    )
    for cycles in counts:
        groups = matrix.shape[0] // cycles
        if groups < 1:
            continue
        grouped = matrix[: groups * cycles].reshape(groups, cycles, 6).mean(axis=1)
        errors = np.angle(grouped / np.where(np.abs(mean) > 0, mean, 1e-300), deg=True)
        rms = np.sqrt(np.mean(errors**2, axis=0))
        weighted = float(np.sqrt(np.average(rms**2, weights=weights)))
        studies.append(
            {
                "cycles": cycles,
                "groups": groups,
                "wall_ms": cycles * cycle_ms,
                "weighted_phase_rms_deg": weighted,
                "per_port_phase_rms_deg": rms.tolist(),
                "passed": groups >= 8 and weighted <= 10,
            }
        )
    first = next((s["wall_ms"] for s in studies if s["passed"]), None)
    return {
        "closure": independent,
        "studies": studies,
        "first_phase_pass_ms": first,
        "passed": first is not None and independent["passed"],
        "mean_transfer": [complex_json(x) for x in mean],
    }


def analyze_rate_capture(
    run_path: Path, reference_paths: dict[str, Path], weight_paths: dict[str, Path]
) -> dict[str, Any]:
    """TX1 retrospective rate comparison with disjoint static references."""
    from smateway.fast_tracking import FastTrackingProfile, decode_fast_schedule

    run = load(run_path)
    cfg = run["configuration"]
    if run["status"] != "passed" or cfg["mode"] != "fast" or cfg["tx_channel"] != 0:
        raise ValueError("analysis requires a passed TX1 fast capture")
    fs = cfg["sample_rate_hz"]
    dwell = cfg["dwell_us"]
    root = Path(__file__).resolve().parents[2]
    profile = FastTrackingProfile.load(
        root / f"profiles/tracking-c6-{dwell}us-v1/control_profile.json"
    )
    refs = {p: load(reference_paths[p]) for p in PORTS}
    for p, r in refs.items():
        if (
            r["status"] != "passed"
            or r["configuration"]["mode"] != "static"
            or r["configuration"]["name"] != cfg["name"]
            or r["configuration"]["frequency_hz"] != cfg["frequency_hz"]
            or r["configuration"]["port"] != p
            or r["configuration"].get("receiver_gain_db", 60) != cfg.get("receiver_gain_db", 60)
        ):
            raise ValueError("independent reference identity/configuration mismatch")
    weight_records = {p: load(weight_paths[p]) for p in PORTS}
    for p, r in weight_records.items():
        if (
            r["status"] != "passed"
            or r["configuration"]["mode"] != "static"
            or r["configuration"]["name"] != "A"
            or r["configuration"]["frequency_hz"] != cfg["frequency_hz"]
            or r["configuration"]["port"] != p
            or r["configuration"].get("receiver_gain_db", 60) != cfg.get("receiver_gain_db", 60)
        ):
            raise ValueError("independent weight reference identity/configuration mismatch")
    frozen = frozen_reference({p: weight_records[p]["reference"] for p in PORTS})
    weights = np.asarray(frozen["weights"])
    observable = np.asarray(frozen["observable"])
    references = np.asarray([complex_value(refs[p]["reference"]["transfer"]) for p in PORTS])
    raw = run["capture"]["raw"]
    for item in raw:
        path = Path(item["path"])
        if path.stat().st_size != run["capture"]["samples_per_channel"] * 8:
            raise ValueError("raw length differs")
        if sha256(path) != item["sha256"]:
            raise ValueError("raw hash differs")
    one, two = [np.memmap(item["path"], dtype=np.complex64, mode="r") for item in raw]
    coarse = coarse_product(one, two, fs)
    decode = decode_fast_schedule(
        coarse, sample_rate_hz=1_000_000, tone_offset_hz=0, profile=profile, edge_trim_us=5
    )
    cycle_hz = decode.periodicity_frequency_hz or 1e6 / (
        profile.cycle_us * decode.cycle_scale_median
    )
    fold, counts = native_fold(
        one,
        two,
        fs=fs,
        cycle_hz=cycle_hz,
        marker_us=decode.marker_end_bins[0],
        cycle_us=profile.cycle_us,
    )
    refined = refine_offset(fold, fs=fs, dwell_us=dwell, guard_us=profile.guard_us)
    offsets = {"legacy": 0.0, "native_refined": refined["offset_us"]}
    discards = [x for x in (0, 1, 2, 5, 10, 15, 20, 30) if x + 5 < dwell * 0.95]
    base_left = np.asarray([i.predicted_start for i in decode.intervals]) * fs / 1e6
    base_right = np.asarray([i.predicted_stop for i in decode.intervals]) * fs / 1e6
    definitions = []
    starts = []
    stops = []
    for method, offset in offsets.items():
        for discard in discards:
            left = np.ceil(base_left + (offset + discard) * fs / 1e6).astype(np.int64)
            right = np.floor(base_right + (offset - 5) * fs / 1e6).astype(np.int64)
            # Reject complete leading/trailing frames affected by the refinement.
            valid = ((left >= 0) & (right <= one.size) & (left < right)).reshape(-1, 6).all(axis=1)
            keep = np.repeat(valid, 6)
            starts.append(left[keep])
            stops.append(right[keep])
            definitions.append((method, discard, len(left[keep])))
    all_starts, all_stops = np.concatenate(starts), np.concatenate(stops)
    cross, power = interval_moments(one, two, all_starts, all_stops, fs=fs)
    variants = []
    cursor = 0
    for (method, discard, size), left, right in zip(definitions, starts, stops, strict=True):
        h = cross[cursor : cursor + size] / np.maximum(power[cursor : cursor + size], 1e-300)
        raw_means = cross[cursor : cursor + size] / (right - left)
        cursor += size
        metrics = integration_metrics(
            h.reshape(-1, 6),
            cycle_ms=1000 / cycle_hz,
            weights=weights,
            references=references,
            observable=observable,
        )
        legacy_mean = raw_means.reshape(-1, 6)
        legacy_reference = legacy_mean.mean(axis=0)
        legacy = integration_metrics(
            legacy_mean,
            cycle_ms=1000 / cycle_hz,
            weights=weights,
            references=legacy_reference,
            observable=observable,
        )
        variants.append(
            {
                "method": method,
                "leading_discard_us": discard,
                "trailing_discard_us": 5,
                "median_samples": float(np.median(right - left)),
                "metrics": metrics,
                "legacy_cross_product_studies": legacy["studies"],
            }
        )
    settling = []
    for age in range(dwell - 5):
        values = []
        for port in range(6):
            begin = round((port * (dwell + 20) + age + refined["offset_us"]) * fs / 1e6)
            index = np.arange(begin, begin + round(5 * fs / 1e6)) % fold.size
            values.append(np.mean(fold[index]))
        result = closure(values, references, weights, observable)
        settling.append({"age_us": age, "window_end_us": age + 5, **result})
    settled = next(
        (
            row["window_end_us"]
            for i, row in enumerate(settling)
            if all(r["passed"] for r in settling[i:])
        ),
        None,
    )
    return {
        "schema": 1,
        "run_json": str(run_path),
        "run_sha256": sha256(run_path),
        "analysis_scope": "retrospective RF-inferred timing; no causal delivery-latency claim",
        "configuration": cfg,
        "frozen_weights": frozen,
        "references": {
            p: {"path": str(reference_paths[p]), "sha256": sha256(reference_paths[p])}
            for p in PORTS
        },
        "decode": {
            "method": decode.decode_method,
            "cycle_hz": cycle_hz,
            "alignment_score": decode.alignment_score,
            "frame_count": len(decode.marker_end_bins) - 1,
        },
        "refinement": refined,
        "variants": variants,
        "settling": settling,
        "strict_settled_window_end_us": settled,
        "fold_minimum_samples_per_bin": int(counts.min()),
    }
