"""Deterministic, local-only inventory of exact-5.8-GHz SigMF evidence."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from smateway.capture_continuity import CaptureContinuitySummary, validate_sigmf_continuity

EXACT_CENTER_FREQUENCY_HZ = 5_800_000_000
EXACT_SAMPLE_RATE_HZ = 1_000_000
EXACT_DATATYPE = "ci16_le"
EXACT_RECEIVER_COUNT = 2
BYTES_PER_COMPLEX_SAMPLE_PER_RECEIVER = 4

FAMILY_RX_GAIN = "rx_gain_qualification"
FAMILY_TX_GAIN = "tx_gain_stimulus"
FAMILY_ROTATION = "rotation0_broadband_sweep"
FAMILY_PERMUTATION = "conducted_physical_permutation"
FAMILY_DUAL_TX = "dual_tx_localization"
FAMILY_EARLY_PHASE = "earlier_unreferenced_phase"
FAMILY_ORDER = (
    FAMILY_RX_GAIN,
    FAMILY_TX_GAIN,
    FAMILY_ROTATION,
    FAMILY_PERMUTATION,
    FAMILY_DUAL_TX,
    FAMILY_EARLY_PHASE,
)
CURRENT_CORPUS_FAMILY_COUNTS = {
    FAMILY_RX_GAIN: 63,
    FAMILY_TX_GAIN: 24,
    FAMILY_ROTATION: 26,
    FAMILY_PERMUTATION: 6,
    FAMILY_DUAL_TX: 10,
    FAMILY_EARLY_PHASE: 1,
}


class EvidenceInventoryError(RuntimeError):
    """The supplied board-state root cannot prove a consistent inventory."""


@dataclass(frozen=True, slots=True)
class SourceReference:
    """One authoritative local document that assigns a capture's experiment role."""

    kind: str
    path: str
    sha256: str
    json_pointer: str

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "path": self.path,
            "sha256": self.sha256,
            "json_pointer": self.json_pointer,
        }


@dataclass(slots=True)
class SourceClaim:
    """Classification and cross-checks derived from one authoritative source."""

    artifact_id: str
    family: str
    role: str
    source_reference: SourceReference
    receiver_gain_db: float | None = None
    tx_hardware_gain_db: float | None = None
    tx_channel: int | None = None
    expected_raw_sha256: set[str] = field(default_factory=set)
    expected_metadata_sha256: set[str] = field(default_factory=set)
    expected_raw_sizes: set[int] = field(default_factory=set)
    expected_metadata_sizes: set[int] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class ValidatedCapture:
    """One validated SigMF record before raw-SHA deduplication."""

    artifact_id: str
    relative_directory: str
    metadata_path: str
    data_path: str
    metadata_sha256: str
    raw_data_sha256: str
    raw_data_size_bytes: int
    created_at: str
    receiver_gain_db: float
    tx_hardware_gain_db: float | None
    tx_channel: int | None
    sample_count: int
    family: str
    role: str
    source_references: tuple[SourceReference, ...]
    companion_analyses: tuple[dict[str, object], ...]
    continuity: CaptureContinuitySummary | None


def sha256_file(path: Path) -> str:
    """Hash one ordinary file in bounded blocks."""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise EvidenceInventoryError(f"cannot hash {path}: {error}") from error
    return digest.hexdigest()


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceInventoryError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise EvidenceInventoryError(f"{label} must be an array")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvidenceInventoryError(f"{label} must be a nonempty string")
    return value


def _integer(value: object, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvidenceInventoryError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise EvidenceInventoryError(f"{label} must be at least {minimum}")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvidenceInventoryError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise EvidenceInventoryError(f"{label} must be finite")
    return result


def _optional_number(value: object, label: str) -> float | None:
    return None if value is None else _number(value, label)


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")), label)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceInventoryError(f"cannot read {label} {path}: {error}") from error


def _relative_file(path: Path, root: Path, label: str) -> str:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise EvidenceInventoryError(f"cannot resolve {label} {path}: {error}") from error
    try:
        relative = resolved.relative_to(root)
    except ValueError as error:
        raise EvidenceInventoryError(
            f"{label} escapes the supplied board-state root: {path}"
        ) from error
    if not resolved.is_file():
        raise EvidenceInventoryError(f"{label} is not an ordinary file: {path}")
    return relative.as_posix()


def _source_reference(
    path: Path,
    root: Path,
    *,
    kind: str,
    json_pointer: str,
    hash_cache: dict[Path, str],
) -> SourceReference:
    relative = _relative_file(path, root, "source reference")
    resolved = path.resolve()
    digest = hash_cache.get(resolved)
    if digest is None:
        digest = sha256_file(path)
        hash_cache[resolved] = digest
    return SourceReference(kind=kind, path=relative, sha256=digest, json_pointer=json_pointer)


def _normalized_utc(value: object, label: str) -> str:
    text = _string(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise EvidenceInventoryError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise EvidenceInventoryError(f"{label} must contain a UTC offset")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _artifact_path_agrees(value: object, artifact_id: str, label: str) -> None:
    if value is None:
        return
    path = Path(_string(value, label))
    if artifact_id not in path.parts and artifact_id not in path.name:
        raise EvidenceInventoryError(f"{label} does not agree with artifact {artifact_id}")


def _add_claim(claims: dict[str, list[SourceClaim]], claim: SourceClaim) -> None:
    claims.setdefault(claim.artifact_id, []).append(claim)


def _qualification_claims(
    root: Path, hash_cache: dict[Path, str], claims: dict[str, list[SourceClaim]]
) -> None:
    specifications = (
        (
            "hexcal-gain-qualifications/**/gain-qualification.json",
            FAMILY_RX_GAIN,
            "rx_gain_qualification_manifest",
        ),
        (
            "hexcal-stimulus-qualifications/**/stimulus-qualification.json",
            FAMILY_TX_GAIN,
            "tx_gain_stimulus_manifest",
        ),
    )
    for pattern, family, kind in specifications:
        for manifest_path in sorted(root.glob(pattern)):
            _relative_file(manifest_path, root, "qualification manifest")
            document = _read_json(manifest_path, "qualification manifest")
            for index, value in enumerate(_sequence(document.get("conditions"), "conditions")):
                condition = _mapping(value, f"conditions[{index}]")
                frequency = condition.get("center_frequency_hz")
                if frequency != EXACT_CENTER_FREQUENCY_HZ:
                    continue
                evidence = _mapping(condition.get("artifact_evidence"), "artifact_evidence")
                artifact_id = _string(evidence.get("artifact_id"), "artifact_evidence.artifact_id")
                _artifact_path_agrees(evidence.get("path"), artifact_id, "artifact_evidence.path")
                _artifact_path_agrees(
                    evidence.get("data_path"), artifact_id, "artifact_evidence.data_path"
                )
                _artifact_path_agrees(
                    evidence.get("metadata_path"), artifact_id, "artifact_evidence.metadata_path"
                )
                receiver_gain = _number(
                    condition.get("receiver_gain_db"), "condition.receiver_gain_db"
                )
                tx_gain = _optional_number(
                    condition.get("tx_hardware_gain_db"), "condition.tx_hardware_gain_db"
                )
                if tx_gain is None:
                    readback = condition.get("rf_readback_evidence")
                    if isinstance(readback, dict):
                        tx_gain = _optional_number(
                            readback.get("tx_hardware_gain_db_requested"),
                            "rf_readback_evidence.tx_hardware_gain_db_requested",
                        )
                tx_channel = _integer(condition.get("tx_channel"), "condition.tx_channel")
                role_value = receiver_gain if family == FAMILY_RX_GAIN else tx_gain
                role_name = "receiver_gain_db" if family == FAMILY_RX_GAIN else "tx_gain_db"
                if role_value is None:
                    raise EvidenceInventoryError(f"{artifact_id} lacks {role_name}")
                claim = SourceClaim(
                    artifact_id=artifact_id,
                    family=family,
                    role=f"{manifest_path.parent.name}:{role_name}={role_value:g}",
                    source_reference=_source_reference(
                        manifest_path,
                        root,
                        kind=kind,
                        json_pointer=f"/conditions/{index}",
                        hash_cache=hash_cache,
                    ),
                    receiver_gain_db=receiver_gain,
                    tx_hardware_gain_db=tx_gain,
                    tx_channel=tx_channel,
                )
                if raw_sha := evidence.get("data_sha256"):
                    claim.expected_raw_sha256.add(_string(raw_sha, "data_sha256"))
                if metadata_sha := evidence.get("metadata_sha256"):
                    claim.expected_metadata_sha256.add(
                        _string(metadata_sha, "metadata_sha256")
                    )
                if raw_size := evidence.get("data_size_bytes"):
                    claim.expected_raw_sizes.add(_integer(raw_size, "data_size_bytes", minimum=1))
                if metadata_size := evidence.get("metadata_size_bytes"):
                    claim.expected_metadata_sizes.add(
                        _integer(metadata_size, "metadata_size_bytes", minimum=1)
                    )
                _add_claim(claims, claim)


def _sweep_claims(
    root: Path, hash_cache: dict[Path, str], claims: dict[str, list[SourceClaim]]
) -> None:
    pattern = "closed-loop-frequency-sweeps/*/manifest.json"
    for manifest_path in sorted(root.glob(pattern)):
        _relative_file(manifest_path, root, "frequency-sweep manifest")
        document = _read_json(manifest_path, "frequency-sweep manifest")
        for index, value in enumerate(_sequence(document.get("attempts"), "attempts")):
            attempt = _mapping(value, f"attempts[{index}]")
            if attempt.get("center_frequency_hz") != EXACT_CENTER_FREQUENCY_HZ:
                continue
            artifact_id = _string(attempt.get("artifact_id"), "attempt.artifact_id")
            identity = _mapping(attempt.get("artifact_identity"), "attempt.artifact_identity")
            if _string(identity.get("artifact_id"), "identity.artifact_id") != artifact_id:
                raise EvidenceInventoryError(f"sweep identity disagrees for {artifact_id}")
            _artifact_path_agrees(identity.get("path"), artifact_id, "identity.path")
            quality = _mapping(attempt.get("quality_result"), "attempt.quality_result")
            if _string(quality.get("artifact_id"), "quality_result.artifact_id") != artifact_id:
                raise EvidenceInventoryError(f"sweep quality result disagrees for {artifact_id}")
            _artifact_path_agrees(
                quality.get("artifact_path"), artifact_id, "quality_result.artifact_path"
            )
            receiver_gain = _number(
                attempt.get("receiver_gain_db"), "attempt.receiver_gain_db"
            )
            tx_channel = _integer(attempt.get("tx_channel"), "attempt.tx_channel")
            run_label = manifest_path.parent.name
            claim = SourceClaim(
                artifact_id=artifact_id,
                family=FAMILY_ROTATION,
                role=f"{run_label}:rotation={attempt.get('rotation')}:outcome={attempt.get('outcome')}",
                source_reference=_source_reference(
                    manifest_path,
                    root,
                    kind="closed_loop_frequency_sweep_manifest",
                    json_pointer=f"/attempts/{index}",
                    hash_cache=hash_cache,
                ),
                receiver_gain_db=receiver_gain,
                tx_channel=tx_channel,
            )
            claim.expected_raw_sha256.add(_string(identity.get("sha256"), "identity.sha256"))
            if artifact_sha := quality.get("artifact_sha256"):
                claim.expected_raw_sha256.add(_string(artifact_sha, "artifact_sha256"))
            _add_claim(claims, claim)


def _permutation_claims(
    root: Path, hash_cache: dict[Path, str], claims: dict[str, list[SourceClaim]]
) -> None:
    paths = sorted(root.glob("closed-loop-calibration-*/closed-loop-permutation-manifest.json"))
    for manifest_path in paths:
        _relative_file(manifest_path, root, "permutation manifest")
        document = _read_json(manifest_path, "permutation manifest")
        stimulus = _mapping(document.get("stimulus"), "permutation stimulus")
        tx_channel = _integer(stimulus.get("tx_channel"), "stimulus.tx_channel")
        receiver_gains = _mapping(
            stimulus.get("receiver_gain_db_by_frequency_hz"),
            "stimulus.receiver_gain_db_by_frequency_hz",
        )
        receiver_gain = _number(
            receiver_gains.get(str(EXACT_CENTER_FREQUENCY_HZ)), "5.8 GHz receiver gain"
        )
        tx_gain = _number(
            stimulus.get("tx_hardware_gain_db"), "stimulus.tx_hardware_gain_db"
        )

        def add(
            artifact: object,
            pointer: str,
            role: str,
            gain: float = receiver_gain,
            *,
            source_path: Path = manifest_path,
            frozen_tx_gain: float = tx_gain,
            frozen_tx_channel: int = tx_channel,
        ) -> None:
            artifact_id = _string(artifact, pointer)
            _add_claim(
                claims,
                SourceClaim(
                    artifact_id=artifact_id,
                    family=FAMILY_PERMUTATION,
                    role=role,
                    source_reference=_source_reference(
                        source_path,
                        root,
                        kind="closed_loop_permutation_manifest",
                        json_pointer=pointer,
                        hash_cache=hash_cache,
                    ),
                    receiver_gain_db=gain,
                    tx_hardware_gain_db=frozen_tx_gain,
                    tx_channel=frozen_tx_channel,
                ),
            )

        for round_index, value in enumerate(_sequence(document.get("rounds"), "rounds")):
            round_document = _mapping(value, f"rounds[{round_index}]")
            rotation = _integer(round_document.get("rotation"), "round.rotation")
            artifacts = _mapping(
                round_document.get("artifacts_by_frequency_hz"),
                "round.artifacts_by_frequency_hz",
            )
            if artifact := artifacts.get(str(EXACT_CENTER_FREQUENCY_HZ)):
                pointer = (
                    f"/rounds/{round_index}/artifacts_by_frequency_hz/"
                    f"{EXACT_CENTER_FREQUENCY_HZ}"
                )
                add(artifact, pointer, f"rotation={rotation}:accepted")
            repeat_key = "additional_5800mhz_repeat_artifact_ids"
            repeats = round_document.get(repeat_key, [])
            for repeat_index, artifact in enumerate(_sequence(repeats, repeat_key)):
                pointer = f"/rounds/{round_index}/{repeat_key}/{repeat_index}"
                add(artifact, pointer, f"rotation={rotation}:repeat={repeat_index + 1}")
            if screen := round_document.get("5800mhz_rx40_screen_artifact_id"):
                pointer = f"/rounds/{round_index}/5800mhz_rx40_screen_artifact_id"
                add(screen, pointer, f"rotation={rotation}:rx40_screen", 40.0)
            rejected = round_document.get("rejected_attempts", [])
            for rejected_index, rejected_value in enumerate(
                _sequence(rejected, "rejected_attempts")
            ):
                rejected_document = _mapping(rejected_value, "rejected attempt")
                if rejected_document.get("frequency_hz") == EXACT_CENTER_FREQUENCY_HZ:
                    pointer = (
                        f"/rounds/{round_index}/rejected_attempts/{rejected_index}/artifact_id"
                    )
                    add(
                        rejected_document.get("artifact_id"),
                        pointer,
                        f"rotation={rotation}:quality_rejected",
                    )
        closure = document.get("closure")
        if isinstance(closure, dict):
            artifacts = _mapping(
                closure.get("artifacts_by_frequency_hz"),
                "closure.artifacts_by_frequency_hz",
            )
            if artifact := artifacts.get(str(EXACT_CENTER_FREQUENCY_HZ)):
                pointer = f"/closure/artifacts_by_frequency_hz/{EXACT_CENTER_FREQUENCY_HZ}"
                add(artifact, pointer, "rotation=0:return_to_original_wiring_closure")


def _phase_claims(
    root: Path, hash_cache: dict[Path, str], claims: dict[str, list[SourceClaim]]
) -> None:
    for manifest_path in sorted(root.glob("phase-distributions/*/manifest.json")):
        _relative_file(manifest_path, root, "phase-distribution manifest")
        document = _read_json(manifest_path, "phase-distribution manifest")
        for index, value in enumerate(_sequence(document.get("attempts"), "attempts")):
            attempt = _mapping(value, f"attempts[{index}]")
            if attempt.get("center_frequency_hz") != EXACT_CENTER_FREQUENCY_HZ:
                continue
            artifact_id = _string(attempt.get("artifact_id"), "attempt.artifact_id")
            tx_channel = _integer(attempt.get("tx_channel"), "attempt.tx_channel")
            tx_name = _string(attempt.get("tx_name"), "attempt.tx_name")
            round_number = _integer(attempt.get("round"), "attempt.round", minimum=1)
            _add_claim(
                claims,
                SourceClaim(
                    artifact_id=artifact_id,
                    family=FAMILY_DUAL_TX,
                    role=f"{manifest_path.parent.name}:{tx_name}:round={round_number}",
                    source_reference=_source_reference(
                        manifest_path,
                        root,
                        kind="dual_tx_phase_distribution_manifest",
                        json_pointer=f"/attempts/{index}",
                        hash_cache=hash_cache,
                    ),
                    tx_channel=tx_channel,
                ),
            )


def _build_claim_index(root: Path) -> dict[str, list[SourceClaim]]:
    claims: dict[str, list[SourceClaim]] = {}
    hash_cache: dict[Path, str] = {}
    _qualification_claims(root, hash_cache, claims)
    _sweep_claims(root, hash_cache, claims)
    _permutation_claims(root, hash_cache, claims)
    _phase_claims(root, hash_cache, claims)
    return claims


def _exact_5g8_metadata(root: Path) -> list[tuple[Path, Mapping[str, Any]]]:
    exact: list[tuple[Path, Mapping[str, Any]]] = []
    for path in sorted(root.rglob("*.sigmf-meta")):
        _relative_file(path, root, "SigMF metadata")
        metadata = _read_json(path, "SigMF metadata")
        captures = _sequence(metadata.get("captures"), "SigMF captures")
        frequencies: list[float] = []
        for index, value in enumerate(captures):
            capture = _mapping(value, f"captures[{index}]")
            settings = _mapping(capture.get("settings"), f"captures[{index}].settings")
            frequencies.append(
                _number(
                    settings.get("center_frequency_hz"),
                    f"captures[{index}].settings.center_frequency_hz",
                )
            )
        if EXACT_CENTER_FREQUENCY_HZ not in frequencies:
            continue
        if not frequencies or any(value != EXACT_CENTER_FREQUENCY_HZ for value in frequencies):
            raise EvidenceInventoryError(f"{path} mixes exact 5.8 GHz with other centers")
        exact.append((path, metadata))
    return exact


def _companion_analyses(
    directory: Path,
    root: Path,
    artifact_id: str,
    raw_sha256: str,
    sample_count: int,
) -> tuple[tuple[dict[str, object], ...], float | None, int | None]:
    analyses: list[dict[str, object]] = []
    tx_gains: set[float] = set()
    tx_channels: set[int] = set()
    for path in sorted(directory.glob("*.json")):
        relative = _relative_file(path, root, "companion analysis")
        document = _read_json(path, "companion analysis")
        artifact = document.get("artifact")
        if isinstance(artifact, dict):
            if _string(artifact.get("artifact_id"), "analysis artifact ID") != artifact_id:
                raise EvidenceInventoryError(f"companion analysis disagrees for {artifact_id}")
            _artifact_path_agrees(artifact.get("path"), artifact_id, "analysis artifact path")
            if (declared_sha := artifact.get("sha256")) and (
                _string(declared_sha, "analysis artifact SHA") != raw_sha256
            ):
                raise EvidenceInventoryError(f"analysis raw SHA disagrees for {artifact_id}")
            if (declared_samples := artifact.get("sample_count")) and (
                _integer(declared_samples, "analysis sample_count") != sample_count
            ):
                raise EvidenceInventoryError(
                    f"analysis sample count disagrees for {artifact_id}"
                )
        aggregation_key = document.get("aggregation_key")
        if isinstance(aggregation_key, dict) and aggregation_key.get("artifact_id") != artifact_id:
            raise EvidenceInventoryError(f"analysis aggregation key disagrees for {artifact_id}")
        capture = document.get("capture")
        if isinstance(capture, dict):
            raw_tx_gain = capture.get("tx_gain_readback_db")
            if raw_tx_gain is None:
                raw_tx_gain = capture.get("tx_hardware_gain_db")
            if raw_tx_gain is not None:
                tx_gains.add(_number(raw_tx_gain, "analysis TX gain"))
            if capture.get("tx_channel") is not None:
                tx_channels.add(_integer(capture.get("tx_channel"), "analysis TX channel"))
        analyses.append(
            {
                "analysis_kind": str(document.get("analysis_kind", path.stem)),
                "path": relative,
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )
    if len(tx_gains) > 1:
        raise EvidenceInventoryError(f"companion analyses disagree on TX gain for {artifact_id}")
    if len(tx_channels) > 1:
        raise EvidenceInventoryError(f"companion analyses disagree on TX channel for {artifact_id}")
    return (
        tuple(analyses),
        next(iter(tx_gains)) if tx_gains else None,
        next(iter(tx_channels)) if tx_channels else None,
    )


def _validate_claim(
    claim: SourceClaim,
    *,
    metadata_sha256: str,
    metadata_size: int,
    raw_sha256: str,
    raw_size: int,
    receiver_gain_db: float,
    companion_tx_gain: float | None,
    companion_tx_channel: int | None,
) -> tuple[float | None, int | None]:
    if claim.expected_raw_sha256 and claim.expected_raw_sha256 != {raw_sha256}:
        raise EvidenceInventoryError(f"source raw SHA disagrees for {claim.artifact_id}")
    if claim.expected_metadata_sha256 and claim.expected_metadata_sha256 != {metadata_sha256}:
        raise EvidenceInventoryError(f"source metadata SHA disagrees for {claim.artifact_id}")
    if claim.expected_raw_sizes and claim.expected_raw_sizes != {raw_size}:
        raise EvidenceInventoryError(f"source raw size disagrees for {claim.artifact_id}")
    if claim.expected_metadata_sizes and claim.expected_metadata_sizes != {metadata_size}:
        raise EvidenceInventoryError(f"source metadata size disagrees for {claim.artifact_id}")
    if claim.receiver_gain_db is not None and claim.receiver_gain_db != receiver_gain_db:
        raise EvidenceInventoryError(f"source receiver gain disagrees for {claim.artifact_id}")
    if (
        claim.tx_hardware_gain_db is not None
        and companion_tx_gain is not None
        and claim.tx_hardware_gain_db != companion_tx_gain
    ):
        raise EvidenceInventoryError(f"source TX gain disagrees for {claim.artifact_id}")
    if (
        claim.tx_channel is not None
        and companion_tx_channel is not None
        and claim.tx_channel != companion_tx_channel
    ):
        raise EvidenceInventoryError(f"source TX channel disagrees for {claim.artifact_id}")
    return (
        claim.tx_hardware_gain_db
        if claim.tx_hardware_gain_db is not None
        else companion_tx_gain,
        claim.tx_channel if claim.tx_channel is not None else companion_tx_channel,
    )


def _validate_capture(
    path: Path,
    metadata: Mapping[str, Any],
    root: Path,
    claims: dict[str, list[SourceClaim]],
) -> ValidatedCapture:
    relative_metadata = _relative_file(path, root, "SigMF metadata")
    global_metadata = _mapping(metadata.get("global"), "SigMF global")
    if _string(global_metadata.get("core:datatype"), "core:datatype") != EXACT_DATATYPE:
        raise EvidenceInventoryError(f"{path} is not {EXACT_DATATYPE}")
    receiver_count = _integer(global_metadata.get("core:num_channels"), "core:num_channels")
    if receiver_count != EXACT_RECEIVER_COUNT:
        raise EvidenceInventoryError(f"{path} is not a dual-receiver capture")
    sample_rate = _number(global_metadata.get("core:sample_rate"), "core:sample_rate")
    if sample_rate != EXACT_SAMPLE_RATE_HZ:
        raise EvidenceInventoryError(f"{path} is not sampled at exactly 1 MS/s")
    artifact_id = _string(global_metadata.get("pluto:artifact_id"), "pluto:artifact_id")
    if path.stem != artifact_id or path.parent.name != artifact_id:
        raise EvidenceInventoryError(f"artifact ID/path disagreement at {path}")
    created_at = _normalized_utc(global_metadata.get("pluto:created_at"), "pluto:created_at")
    declared_raw_sha = _string(global_metadata.get("pluto:sha256"), "pluto:sha256")

    captures = _sequence(metadata.get("captures"), "captures")
    receiver_gains: set[float] = set()
    for index, value in enumerate(captures):
        settings = _mapping(_mapping(value, f"captures[{index}]").get("settings"), "settings")
        if _number(settings.get("center_frequency_hz"), "center_frequency_hz") != (
            EXACT_CENTER_FREQUENCY_HZ
        ):
            raise EvidenceInventoryError(f"{artifact_id} is not entirely exact 5.8 GHz")
        if _number(settings.get("sample_rate_hz"), "sample_rate_hz") != EXACT_SAMPLE_RATE_HZ:
            raise EvidenceInventoryError(f"{artifact_id} capture settings are not 1 MS/s")
        channels = _sequence(settings.get("channels"), "capture channels")
        if list(channels) != [0, 1]:
            raise EvidenceInventoryError(f"{artifact_id} capture channels are not [0, 1]")
        receiver_gains.add(_number(settings.get("gain_db"), "capture gain_db"))
    if len(receiver_gains) != 1:
        raise EvidenceInventoryError(f"{artifact_id} does not have one fixed receiver gain")
    receiver_gain = next(iter(receiver_gains))

    capture_summary = _mapping(metadata.get("pluto:capture"), "pluto:capture")
    sample_count = _integer(capture_summary.get("sample_count"), "sample_count", minimum=1)
    if _integer(capture_summary.get("receiver_count"), "receiver_count") != receiver_count:
        raise EvidenceInventoryError(f"{artifact_id} receiver counts disagree")
    initial = _mapping(capture_summary.get("initial_settings"), "initial_settings")
    if _number(initial.get("center_frequency_hz"), "initial center") != EXACT_CENTER_FREQUENCY_HZ:
        raise EvidenceInventoryError(f"{artifact_id} initial center differs")
    if _number(initial.get("sample_rate_hz"), "initial sample rate") != EXACT_SAMPLE_RATE_HZ:
        raise EvidenceInventoryError(f"{artifact_id} initial sample rate differs")
    if list(_sequence(initial.get("channels"), "initial channels")) != [0, 1]:
        raise EvidenceInventoryError(f"{artifact_id} initial channels differ")

    data_path = path.with_suffix("").with_suffix(".sigmf-data")
    relative_data = _relative_file(data_path, root, "SigMF data")
    expected_size = (
        sample_count * receiver_count * BYTES_PER_COMPLEX_SAMPLE_PER_RECEIVER
    )
    raw_size = data_path.stat().st_size
    if raw_size != expected_size:
        raise EvidenceInventoryError(
            f"{artifact_id} raw size {raw_size} != expected {expected_size}"
        )
    raw_sha = sha256_file(data_path)
    if raw_sha != declared_raw_sha:
        raise EvidenceInventoryError(f"{artifact_id} data SHA differs from SigMF metadata")
    metadata_sha = sha256_file(path)
    metadata_size = path.stat().st_size

    if metadata.get("pluto:continuity") is None:
        raise EvidenceInventoryError(f"{artifact_id} lacks required ABI-2 continuity evidence")
    try:
        continuity = validate_sigmf_continuity(metadata, expected_total_samples=sample_count)
    except ValueError as error:
        raise EvidenceInventoryError(
            f"{artifact_id} continuity validation failed: {error}"
        ) from error

    companion, companion_tx_gain, companion_tx_channel = _companion_analyses(
        path.parent, root, artifact_id, raw_sha, sample_count
    )
    artifact_claims = claims.pop(artifact_id, [])
    if not artifact_claims:
        description = _string(global_metadata.get("core:description"), "core:description")
        if (
            path.parts[-3] != "pluto-usb-captures"
            or not description.startswith("fast20 phase ")
        ):
            raise EvidenceInventoryError(f"no authoritative family source for {artifact_id}")
        metadata_reference = SourceReference(
            kind="sigmf_metadata_only_unreferenced_predecessor",
            path=relative_metadata,
            sha256=metadata_sha,
            json_pointer="/global/core:description",
        )
        artifact_claims = [
            SourceClaim(
                artifact_id=artifact_id,
                family=FAMILY_EARLY_PHASE,
                role="pre-manifest exact-5.8 phase trial",
                source_reference=metadata_reference,
            )
        ]
    families = {claim.family for claim in artifact_claims}
    if len(families) != 1:
        raise EvidenceInventoryError(f"cross-family source overlap for {artifact_id}: {families}")
    roles = {claim.role for claim in artifact_claims}
    if len(roles) != 1:
        raise EvidenceInventoryError(f"multiple authoritative roles for {artifact_id}: {roles}")
    tx_gains: set[float] = set()
    tx_channels: set[int] = set()
    for claim in artifact_claims:
        tx_gain, tx_channel = _validate_claim(
            claim,
            metadata_sha256=metadata_sha,
            metadata_size=metadata_size,
            raw_sha256=raw_sha,
            raw_size=raw_size,
            receiver_gain_db=receiver_gain,
            companion_tx_gain=companion_tx_gain,
            companion_tx_channel=companion_tx_channel,
        )
        if tx_gain is not None:
            tx_gains.add(tx_gain)
        if tx_channel is not None:
            tx_channels.add(tx_channel)
    if len(tx_gains) > 1 or len(tx_channels) > 1:
        raise EvidenceInventoryError(f"gain or channel sources disagree for {artifact_id}")
    source_references = tuple(
        sorted(
            {claim.source_reference for claim in artifact_claims},
            key=lambda value: (value.path, value.json_pointer),
        )
    )
    return ValidatedCapture(
        artifact_id=artifact_id,
        relative_directory=path.parent.relative_to(root).as_posix(),
        metadata_path=relative_metadata,
        data_path=relative_data,
        metadata_sha256=metadata_sha,
        raw_data_sha256=raw_sha,
        raw_data_size_bytes=raw_size,
        created_at=created_at,
        receiver_gain_db=receiver_gain,
        tx_hardware_gain_db=next(iter(tx_gains)) if tx_gains else companion_tx_gain,
        tx_channel=next(iter(tx_channels)) if tx_channels else companion_tx_channel,
        sample_count=sample_count,
        family=next(iter(families)),
        role=next(iter(roles)),
        source_references=source_references,
        companion_analyses=companion,
        continuity=continuity,
    )


def _capture_document(group: Sequence[ValidatedCapture]) -> dict[str, object]:
    ordered = sorted(group, key=lambda value: (value.artifact_id, value.metadata_path))
    primary = ordered[0]
    if {value.family for value in ordered} != {primary.family}:
        raise EvidenceInventoryError(
            f"raw SHA {primary.raw_data_sha256} occurs in more than one family"
        )
    signatures = {
        (
            value.family,
            value.role,
            value.created_at,
            value.receiver_gain_db,
            value.tx_hardware_gain_db,
            value.tx_channel,
            value.sample_count,
            value.raw_data_size_bytes,
            json.dumps(
                value.continuity.as_dict() if value.continuity is not None else None,
                sort_keys=True,
            ),
        )
        for value in ordered
    }
    if len(signatures) != 1:
        raise EvidenceInventoryError(
            f"raw SHA {primary.raw_data_sha256} has conflicting scientific metadata"
        )
    aliases = [
        {
            "artifact_id": value.artifact_id,
            "relative_directory": value.relative_directory,
            "metadata_path": value.metadata_path,
            "metadata_sha256": value.metadata_sha256,
        }
        for value in ordered[1:]
    ]
    source_references = sorted(
        {
            (reference.kind, reference.path, reference.sha256, reference.json_pointer): reference
            for value in ordered
            for reference in value.source_references
        }.values(),
        key=lambda value: (value.path, value.json_pointer, value.kind),
    )
    companion_analyses = sorted(
        {
            str(analysis["path"]): analysis
            for value in ordered
            for analysis in value.companion_analyses
        }.values(),
        key=lambda value: str(value["path"]),
    )
    continuity = primary.continuity
    return {
        "artifact_id": primary.artifact_id,
        "artifact_aliases_after_raw_sha_deduplication": aliases,
        "relative_directory": primary.relative_directory,
        "metadata_path": primary.metadata_path,
        "data_path": primary.data_path,
        "metadata_sha256": primary.metadata_sha256,
        "raw_data_sha256": primary.raw_data_sha256,
        "raw_data_size_bytes": primary.raw_data_size_bytes,
        "created_at": primary.created_at,
        "center_frequency_hz": EXACT_CENTER_FREQUENCY_HZ,
        "sample_rate_hz": EXACT_SAMPLE_RATE_HZ,
        "datatype": EXACT_DATATYPE,
        "receiver_count": EXACT_RECEIVER_COUNT,
        "channels": [0, 1],
        "receiver_gain_db": primary.receiver_gain_db,
        "tx_hardware_gain_db": primary.tx_hardware_gain_db,
        "tx_channel": primary.tx_channel,
        "sample_count": primary.sample_count,
        "family": primary.family,
        "role": primary.role,
        "source_references": [reference.as_dict() for reference in source_references],
        "companion_analyses": companion_analyses,
        "continuity": {
            "present": continuity is not None,
            "validated": continuity is not None,
            "summary": continuity.as_dict() if continuity is not None else None,
        },
    }


def _count_document(values: Iterable[int | float | str]) -> dict[str, int]:
    counts = Counter(values)
    return {str(key): counts[key] for key in sorted(counts, key=str)}


def _assert_no_absolute_paths(value: object, label: str = "inventory") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"path", "data_path", "metadata_path", "relative_directory"} and (
                isinstance(child, str) and Path(child).is_absolute()
            ):
                raise EvidenceInventoryError(f"{label}.{key} embeds an absolute path")
            _assert_no_absolute_paths(child, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_absolute_paths(child, f"{label}[{index}]")


def build_evidence_inventory(
    board_state_root: Path,
    *,
    generator_bindings: Sequence[Mapping[str, str]],
    expected_family_counts: Mapping[str, int] | None = CURRENT_CORPUS_FAMILY_COUNTS,
) -> dict[str, object]:
    """Scan, validate, classify, raw-SHA-dedupe, and summarize one board-state root."""

    try:
        root = board_state_root.resolve(strict=True)
    except OSError as error:
        raise EvidenceInventoryError(f"cannot resolve board-state root: {error}") from error
    if not root.is_dir():
        raise EvidenceInventoryError("board-state root must be a directory")
    claims = _build_claim_index(root)
    exact_metadata = _exact_5g8_metadata(root)
    captures = [
        _validate_capture(path, metadata, root, claims) for path, metadata in exact_metadata
    ]
    claimed_exact_ids = {
        artifact_id
        for artifact_id in claims
        if any(claim.family in FAMILY_ORDER for claim in claims[artifact_id])
    }
    if claimed_exact_ids:
        preview = ", ".join(sorted(claimed_exact_ids)[:5])
        raise EvidenceInventoryError(
            f"authoritative 5.8 GHz sources refer to missing SigMF artifacts: {preview}"
        )
    artifact_ids = [capture.artifact_id for capture in captures]
    if len(set(artifact_ids)) != len(artifact_ids):
        raise EvidenceInventoryError("one artifact ID names multiple exact-5.8 metadata records")
    by_raw_sha: dict[str, list[ValidatedCapture]] = defaultdict(list)
    for capture in captures:
        by_raw_sha[capture.raw_data_sha256].append(capture)
    documents = [
        _capture_document(by_raw_sha[digest]) for digest in sorted(by_raw_sha)
    ]
    documents.sort(key=lambda value: str(value["artifact_id"]))
    family_counts = Counter(str(document["family"]) for document in documents)
    if expected_family_counts is not None:
        expected = {family: int(expected_family_counts.get(family, 0)) for family in FAMILY_ORDER}
        actual = {family: family_counts.get(family, 0) for family in FAMILY_ORDER}
        if actual != expected:
            raise EvidenceInventoryError(
                f"exact-5.8 family counts changed: expected {expected}, observed {actual}"
            )
    duplicate_groups = [
        {
            "raw_data_sha256": digest,
            "artifact_ids": sorted(value.artifact_id for value in group),
            "metadata_record_count": len(group),
        }
        for digest, group in sorted(by_raw_sha.items())
        if len(group) > 1
    ]
    raw_sizes = [
        _integer(document.get("raw_data_size_bytes"), "raw_data_size_bytes")
        for document in documents
    ]
    sample_counts = [
        _integer(document.get("sample_count"), "sample_count") for document in documents
    ]
    continuity_present = sum(
        bool(_mapping(document["continuity"], "continuity")["present"])
        for document in documents
    )
    family_count_document = {family: family_counts.get(family, 0) for family in FAMILY_ORDER}
    inventory: dict[str, object] = {
        "schema": 1,
        "evidence_kind": "exact_5g8_local_raw_evidence_inventory",
        "scope": {
            "root_reference": "supplied_board_state_root",
            "board_id": root.name,
            "center_frequency_hz": EXACT_CENTER_FREQUENCY_HZ,
            "selection": "all SigMF metadata whose complete capture list is exactly 5.8 GHz",
        },
        "generator": {
            "hash_binding_is_non_self_referential": True,
            "binding_policy": (
                "SHA-256 binds generator sources only; this output is intentionally not "
                "embedded in its own hash domain"
            ),
            "sources": [dict(binding) for binding in generator_bindings],
            "canonical_json": "UTF-8, sorted keys, indent=2, one trailing newline",
        },
        "validation_contract": {
            "datatype": EXACT_DATATYPE,
            "receiver_count": EXACT_RECEIVER_COUNT,
            "channels": [0, 1],
            "sample_rate_hz": EXACT_SAMPLE_RATE_HZ,
            "bytes_per_time_sample": (
                EXACT_RECEIVER_COUNT * BYTES_PER_COMPLEX_SAMPLE_PER_RECEIVER
            ),
            "raw_sha_policy": "computed SHA-256 must equal global pluto:sha256",
            "size_policy": (
                "raw bytes must equal sample_count * receiver_count * 4-byte CI16 sample"
            ),
            "continuity_policy": (
                "validate every present pluto:continuity ledger with strict ABI2 sequence, "
                "stream, missing-sample, block, span, and total-count checks"
            ),
            "classification_policy": (
                "qualification, sweep, permutation, and dual-TX manifests take precedence; "
                "the sole predecessor is identified by its SigMF phase description only after "
                "all manifest-backed artifact identities are claimed"
            ),
        },
        "overlap_and_deduplication": {
            "canonical_identity_key": "raw_data_sha256",
            "policy": (
                "validate every metadata record first, then group identical raw SHA-256 values; "
                "retain the lexicographically first artifact identity and record all other "
                "metadata identities as aliases; source/package references never increase the "
                "unique-capture count"
            ),
            "cross_family_duplicate_policy": "fail closed",
            "duplicate_raw_sha_groups": duplicate_groups,
        },
        "aggregate_invariants": {
            "sigmf_metadata_record_count": len(captures),
            "unique_artifact_id_count": len(set(artifact_ids)),
            "unique_raw_capture_count": len(documents),
            "unique_raw_sha256_count": len(by_raw_sha),
            "total_unique_raw_data_bytes": sum(raw_sizes),
            "family_counts_after_raw_sha_deduplication": family_count_document,
            "sample_count_distribution": _count_document(sample_counts),
            "raw_data_size_distribution_bytes": _count_document(raw_sizes),
            "continuity_ledger_present_count": continuity_present,
            "continuity_ledger_validated_count": continuity_present,
            "all_artifact_id_path_checks_passed": True,
            "all_raw_size_checks_passed": True,
            "all_declared_raw_sha256_checks_passed": True,
            "all_source_identity_checks_passed": True,
            "absolute_paths_embedded": False,
        },
        "captures": documents,
    }
    _assert_no_absolute_paths(inventory)
    return inventory


def canonical_json_bytes(document: Mapping[str, object]) -> bytes:
    """Serialize an inventory deterministically."""

    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


__all__ = [
    "CURRENT_CORPUS_FAMILY_COUNTS",
    "EXACT_CENTER_FREQUENCY_HZ",
    "EvidenceInventoryError",
    "FAMILY_DUAL_TX",
    "FAMILY_EARLY_PHASE",
    "FAMILY_ORDER",
    "FAMILY_PERMUTATION",
    "FAMILY_ROTATION",
    "FAMILY_RX_GAIN",
    "FAMILY_TX_GAIN",
    "build_evidence_inventory",
    "canonical_json_bytes",
    "sha256_file",
]
