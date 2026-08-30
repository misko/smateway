from __future__ import annotations

import importlib.util
import re
import sys
from argparse import ArgumentParser
from pathlib import Path
from types import ModuleType

REPOSITORY = Path(__file__).resolve().parents[1]
PLAN = REPOSITORY / "5p8_ghz_debug_plan.md"
T8_WORKFLOW = REPOSITORY / "docs/5g8_root_cause_analysis/t8_selected_state_workflow.md"


def _load_script(name: str) -> ModuleType:
    path = REPOSITORY / f"scripts/{name}.py"
    specification = importlib.util.spec_from_file_location(f"{name}_operator_contract_test", path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _options(parser: ArgumentParser) -> set[str]:
    return {
        option
        for action in parser._actions
        for option in action.option_strings
        if option.startswith("--")
    }


def _section(text: str, heading: str, next_heading: str) -> str:
    start = text.index(heading)
    end = text.index(next_heading, start)
    return text[start:end]


def _bash_blocks(text: str) -> list[str]:
    return re.findall(r"```bash\n(.*?)\n```", text, flags=re.DOTALL)


def _long_options(text: str) -> set[str]:
    return set(re.findall(r"(?<![A-Za-z0-9_])--[a-z0-9-]+", text))


def test_p2_documented_commands_match_cli_and_exact_five_run_contract() -> None:
    plan = PLAN.read_text(encoding="utf-8")
    section = _section(
        plan,
        "### P2 input-drive-off command template",
        "### A/B/C/E hardened runner template",
    )
    blocks = _bash_blocks(section)
    normalizer = _load_script("analyze_5g8_input_off_cohort")
    generator = _load_script("generate_5g8_input_off_setup")
    runner = _load_script("run_5g8_input_off_control")

    normalize_block = next(block for block in blocks if "--normalize-p0" in block)
    generator_block = next(block for block in blocks if "generate_5g8_input_off_setup.py" in block)
    plan_block = next(
        block
        for block in blocks
        if "run_5g8_input_off_control.py" in block and "--plan-only" in block
    )
    execute_block = next(
        block
        for block in blocks
        if "run_5g8_input_off_control.py" in block and "--execute" in block
    )
    compare_block = next(block for block in blocks if "--compare" in block)

    assert normalize_block.count("scripts/analyze_5g8_input_off_cohort.py") == 5
    assert normalize_block.count("--normalize-p0") == 5
    assert plan_block.count("--p0-observation") == 5
    assert execute_block.count("--p0-observation") == 5
    assert compare_block.count("--p0-observation") == 5
    assert compare_block.count("--p2-observation") == 5
    assert compare_block.count("--p2-manifest") == 5
    assert "--bootstrap-replicates 32768" in compare_block
    assert "--seed 94904358" in compare_block

    assert _long_options(normalize_block) <= _options(normalizer._parser())
    assert _long_options(compare_block) <= _options(normalizer._parser())
    assert _long_options(generator_block) <= _options(generator._parser())
    assert _long_options(plan_block) <= _options(runner._parser())
    assert _long_options(execute_block) <= _options(runner._parser())


def test_every_documented_operational_flash_has_fresh_exact_pluto_mute_gate() -> None:
    plan = PLAN.read_text(encoding="utf-8")
    flash = _load_script("flash_and_attest_selector")
    mute = _load_script("attest_selector_flash_pluto_mute")
    operational_blocks = [
        block
        for block in _bash_blocks(plan)
        if "scripts/flash_and_attest_selector.py" in block
        and ("--prepare-and-program" in block or "--verify-after-power-cycle" in block)
    ]

    assert len(operational_blocks) == 6
    for block in operational_blocks:
        assert block.count("--pluto-serial") == 1
        assert block.count("--pluto-uri") == 1
        assert block.count("--pluto-mute-evidence") == 1
        flash_command = block[block.index("scripts/flash_and_attest_selector.py") :]
        assert _long_options(flash_command) <= _options(flash._parser())

    mute_blocks = [
        block
        for block in _bash_blocks(plan)
        if "scripts/attest_selector_flash_pluto_mute.py" in block
    ]
    mute_commands = "\n".join(mute_blocks)
    assert mute_commands.count("--checkpoint phase1_pre_openocd") == 3
    assert mute_commands.count("--checkpoint phase2_pre_openocd") == 3
    for block in mute_blocks:
        mute_command = block[block.index("scripts/attest_selector_flash_pluto_mute.py") :]
        if "scripts/flash_and_attest_selector.py" in mute_command:
            mute_command = mute_command[
                : mute_command.index("scripts/flash_and_attest_selector.py")
            ]
        assert _long_options(mute_command) <= _options(mute._parser())

    seal_blocks = [
        block for block in _bash_blocks(plan) if "--seal-power-cycle-attestation" in block
    ]
    assert len(seal_blocks) == 3
    for block in seal_blocks:
        assert "--power-cycle-draft" in block
        assert "--pluto-mute-evidence" not in block
        assert _long_options(block) <= _options(flash._parser())


def test_bench_phase_does_not_precreate_future_q_mute_evidence() -> None:
    plan = PLAN.read_text(encoding="utf-8")
    bench = _section(
        plan,
        "T5 must provide the fail-closed two-phase wrapper used below.",
        "### Post-fix Q firmware re-entry checkpoint",
    )
    q = plan[plan.index("### Post-fix Q firmware re-entry checkpoint") :]
    assert "fast20-restore-r01" not in bench
    assert q.count("--checkpoint phase1_pre_openocd") == 1
    assert q.count("fast20-restore-r01-20260829.phase1-pluto-mute.json") == 2


def test_t8_fast20_flash_uses_profile_fresh_mutes_and_sealed_power_authority() -> None:
    workflow = T8_WORKFLOW.read_text(encoding="utf-8")
    transition = _section(
        workflow,
        "### STOP — exact image transition and new attestation",
        "### Q2 — sealed Fast20 image: timing, then matrix",
    )
    assert "--build-manifest" not in transition
    assert transition.count('--profile "$PROFILE"') == 2
    assert transition.count("--checkpoint phase1_pre_openocd") == 1
    assert transition.count("--checkpoint phase2_pre_openocd") == 1
    assert transition.count('--pluto-serial "$SERIAL"') == 2
    assert transition.count('--pluto-uri "$URI"') == 2
    assert transition.count("--pluto-mute-evidence") == 2
    assert transition.count("--seal-power-cycle-attestation") == 1
    assert transition.count('--power-cycle-draft "$POWER_DRAFT"') == 1
