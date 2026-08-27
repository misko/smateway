from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/verify_hexcal_elf.py"
SPEC = importlib.util.spec_from_file_location("verify_hexcal_elf_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
VERIFIER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = VERIFIER
SPEC.loader.exec_module(VERIFIER)

FIXTURE = ROOT / "tests/hexcal_final_objdump_fixture.txt"
DISASSEMBLY = FIXTURE.read_text(encoding="utf-8")
ROGUE_GPIO_BOOT = """
08000480 <rogue_boot>:
 8000480: 20a0       movs r0, #160
 8000482: 05c0       lsls r0, r0, #23
 8000484: 2155       movs r1, #85
 8000486: 6001       str r1, [r0, #0]
 8000488: 4a01       ldr r2, [pc, #4] @ (8000490 <rogue_boot+0x10>)
 800048a: 6182       str r2, [r0, #24]
 800048c: 4770       bx lr
 800048e: 46c0       nop
 8000490: 00080001   .word 0x00080001
"""


def _mutate_in(disassembly: str, address: int, replacement: str) -> str:
    pattern = re.compile(
        rf"^(\s*{address:x}:\s+(?:[0-9a-f]{{4,8}}\s+)+).*$",
        re.MULTILINE,
    )
    matches = list(pattern.finditer(disassembly))
    assert len(matches) == 1, hex(address)
    return pattern.sub(lambda match: match.group(1) + replacement, disassembly)


def _mutate_instruction(address: int, replacement: str) -> str:
    return _mutate_in(DISASSEMBLY, address, replacement)


def _insert_after(address: int, instruction_line: str) -> str:
    pattern = re.compile(rf"^(\s*{address:x}:.*)$", re.MULTILINE)
    matches = list(pattern.finditer(DISASSEMBLY))
    assert len(matches) == 1, hex(address)
    return pattern.sub(lambda match: match.group(1) + "\n" + instruction_line, DISASSEMBLY)


def _rejects(disassembly: str, match: str) -> None:
    with pytest.raises(VERIFIER.HexcalElfVerificationError, match=match):
        VERIFIER.verify_main_timing_control_flow(disassembly)


def _uint_macro(source: str, name: str) -> int:
    match = re.search(
        rf"^#define\s+{re.escape(name)}\s+"
        rf"(?:UINT(?:16|32)_C\()?([0-9]+)(?:\))?u?\s*$",
        source,
        re.MULTILINE,
    )
    assert match is not None, name
    return int(match.group(1))


def _exact_vector_table() -> bytes:
    words = [0] * VERIFIER.VECTOR_TABLE_WORDS
    words[0] = VERIFIER.SRAM_END
    words[1] = 0x080003C5
    for index in VERIFIER.DEFAULT_VECTOR_INDICES:
        words[index] = 0x08000415
    return b"".join(word.to_bytes(4, "little") for word in words)


def test_accepts_exact_final_elf_control_flow() -> None:
    evidence = VERIFIER.verify_main_timing_control_flow(DISASSEMBLY)

    assert evidence.control_schedule_address == 0x08000424
    assert evidence.tight_poll_instruction_count == 5
    assert evidence.tight_poll_window_us == 8
    assert evidence.far_outer_sample_core_cycles == 54
    assert evidence.far_outer_sample_max_core_cycles == 54
    assert evidence.staging_entry_to_tight_sample_core_cycles == 22
    assert evidence.staging_entry_to_tight_sample_max_core_cycles == 22
    assert evidence.tight_poll_sample_core_cycles == 11
    assert evidence.tight_poll_sample_max_core_cycles == 11
    assert evidence.prewrite_max_lateness_us == 2
    assert evidence.deadline_to_final_sample_core_cycles == 23
    assert evidence.deadline_to_final_sample_max_core_cycles == 23
    assert evidence.gpio_write_path_core_cycles == 16
    assert evidence.gpio_write_max_core_cycles == 16
    assert evidence.transition_turnover_core_cycles == 165
    assert evidence.transition_turnover_max_core_cycles == 165
    assert evidence.shortest_phase_chain_max_core_cycles == 233
    assert evidence.shortest_phase_core_cycles == 240


def test_verifier_caps_are_bound_to_firmware_and_profile_constants() -> None:
    core = (ROOT / "firmware/stm32c011/core/high_rate_autonomous_core.h").read_text(
        encoding="utf-8"
    )
    profile = (ROOT / "profiles/hexcal-v1/control_profile.h").read_text(encoding="utf-8")

    assert _uint_macro(core, "CONTROL_TIGHT_POLL_WINDOW_US") == VERIFIER.TIGHT_POLL_WINDOW_US
    assert (
        _uint_macro(core, "CONTROL_FAR_POLL_SAMPLE_MAX_CORE_CYCLES")
        == VERIFIER.FAR_OUTER_SAMPLE_MAX_CORE_CYCLES
    )
    assert (
        _uint_macro(core, "CONTROL_STAGING_TO_TIGHT_SAMPLE_MAX_CORE_CYCLES")
        == VERIFIER.STAGING_ENTRY_TO_TIGHT_SAMPLE_MAX_CORE_CYCLES
    )
    assert (
        _uint_macro(core, "CONTROL_TIGHT_POLL_SAMPLE_MAX_CORE_CYCLES")
        == VERIFIER.TIGHT_POLL_SAMPLE_MAX_CORE_CYCLES
    )
    assert (
        _uint_macro(core, "CONTROL_DUE_SAMPLE_TO_FINAL_SAMPLE_MAX_CORE_CYCLES")
        == VERIFIER.DEADLINE_TO_FINAL_SAMPLE_MAX_CORE_CYCLES
    )
    assert (
        _uint_macro(core, "CONTROL_GPIO_WRITE_MAX_CORE_CYCLES")
        == VERIFIER.GPIO_WRITE_MAX_CORE_CYCLES
    )
    assert (
        _uint_macro(core, "CONTROL_COUNTER_QUANTIZATION_TICKS")
        == VERIFIER.COUNTER_QUANTIZATION_TICKS
    )
    assert (
        _uint_macro(core, "CONTROL_ENDPOINT_MEMORY_ACCESS_CORE_CYCLES")
        == VERIFIER.ENDPOINT_MEMORY_ACCESS_CORE_CYCLES
    )
    assert (
        _uint_macro(core, "CONTROL_TRANSITION_TURNOVER_MAX_CORE_CYCLES")
        == VERIFIER.TRANSITION_TURNOVER_MAX_CORE_CYCLES
    )
    assert _uint_macro(profile, "CONTROL_GUARD_US") == VERIFIER.SHORTEST_PHASE_US
    assert VERIFIER.DEADLINE_TO_GPIO_MAX_CORE_CYCLES == 52
    assert VERIFIER.SHORTEST_PHASE_CHAIN_MAX_CORE_CYCLES == 233


@pytest.mark.parametrize(
    "replacement",
    ("sub sp, #0", "sub sp, #8", "add sp, #44", "nop"),
)
def test_rejects_inexact_main_stack_prologue(replacement: str) -> None:
    _rejects(_mutate_instruction(0x800011A, replacement), "main entry prologue")


def test_rejects_external_entry_after_main_stack_allocation() -> None:
    changed = (
        DISASSEMBLY
        + """
08000480 <adversarial_main_entry>:
 8000480: e000       b.n 80001e4 <main+0xcc>
"""
    )
    _rejects(changed, "external interior control-flow entry")


@pytest.mark.parametrize(
    ("address", "replacement", "message"),
    (
        (0x8000124, "bics r3, r6", "GPIOA IOPENR enable operation"),
        (0x8000126, "str r3, [r5, #48]", "GPIOA IOPENR enable store"),
        (0x800013A, "str r6, [r3, #24]", "GPIOA preload does not write"),
        (0x8000142, "cmp r3, #1", "preload ODR comparison"),
        (0x8000144, "beq.n 8000234", "preload ODR mismatch"),
        (0x8000146, "bl 80000c0 <clock_register_configuration_valid>", "reset-clock"),
        (0x8000150, "bne.n 8000234", "invalid reset RCC"),
        (0x800015A, "beq.n 8000234", "invalid reset SystemCoreClock"),
        (0x8000160, "bics r2, r3", "TIM3 APBENR1 enable operation"),
        (0x800016C, "bne.n 8000234", "TIM3 APBENR1 enable failure"),
        (0x8000170, "bics r2, r3", "TIM3 reset assert operation"),
        (0x800017C, "bne.n 8000234", "TIM3 reset-assert failure"),
        (0x8000180, "orrs r2, r3", "TIM3 reset deassert operation"),
        (0x8000190, "beq.n 8000234", "TIM3 reset-deassert failure"),
    ),
)
def test_rejects_boot_clock_gpio_and_tim3_mutations(
    address: int, replacement: str, message: str
) -> None:
    _rejects(_mutate_instruction(address, replacement), message)


@pytest.mark.parametrize(
    ("address", "replacement", "message"),
    (
        (0x80001E0, "cmp r2, #1", "post-output GPIOA ODR comparison"),
        (0x80001E2, "beq.n 8000238", "post-output GPIOA ODR mismatch"),
        (0x800023A, "str r6, [r3, #24]", "failure loop does not write ALL_OFF"),
    ),
)
def test_rejects_post_output_all_off_mutations(
    address: int, replacement: str, message: str
) -> None:
    _rejects(_mutate_instruction(address, replacement), message)


@pytest.mark.parametrize(
    ("address", "replacement", "message"),
    (
        (0x80001E4, "movs r6, r6", "live frame is not materialized"),
        (0x80001E4, "b.n 80001ec", "live frame is not materialized"),
        (0x80001E6, "movs r0, r7", "argument provenance"),
        (
            0x80001E8,
            "bl 80002ee <high_rate_frame_init+0x2>",
            "reinitialize the full marker",
        ),
    ),
)
def test_rejects_live_frame_initialization_mutations(
    address: int, replacement: str, message: str
) -> None:
    _rejects(_mutate_instruction(address, replacement), message)


def test_rejects_cycle_result_aliasing_the_planned_bsrr_slot() -> None:
    changed = _mutate_in(DISASSEMBLY, 0x800020A, "str r0, [sp, #12]")
    changed = _mutate_in(changed, 0x80002B0, "ldr r3, [sp, #12]")
    _rejects(changed, "exact dedicated stack slot")


def test_accepts_exact_binary_vector_table() -> None:
    VERIFIER.verify_vector_table_bytes(
        _exact_vector_table(),
        reset_handler_address=0x080003C4,
        default_handler_address=0x08000414,
    )


@pytest.mark.parametrize(
    ("word_index", "value", "message"),
    (
        (0, 0x200017FC, "initial stack pointer"),
        (1, 0x080003C1, "reset vector"),
        (2, 0x08000365, "exception/IRQ vectors"),
        (4, 0x08000415, "exception/IRQ vectors"),
    ),
)
def test_rejects_binary_vector_table_mutations(word_index: int, value: int, message: str) -> None:
    changed = bytearray(_exact_vector_table())
    changed[word_index * 4 : word_index * 4 + 4] = value.to_bytes(4, "little")
    with pytest.raises(VERIFIER.HexcalElfVerificationError, match=message):
        VERIFIER.verify_vector_table_bytes(
            bytes(changed),
            reset_handler_address=0x080003C4,
            default_handler_address=0x08000414,
        )


def test_rejects_executable_text_digest_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text_section = bytes(index % 251 for index in range(VERIFIER.TEXT_SECTION_SIZE))
    monkeypatch.setattr(
        VERIFIER,
        "EXPECTED_TEXT_SHA256",
        hashlib.sha256(text_section).hexdigest(),
    )
    VERIFIER.verify_text_section_bytes(text_section)
    changed = bytearray(text_section)
    changed[17] ^= 1
    with pytest.raises(VERIFIER.HexcalElfVerificationError, match="digest"):
        VERIFIER.verify_text_section_bytes(bytes(changed))


def test_rejects_wrong_vector_table_section_size() -> None:
    with pytest.raises(VERIFIER.HexcalElfVerificationError, match="size"):
        VERIFIER.verify_vector_table_bytes(
            _exact_vector_table()[:-4],
            reset_handler_address=0x080003C4,
            default_handler_address=0x08000414,
        )


def test_rejects_nonterminal_default_handler() -> None:
    changed = _mutate_instruction(0x8000414, "b.n 8000418 <HexcalStartupSystemInit>")
    with pytest.raises(VERIFIER.HexcalElfVerificationError, match="terminal self-loop"):
        VERIFIER.verify_default_handler_terminal_loop(
            changed,
            default_handler_address=0x08000414,
        )


@pytest.mark.parametrize(
    ("address", "replacement", "message"),
    (
        (0x80003C4, "ldr r0, [pc, #48] @ (80003f8)", "initial stack pointer"),
        (
            0x80003C8,
            "bl 800041a <HexcalStartupSystemInit+0x2>",
            "owned erratum wrapper",
        ),
        (0x800041A, "ldr r1, [r0, #4]", "first reset-time data access"),
        (0x800041C, "b.n 8000366 <SystemInit+0x2>", "tail-branch directly"),
    ),
)
def test_rejects_reset_erratum_workaround_mutations(
    address: int, replacement: str, message: str
) -> None:
    _rejects(_mutate_instruction(address, replacement), message)


@pytest.mark.parametrize(
    ("address", "message"),
    (
        (0x80003CC, "data-copy offset seed"),
        (0x8000362, "__libc_init_array body"),
        (0x8000364, "SystemInit body"),
        (0x8000374, "SystemCoreClockUpdate body"),
    ),
)
def test_rejects_rogue_gpio_call_from_every_pre_output_callee(address: int, message: str) -> None:
    changed = _mutate_instruction(address, "bl 8000480 <rogue_boot>") + ROGUE_GPIO_BOOT
    _rejects(changed, message)


def test_forbidden_access_scanner_rejects_external_gpio_writer() -> None:
    with pytest.raises(
        VERIFIER.HexcalElfVerificationError,
        match="unapproved GPIOA write outside main",
    ):
        VERIFIER.reject_forbidden_peripheral_access(DISASSEMBLY + ROGUE_GPIO_BOOT)


@pytest.mark.parametrize(
    ("address", "replacement", "message"),
    (
        (0x80003CE, "b.n 80003d0 <CopyDataInit>", "data-copy bound check"),
        (0x80003E0, "bcs.n 80003d0 <CopyDataInit>", "data-copy loop branch"),
        (0x80003E4, "b.n 80003e6 <FillZerobss>", "BSS bound check"),
        (0x80003F0, "bcs.n 80003e6 <FillZerobss>", "BSS fill loop branch"),
        (0x80003F2, "bl 8000118 <main>", "libc initialization call"),
        (0x80003F6, "bl 800011a <main+0x2>", "main call"),
        (0x80003FA, "b.n 8000414 <ADC1_IRQHandler>", "LoopForever"),
    ),
)
def test_rejects_reset_tail_cfg_and_call_bypasses(
    address: int, replacement: str, message: str
) -> None:
    _rejects(_mutate_instruction(address, replacement), message)


@pytest.mark.parametrize(
    ("address", "replacement", "message"),
    (
        (0x8000370, ".word 0x50000000", "SystemInit body"),
        (0x8000400, ".word 0x50000000", "_sidata literal"),
        (0x8000404, ".word 0x50000000", "_sdata/_edata literals"),
        (0x8000408, ".word 0x50000000", "_sdata/_edata literals"),
        (0x800040C, ".word 0x50000000", "_sbss literal"),
        (0x8000410, ".word 0x50000000", "_ebss literal"),
    ),
)
def test_rejects_pre_main_pointer_literal_mutations(
    address: int, replacement: str, message: str
) -> None:
    _rejects(_mutate_instruction(address, replacement), message)


@pytest.mark.parametrize(
    ("address", "replacement", "message"),
    (
        (0x80000C0, "movs r1, #243", "HSI mask seed"),
        (0x80000C6, "ldr r2, [r3, #4]", "does not read RCC CR"),
        (0x80000D2, "cmp r2, r2", "CR comparison"),
        (0x80000D4, "beq.n 80000de", "CR mismatch"),
        (0x80000E4, ".word 0x00007f3e", "CFGR policy mask"),
    ),
)
def test_rejects_full_rcc_validator_mutations(address: int, replacement: str, message: str) -> None:
    _rejects(_mutate_instruction(address, replacement), message)


@pytest.mark.parametrize(
    ("address", "replacement", "message"),
    (
        (0x8000194, "adds r3, #8", "prescaler calculation"),
        (0x8000198, "str r3, [r4, #8]", "DIER zero store"),
        (0x80001AA, "cmp r2, #12", "comparison is not exactly 11"),
        (0x80001B8, "beq.n 8000234", "DIER mismatch"),
        (0x80001BC, "tst r3, r3", "CEN readback test"),
        (0x80001B2, "beq.n 8000234", "TIM3 ARR mismatch"),
    ),
)
def test_rejects_tim3_initialization_or_fail_closed_mutations(
    address: int, replacement: str, message: str
) -> None:
    _rejects(_mutate_instruction(address, replacement), message)


@pytest.mark.parametrize(
    ("address", "replacement", "message"),
    (
        (0x80001CC, "orrs r2, r0", "OTYPER is not cleared"),
        (0x80001D6, "subs r2, #169", "output-mode value"),
        (0x80001D8, "eors r2, r1", "output-mode merge"),
        (0x80001DA, "str r2, [r6, #4]", "MODER write"),
    ),
)
def test_rejects_gpio_output_configuration_mutations(
    address: int, replacement: str, message: str
) -> None:
    _rejects(_mutate_instruction(address, replacement), message)


@pytest.mark.parametrize(
    ("address", "replacement", "message"),
    (
        (0x80000F2, "movs r2, #1", "IWDG /4 prescaler"),
        (0x80000F6, "adds r2, #126", "IWDG reload"),
        (0x800010C, ".word 0x0000cccd", "IWDG enable key"),
        (0x8000114, ".word 0x0000aaab", "IWDG refresh key"),
        (
            0x80001C0,
            "bl 80000ea <watchdog_initialize+0x2>",
            "exactly once",
        ),
    ),
)
def test_rejects_watchdog_mutations(address: int, replacement: str, message: str) -> None:
    _rejects(_mutate_instruction(address, replacement), message)


def test_rejects_interrupt_reenable_or_missing_disable() -> None:
    _rejects(_mutate_instruction(0x800011C, "nop"), "disable interrupts")
    changed = _insert_after(0x800011C, " 800011d: b662       cpsie i")
    _rejects(changed, "interrupt disable")


@pytest.mark.parametrize(
    ("address", "replacement", "message"),
    (
        (0x8000268, "ldr r3, [r4, #32]", "five-instruction TIM3 deadline poll"),
        (0x800026A, "ldr r2, [sp, #4]", "exact dedicated stack slot"),
        (0x8000270, "bpl.n 8000268", "five-instruction TIM3 deadline poll"),
        (0x8000250, "cmp r3, #7", "staging window is not 8 us"),
        (0x8000252, "bhi.n 8000268", "staging-window admission"),
    ),
)
def test_rejects_tight_poll_or_staging_mutations(
    address: int, replacement: str, message: str
) -> None:
    _rejects(_mutate_instruction(address, replacement), message)


def test_rejects_adversarial_padded_tight_poll() -> None:
    changed = _insert_after(0x800026A, " 800026b: 46c0       nop")
    _rejects(changed, "five-instruction TIM3 deadline poll")


@pytest.mark.parametrize(
    ("address", "replacement", "message"),
    (
        (
            0x8000220,
            "bl 80002ec <high_rate_frame_init>",
            "mandatory full RCC prevalidation",
        ),
        (0x8000226, "beq.n 800023e", "mandatory full RCC prevalidation"),
        (0x800022E, "str r2, [r3, #20]", "mandatory pre-poll.*ALL_OFF"),
        (
            0x8000254,
            "bl 80002ec <high_rate_frame_init>",
            "far-deadline path lacks",
        ),
        (0x800025A, "beq.n 800023e", "only a valid far-deadline"),
        (0x8000262, "str r2, [r3, #20]", "far-deadline.*ALL_OFF"),
    ),
)
def test_rejects_mandatory_or_far_rcc_gate_mutations(
    address: int, replacement: str, message: str
) -> None:
    _rejects(_mutate_instruction(address, replacement), message)


@pytest.mark.parametrize(
    ("address", "replacement", "message"),
    (
        (0x8000272, "movs r3, #167", "expected signature seed"),
        (0x8000274, "ldr r2, [r5, #4]", "does not read RCC CR"),
        (0x8000276, "movs r1, #243", "signature mask seed"),
        (0x8000284, "bne.n 800028e", "only an exact HSI"),
        (0x8000288, "str r3, [r2, #20]", "final HSI.*ALL_OFF"),
    ),
)
def test_rejects_inline_hsi_gate_mutations(address: int, replacement: str, message: str) -> None:
    _rejects(_mutate_instruction(address, replacement), message)


@pytest.mark.parametrize(
    ("address", "replacement", "message"),
    (
        (0x8000296, "cmp r3, #3", "pre-write lateness threshold"),
        (0x8000298, "bls.n 80002c0", "unsigned reject-only"),
        (0x800029A, "bl 80002ec <high_rate_frame_init>", "precomputed stack BSRR"),
        (0x800029C, "str r3, [r2, #20]", "directly to GPIOA BSRR"),
        (0x8000248, "bpl.n 800029a", "share fail-closed resynchronization"),
        (0x80002C6, "str r2, [r3, #20]", "resynchronization path.*ALL_OFF"),
        (0x80002C8, "b.n 80001f6", "restart does not reinitialize"),
    ),
)
def test_rejects_final_admission_or_resynchronization_mutations(
    address: int, replacement: str, message: str
) -> None:
    _rejects(_mutate_instruction(address, replacement), message)


@pytest.mark.parametrize(
    ("address", "replacement", "message"),
    (
        (0x800021A, "orrs r2, r1", "set-bit mask"),
        (0x800021E, "str r3, [sp, #16]", "stack word has no producer"),
        (0x80001F4, "str r3, [sp, #4]", "deadline stack word has no initial producer"),
        (0x8000206, "bl 80002fe <high_rate_frame_advance+0x2>", "planned frame"),
    ),
)
def test_rejects_planned_bsrr_or_deadline_provenance_mutations(
    address: int, replacement: str, message: str
) -> None:
    _rejects(_mutate_instruction(address, replacement), message)


@pytest.mark.parametrize(
    ("address", "replacement", "message"),
    (
        (0x8000302, "bne.n 8000318", "frame-advance branches"),
        (0x800033E, "cmp r2, #5", "frame-advance instruction sequence"),
        (0x8000354, "movs r2, #179", "frame-advance instruction sequence"),
    ),
)
def test_rejects_profile_state_machine_mutations(
    address: int, replacement: str, message: str
) -> None:
    _rejects(_mutate_instruction(address, replacement), message)


def test_rejects_schedule_pointer_that_does_not_match_the_elf_symbol() -> None:
    changed = _mutate_instruction(0x8000358, ".word 0x08000428")
    evidence = VERIFIER.verify_main_timing_control_flow(changed)
    with pytest.raises(VERIFIER.HexcalElfVerificationError, match="symbol identity"):
        VERIFIER.verify_control_schedule_symbol_identity(
            evidence,
            schedule_address=0x08000424,
            schedule_size=len(VERIFIER.EXPECTED_CONTROL_SCHEDULE),
        )


def test_rejects_illegal_schedule_code_or_dwell() -> None:
    schedule = bytearray(VERIFIER.EXPECTED_CONTROL_SCHEDULE)
    schedule[0] = 7
    with pytest.raises(VERIFIER.HexcalElfVerificationError, match="CONTROL_SCHEDULE"):
        VERIFIER.verify_control_schedule_bytes(bytes(schedule))

    schedule = bytearray(VERIFIER.EXPECTED_CONTROL_SCHEDULE)
    schedule[2:4] = (199).to_bytes(2, "little")
    with pytest.raises(VERIFIER.HexcalElfVerificationError, match="CONTROL_SCHEDULE"):
        VERIFIER.verify_control_schedule_bytes(bytes(schedule))


@pytest.mark.parametrize(
    ("address", "replacement", "message"),
    (
        (0x800035E, "movs r0, r1", "timer-width truncation"),
        (0x80002A2, "strb r3, [r6, #3]", "applied_code commit"),
        (0x80002AC, "bl 800035e <high_rate_next_deadline+0x2>", "exact helper"),
        (0x80002B6, "beq.n 80001f8", "next-frame planning"),
        (0x80002BC, "str r2, [r3, #4]", "IWDG refresh store"),
    ),
)
def test_rejects_turnover_identity_mutations(address: int, replacement: str, message: str) -> None:
    _rejects(_mutate_instruction(address, replacement), message)


@pytest.mark.parametrize(
    ("constant", "value", "message"),
    (
        ("FAR_OUTER_SAMPLE_MAX_CORE_CYCLES", 53, "costs 54 cycles"),
        ("STAGING_ENTRY_TO_TIGHT_SAMPLE_MAX_CORE_CYCLES", 21, "costs 22 cycles"),
        ("TIGHT_POLL_SAMPLE_MAX_CORE_CYCLES", 10, "costs 11 cycles"),
        ("DEADLINE_TO_FINAL_SAMPLE_MAX_CORE_CYCLES", 22, "costs 23 cycles"),
        ("GPIO_WRITE_MAX_CORE_CYCLES", 15, "components are internally inconsistent"),
        ("TRANSITION_TURNOVER_MAX_CORE_CYCLES", 164, "costs 165 cycles"),
        ("DEADLINE_TO_GPIO_MAX_CORE_CYCLES", 51, "components are internally inconsistent"),
        ("SHORTEST_PHASE_CHAIN_MAX_CORE_CYCLES", 232, "does not fit strictly"),
    ),
)
def test_rejects_any_tightened_binary_cycle_cap(
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    value: int,
    message: str,
) -> None:
    monkeypatch.setattr(VERIFIER, constant, value)
    _rejects(DISASSEMBLY, message)


@pytest.mark.parametrize(
    ("address", "description"),
    (
        ("40015808", "DBGMCU/IWDG-freeze"),
        ("40022000", "flash/option"),
        ("40002c00", "window watchdog"),
        ("e000e010", "SysTick"),
    ),
)
def test_rejects_raw_forbidden_peripheral_addresses(address: str, description: str) -> None:
    with pytest.raises(VERIFIER.HexcalElfVerificationError, match=description):
        VERIFIER.reject_forbidden_peripheral_access(DISASSEMBLY + address)


def test_rejects_computed_dbgmcu_address_across_a_branch() -> None:
    computed_access = """
08000450 <computed_dbg_access>:
 8000450: 2080       movs r0, #128
 8000452: 05c0       lsls r0, r0, #23
 8000454: 212b       movs r1, #43
 8000456: 02c9       lsls r1, r1, #11
 8000458: 1840       adds r0, r0, r1
 800045a: e000       b.n 800045e <computed_dbg_access+0xe>
 800045c: 46c0       nop
 800045e: 6082       str r2, [r0, #8]
 8000460: 4770       bx lr
"""
    with pytest.raises(
        VERIFIER.HexcalElfVerificationError,
        match="computes access to unexpected DBGMCU/IWDG-freeze",
    ):
        VERIFIER.reject_forbidden_peripheral_access(DISASSEMBLY + computed_access)


def test_rejects_forbidden_address_computed_with_bitwise_operations() -> None:
    computed_access = """
08000470 <computed_dbg_or_access>:
 8000470: 2080       movs r0, #128
 8000472: 05c0       lsls r0, r0, #23
 8000474: 212b       movs r1, #43
 8000476: 02c9       lsls r1, r1, #11
 8000478: 4308       orrs r0, r1
 800047a: 6082       str r2, [r0, #8]
 800047c: 4770       bx lr
"""
    with pytest.raises(
        VERIFIER.HexcalElfVerificationError,
        match="computes access to unexpected DBGMCU/IWDG-freeze",
    ):
        VERIFIER.reject_forbidden_peripheral_access(DISASSEMBLY + computed_access)


def test_rejects_forbidden_address_split_across_base_and_offset_registers() -> None:
    computed_access = """
08000480 <computed_dbg_register_offset>:
 8000480: 2080       movs r0, #128
 8000482: 05c0       lsls r0, r0, #23
 8000484: 212b       movs r1, #43
 8000486: 02c9       lsls r1, r1, #11
 8000488: 5042       str r2, [r0, r1]
 800048a: 4770       bx lr
"""
    with pytest.raises(
        VERIFIER.HexcalElfVerificationError,
        match="computes access to unexpected DBGMCU/IWDG-freeze",
    ):
        VERIFIER.reject_forbidden_peripheral_access(DISASSEMBLY + computed_access)
