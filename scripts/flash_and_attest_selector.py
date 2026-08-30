#!/usr/bin/env python3
"""Build, program, power-cycle, and attest one reviewed selector image."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE = REPOSITORY / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from smateway.selector_flash_attestation import (  # noqa: E402
    SelectorFlashError,
    prepare_and_program,
    seal_power_cycle_attestation,
    sha256_path,
    verify_after_power_cycle,
    write_pre_program_attestation_template,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--board-id", required=True)
    parser.add_argument("--image-role", choices=("bench", "fast20"), required=True)
    parser.add_argument("--elf", type=Path)
    parser.add_argument("--bin", dest="firmware_bin", type=Path)
    parser.add_argument("--build-manifest", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--openocd-config", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--pre-program-attestation", type=Path)
    parser.add_argument("--power-cycle-draft", type=Path)
    parser.add_argument("--power-cycle-attestation", type=Path)
    parser.add_argument("--pluto-serial")
    parser.add_argument("--pluto-uri")
    parser.add_argument("--pluto-mute-evidence", type=Path)
    parser.add_argument(
        "--require-current-image-match",
        action="store_true",
        help="before programming, read back the current BIN extent and refuse any byte mismatch",
    )
    phase = parser.add_mutually_exclusive_group(required=True)
    phase.add_argument("--write-pre-program-attestation-template", type=Path, metavar="PATH")
    phase.add_argument("--prepare-and-program", action="store_true")
    phase.add_argument("--seal-power-cycle-attestation", action="store_true")
    phase.add_argument("--verify-after-power-cycle", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.write_pre_program_attestation_template is not None:
            if (
                args.pre_program_attestation is not None
                or args.power_cycle_draft is not None
                or args.power_cycle_attestation is not None
                or args.require_current_image_match
                or args.pluto_serial is not None
                or args.pluto_uri is not None
                or args.pluto_mute_evidence is not None
            ):
                raise SelectorFlashError(
                    "operator attestation inputs are not valid while generating a template"
                )
            output = write_pre_program_attestation_template(
                args.write_pre_program_attestation_template,
                campaign_id=args.campaign_id,
                run_id=args.run_id,
                board_id=args.board_id,
                image_role=args.image_role,
            )
            print(
                json.dumps(
                    {
                        "status": "template_created",
                        "pre_program_attestation_template_path": str(output),
                        "pre_program_attestation_template_sha256": sha256_path(output),
                        "target_access": False,
                        "rf_activity": False,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.seal_power_cycle_attestation:
            forbidden = {
                "--elf": args.elf,
                "--bin": args.firmware_bin,
                "--build-manifest": args.build_manifest,
                "--profile": args.profile,
                "--openocd-config": args.openocd_config,
                "--pre-program-attestation": args.pre_program_attestation,
                "--power-cycle-attestation": args.power_cycle_attestation,
                "--pluto-serial": args.pluto_serial,
                "--pluto-uri": args.pluto_uri,
                "--pluto-mute-evidence": args.pluto_mute_evidence,
            }
            supplied_forbidden = [name for name, value in forbidden.items() if value is not None]
            if supplied_forbidden or args.require_current_image_match:
                raise SelectorFlashError(
                    "power-cycle sealing is hardware-inert and forbids operational arguments: "
                    + ", ".join(supplied_forbidden)
                )
            if args.evidence_root is None or args.power_cycle_draft is None:
                raise SelectorFlashError(
                    "--seal-power-cycle-attestation requires --evidence-root and "
                    "--power-cycle-draft"
                )
            output = seal_power_cycle_attestation(
                campaign_id=args.campaign_id,
                run_id=args.run_id,
                board_id=args.board_id,
                image_role=args.image_role,
                evidence_root=args.evidence_root,
                power_cycle_draft=args.power_cycle_draft,
            )
            print(
                json.dumps(
                    {
                        "status": "power_cycle_attestation_sealed",
                        "power_cycle_attestation_path": str(output),
                        "power_cycle_attestation_sha256": sha256_path(output),
                        "target_access": False,
                        "rf_activity": False,
                    },
                    sort_keys=True,
                )
            )
            return 0
        operational_paths = {
            "--elf": args.elf,
            "--bin": args.firmware_bin,
            "--openocd-config": args.openocd_config,
            "--evidence-root": args.evidence_root,
        }
        missing = [name for name, value in operational_paths.items() if value is None]
        mute_inputs = {
            "--pluto-serial": args.pluto_serial,
            "--pluto-uri": args.pluto_uri,
            "--pluto-mute-evidence": args.pluto_mute_evidence,
        }
        missing.extend(name for name, value in mute_inputs.items() if value is None)
        if missing:
            raise SelectorFlashError(
                "program/verify phase requires operational paths: " + ", ".join(missing)
            )
        assert args.elf is not None
        assert args.firmware_bin is not None
        assert args.openocd_config is not None
        assert args.evidence_root is not None
        assert args.pluto_serial is not None
        assert args.pluto_uri is not None
        assert args.pluto_mute_evidence is not None
        if args.power_cycle_draft is not None:
            raise SelectorFlashError(
                "--power-cycle-draft is only valid with --seal-power-cycle-attestation"
            )
        common = {
            "campaign_id": args.campaign_id,
            "run_id": args.run_id,
            "board_id": args.board_id,
            "image_role": args.image_role,
            "elf": args.elf,
            "firmware_bin": args.firmware_bin,
            "build_manifest": args.build_manifest,
            "profile": args.profile,
            "openocd_config": args.openocd_config,
            "evidence_root": args.evidence_root,
            "pluto_serial": args.pluto_serial,
            "pluto_uri": args.pluto_uri,
            "pluto_mute_evidence": args.pluto_mute_evidence,
            "repository": REPOSITORY,
        }
        if args.prepare_and_program:
            if args.pre_program_attestation is None:
                raise SelectorFlashError("--prepare-and-program requires --pre-program-attestation")
            if args.power_cycle_attestation is not None:
                raise SelectorFlashError("--power-cycle-attestation is only valid in phase 2")
            result = prepare_and_program(
                **common,
                pre_program_attestation=args.pre_program_attestation,
                python_executable=Path(sys.executable),
                require_current_image_match=args.require_current_image_match,
            )
            print(
                json.dumps(
                    {
                        "status": "awaiting_power_cycle",
                        "run_directory": str(result.run_directory),
                        "phase1_path": str(result.phase1_path),
                        "phase1_sha256": result.phase1_sha256,
                        "power_cycle_template_path": str(result.power_cycle_template_path),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.power_cycle_attestation is None:
            raise SelectorFlashError(
                "--verify-after-power-cycle requires --power-cycle-attestation"
            )
        if args.pre_program_attestation is not None:
            raise SelectorFlashError("--pre-program-attestation is only valid in phase 1")
        if args.require_current_image_match:
            raise SelectorFlashError("--require-current-image-match is only valid in phase 1")
        sealed = verify_after_power_cycle(
            **common,
            power_cycle_attestation=args.power_cycle_attestation,
        )
        print(
            json.dumps(
                {
                    "status": "passed",
                    "selector_flash_evidence_path": str(sealed.path),
                    "selector_flash_evidence_sha256": sealed.sha256,
                },
                sort_keys=True,
            )
        )
        return 0
    except SelectorFlashError as error:
        print(f"selector flash attestation failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
