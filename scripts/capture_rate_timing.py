#!/usr/bin/env python3
"""Serial-pinned muted, static and switched captures for the A–E rate comparison."""

from __future__ import annotations

# ruff: noqa: E402, I001
import argparse
import importlib
import os
import signal
import sys
import threading
import time
from contextlib import ExitStack
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

import numpy as np
from pluto_plus.hardware import IioRadioDevice
from pluto_plus.hardware.iio import _release_device
from pluto_plus.hardware.preflight import V7_FIRMWARE_VERSION, verify_metadata_runtime
from pluto_plus.models import GainMode, RadioSettings
from pluto_plus.tandem import TandemMode, TandemSessionRequestV1

from capture_fast_tracking_timing import (
    _enable_source,
    _lock,
    _mute_readback,
    _validate_flash,
    _write_json_atomic,
)
from smateway.bench import BenchManifest, OpenOcdBench
from smateway.campaign_protocol import admit_capture
from smateway.fast_tracking import FastTrackingProfile
from smateway.rate_timing import (
    CONFIGURATIONS,
    PORTS,
    RECEIVER_SERIAL,
    SOURCE_SERIAL,
    configuration_json,
    load,
    reference_summary,
    sha256,
    validate_block,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--configuration", choices=CONFIGURATIONS, required=True)
    result.add_argument("--mode", choices=("muted", "ambient", "static", "fast"), required=True)
    result.add_argument("--duration-s", type=float, default=4)
    result.add_argument("--frame-samples", type=int)
    result.add_argument("--gain-db", type=int, default=60, choices=range(0, 61))
    result.add_argument("--frequency-hz", type=int, default=5_800_000_000)
    result.add_argument("--port", choices=PORTS)
    result.add_argument("--tx-channel", type=int, choices=(0, 1), default=0)
    result.add_argument("--profile", type=Path)
    result.add_argument("--flash-evidence", type=Path)
    result.add_argument("--output-root", type=Path, required=True)
    result.add_argument("--receiver-uri", default="ip:192.168.1.15")
    result.add_argument("--source-uri", default="ip:192.168.1.179")
    result.add_argument("--tag", default="capture")
    result.add_argument("--protocol-json", type=Path)
    result.add_argument("--fixture-json", type=Path)
    result.add_argument("--acknowledge-ota-authorization", action="store_true")
    return result


def filter_facts(device):
    result = {}
    for name in ("ad9361-phy", "cf-ad9361-lpc", "tandem-agc"):
        item = device.ctx.find_device(name)
        if item is None:
            continue
        for key, attr in item.attrs.items():
            if any(x in key for x in ("path_rates", "filter", "sampling", "bandwidth")):
                try:
                    result[f"{name}/{key}"] = str(attr.value)
                except Exception as error:
                    result[f"{name}/{key}"] = {"unavailable": str(error)}
        for channel in item.channels:
            for key, attr in channel.attrs.items():
                if any(x in key for x in ("filter", "sampling", "bandwidth", "hardwaregain")):
                    label = f"{name}/{channel.id}/{'out' if channel.output else 'in'}/{key}"
                    try:
                        result[label] = str(attr.value)
                    except Exception as error:
                        result[label] = {"unavailable": str(error)}
    return result


def open_source(uri):
    """Do not admit a device to RF cleanup/control until its identity matches."""
    device = importlib.import_module("adi").ad9361(uri=uri)
    facts = {str(k): str(v) for k, v in device.ctx.attrs.items()}
    if facts.get("hw_serial") != SOURCE_SERIAL:
        _release_device(device)
        raise RuntimeError("source serial differs")
    return device, facts


def gain_telemetry(block):
    metadata = getattr(block, "tandem_metadata", None)
    if metadata is None:
        return {"available": False}
    names = (
        "tandem_state",
        "tandem_fault_flags",
        "initial_gain_db",
        "gain_table_id",
        "rx1_gain_index",
        "rx2_gain_index",
        "tandem_transition_count",
        "rx1_gain_db_start",
        "rx2_gain_db_start",
        "rx1_gain_db_end",
        "rx2_gain_db_end",
    )
    return {"available": True, **{k: getattr(metadata, k) for k in names if hasattr(metadata, k)}}


def record_interrupt(signum, events):
    events.append(
        {
            "signal": signal.Signals(signum).name,
            "number": int(signum),
            "received_at": datetime.now(UTC).isoformat(),
        }
    )
    raise KeyboardInterrupt()


def main() -> int:
    interrupt_events = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda received, _frame: record_interrupt(received, interrupt_events))
    args = parser().parse_args()
    cfg = CONFIGURATIONS[args.configuration]
    frame_samples, frames = cfg.frame_plan(args.duration_s)
    if args.frame_samples is not None:
        total = cfg.samples(args.duration_s)
        if not 1 <= args.frame_samples <= 250_000 or total % args.frame_samples:
            raise SystemExit("frame override must divide the capture and be at most 250000")
        frame_samples, frames = args.frame_samples, total // args.frame_samples
    campaign_binding = None
    if args.protocol_json is not None:
        campaign_binding = admit_capture(
            args.protocol_json,
            args.frequency_hz,
            muted=args.mode in ("muted", "ambient"),
            fixture_path=args.fixture_json,
        )
    elif not 5_726_000_000 <= args.frequency_hz <= 5_874_000_000:
        raise SystemExit("frequency outside the existing 5.8 GHz campaign")
    if args.mode in ("static", "fast") and not args.acknowledge_ota_authorization:
        raise SystemExit("RF capture requires acknowledgement")
    if args.mode in ("static", "ambient") and (args.port is None or args.tx_channel != 0):
        raise SystemExit("static/ambient acquisition requires a port and TX1 selection")
    profile = flash = None
    if args.mode == "fast":
        if args.profile is None or args.flash_evidence is None:
            raise SystemExit("fast capture requires profile and flash evidence")
        profile = FastTrackingProfile.load(args.profile)
        flash = _validate_flash(args.flash_evidence, profile)
    if not args.tag.replace("-", "").replace("_", "").isalnum():
        raise SystemExit("tag must be alphanumeric with hyphen/underscore")
    verify_metadata_runtime(2, expected_firmware_version=V7_FIRMWARE_VERSION)
    run_id = datetime.now(UTC).strftime(f"{args.tag}-%Y%m%dT%H%M%S.%fZ")
    output = args.output_root / "captures" / run_id
    output.mkdir(parents=True, exist_ok=False)
    record = {
        "schema": 1,
        "status": "running",
        "run_id": run_id,
        "evidence_kind": "sample_rate_timing_capture",
        "started_at": datetime.now(UTC).isoformat(),
        "configuration": {
            **configuration_json(args.configuration),
            "mode": args.mode,
            "duration_s": args.duration_s,
            "frame_samples": frame_samples,
            "frames": frames,
            "frequency_hz": args.frequency_hz,
            "tx_channel": args.tx_channel,
            "port": args.port,
            "dwell_us": profile.dwell_us if profile else None,
            "source_sample_rate_hz": 2_000_000,
            "source_bandwidth_hz": 1_600_000,
            "receiver_gain_db": args.gain_db,
        },
        "identities": {
            "receiver_serial": RECEIVER_SERIAL,
            "receiver_uri": args.receiver_uri,
            "source_serial": SOURCE_SERIAL,
            "source_uri": args.source_uri,
        },
        "campaign_binding": campaign_binding,
        "source_contract": {
            str(p.relative_to(ROOT)): sha256(p)
            for p in (
                Path(__file__).resolve(),
                ROOT / "src/smateway/rate_timing.py",
                ROOT / "src/smateway/campaign_protocol.py",
                ROOT / "scripts/capture_fast_tracking_timing.py",
            )
        },
        "flash": (
            {
                "path": str(args.flash_evidence.resolve()),
                "sha256": sha256(args.flash_evidence),
                "run_id": flash["run_id"],
            }
            if flash
            else None
        ),
        "safety": {},
        "capture": {},
        "error": None,
    }
    record["interrupt_events"] = interrupt_events
    _write_json_atomic(output / "run.json", record)
    radio = session = source = controller = None
    lease_stop = threading.Event()
    lease_thread = None
    lease_errors = []
    timeline = []
    previous = None
    component_peak = [0.0, 0.0]
    clips = [0, 0]
    chunk_times = []
    try:
        with _lock(args.output_root / ".hardware.lock"):
            source, facts = open_source(args.source_uri)
            record["source_identity"] = facts
            record["safety"]["initial_source_mute"] = _mute_readback(source)
            radio = IioRadioDevice(
                args.receiver_uri,
                serial=RECEIVER_SERIAL,
                expected_metadata_abi=2,
                expected_firmware_version=V7_FIRMWARE_VERSION,
            )
            radio.open()
            actual = radio.apply_settings(
                RadioSettings(
                    center_frequency_hz=args.frequency_hz,
                    sample_rate_hz=cfg.sample_rate_hz,
                    bandwidth_hz=cfg.bandwidth_hz,
                    gain_mode=GainMode.MANUAL,
                    gain_db=args.gain_db,
                    channels=(0, 1),
                )
            )
            if (
                actual.sample_rate_hz != cfg.sample_rate_hz
                or actual.bandwidth_hz != cfg.bandwidth_hz
                or abs(actual.center_frequency_hz - args.frequency_hz) > 5
                or actual.channels != (0, 1)
                or actual.gain_db != args.gain_db
            ):
                raise RuntimeError("RX settings readback differs")
            record["receiver_settings"] = actual.model_dump(mode="json")
            record["receiver_filters"] = filter_facts(radio._require_device())
            if args.mode in ("static", "muted", "ambient"):
                controller = OpenOcdBench(
                    BenchManifest.load(
                        ROOT / "build/STM32C011F4P6/bench/pluto_bench.manifest.json"
                    ),
                    ROOT / "openocd/stlink-v3-stm32c011.cfg",
                )
                if args.mode in ("static", "ambient"):
                    states = load(ROOT / "profiles/tracking-c6-200us-v1/control_profile.json")
                    code = next(
                        int(s["gpio_code_pa3_pa0"], 2)
                        for s in states["states"]
                        if s["name"] == args.port
                    )
                    if args.duration_s > 4:
                        raise ValueError("static reference duration exceeds the bounded lease")
                    status = controller.request(code, 5000, wait_until_applied=True)
                    if status.applied_code != code or not status.lease_active:
                        raise RuntimeError("static selector did not apply the requested port")

                    def renew_lease():
                        while not lease_stop.wait(1):
                            try:
                                observed = controller.request(code, 5000, wait_until_applied=True)
                                if not observed.lease_active or observed.applied_code != code:
                                    raise RuntimeError("static lease renewal failed")
                            except Exception as error:
                                lease_errors.append(str(error))
                                return

                    lease_thread = threading.Thread(target=renew_lease, daemon=True)
                    lease_thread.start()
                else:
                    status = controller.request(8, 0, wait_until_applied=False)
                record["selector_before_capture"] = status.as_dict()
            if args.mode in ("static", "fast"):
                record["source_settings"] = _enable_source(
                    source, args.frequency_hz, args.tx_channel
                )
            else:
                record["source_settings"] = {"mode": "muted"}
            time.sleep(1.0)
            session = radio.begin_metadata_capture(
                frame_samples,
                kernel_buffers=64,
                tandem_request=TandemSessionRequestV1(
                    mode=TandemMode.HOLD, initial_gain_db=args.gain_db
                ),
            )
            paths = [output / "rx1.cf32", output / "rx2.cf32"]
            started = time.monotonic()
            with ExitStack() as stack:
                streams = (
                    [stack.enter_context(p.open("xb")) for p in paths]
                    if args.mode != "muted"
                    else []
                )
                for _ in range(frames):
                    before = time.monotonic()
                    block = session.read_block()
                    try:
                        validate_block(block, previous, frame_samples)
                    except ValueError:
                        record["rejected_block"] = {
                            "buffer_sequence": block.buffer_sequence,
                            "first_sample_sequence": block.first_sample_sequence,
                            "last_sample_sequence_exclusive": block.last_sample_sequence_exclusive,
                            "missing_samples_before": block.missing_samples_before,
                            "overflow_observed": block.overflow_observed,
                            "metadata_flags": block.metadata_flags,
                        }
                        raise
                    received = time.monotonic()
                    chunk_times.append(received - before)
                    block_peaks = []
                    block_clips = []
                    for channel in (0, 1):
                        values = block.samples[channel]
                        if streams:
                            np.asarray(values, dtype=np.complex64).tofile(streams[channel])
                        block_peaks.append(
                            float(max(np.max(np.abs(values.real)), np.max(np.abs(values.imag))))
                        )
                        block_clips.append(
                            int(
                                np.count_nonzero(
                                    (np.abs(values.real) >= 2047) | (np.abs(values.imag) >= 2047)
                                )
                            )
                        )
                        component_peak[channel] = max(component_peak[channel], block_peaks[-1])
                        clips[channel] += block_clips[-1]
                    timeline.append(
                        {
                            "buffer_sequence": block.buffer_sequence,
                            "first_sample_sequence": block.first_sample_sequence,
                            "last_sample_sequence_exclusive": block.last_sample_sequence_exclusive,
                            "stream_id": block.stream_id,
                            "missing_samples_before": block.missing_samples_before,
                            "overflow_observed": block.overflow_observed,
                            "arrival_elapsed_s": received - started,
                            "peak_component_counts": block_peaks,
                            "clipped_samples": block_clips,
                            "gain_telemetry": gain_telemetry(block),
                        }
                    )
                    previous = block
                for stream in streams:
                    stream.flush()
                    os.fsync(stream.fileno())
            elapsed = time.monotonic() - started
            # End RF activity before offline work or hashing.
            record["safety"]["source_after_capture_mute"] = _mute_readback(source)
            session.close()
            session = None
            lease_stop.set()
            if lease_thread:
                lease_thread.join(timeout=5)
                if lease_thread.is_alive() or lease_errors:
                    raise RuntimeError(f"static lease failure: {lease_errors}")
                selected_status = controller.status()
                if selected_status.applied_code != code or not selected_status.lease_active:
                    raise RuntimeError("static port lease expired during capture")
            if controller is not None:
                status = controller.request(8, 0, wait_until_applied=False)
                record["selector_after_capture"] = status.as_dict()
            record["capture"] = {
                "samples_per_channel": frames * frame_samples,
                "timeline": timeline,
                "wall_time_s": elapsed,
                "acquired_duration_s": args.duration_s,
                "effective_samples_per_second": frames * frame_samples / elapsed,
                "read_block_p50_s": float(np.median(chunk_times)),
                "read_block_p95_s": float(np.percentile(chunk_times, 95)),
                "peak_component_counts": component_peak,
                "clipped_samples": clips,
                "raw": [
                    {"path": str(p), "sha256": sha256(p), "bytes": p.stat().st_size} for p in paths
                ]
                if args.mode != "muted"
                else [],
            }
            if any(clips):
                raise RuntimeError("full-scale samples observed")
            if args.mode == "static":
                one, two = [np.memmap(p, mode="r", dtype=np.complex64) for p in paths]
                record["reference"] = reference_summary(one, two, cfg.sample_rate_hz)
            record["status"] = "passed"
    except BaseException as error:
        record["status"] = "failed"
        record["error"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        lease_stop.set()
        if lease_thread:
            lease_thread.join(timeout=5)
        if source is not None:
            try:
                record["safety"]["final_source_mute"] = _mute_readback(source)
            except Exception as error:
                record["status"] = "failed"
                record["safety"]["mute_error"] = str(error)
        for name, action in (
            ("session", lambda: session.close() if session else None),
            ("selector", lambda: controller.request(8, 0) if controller else None),
            ("source", lambda: _release_device(source) if source else None),
            ("receiver", lambda: radio.close() if radio else None),
        ):
            try:
                action()
            except Exception as error:
                record["status"] = "failed"
                record["safety"][f"{name}_cleanup_error"] = str(error)
        record["completed_at"] = datetime.now(UTC).isoformat()
        if not record["capture"]:
            record["partial_timeline"] = timeline
        _write_json_atomic(output / "run.json", record)
        print(f"run_dir={output}", flush=True)
        if record["error"]:
            print(record["error"], flush=True)
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
