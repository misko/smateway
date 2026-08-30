from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/flash_and_attest_selector.py"
SPEC = importlib.util.spec_from_file_location("flash_and_attest_selector_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
cli = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cli
SPEC.loader.exec_module(cli)


def _common_arguments(role: str = "bench") -> list[str]:
    arguments = [
        "--campaign-id",
        "5p8-debug-r1",
        "--run-id",
        "flash-r01",
        "--board-id",
        "stm32c011-4c0055000950313950363920",
        "--image-role",
        role,
        "--elf",
        f"build/STM32C011F4P6/{role}/image.elf",
        "--bin",
        f"build/STM32C011F4P6/{role}/image.bin",
        "--openocd-config",
        "openocd/rpi4-swd.cfg",
        "--evidence-root",
        "/tmp/selector-flash-evidence",
        "--pluto-serial",
        "104000b29905000e17000800065934759d",
        "--pluto-uri",
        "usb:1.2.3",
        "--pluto-mute-evidence",
        "/operator/phase-specific-pluto-mute.json",
    ]
    if role == "bench":
        arguments += ["--build-manifest", "build/bench.manifest.json"]
    else:
        arguments += ["--profile", "profiles/fast20-v1/control_profile.json"]
    return arguments


def test_phase1_cli_requires_pre_program_attestation(capsys: Any) -> None:
    result = cli.main([*_common_arguments(), "--prepare-and-program"])
    assert result == 2
    assert "requires --pre-program-attestation" in capsys.readouterr().err


def test_operational_cli_requires_exact_pluto_mute_checkpoint(capsys: Any) -> None:
    arguments = _common_arguments()
    mute_index = arguments.index("--pluto-mute-evidence")
    del arguments[mute_index : mute_index + 2]
    result = cli.main(
        [
            *arguments,
            "--pre-program-attestation",
            "/operator/pre-program.json",
            "--prepare-and-program",
        ]
    )
    assert result == 2
    assert "--pluto-mute-evidence" in capsys.readouterr().err


def test_cli_creates_editable_pre_program_template_without_operational_paths(
    tmp_path: Path,
    capsys: Any,
) -> None:
    output_path = tmp_path / "pre-program-attestation.json"
    result = cli.main(
        [
            "--campaign-id",
            "5p8-debug-r1",
            "--run-id",
            "flash-r01",
            "--board-id",
            "stm32c011-4c0055000950313950363920",
            "--image-role",
            "bench",
            "--write-pre-program-attestation-template",
            str(output_path),
        ]
    )

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    template = json.loads(output_path.read_text(encoding="utf-8"))
    assert output["status"] == "template_created"
    assert output["target_access"] is False
    assert output["rf_activity"] is False
    assert output["pre_program_attestation_template_path"] == str(output_path)
    assert template["campaign_id"] == "5p8-debug-r1"
    assert template["run_id"] == "flash-r01"
    assert template["supply_displayed_current_a"] is None
    assert output_path.stat().st_mode & 0o200


def test_cli_template_generation_refuses_overwrite(tmp_path: Path, capsys: Any) -> None:
    output_path = tmp_path / "pre-program-attestation.json"
    arguments = [
        "--campaign-id",
        "5p8-debug-r1",
        "--run-id",
        "flash-r01",
        "--board-id",
        "stm32c011-4c0055000950313950363920",
        "--image-role",
        "bench",
        "--write-pre-program-attestation-template",
        str(output_path),
    ]
    assert cli.main(arguments) == 0
    capsys.readouterr()

    assert cli.main(arguments) == 2
    assert "refusing to overwrite" in capsys.readouterr().err


def test_cli_routes_hardware_inert_power_cycle_seal(monkeypatch: Any, capsys: Any) -> None:
    observed: dict[str, Any] = {}

    def fake_seal(**kwargs: Any) -> Path:
        observed.update(kwargs)
        return Path("/evidence/run/power-cycle-attestation.json")

    monkeypatch.setattr(cli, "seal_power_cycle_attestation", fake_seal)
    monkeypatch.setattr(cli, "sha256_path", lambda _path: "c" * 64)
    result = cli.main(
        [
            "--campaign-id",
            "5p8-debug-r1",
            "--run-id",
            "flash-r01",
            "--board-id",
            "stm32c011-4c0055000950313950363920",
            "--image-role",
            "fast20",
            "--evidence-root",
            "/evidence",
            "--power-cycle-draft",
            "/evidence/run/power-cycle-attestation.template.json",
            "--seal-power-cycle-attestation",
        ]
    )

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "power_cycle_attestation_sealed"
    assert output["target_access"] is False
    assert output["rf_activity"] is False
    assert observed["power_cycle_draft"] == Path(
        "/evidence/run/power-cycle-attestation.template.json"
    )


def test_cli_power_cycle_seal_forbids_operational_paths(capsys: Any) -> None:
    result = cli.main(
        [
            "--campaign-id",
            "5p8-debug-r1",
            "--run-id",
            "flash-r01",
            "--board-id",
            "stm32c011-4c0055000950313950363920",
            "--image-role",
            "fast20",
            "--evidence-root",
            "/evidence",
            "--power-cycle-draft",
            "/evidence/run/power-cycle-attestation.template.json",
            "--pluto-uri",
            "usb:1.2.3",
            "--seal-power-cycle-attestation",
        ]
    )
    assert result == 2
    assert "hardware-inert" in capsys.readouterr().err


def test_phase2_cli_requires_power_cycle_attestation(capsys: Any) -> None:
    result = cli.main([*_common_arguments(), "--verify-after-power-cycle"])
    assert result == 2
    assert "requires --power-cycle-attestation" in capsys.readouterr().err


def test_phase1_cli_routes_exact_arguments_and_reports_template(
    monkeypatch: Any, capsys: Any
) -> None:
    observed: dict[str, Any] = {}

    def fake_prepare(**kwargs: Any) -> Any:
        observed.update(kwargs)
        return SimpleNamespace(
            run_directory=Path("/evidence/run"),
            phase1_path=Path("/evidence/run/phase1-programming-evidence.json"),
            phase1_sha256="a" * 64,
            power_cycle_template_path=Path("/evidence/run/power-cycle-attestation.template.json"),
        )

    monkeypatch.setattr(cli, "prepare_and_program", fake_prepare)
    result = cli.main(
        [
            *_common_arguments(),
            "--pre-program-attestation",
            "/operator/pre-program.json",
            "--prepare-and-program",
        ]
    )
    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "awaiting_power_cycle"
    assert observed["pre_program_attestation"] == Path("/operator/pre-program.json")
    assert observed["pluto_serial"] == "104000b29905000e17000800065934759d"
    assert observed["pluto_uri"] == "usb:1.2.3"
    assert observed["pluto_mute_evidence"] == Path("/operator/phase-specific-pluto-mute.json")
    assert observed["build_manifest"] == Path("build/bench.manifest.json")
    assert observed["profile"] is None


def test_phase1_cli_routes_current_image_match_gate(monkeypatch: Any, capsys: Any) -> None:
    observed: dict[str, Any] = {}

    def fake_prepare(**kwargs: Any) -> Any:
        observed.update(kwargs)
        return SimpleNamespace(
            run_directory=Path("/evidence/run"),
            phase1_path=Path("/evidence/run/phase1-programming-evidence.json"),
            phase1_sha256="a" * 64,
            power_cycle_template_path=Path("/evidence/run/power-cycle-attestation.template.json"),
        )

    monkeypatch.setattr(cli, "prepare_and_program", fake_prepare)
    result = cli.main(
        [
            *_common_arguments("fast20"),
            "--pre-program-attestation",
            "/operator/pre-program.json",
            "--require-current-image-match",
            "--prepare-and-program",
        ]
    )

    assert result == 0
    capsys.readouterr()
    assert observed["require_current_image_match"] is True


def test_phase2_cli_reports_downstream_path_and_hash(monkeypatch: Any, capsys: Any) -> None:
    observed: dict[str, Any] = {}

    def fake_verify(**kwargs: Any) -> Any:
        observed.update(kwargs)
        return SimpleNamespace(
            path=Path("/evidence/run/selector-flash-evidence.json"),
            sha256="b" * 64,
        )

    monkeypatch.setattr(cli, "verify_after_power_cycle", fake_verify)
    result = cli.main(
        [
            *_common_arguments("fast20"),
            "--power-cycle-attestation",
            "/operator/power-cycle.json",
            "--verify-after-power-cycle",
        ]
    )
    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "status": "passed",
        "selector_flash_evidence_path": "/evidence/run/selector-flash-evidence.json",
        "selector_flash_evidence_sha256": "b" * 64,
    }
    assert observed["power_cycle_attestation"] == Path("/operator/power-cycle.json")
    assert observed["profile"] == Path("profiles/fast20-v1/control_profile.json")
    assert observed["build_manifest"] is None


def test_cli_rejects_cross_phase_attestation_argument(capsys: Any) -> None:
    result = cli.main(
        [
            *_common_arguments(),
            "--pre-program-attestation",
            "/operator/pre-program.json",
            "--power-cycle-attestation",
            "/operator/power-cycle.json",
            "--prepare-and-program",
        ]
    )
    assert result == 2
    assert "only valid in phase 2" in capsys.readouterr().err
