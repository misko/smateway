#!/usr/bin/env python3
"""Fail closed on unexpected high-rate calibration-image content."""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

FLASH_LIMIT = 16 * 1024
RAM_LIMIT = 6 * 1024

TIM3_BASE = 0x40000400
TIM3_CNT_OFFSET = 36
TIM3_CR1_OFFSET = 0
TIM3_DIER_OFFSET = 12
TIM3_SR_OFFSET = 16
TIM3_EGR_OFFSET = 20
TIM3_PSC_OFFSET = 40
TIM3_ARR_OFFSET = 44
TIM3_PRESCALER = 11
TIM3_AUTO_RELOAD = 0xFFFF
TIM3_CR1_CEN = 1
RCC_BASE = 0x40021000
RCC_CR_OFFSET = 0
RCC_CFGR_OFFSET = 8
RCC_APBRSTR1_OFFSET = 44
RCC_IOPENR_OFFSET = 52
RCC_APBENR1_OFFSET = 60
RCC_CLOCK_SIGNATURE = 0x00001500
RCC_CLOCK_SIGNATURE_MASK = 0x00003D00
RCC_CFGR_POLICY_MASK = 0x00007F3F
GPIOA_BASE = 0x50000000
GPIO_BSRR_OFFSET = 24
GPIO_ODR_OFFSET = 20
GPIO_CONTROL_PIN_MASK = 0x0F
IWDG_BASE = 0x40003000
SRAM_BASE = 0x20000000
SRAM_END = SRAM_BASE + RAM_LIMIT
ALL_OFF_BSRR_WORD = 0x00070008
HSI_SIGNATURE_EXPECTED = 0x00001500
HSI_SIGNATURE_MASK = 0x00003D00
TIGHT_POLL_WINDOW_US = 8
HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK = 12
DIRECT_CALL_CORE_CYCLES = 4
FAR_OUTER_SAMPLE_MAX_CORE_CYCLES = 54
STAGING_ENTRY_TO_TIGHT_SAMPLE_MAX_CORE_CYCLES = 22
PREWRITE_MAX_LATENESS_US = 2
COUNTER_QUANTIZATION_TICKS = 1
TIGHT_POLL_SAMPLE_MAX_CORE_CYCLES = 11
DEADLINE_TO_FINAL_SAMPLE_MAX_CORE_CYCLES = 23
GPIO_WRITE_MAX_CORE_CYCLES = 16
ENDPOINT_MEMORY_ACCESS_CORE_CYCLES = 3
TRANSITION_TURNOVER_MAX_CORE_CYCLES = 165
DEADLINE_TO_GPIO_MAX_CORE_CYCLES = 52
SHORTEST_PHASE_US = 20
SHORTEST_PHASE_CHAIN_MAX_CORE_CYCLES = 233
RESET_CORE_CLOCK_HZ = 12_000_000
DEADLINE_STACK_OFFSET = 0
CYCLE_RESULT_STACK_OFFSET = 4
PLANNED_BSRR_STACK_OFFSET = 12
LIVE_FRAME_STACK_OFFSET = 24
PLANNED_FRAME_STACK_OFFSET = 32
FRAME_STACK_SIZE = 6
VECTOR_TABLE_ADDRESS = 0x08000000
VECTOR_TABLE_WORDS = 48
DEFAULT_VECTOR_INDICES = frozenset(
    {
        2,
        3,
        11,
        14,
        15,
        16,
        18,
        19,
        20,
        21,
        22,
        23,
        25,
        26,
        27,
        28,
        29,
        30,
        32,
        35,
        37,
        38,
        39,
        41,
        43,
        44,
    }
)
TEXT_SECTION_ADDRESS = 0x080000C0
TEXT_SECTION_SIZE = 956
EXPECTED_TEXT_SHA256 = "579562f7d0f6a766c9faefd5ecff054372eadbb0db220efcc4cf0a316ae0af50"
DATA_LOAD_ADDRESS = 0x0800047C

IWDG_ENABLE_KEY = 0xCCCC
IWDG_WRITE_ACCESS_KEY = 0x5555
IWDG_REFRESH_KEY = 0xAAAA
IWDG_PRESCALER_REGISTER_VALUE = 0
IWDG_RELOAD_VALUE = 127

EXPECTED_CONTROL_SCHEDULE = bytes.fromhex(
    "0000c800"  # ANT1, 200 us
    "0400c800"  # ANT2, 200 us
    "0200c800"  # ANT3, 200 us
    "0600c800"  # ANT4, 200 us
    "0100c800"  # ANT5, 200 us
    "0500c800"  # ANT6, 200 us
)

FORBIDDEN_ADDRESS_PREFIXES = {
    "400220": "flash/option registers",
    "40002c": "window watchdog",
    "400158": "DBGMCU/IWDG-freeze registers",
    "e000e010": "SysTick",
}

FORBIDDEN_ADDRESS_RANGES = (
    (0x40022000, 0x40022400, "flash/option registers"),
    (0x40002C00, 0x40003000, "window watchdog"),
    (0x40015800, 0x40015C00, "DBGMCU/IWDG-freeze registers"),
    (0xE000E010, 0xE000E020, "SysTick"),
)

FUNCTION_HEADER_RE = re.compile(r"^([0-9a-f]+) <([^>]+)>:$", re.MULTILINE)
INSTRUCTION_RE = re.compile(r"\s*([0-9a-f]+):\s+((?:[0-9a-f]{4,8}\s+)+)([a-z.]+)\s*(.*)")
REGISTER = r"(?:r(?:1[0-5]|[0-9])|sp|lr|pc)"
IMMEDIATE = r"(?:0x[0-9a-f]+|[0-9]+)"
MEMORY_OPERAND_RE = re.compile(rf"^({REGISTER}),\s*\[({REGISTER})(?:,\s*#({IMMEDIATE}))?\]$")
REGISTER_MEMORY_OPERAND_RE = re.compile(rf"^({REGISTER}),\s*\[({REGISTER}),\s*({REGISTER})\]$")
BINARY_OPERAND_RE = re.compile(rf"^({REGISTER}),\s*({REGISTER}),\s*({REGISTER}|#{IMMEDIATE})$")
TWO_OPERAND_RE = re.compile(rf"^({REGISTER}),\s*({REGISTER}|#{IMMEDIATE})$")
CONDITIONAL_BRANCH_OPERATIONS = frozenset(
    {
        "beq",
        "bne",
        "bcs",
        "bcc",
        "bhs",
        "blo",
        "bmi",
        "bpl",
        "bvs",
        "bvc",
        "bhi",
        "bls",
        "bge",
        "blt",
        "bgt",
        "ble",
        "cbz",
        "cbnz",
    }
)
BRANCH_OPERATIONS = CONDITIONAL_BRANCH_OPERATIONS | {"b", "bl", "blx", "bx"}


class HexcalElfVerificationError(ValueError):
    """The ELF does not implement the frozen Hexcal machine policy."""


@dataclass(frozen=True)
class Instruction:
    address: int
    size_bytes: int
    mnemonic: str
    operands: str

    @property
    def operation(self) -> str:
        if self.mnemonic.startswith("."):
            return self.mnemonic
        return self.mnemonic.split(".", 1)[0]

    @property
    def next_address(self) -> int:
        return self.address + self.size_bytes


@dataclass(frozen=True)
class FunctionDisassembly:
    name: str
    instructions: tuple[Instruction, ...]
    literals: dict[int, int]

    @property
    def code(self) -> tuple[Instruction, ...]:
        return tuple(item for item in self.instructions if not item.operation.startswith("."))


@dataclass(frozen=True)
class HexcalControlFlowEvidence:
    control_schedule_address: int
    tight_poll_instruction_count: int
    tight_poll_window_us: int
    far_outer_sample_core_cycles: int
    far_outer_sample_max_core_cycles: int
    staging_entry_to_tight_sample_core_cycles: int
    staging_entry_to_tight_sample_max_core_cycles: int
    tight_poll_sample_core_cycles: int
    tight_poll_sample_max_core_cycles: int
    prewrite_max_lateness_us: int
    deadline_to_final_sample_core_cycles: int
    deadline_to_final_sample_max_core_cycles: int
    gpio_write_path_core_cycles: int
    gpio_write_max_core_cycles: int
    transition_turnover_core_cycles: int
    transition_turnover_max_core_cycles: int
    shortest_phase_chain_max_core_cycles: int
    shortest_phase_core_cycles: int


@dataclass(frozen=True)
class BootHardwareEvidence:
    gpio_clock_store_index: int
    gpio_clock_readback_index: int
    gpio_preload_store_index: int
    reset_clock_gate_index: int
    tim_clock_store_index: int
    tim_clock_readback_index: int
    tim_reset_assert_store_index: int
    tim_reset_assert_readback_index: int
    tim_reset_deassert_store_index: int
    tim_reset_deassert_readback_index: int
    tim_zero_register: str
    tim_prescaler_register: str
    startup_failure_address: int


def output(*command: str) -> str:
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise HexcalElfVerificationError(message)


def _operand_text(instruction: Instruction) -> str:
    return instruction.operands.split("@", 1)[0].strip()


def _parse_immediate(value: str) -> int:
    return int(value, 0)


def _memory_operands(instruction: Instruction) -> tuple[str, str, int] | None:
    match = MEMORY_OPERAND_RE.fullmatch(_operand_text(instruction))
    if match is None:
        return None
    destination_or_source, base, offset = match.groups()
    return destination_or_source, base, 0 if offset is None else _parse_immediate(offset)


def _register_memory_operands(instruction: Instruction) -> tuple[str, str, str] | None:
    match = REGISTER_MEMORY_OPERAND_RE.fullmatch(_operand_text(instruction))
    if match is None:
        return None
    return match.group(1), match.group(2), match.group(3)


def _binary_operands(instruction: Instruction) -> tuple[str, str, str] | None:
    match = BINARY_OPERAND_RE.fullmatch(_operand_text(instruction))
    if match is None:
        return None
    return match.group(1), match.group(2), match.group(3)


def _two_operands(instruction: Instruction) -> tuple[str, str] | None:
    match = TWO_OPERAND_RE.fullmatch(_operand_text(instruction))
    if match is None:
        return None
    return match.group(1), match.group(2)


def _branch_target(instruction: Instruction) -> int | None:
    if instruction.operation not in BRANCH_OPERATIONS:
        return None
    match = (
        re.search(r",\s*([0-9a-f]+)\b", instruction.operands)
        if instruction.operation in {"cbz", "cbnz"}
        else re.match(r"([0-9a-f]+)\b", instruction.operands)
    )
    return None if match is None else int(match.group(1), 16)


def _calls(instruction: Instruction, symbol: str) -> bool:
    return (
        instruction.operation == "bl"
        and re.search(rf"<{re.escape(symbol)}>", instruction.operands) is not None
    )


def _branches_to(instruction: Instruction, symbol: str) -> bool:
    return (
        instruction.operation == "b"
        and re.search(rf"<{re.escape(symbol)}>", instruction.operands) is not None
    )


def _literal_value(function: FunctionDisassembly, instruction: Instruction) -> int | None:
    if instruction.operation != "ldr":
        return None
    memory = _memory_operands(instruction)
    if memory is None or memory[1] != "pc":
        return None
    target = re.search(r"@\s*\(([0-9a-f]+)\b", instruction.operands)
    if target is None:
        return None
    return function.literals.get(int(target.group(1), 16))


def parse_function(disassembly: str, name: str) -> FunctionDisassembly:
    normalized = disassembly.lower()
    headers = list(FUNCTION_HEADER_RE.finditer(normalized))
    selected_index = next(
        (index for index, header in enumerate(headers) if header.group(2) == name), None
    )
    if selected_index is None:
        raise HexcalElfVerificationError(f"cannot isolate {name} disassembly")
    selected = headers[selected_index]
    body_end = (
        headers[selected_index + 1].start()
        if selected_index + 1 < len(headers)
        else len(normalized)
    )
    body = normalized[selected.end() : body_end]
    instructions: list[Instruction] = []
    literals: dict[int, int] = {}
    for line in normalized.splitlines():
        match = INSTRUCTION_RE.fullmatch(line)
        if match is None or not match.group(3).startswith(".word"):
            continue
        literal = re.match(r"0x([0-9a-f]+)\b", match.group(4))
        if literal is not None:
            literals[int(match.group(1), 16)] = int(literal.group(1), 16)
    for line in body.splitlines():
        match = INSTRUCTION_RE.fullmatch(line)
        if match is None:
            continue
        address_text, encoding_text, mnemonic, operands = match.groups()
        encoding_words = encoding_text.split()
        instruction = Instruction(
            address=int(address_text, 16),
            size_bytes=sum(len(word) // 2 for word in encoding_words),
            mnemonic=mnemonic,
            operands=operands.strip(),
        )
        instructions.append(instruction)
    _require(bool(instructions), f"{name} disassembly contains no parsed instructions")
    return FunctionDisassembly(name=name, instructions=tuple(instructions), literals=literals)


def reject_forbidden_peripheral_access(disassembly: str) -> None:
    normalized = disassembly.lower()
    for address_prefix, description in FORBIDDEN_ADDRESS_PREFIXES.items():
        if address_prefix in normalized:
            raise HexcalElfVerificationError(f"image accesses unexpected {description}")

    function_names = dict.fromkeys(
        header.group(2) for header in FUNCTION_HEADER_RE.finditer(normalized)
    )
    for function_name in function_names:
        function = parse_function(normalized, function_name)
        code = function.code
        if not code:
            continue
        states: dict[int, dict[str, int]] = {0: {}}
        pending = [0]
        while pending:
            index = pending.pop()
            constants = dict(states[index])
            instruction = code[index]
            memory = _memory_operands(instruction)
            register_memory = _register_memory_operands(instruction)
            effective_address: int | None = None
            if memory is not None and memory[1] != "pc" and memory[1] in constants:
                effective_address = (constants[memory[1]] + memory[2]) & 0xFFFFFFFF
            elif (
                register_memory is not None
                and register_memory[1] in constants
                and register_memory[2] in constants
            ):
                effective_address = (
                    constants[register_memory[1]] + constants[register_memory[2]]
                ) & 0xFFFFFFFF
            if effective_address is not None:
                if (
                    function_name != "main"
                    and instruction.operation in {"str", "strb", "strh"}
                    and GPIOA_BASE <= effective_address < GPIOA_BASE + 0x400
                ):
                    raise HexcalElfVerificationError(
                        "image computes an unapproved GPIOA write outside main"
                    )
                for start, end, description in FORBIDDEN_ADDRESS_RANGES:
                    if start <= effective_address < end:
                        raise HexcalElfVerificationError(
                            f"image computes access to unexpected {description}"
                        )
            _propagate_simple_constant(function, instruction, constants)
            for successor in _successor_indices(code, index):
                previous = states.get(successor)
                if previous is None:
                    states[successor] = dict(constants)
                    pending.append(successor)
                    continue
                merged = {
                    register: value
                    for register, value in previous.items()
                    if constants.get(register) == value
                }
                if merged != previous:
                    states[successor] = merged
                    pending.append(successor)


def _propagate_simple_constant(
    function: FunctionDisassembly,
    instruction: Instruction,
    constants: dict[str, int],
) -> None:
    operation = instruction.operation
    two = _two_operands(instruction)
    binary = _binary_operands(instruction)
    memory = _memory_operands(instruction)

    if operation in {"bl", "blx"}:
        for register in ("r0", "r1", "r2", "r3", "lr"):
            constants.pop(register, None)
        return
    if operation in BRANCH_OPERATIONS:
        return
    if operation == "movs" and two is not None and two[1].startswith("#"):
        constants[two[0]] = _parse_immediate(two[1][1:])
        return
    if operation == "mov" and two is not None and two[1] in constants:
        constants[two[0]] = constants[two[1]]
        return
    if operation == "ldr" and memory is not None and memory[1] == "pc":
        literal = _literal_value(function, instruction)
        if literal is not None:
            constants[memory[0]] = literal
            return
    if operation in {"lsls", "lsrs", "asrs"} and binary is not None:
        source = constants.get(binary[1])
        shift_text = binary[2]
        shift = (
            _parse_immediate(shift_text[1:])
            if shift_text.startswith("#")
            else constants.get(shift_text)
        )
        if source is not None and shift is not None:
            shift &= 0xFF
            if operation == "lsls":
                value = 0 if shift >= 32 else source << shift
            elif operation == "lsrs":
                value = 0 if shift >= 32 else source >> shift
            else:
                signed = source if source < 0x80000000 else source - 0x100000000
                value = (-1 if signed < 0 else 0) if shift >= 32 else signed >> shift
            constants[binary[0]] = value & 0xFFFFFFFF
            return
    if operation in {"adds", "subs"} and two is not None and two[1].startswith("#"):
        source = constants.get(two[0])
        if source is not None:
            immediate = _parse_immediate(two[1][1:])
            constants[two[0]] = (
                source + immediate if operation == "adds" else source - immediate
            ) & 0xFFFFFFFF
            return
    if operation in {"adds", "subs"} and binary is not None:
        left = constants.get(binary[1])
        right_text = binary[2]
        right = (
            _parse_immediate(right_text[1:])
            if right_text.startswith("#")
            else constants.get(right_text)
        )
        if left is not None and right is not None:
            constants[binary[0]] = (
                left + right if operation == "adds" else left - right
            ) & 0xFFFFFFFF
            return
    if operation in {"ands", "orrs", "eors", "bics", "muls"} and two is not None:
        left = constants.get(two[0])
        right = constants.get(two[1])
        if left is not None and right is not None:
            if operation == "ands":
                value = left & right
            elif operation == "orrs":
                value = left | right
            elif operation == "eors":
                value = left ^ right
            elif operation == "bics":
                value = left & ~right
            else:
                value = left * right
            constants[two[0]] = value & 0xFFFFFFFF
            return
    if operation in {"mvns", "negs"} and two is not None:
        source = constants.get(two[1])
        if source is not None:
            constants[two[0]] = ((~source) if operation == "mvns" else -source) & 0xFFFFFFFF
            return
    if operation in {"uxtb", "uxth", "sxtb", "sxth"} and two is not None:
        source = constants.get(two[1])
        if source is not None:
            bits = 8 if operation.endswith("b") else 16
            mask = (1 << bits) - 1
            value = source & mask
            if operation.startswith("s") and value & (1 << (bits - 1)):
                value |= ~mask
            constants[two[0]] = value & 0xFFFFFFFF
            return

    if operation == "ldr" and memory is not None:
        constants.pop(memory[0], None)
        return
    no_destination = {"str", "strb", "strh", "cmp", "tst", "push", "pop", "bx"}
    if operation not in no_destination:
        destination = _operand_text(instruction).split(",", 1)[0]
        if re.fullmatch(REGISTER, destination):
            constants.pop(destination, None)


def _require_memory(
    instruction: Instruction,
    *,
    operation: str,
    value_register: str | None = None,
    base_register: str | None = None,
    offset: int | None = None,
    message: str,
) -> tuple[str, str, int]:
    memory = _memory_operands(instruction)
    _require(instruction.operation == operation and memory is not None, message)
    assert memory is not None
    _require(value_register is None or memory[0] == value_register, message)
    _require(base_register is None or memory[1] == base_register, message)
    _require(offset is None or memory[2] == offset, message)
    return memory


def _require_two_operands(
    instruction: Instruction,
    *,
    operation: str,
    first: str | None = None,
    second: str | None = None,
    message: str,
) -> tuple[str, str]:
    operands = _two_operands(instruction)
    _require(instruction.operation == operation and operands is not None, message)
    assert operands is not None
    _require(first is None or operands[0] == first, message)
    _require(second is None or operands[1] == second, message)
    return operands


def _require_binary(
    instruction: Instruction,
    *,
    operation: str,
    first: str | None = None,
    second: str | None = None,
    third: str | None = None,
    message: str,
) -> tuple[str, str, str]:
    operands = _binary_operands(instruction)
    _require(instruction.operation == operation and operands is not None, message)
    assert operands is not None
    _require(first is None or operands[0] == first, message)
    _require(second is None or operands[1] == second, message)
    _require(third is None or operands[2] == third, message)
    return operands


def _is_contiguous(instructions: Sequence[Instruction]) -> bool:
    return all(
        previous.next_address == current.address
        for previous, current in zip(instructions, instructions[1:], strict=False)
    )


def _successor_indices(code: Sequence[Instruction], index: int) -> tuple[int, ...]:
    instruction = code[index]
    address_to_index = {item.address: item_index for item_index, item in enumerate(code)}
    fallthrough = (
        index + 1
        if index + 1 < len(code) and instruction.next_address == code[index + 1].address
        else None
    )
    operation = instruction.operation
    if operation == "b":
        target = _branch_target(instruction)
        return () if target not in address_to_index else (address_to_index[target],)
    if operation in CONDITIONAL_BRANCH_OPERATIONS:
        target = _branch_target(instruction)
        successors: list[int] = []
        if target in address_to_index:
            successors.append(address_to_index[target])
        if fallthrough is not None:
            successors.append(fallthrough)
        return tuple(successors)
    if operation == "bx" or (operation == "pop" and "pc" in _operand_text(instruction)):
        return ()
    return () if fallthrough is None else (fallthrough,)


def _reachable_indices(
    code: Sequence[Instruction],
    *,
    start_index: int = 0,
    blocked_index: int | None = None,
) -> set[int]:
    if start_index == blocked_index:
        return set()
    pending = [start_index]
    reached: set[int] = set()
    while pending:
        index = pending.pop()
        if index == blocked_index or index in reached:
            continue
        reached.add(index)
        pending.extend(
            successor
            for successor in _successor_indices(code, index)
            if successor != blocked_index and successor not in reached
        )
    return reached


def _require_dominates(
    code: Sequence[Instruction],
    dominator_index: int,
    target_indices: Sequence[int],
    message: str,
) -> None:
    reachable = _reachable_indices(code)
    without_dominator = _reachable_indices(code, blocked_index=dominator_index)
    _require(
        all(target in reachable and target not in without_dominator for target in target_indices),
        message,
    )


def _require_exact_branch_entries(
    code: Sequence[Instruction],
    block: Sequence[Instruction],
    expected: Sequence[tuple[int, int]],
    message: str,
) -> None:
    block_addresses = {item.address for item in block}
    actual = [
        (item.address, target)
        for item in code
        if (target := _branch_target(item)) in block_addresses
    ]
    _require(actual == list(expected), message)


def _require_terminal_spin(block: Sequence[Instruction], message: str) -> None:
    _require(bool(block), message)
    final = block[-1]
    target = _branch_target(final)
    addresses = {item.address for item in block}
    _require(
        all(item.operation not in BRANCH_OPERATIONS for item in block[:-1]),
        message,
    )
    _require(final.operation == "b" and target in addresses, message)


def _failure_block_until_spin(
    code: Sequence[Instruction],
    start_index: int,
    limit_index: int,
    message: str,
) -> tuple[Instruction, ...]:
    _require(start_index < limit_index, message)
    for end_index in range(start_index, limit_index):
        instruction = code[end_index]
        if instruction.operation not in BRANCH_OPERATIONS:
            continue
        block = tuple(code[start_index : end_index + 1])
        _require_terminal_spin(block, message)
        return block
    raise HexcalElfVerificationError(message)


def _last_register_write(
    block: Sequence[Instruction], register: str, before_index: int
) -> Instruction | None:
    no_destination = {"str", "strb", "strh", "cmp", "tst", "b", "bl", "bx"}
    for instruction in reversed(block[:before_index]):
        operation = instruction.operation
        if operation in no_destination or operation in BRANCH_OPERATIONS:
            continue
        first = _operand_text(instruction).split(",", 1)[0]
        if first == register:
            return instruction
    return None


def _require_gpio_base_materialization(
    block: Sequence[Instruction], register: str, before_index: int, message: str
) -> None:
    prior = list(block[:before_index])
    shift_index = next(
        (
            index
            for index in range(len(prior) - 1, -1, -1)
            if _operand_text(prior[index]).split(",", 1)[0] == register
        ),
        None,
    )
    _require(shift_index is not None, message)
    assert shift_index is not None
    _require_binary(
        prior[shift_index],
        operation="lsls",
        first=register,
        second=register,
        third="#23",
        message=message,
    )
    source_index = next(
        (
            index
            for index in range(shift_index - 1, -1, -1)
            if _operand_text(prior[index]).split(",", 1)[0] == register
        ),
        None,
    )
    _require(source_index is not None, message)
    assert source_index is not None
    _require_two_operands(
        prior[source_index],
        operation="movs",
        first=register,
        second="#160",
        message=message,
    )


def _require_all_off_bsrr_store(
    function: FunctionDisassembly,
    block: Sequence[Instruction],
    *,
    known_gpio_register: str | None = None,
    message: str,
) -> Instruction:
    for index, instruction in enumerate(block):
        memory = _memory_operands(instruction)
        if instruction.operation != "str" or memory is None or memory[2] != GPIO_BSRR_OFFSET:
            continue
        source_register, base_register, _ = memory
        source_write = _last_register_write(block, source_register, index)
        if source_write is None or _literal_value(function, source_write) != ALL_OFF_BSRR_WORD:
            continue
        if known_gpio_register is not None:
            if base_register != known_gpio_register:
                continue
        else:
            try:
                _require_gpio_base_materialization(block, base_register, index, message)
            except HexcalElfVerificationError:
                continue
        return instruction
    raise HexcalElfVerificationError(message)


def _instruction_signatures(
    function: FunctionDisassembly,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (
            instruction.operation,
            re.sub(r"\s*<[^>]+>$", "", _operand_text(instruction)),
        )
        for instruction in function.code
    )


def verify_pre_output_callee_bodies(disassembly: str) -> None:
    libc = parse_function(disassembly, "__libc_init_array")
    _require(
        _instruction_signatures(libc) == (("bx", "lr"),),
        "__libc_init_array body is not the exact no-op return",
    )

    system_init = parse_function(disassembly, "systeminit")
    _require(
        _instruction_signatures(system_init)
        == (
            ("movs", "r2, #128"),
            ("ldr", "r3, [pc, #8]"),
            ("lsls", "r2, r2, #20"),
            ("str", "r2, [r3, #8]"),
            ("bx", "lr"),
            ("nop", ""),
        )
        and _literal_value(system_init, system_init.code[1]) == 0xE000ED00,
        "SystemInit body is not the exact VTOR-only implementation",
    )

    clock_update = parse_function(disassembly, "systemcoreclockupdate")
    _require(
        _instruction_signatures(clock_update)
        == (
            ("movs", "r2, #56"),
            ("ldr", "r1, [pc, #60]"),
            ("ldr", "r3, [r1, #8]"),
            ("ands", "r3, r2"),
            ("cmp", "r3, #24"),
            ("beq", "80003a8"),
            ("cmp", "r3, #32"),
            ("beq", "80003ae"),
            ("ldr", "r0, [pc, #48]"),
            ("movs", "r2, r0"),
            ("cmp", "r3, #8"),
            ("beq", "8000394"),
            ("ldr", "r3, [r1, #0]"),
            ("lsls", "r3, r3, #18"),
            ("lsrs", "r3, r3, #29"),
            ("lsrs", "r2, r3"),
            ("ldr", "r3, [r1, #8]"),
            ("ldr", "r1, [pc, #36]"),
            ("lsls", "r3, r3, #20"),
            ("lsrs", "r3, r3, #28"),
            ("lsls", "r3, r3, #2"),
            ("ldr", "r3, [r3, r1]"),
            ("ldr", "r0, [pc, #28]"),
            ("lsrs", "r2, r3"),
            ("str", "r2, [r0, #0]"),
            ("bx", "lr"),
            ("movs", "r2, #250"),
            ("lsls", "r2, r2, #7"),
            ("b", "8000394"),
            ("movs", "r2, #128"),
            ("lsls", "r2, r2, #8"),
            ("b", "8000394"),
        ),
        "SystemCoreClockUpdate body is not the exact frozen implementation",
    )
    _require(
        _literal_value(clock_update, clock_update.code[1]) == RCC_BASE
        and _literal_value(clock_update, clock_update.code[8]) == 48_000_000
        and _literal_value(clock_update, clock_update.code[17]) == 0x0800043C
        and _literal_value(clock_update, clock_update.code[22]) == SRAM_BASE,
        "SystemCoreClockUpdate literal identities are not exact",
    )


def verify_reset_handler_sram_workaround(disassembly: str) -> None:
    verify_pre_output_callee_bodies(disassembly)
    reset = parse_function(disassembly, "reset_handler")
    reset_code = reset.code
    _require(
        len(reset_code) >= 3 and _is_contiguous(reset_code[:3]),
        "Reset_Handler entry sequence is not exact",
    )
    stack_register, _, _ = _require_memory(
        reset_code[0],
        operation="ldr",
        base_register="pc",
        message="Reset_Handler does not load the exact initial stack pointer",
    )
    _require(
        _literal_value(reset, reset_code[0]) == SRAM_END,
        "Reset_Handler initial stack pointer is not the SRAM limit",
    )
    _require_two_operands(
        reset_code[1],
        operation="mov",
        first="sp",
        second=stack_register,
        message="Reset_Handler does not set SP before the erratum workaround",
    )
    _require(
        _calls(reset_code[2], "hexcalstartupsysteminit"),
        "Reset_Handler does not call the exact owned erratum wrapper",
    )
    _require(
        len(reset_code) == 5 and _is_contiguous(reset_code),
        "Reset_Handler body is not exact and contiguous",
    )
    copy_offset_register, _ = _require_two_operands(
        reset_code[3],
        operation="movs",
        second="#0",
        message="Reset_Handler data-copy offset seed is not exact",
    )

    copy_data = parse_function(disassembly, "copydatainit")
    copy_loop = parse_function(disassembly, "loopcopydatainit")
    fill_zero = parse_function(disassembly, "fillzerobss")
    fill_loop = parse_function(disassembly, "loopfillzerobss")
    forever = parse_function(disassembly, "loopforever")
    _require(
        reset_code[4].operation == "b"
        and _branch_target(reset_code[4]) == copy_loop.code[0].address,
        "Reset_Handler does not enter the exact data-copy bound check",
    )
    _require(
        len(copy_data.code) == 4
        and len(copy_loop.code) == 7
        and len(fill_zero.code) == 3
        and len(fill_loop.code) == 5
        and len(forever.code) == 1
        and _is_contiguous(copy_data.code)
        and _is_contiguous(copy_loop.code)
        and _is_contiguous(fill_zero.code)
        and _is_contiguous(fill_loop.code),
        "Reset_Handler data/BSS/init tail shape is not exact",
    )
    _require(
        reset_code[-1].next_address == copy_data.code[0].address
        and copy_data.code[-1].next_address == copy_loop.code[0].address
        and copy_loop.code[-1].next_address == fill_zero.code[0].address
        and fill_zero.code[-1].next_address == fill_loop.code[0].address
        and fill_loop.code[-1].next_address == forever.code[0].address,
        "Reset_Handler data/BSS/init tail is not physically contiguous",
    )

    copy_source_pointer, _, _ = _require_memory(
        copy_data.code[0],
        operation="ldr",
        base_register="pc",
        message="Reset_Handler data source pointer load is not exact",
    )
    _require(
        _literal_value(copy_data, copy_data.code[0]) == DATA_LOAD_ADDRESS,
        "Reset_Handler _sidata literal is not exact",
    )
    copy_source = _register_memory_operands(copy_data.code[1])
    copy_store = _register_memory_operands(copy_data.code[2])
    _require(
        copy_data.code[1].operation == "ldr"
        and copy_source == (copy_source_pointer, copy_source_pointer, copy_offset_register),
        "Reset_Handler data-copy source read is not exact",
    )
    _require(
        copy_data.code[2].operation == "str"
        and copy_store == (copy_source_pointer, stack_register, copy_offset_register),
        "Reset_Handler data-copy destination store is not exact",
    )
    _require_two_operands(
        copy_data.code[3],
        operation="adds",
        first=copy_offset_register,
        second="#4",
        message="Reset_Handler data-copy stride is not exact",
    )

    data_start_register, _, _ = _require_memory(
        copy_loop.code[0],
        operation="ldr",
        base_register="pc",
        message="Reset_Handler _sdata pointer load is not exact",
    )
    data_end_register, _, _ = _require_memory(
        copy_loop.code[1],
        operation="ldr",
        base_register="pc",
        message="Reset_Handler _edata pointer load is not exact",
    )
    _require(
        _literal_value(copy_loop, copy_loop.code[0]) == SRAM_BASE
        and _literal_value(copy_loop, copy_loop.code[1]) == SRAM_BASE + 4,
        "Reset_Handler _sdata/_edata literals are not exact",
    )
    data_position_register, _, _ = _require_binary(
        copy_loop.code[2],
        operation="adds",
        second=data_start_register,
        third=copy_offset_register,
        message="Reset_Handler data-copy bound address is not exact",
    )
    _require_two_operands(
        copy_loop.code[3],
        operation="cmp",
        first=data_position_register,
        second=data_end_register,
        message="Reset_Handler data-copy bound comparison is not exact",
    )
    _require(
        copy_loop.code[4].operation == "bcc"
        and _branch_target(copy_loop.code[4]) == copy_data.code[0].address,
        "Reset_Handler data-copy loop branch is not exact",
    )
    bss_pointer, _, _ = _require_memory(
        copy_loop.code[5],
        operation="ldr",
        base_register="pc",
        message="Reset_Handler _sbss pointer load is not exact",
    )
    _require(
        _literal_value(copy_loop, copy_loop.code[5]) == SRAM_BASE + 4,
        "Reset_Handler _sbss literal is not exact",
    )
    _require(
        copy_loop.code[6].operation == "b"
        and _branch_target(copy_loop.code[6]) == fill_loop.code[0].address,
        "Reset_Handler does not enter the exact BSS bound check",
    )

    zero_register, _ = _require_two_operands(
        fill_zero.code[0],
        operation="movs",
        second="#0",
        message="Reset_Handler BSS zero seed is not exact",
    )
    _require_memory(
        fill_zero.code[1],
        operation="str",
        value_register=zero_register,
        base_register=bss_pointer,
        offset=0,
        message="Reset_Handler BSS zero store is not exact",
    )
    _require_two_operands(
        fill_zero.code[2],
        operation="adds",
        first=bss_pointer,
        second="#4",
        message="Reset_Handler BSS fill stride is not exact",
    )
    bss_end_register, _, _ = _require_memory(
        fill_loop.code[0],
        operation="ldr",
        base_register="pc",
        message="Reset_Handler _ebss pointer load is not exact",
    )
    _require(
        _literal_value(fill_loop, fill_loop.code[0]) == SRAM_BASE + 4,
        "Reset_Handler _ebss literal is not exact",
    )
    _require_two_operands(
        fill_loop.code[1],
        operation="cmp",
        first=bss_pointer,
        second=bss_end_register,
        message="Reset_Handler BSS bound comparison is not exact",
    )
    _require(
        fill_loop.code[2].operation == "bcc"
        and _branch_target(fill_loop.code[2]) == fill_zero.code[0].address,
        "Reset_Handler BSS fill loop branch is not exact",
    )
    _require(
        _calls(fill_loop.code[3], "__libc_init_array"),
        "Reset_Handler libc initialization call is not exact",
    )
    _require(
        _calls(fill_loop.code[4], "main"),
        "Reset_Handler main call is not exact",
    )
    _require(
        forever.code[0].operation == "b"
        and _branch_target(forever.code[0]) == forever.code[0].address,
        "Reset_Handler post-main LoopForever is not terminal",
    )

    wrapper = parse_function(disassembly, "hexcalstartupsysteminit")
    wrapper_code = wrapper.code
    _require(
        len(wrapper_code) == 3 and _is_contiguous(wrapper_code),
        "owned erratum wrapper instruction sequence is not exact",
    )
    sram_register, _, _ = _require_memory(
        wrapper_code[0],
        operation="ldr",
        base_register="pc",
        message="erratum wrapper does not load _sdata",
    )
    _require(
        _literal_value(wrapper, wrapper_code[0]) == SRAM_BASE,
        "erratum wrapper is not bound to _sdata at 0x20000000",
    )
    _require_memory(
        wrapper_code[1],
        operation="ldr",
        base_register=sram_register,
        offset=0,
        message="first reset-time data access is not an SRAM read",
    )
    _require(
        _branches_to(wrapper_code[2], "systeminit"),
        "erratum wrapper does not tail-branch directly to SystemInit",
    )


def verify_clock_register_configuration(disassembly: str) -> int:
    validator = parse_function(disassembly, "clock_register_configuration_valid")
    code = validator.code
    _require(len(code) == 16 and _is_contiguous(code), "full RCC validator shape is not exact")

    hsi_mask_register, _ = _require_two_operands(
        code[0],
        operation="movs",
        second="#244",
        message="full RCC HSI mask seed is not exact",
    )
    rcc_register, _, _ = _require_memory(
        code[1],
        operation="ldr",
        base_register="pc",
        message="full RCC validator base load is not exact",
    )
    _require(
        _literal_value(validator, code[1]) == RCC_BASE,
        "full RCC validator is not anchored to the exact RCC base",
    )
    _require_binary(
        code[2],
        operation="lsls",
        first=hsi_mask_register,
        second=hsi_mask_register,
        third="#6",
        message="full RCC HSI mask shift is not exact",
    )
    cr_register, _, _ = _require_memory(
        code[3],
        operation="ldr",
        base_register=rcc_register,
        offset=RCC_CR_OFFSET,
        message="full RCC validator does not read RCC CR",
    )
    return_register, _ = _require_two_operands(
        code[4],
        operation="movs",
        second="#0",
        message="full RCC validator false result seed is not exact",
    )
    _require_two_operands(
        code[5],
        operation="ands",
        first=cr_register,
        second=hsi_mask_register,
        message="full RCC CR mask operation is not exact",
    )
    expected_register, _ = _require_two_operands(
        code[6],
        operation="movs",
        second="#168",
        message="full RCC expected HSI seed is not exact",
    )
    cfgr_register, _, _ = _require_memory(
        code[7],
        operation="ldr",
        base_register=rcc_register,
        offset=RCC_CFGR_OFFSET,
        message="full RCC validator does not read RCC CFGR",
    )
    _require_binary(
        code[8],
        operation="lsls",
        first=expected_register,
        second=expected_register,
        third="#5",
        message="full RCC expected HSI shift is not exact",
    )
    _require_two_operands(
        code[9],
        operation="cmp",
        first=cr_register,
        second=expected_register,
        message="full RCC CR comparison is not exact",
    )
    _require(
        code[10].operation == "bne" and _branch_target(code[10]) == code[15].address,
        "full RCC CR mismatch does not return false",
    )
    cfgr_mask_register, _, _ = _require_memory(
        code[11],
        operation="ldr",
        base_register="pc",
        message="full RCC CFGR mask load is not exact",
    )
    _require(
        _literal_value(validator, code[11]) == RCC_CFGR_POLICY_MASK,
        "full RCC CFGR policy mask is not exact",
    )
    _require_two_operands(
        code[12],
        operation="ands",
        first=cfgr_register,
        second=cfgr_mask_register,
        message="full RCC CFGR mask operation is not exact",
    )
    _require_two_operands(
        code[13],
        operation="negs",
        first=return_register,
        second=cfgr_register,
        message="full RCC zero-result reduction is not exact",
    )
    _require_two_operands(
        code[14],
        operation="adcs",
        first=return_register,
        second=cfgr_register,
        message="full RCC boolean result reduction is not exact",
    )
    _require(
        code[15].operation == "bx" and _operand_text(code[15]) == "lr",
        "full RCC validator does not return directly",
    )
    _require(
        RCC_CLOCK_SIGNATURE_MASK == (244 << 6) and RCC_CLOCK_SIGNATURE == (168 << 5),
        "verifier full RCC constants are internally inconsistent",
    )
    return _estimate_path_cycles(
        code,
        branch_cycles={code[10].address: 1, code[15].address: 3},
        message="full RCC valid path",
    )


def verify_control_profile_state_machine(disassembly: str) -> int:
    frame_init = parse_function(disassembly, "high_rate_frame_init")
    init = frame_init.code
    _require(
        len(init) == 7 and _is_contiguous(init),
        "profile frame-initialization shape is not exact",
    )
    expected_init = (
        ("movs", "r3, #0"),
        ("strh", "r3, [r0, #0]"),
        ("adds", "r3, #8"),
        ("strb", "r3, [r0, #2]"),
        ("adds", "r3, #172"),
        ("strh", "r3, [r0, #4]"),
        ("bx", "lr"),
    )
    _require(
        tuple((item.operation, _operand_text(item)) for item in init) == expected_init,
        "profile frame initialization is not ALL_OFF=8/marker=180",
    )

    frame_advance = parse_function(disassembly, "high_rate_frame_advance")
    advance = frame_advance.code
    _require(
        len(advance) == 46 and _is_contiguous(advance),
        "profile frame-advance shape is not exact",
    )
    expected_advance: tuple[tuple[str, str] | None, ...] = (
        ("movs", "r3, r0"),
        ("ldrb", "r0, [r0, #0]"),
        ("cmp", "r0, #1"),
        None,
        ("movs", "r2, #8"),
        ("cmp", "r0, #2"),
        None,
        ("cmp", "r0, #0"),
        None,
        ("movs", "r1, #1"),
        ("strb", "r2, [r3, #2]"),
        ("strh", "r1, [r3, #0]"),
        ("movs", "r2, #20"),
        None,
        ("movs", "r2, #2"),
        ("ldrb", "r1, [r3, #1]"),
        ("strb", "r2, [r3, #0]"),
        None,
        ("lsls", "r1, r1, #2"),
        ("ldrb", "r0, [r1, r2]"),
        ("adds", "r2, r2, r1"),
        ("strb", "r0, [r3, #2]"),
        ("movs", "r0, #0"),
        ("ldrh", "r2, [r2, #2]"),
        ("strh", "r2, [r3, #4]"),
        ("bx", "lr"),
        ("strb", "r2, [r3, #2]"),
        ("ldrb", "r2, [r3, #1]"),
        ("movs", "r1, #1"),
        ("adds", "r2, #1"),
        ("uxtb", "r2, r2"),
        ("movs", "r0, #0"),
        ("strb", "r2, [r3, #1]"),
        ("cmp", "r2, #6"),
        None,
        ("strh", "r0, [r3, #0]"),
        ("adds", "r2, #174"),
        ("movs", "r0, r1"),
        None,
        ("strb", "r1, [r3, #0]"),
        None,
        ("movs", "r0, #0"),
        ("strb", "r2, [r3, #2]"),
        ("strh", "r0, [r3, #0]"),
        ("movs", "r2, #180"),
        None,
    )
    for instruction, expected in zip(advance, expected_advance, strict=True):
        if expected is not None:
            _require(
                (instruction.operation, _operand_text(instruction)) == expected,
                "profile frame-advance instruction sequence is not exact",
            )
    expected_branches = {
        3: ("beq", 14),
        6: ("beq", 26),
        8: ("bne", 41),
        13: ("b", 24),
        34: ("bne", 39),
        38: ("b", 24),
        40: ("b", 12),
        45: ("b", 24),
    }
    _require(
        all(
            advance[source].operation == operation
            and _branch_target(advance[source]) == advance[target].address
            for source, (operation, target) in expected_branches.items()
        ),
        "profile frame-advance branches are not exact",
    )
    schedule_load = _require_memory(
        advance[17],
        operation="ldr",
        base_register="pc",
        message="profile frame advance does not load CONTROL_SCHEDULE",
    )
    _require(schedule_load[0] == "r2", "profile schedule base register is not exact")
    schedule_address = _literal_value(frame_advance, advance[17])
    _require(schedule_address is not None, "profile schedule address literal is missing")
    assert schedule_address is not None
    return schedule_address


def verify_control_schedule_bytes(schedule: bytes) -> None:
    _require(
        schedule == EXPECTED_CONTROL_SCHEDULE,
        "CONTROL_SCHEDULE is not exactly ANT1..ANT6/200 us with no illegal active codes",
    )


def verify_control_schedule_symbol_identity(
    evidence: HexcalControlFlowEvidence,
    *,
    schedule_address: int,
    schedule_size: int,
) -> None:
    _require(
        schedule_address == evidence.control_schedule_address
        and schedule_size == len(EXPECTED_CONTROL_SCHEDULE),
        "CONTROL_SCHEDULE symbol identity/size does not match the state machine",
    )


def _section_byte_map(section_dump: str) -> dict[int, int]:
    values: dict[int, int] = {}
    for line in section_dump.lower().splitlines():
        match = re.match(r"^\s*([0-9a-f]+)\s+((?:[0-9a-f]{8}\s+){1,4})", line)
        if match is None:
            continue
        address = int(match.group(1), 16)
        data = bytes.fromhex("".join(match.group(2).split()))
        values.update((address + offset, value) for offset, value in enumerate(data))
    return values


def verify_text_section_bytes(text_section: bytes) -> None:
    _require(
        len(text_section) == TEXT_SECTION_SIZE,
        "executable .text section size does not match the frozen image",
    )
    _require(
        hashlib.sha256(text_section).hexdigest() == EXPECTED_TEXT_SHA256,
        "executable .text section digest does not match the frozen image",
    )


def verify_vector_table_bytes(
    vector_table: bytes,
    *,
    reset_handler_address: int,
    default_handler_address: int,
) -> None:
    """Bind every word in the owned STM32C011 vector table."""

    _require(
        len(vector_table) == VECTOR_TABLE_WORDS * 4,
        "interrupt vector table size is not exactly 48 words",
    )
    words = tuple(
        int.from_bytes(vector_table[offset : offset + 4], "little")
        for offset in range(0, len(vector_table), 4)
    )
    _require(words[0] == SRAM_END, "vector initial stack pointer is not the SRAM limit")
    _require(
        reset_handler_address & 1 == 0 and words[1] == reset_handler_address | 1,
        "reset vector is not the exact Thumb Reset_Handler entry",
    )
    _require(
        default_handler_address & 1 == 0,
        "Default_Handler symbol is not halfword aligned",
    )
    default_vector = default_handler_address | 1
    expected_tail = tuple(
        default_vector if index in DEFAULT_VECTOR_INDICES else 0
        for index in range(2, VECTOR_TABLE_WORDS)
    )
    _require(
        words[2:] == expected_tail,
        "exception/IRQ vectors do not match the exact reserved/Default_Handler map",
    )


def verify_default_handler_terminal_loop(disassembly: str, *, default_handler_address: int) -> None:
    """Prove that the shared handler cannot touch hardware or escape."""

    normalized = disassembly.lower()
    header = next(
        (
            item
            for item in FUNCTION_HEADER_RE.finditer(normalized)
            if int(item.group(1), 16) == default_handler_address
        ),
        None,
    )
    _require(header is not None, "cannot isolate Default_Handler disassembly")
    assert header is not None
    handler = parse_function(normalized, header.group(2))
    code = handler.code
    _require(
        len(code) == 1
        and code[0].address == default_handler_address
        and code[0].operation == "b"
        and _branch_target(code[0]) == default_handler_address,
        "Default_Handler is not an exact terminal self-loop",
    )


def _symbol_address(symbols: str, name: str) -> int:
    matches = re.findall(
        rf"^([0-9a-f]+)\s+\w\s+{re.escape(name.lower())}$",
        symbols.lower(),
        re.MULTILINE,
    )
    _require(len(matches) == 1, f"{name} symbol identity is not unique")
    return int(matches[0], 16)


def _require_no_external_function_interior_entries(
    disassembly: str, function: FunctionDisassembly
) -> None:
    """Reject direct transfers from another function into an owned function body."""

    code = function.code
    _require(bool(code), f"{function.name} contains no executable entry")
    start = code[0].address
    end = code[-1].next_address
    normalized = disassembly.lower()
    function_names = dict.fromkeys(
        header.group(2) for header in FUNCTION_HEADER_RE.finditer(normalized)
    )
    for name in function_names:
        if name == function.name:
            continue
        for instruction in parse_function(normalized, name).code:
            target = _branch_target(instruction)
            _require(
                target is None or target == start or not (start < target < end),
                f"{function.name} has an external interior control-flow entry",
            )


def verify_watchdog_initialization(
    disassembly: str, main: FunctionDisassembly | None = None
) -> None:
    watchdog = parse_function(disassembly, "watchdog_initialize")
    code_list = list(watchdog.code)
    while code_list and code_list[-1].operation == "nop":
        code_list.pop()
    code = tuple(code_list)
    _require(len(code) == 15, "watchdog_initialize instruction sequence is not exact")

    base_register, _, _ = _require_memory(
        code[0], operation="ldr", base_register="pc", message="IWDG base load is not exact"
    )
    _require(
        _literal_value(watchdog, code[0]) == IWDG_BASE,
        "watchdog_initialize does not bind the exact IWDG base",
    )
    value_register, _, _ = _require_memory(
        code[1], operation="ldr", base_register="pc", message="IWDG enable-key load missing"
    )
    _require(_literal_value(watchdog, code[1]) == IWDG_ENABLE_KEY, "IWDG enable key is not exact")
    _require_memory(
        code[2],
        operation="str",
        value_register=value_register,
        base_register=base_register,
        offset=0,
        message="IWDG enable-key store is not exact",
    )
    _require_memory(
        code[3],
        operation="ldr",
        value_register=value_register,
        base_register="pc",
        message="IWDG write-access-key load missing",
    )
    _require(
        _literal_value(watchdog, code[3]) == IWDG_WRITE_ACCESS_KEY,
        "IWDG write-access key is not exact",
    )
    _require_memory(
        code[4],
        operation="str",
        value_register=value_register,
        base_register=base_register,
        offset=0,
        message="IWDG write-access-key store is not exact",
    )
    _require_two_operands(
        code[5],
        operation="movs",
        first=value_register,
        second="#0",
        message="IWDG /4 prescaler constant is not exact",
    )
    _require_memory(
        code[6],
        operation="str",
        value_register=value_register,
        base_register=base_register,
        offset=4,
        message="IWDG /4 prescaler store is not exact",
    )
    _require_two_operands(
        code[7],
        operation="adds",
        first=value_register,
        second=f"#{IWDG_RELOAD_VALUE}",
        message="IWDG reload constant is not exact",
    )
    _require_memory(
        code[8],
        operation="str",
        value_register=value_register,
        base_register=base_register,
        offset=8,
        message="IWDG reload store is not exact",
    )
    status_register, _, _ = _require_memory(
        code[9],
        operation="ldr",
        base_register=base_register,
        offset=12,
        message="IWDG status wait load is not exact",
    )
    _require_two_operands(
        code[10],
        operation="cmp",
        first=status_register,
        second="#0",
        message="IWDG status wait comparison is not exact",
    )
    _require(
        code[11].operation == "bne" and _branch_target(code[11]) == code[9].address,
        "IWDG status wait loop is not exact",
    )
    _require_memory(
        code[12],
        operation="ldr",
        value_register=value_register,
        base_register="pc",
        message="IWDG refresh-key load missing",
    )
    _require(
        _literal_value(watchdog, code[12]) == IWDG_REFRESH_KEY,
        "IWDG refresh key is not exact",
    )
    _require_memory(
        code[13],
        operation="str",
        value_register=value_register,
        base_register=base_register,
        offset=0,
        message="IWDG refresh-key store is not exact",
    )
    _require(
        code[14].operation == "bx" and _operand_text(code[14]) == "lr",
        "watchdog_initialize does not return directly after refresh",
    )

    if main is not None:
        calls = [item for item in main.code if _calls(item, "watchdog_initialize")]
        _require(len(calls) == 1, "main must call watchdog_initialize exactly once")


def verify_boot_hardware_initialization(
    main: FunctionDisassembly,
    *,
    cpsid_index: int,
    watchdog_call_index: int,
) -> BootHardwareEvidence:
    code = main.code
    gpio_start = cpsid_index + 1
    gpio_boot = code[gpio_start : gpio_start + 20]
    _require(
        len(gpio_boot) == 20
        and code[cpsid_index].next_address == gpio_boot[0].address
        and _is_contiguous(gpio_boot),
        "GPIOA clock/preload boot sequence is not exact and contiguous",
    )
    gpio_clock_bit, _ = _require_two_operands(
        gpio_boot[0],
        operation="movs",
        second="#1",
        message="GPIOA IOPENR enable bit is not exact",
    )
    rcc_register, _, _ = _require_memory(
        gpio_boot[1],
        operation="ldr",
        base_register="pc",
        message="GPIOA boot sequence does not load the RCC base",
    )
    _require(
        _literal_value(main, gpio_boot[1]) == RCC_BASE,
        "GPIOA boot sequence is not anchored to the exact RCC base",
    )
    iopenr_value, _, _ = _require_memory(
        gpio_boot[2],
        operation="ldr",
        base_register=rcc_register,
        offset=RCC_IOPENR_OFFSET,
        message="GPIOA IOPENR enable read is not exact",
    )
    _require_two_operands(
        gpio_boot[3],
        operation="orrs",
        first=iopenr_value,
        second=gpio_clock_bit,
        message="GPIOA IOPENR enable operation is not exact",
    )
    _require_memory(
        gpio_boot[4],
        operation="str",
        value_register=iopenr_value,
        base_register=rcc_register,
        offset=RCC_IOPENR_OFFSET,
        message="GPIOA IOPENR enable store is not exact",
    )
    iopenr_readback, _, _ = _require_memory(
        gpio_boot[5],
        operation="ldr",
        base_register=rcc_register,
        offset=RCC_IOPENR_OFFSET,
        message="GPIOA IOPENR enable readback is missing",
    )
    _require_memory(
        gpio_boot[6],
        operation="str",
        value_register=iopenr_readback,
        base_register="sp",
        offset=16,
        message="GPIOA IOPENR volatile readback store is not exact",
    )
    _require_memory(
        gpio_boot[7],
        operation="ldr",
        value_register=iopenr_readback,
        base_register="sp",
        offset=16,
        message="GPIOA IOPENR volatile readback reload is not exact",
    )
    _require_two_operands(
        gpio_boot[8],
        operation="tst",
        first=iopenr_readback,
        second=gpio_clock_bit,
        message="GPIOA IOPENR enable readback test is not exact",
    )
    _require(
        gpio_boot[9].operation == "bne" and _branch_target(gpio_boot[9]) == gpio_boot[11].address,
        "GPIOA IOPENR enable readback does not fail closed",
    )
    startup_failure_address = _branch_target(gpio_boot[10])
    _require(
        gpio_boot[10].operation == "b" and startup_failure_address is not None,
        "GPIOA IOPENR failure does not enter the startup terminal stop",
    )
    gpio_register, _ = _require_two_operands(
        gpio_boot[11],
        operation="movs",
        second="#160",
        message="GPIOA preload base seed is not exact",
    )
    all_off_register, _, _ = _require_memory(
        gpio_boot[12],
        operation="ldr",
        base_register="pc",
        message="GPIOA ALL_OFF preload literal load is missing",
    )
    _require(
        _literal_value(main, gpio_boot[12]) == ALL_OFF_BSRR_WORD,
        "GPIOA preload word is not exactly ALL_OFF",
    )
    _require_binary(
        gpio_boot[13],
        operation="lsls",
        first=gpio_register,
        second=gpio_register,
        third="#23",
        message="GPIOA preload base shift is not exact",
    )
    _require_memory(
        gpio_boot[14],
        operation="str",
        value_register=all_off_register,
        base_register=gpio_register,
        offset=GPIO_BSRR_OFFSET,
        message="GPIOA preload does not write the exact ALL_OFF register to BSRR",
    )
    pin_mask_register, _ = _require_two_operands(
        gpio_boot[15],
        operation="movs",
        second=f"#{GPIO_CONTROL_PIN_MASK}",
        message="GPIOA preload ODR mask is not exact",
    )
    odr_value, _, _ = _require_memory(
        gpio_boot[16],
        operation="ldr",
        base_register=gpio_register,
        offset=GPIO_ODR_OFFSET,
        message="GPIOA preload ODR readback is missing",
    )
    _require_two_operands(
        gpio_boot[17],
        operation="ands",
        first=odr_value,
        second=pin_mask_register,
        message="GPIOA preload ODR mask operation is not exact",
    )
    _require_two_operands(
        gpio_boot[18],
        operation="cmp",
        first=odr_value,
        second="#8",
        message="GPIOA preload ODR comparison is not exactly ALL_OFF",
    )
    _require(
        gpio_boot[19].operation == "bne"
        and _branch_target(gpio_boot[19]) == startup_failure_address,
        "GPIOA preload ODR mismatch is not fail-closed",
    )
    _require_exact_branch_entries(
        code,
        gpio_boot,
        ((gpio_boot[9].address, gpio_boot[11].address),),
        "GPIOA clock/preload boot sequence has an interior bypass entry",
    )

    clock_start = gpio_start + len(gpio_boot)
    clock_boot = code[clock_start : clock_start + 9]
    _require(
        len(clock_boot) == 9
        and gpio_boot[-1].next_address == clock_boot[0].address
        and _is_contiguous(clock_boot),
        "reset-clock validation sequence is not exact and contiguous",
    )
    _require(
        _calls(clock_boot[0], "systemcoreclockupdate")
        and _calls(clock_boot[1], "clock_register_configuration_valid"),
        "reset-clock validation does not call the exact helper entries in order",
    )
    _require_two_operands(
        clock_boot[2],
        operation="cmp",
        first="r0",
        second="#0",
        message="reset-clock full RCC result check is not exact",
    )
    _require(
        clock_boot[3].operation == "beq"
        and _branch_target(clock_boot[3]) == startup_failure_address,
        "invalid reset RCC configuration is not fail-closed",
    )
    core_clock_address, _, _ = _require_memory(
        clock_boot[4],
        operation="ldr",
        base_register="pc",
        message="reset SystemCoreClock address load is not exact",
    )
    _require(
        _literal_value(main, clock_boot[4]) == SRAM_BASE,
        "reset SystemCoreClock address is not exact",
    )
    expected_clock, _, _ = _require_memory(
        clock_boot[5],
        operation="ldr",
        base_register="pc",
        message="reset core-clock expected-value load is not exact",
    )
    _require(
        _literal_value(main, clock_boot[5]) == RESET_CORE_CLOCK_HZ,
        "reset core-clock expected value is not exactly 12 MHz",
    )
    actual_clock, _, _ = _require_memory(
        clock_boot[6],
        operation="ldr",
        base_register=core_clock_address,
        offset=0,
        message="reset SystemCoreClock read is not exact",
    )
    _require_two_operands(
        clock_boot[7],
        operation="cmp",
        first=actual_clock,
        second=expected_clock,
        message="reset SystemCoreClock comparison is not exact",
    )
    _require(
        clock_boot[8].operation == "bne"
        and _branch_target(clock_boot[8]) == startup_failure_address,
        "invalid reset SystemCoreClock value is not fail-closed",
    )
    _require_exact_branch_entries(
        code,
        clock_boot,
        (),
        "reset-clock validation sequence has an interior bypass entry",
    )

    tim_start = clock_start + len(clock_boot)
    tim_boot = code[tim_start : tim_start + 27]
    _require(
        len(tim_boot) == 27
        and clock_boot[-1].next_address == tim_boot[0].address
        and _is_contiguous(tim_boot)
        and tim_start + len(tim_boot) == watchdog_call_index - 23,
        "TIM3 clock/reset boot sequence is not exact and contiguous",
    )
    tim_bit_register, _ = _require_two_operands(
        tim_boot[0],
        operation="movs",
        second="#2",
        message="TIM3 clock/reset bit is not exact",
    )
    apbenr_value, _, _ = _require_memory(
        tim_boot[1],
        operation="ldr",
        base_register=rcc_register,
        offset=RCC_APBENR1_OFFSET,
        message="TIM3 APBENR1 enable read is not exact",
    )
    _require_two_operands(
        tim_boot[2],
        operation="orrs",
        first=apbenr_value,
        second=tim_bit_register,
        message="TIM3 APBENR1 enable operation is not exact",
    )
    _require_memory(
        tim_boot[3],
        operation="str",
        value_register=apbenr_value,
        base_register=rcc_register,
        offset=RCC_APBENR1_OFFSET,
        message="TIM3 APBENR1 enable store is not exact",
    )
    apbenr_readback, _, _ = _require_memory(
        tim_boot[4],
        operation="ldr",
        base_register=rcc_register,
        offset=RCC_APBENR1_OFFSET,
        message="TIM3 APBENR1 enable readback is missing",
    )
    for instruction, operation in ((tim_boot[5], "str"), (tim_boot[6], "ldr")):
        _require_memory(
            instruction,
            operation=operation,
            value_register=apbenr_readback,
            base_register="sp",
            offset=20,
            message="TIM3 APBENR1 volatile readback transfer is not exact",
        )
    _require_two_operands(
        tim_boot[7],
        operation="tst",
        first=apbenr_readback,
        second=tim_bit_register,
        message="TIM3 APBENR1 enable readback test is not exact",
    )
    _require(
        tim_boot[8].operation == "beq" and _branch_target(tim_boot[8]) == startup_failure_address,
        "TIM3 APBENR1 enable failure is not fail-closed",
    )

    reset_assert_value, _, _ = _require_memory(
        tim_boot[9],
        operation="ldr",
        base_register=rcc_register,
        offset=RCC_APBRSTR1_OFFSET,
        message="TIM3 reset-assert register read is not exact",
    )
    _require_two_operands(
        tim_boot[10],
        operation="orrs",
        first=reset_assert_value,
        second=tim_bit_register,
        message="TIM3 reset assert operation is not exact",
    )
    _require_memory(
        tim_boot[11],
        operation="str",
        value_register=reset_assert_value,
        base_register=rcc_register,
        offset=RCC_APBRSTR1_OFFSET,
        message="TIM3 reset assert store is not exact",
    )
    reset_assert_readback, _, _ = _require_memory(
        tim_boot[12],
        operation="ldr",
        base_register=rcc_register,
        offset=RCC_APBRSTR1_OFFSET,
        message="TIM3 reset-assert readback is missing",
    )
    for instruction, operation in ((tim_boot[13], "str"), (tim_boot[14], "ldr")):
        _require_memory(
            instruction,
            operation=operation,
            value_register=reset_assert_readback,
            base_register="sp",
            offset=20,
            message="TIM3 reset-assert volatile readback transfer is not exact",
        )
    _require_two_operands(
        tim_boot[15],
        operation="tst",
        first=reset_assert_readback,
        second=tim_bit_register,
        message="TIM3 reset-assert readback test is not exact",
    )
    _require(
        tim_boot[16].operation == "beq" and _branch_target(tim_boot[16]) == startup_failure_address,
        "TIM3 reset-assert failure is not fail-closed",
    )

    reset_deassert_value, _, _ = _require_memory(
        tim_boot[17],
        operation="ldr",
        base_register=rcc_register,
        offset=RCC_APBRSTR1_OFFSET,
        message="TIM3 reset-deassert register read is not exact",
    )
    _require_two_operands(
        tim_boot[18],
        operation="bics",
        first=reset_deassert_value,
        second=tim_bit_register,
        message="TIM3 reset deassert operation is not exact",
    )
    _require_memory(
        tim_boot[19],
        operation="str",
        value_register=reset_deassert_value,
        base_register=rcc_register,
        offset=RCC_APBRSTR1_OFFSET,
        message="TIM3 reset deassert store is not exact",
    )
    reset_deassert_readback, _, _ = _require_memory(
        tim_boot[20],
        operation="ldr",
        base_register=rcc_register,
        offset=RCC_APBRSTR1_OFFSET,
        message="TIM3 reset-deassert readback is missing",
    )
    _require_memory(
        tim_boot[21],
        operation="str",
        value_register=reset_deassert_readback,
        base_register="sp",
        offset=20,
        message="TIM3 reset-deassert volatile readback store is not exact",
    )
    reset_deassert_validation, _, _ = _require_memory(
        tim_boot[22],
        operation="ldr",
        base_register="sp",
        offset=20,
        message="TIM3 reset-deassert volatile readback reload is not exact",
    )
    zero_register, _ = _require_two_operands(
        tim_boot[23],
        operation="movs",
        second=reset_deassert_validation,
        message="TIM3 reset-deassert result transfer is not exact",
    )
    _require_two_operands(
        tim_boot[24],
        operation="ands",
        first=zero_register,
        second=tim_bit_register,
        message="TIM3 reset-deassert masked result is not exact",
    )
    _require_two_operands(
        tim_boot[25],
        operation="tst",
        first=reset_deassert_validation,
        second=tim_bit_register,
        message="TIM3 reset-deassert readback test is not exact",
    )
    _require(
        tim_boot[26].operation == "bne" and _branch_target(tim_boot[26]) == startup_failure_address,
        "TIM3 reset-deassert failure is not fail-closed",
    )
    _require_exact_branch_entries(
        code,
        tim_boot,
        (),
        "TIM3 clock/reset boot sequence has an interior bypass entry",
    )

    address_to_index = {item.address: index for index, item in enumerate(code)}
    _require(
        startup_failure_address in address_to_index,
        "startup failure target is invalid",
    )
    assert startup_failure_address is not None
    startup_failure_index = address_to_index[startup_failure_address]
    _require_terminal_spin(
        code[startup_failure_index : startup_failure_index + 2],
        "startup hardware validation failure is not a terminal stop",
    )

    return BootHardwareEvidence(
        gpio_clock_store_index=gpio_start + 4,
        gpio_clock_readback_index=gpio_start + 8,
        gpio_preload_store_index=gpio_start + 14,
        reset_clock_gate_index=clock_start + 8,
        tim_clock_store_index=tim_start + 3,
        tim_clock_readback_index=tim_start + 7,
        tim_reset_assert_store_index=tim_start + 11,
        tim_reset_assert_readback_index=tim_start + 15,
        tim_reset_deassert_store_index=tim_start + 19,
        tim_reset_deassert_readback_index=tim_start + 25,
        tim_zero_register=zero_register,
        tim_prescaler_register=tim_bit_register,
        startup_failure_address=startup_failure_address,
    )


def verify_gpio_output_configuration(
    main: FunctionDisassembly,
    *,
    watchdog_call_index: int,
) -> tuple[int, int, str, str]:
    code = main.code
    start = watchdog_call_index + 1
    block = code[start : start + 12]
    _require(
        len(block) == 12
        and code[watchdog_call_index].next_address == block[0].address
        and _is_contiguous(block),
        "GPIOA output configuration is not immediately after watchdog initialization",
    )
    gpio_register, _ = _require_two_operands(
        block[0],
        operation="movs",
        second="#160",
        message="GPIOA output configuration base seed is not exact",
    )
    pin_mask_register, _ = _require_two_operands(
        block[1],
        operation="movs",
        second=f"#{GPIO_CONTROL_PIN_MASK}",
        message="GPIOA OTYPER pin mask is not exact",
    )
    _require_binary(
        block[2],
        operation="lsls",
        first=gpio_register,
        second=gpio_register,
        third="#23",
        message="GPIOA output configuration base shift is not exact",
    )
    otyper_value, _, _ = _require_memory(
        block[3],
        operation="ldr",
        base_register=gpio_register,
        offset=4,
        message="GPIOA OTYPER read is not exact",
    )
    _require_two_operands(
        block[4],
        operation="bics",
        first=otyper_value,
        second=pin_mask_register,
        message="GPIOA OTYPER is not cleared to push-pull",
    )
    _require_memory(
        block[5],
        operation="str",
        value_register=otyper_value,
        base_register=gpio_register,
        offset=4,
        message="GPIOA OTYPER write is not exact",
    )
    mode_mask_register, _ = _require_two_operands(
        block[6],
        operation="movs",
        second="#255",
        message="GPIOA MODER clear mask seed is not exact",
    )
    moder_value, moder_base, _ = _require_memory(
        block[7],
        operation="ldr",
        offset=0,
        message="GPIOA MODER read is not exact",
    )
    _require(moder_base == gpio_register, "GPIOA MODER and OTYPER bases differ")
    _require_two_operands(
        block[8],
        operation="bics",
        first=moder_value,
        second=mode_mask_register,
        message="GPIOA MODER low fields are not cleared",
    )
    _require_two_operands(
        block[9],
        operation="subs",
        first=mode_mask_register,
        second="#170",
        message="GPIOA MODER output-mode value is not exactly 0x55",
    )
    _require_two_operands(
        block[10],
        operation="orrs",
        first=mode_mask_register,
        second=moder_value,
        message="GPIOA MODER output-mode merge is not exact",
    )
    _require_memory(
        block[11],
        operation="str",
        value_register=mode_mask_register,
        base_register=gpio_register,
        offset=0,
        message="GPIOA MODER write is not exact",
    )
    _require_exact_branch_entries(
        code,
        block,
        (),
        "GPIOA output configuration has an interior bypass entry",
    )
    return start + 5, start + 11, gpio_register, pin_mask_register


def verify_post_output_all_off_gate(
    main: FunctionDisassembly,
    *,
    moder_store_index: int,
    gpio_register: str,
    pin_mask_register: str,
) -> int:
    code = main.code
    start = moder_store_index + 1
    gate = code[start : start + 4]
    _require(
        len(gate) == 4
        and code[moder_store_index].next_address == gate[0].address
        and _is_contiguous(gate),
        "post-output GPIOA ODR gate is not exact and contiguous",
    )
    odr_value, _, _ = _require_memory(
        gate[0],
        operation="ldr",
        base_register=gpio_register,
        offset=GPIO_ODR_OFFSET,
        message="post-output GPIOA ODR read is not exact",
    )
    _require_two_operands(
        gate[1],
        operation="ands",
        first=odr_value,
        second=pin_mask_register,
        message="post-output GPIOA ODR mask operation is not exact",
    )
    _require_two_operands(
        gate[2],
        operation="cmp",
        first=odr_value,
        second="#8",
        message="post-output GPIOA ODR comparison is not exactly ALL_OFF",
    )
    failure_address = _branch_target(gate[3])
    _require(
        gate[3].operation == "bne" and failure_address is not None,
        "post-output GPIOA ODR mismatch is not fail-closed",
    )
    address_to_index = {item.address: index for index, item in enumerate(code)}
    _require(failure_address in address_to_index, "post-output GPIOA failure target is invalid")
    assert failure_address is not None
    failure_index = address_to_index[failure_address]
    failure = code[failure_index : failure_index + 3]
    _require(
        len(failure) == 3 and _is_contiguous(failure),
        "post-output GPIOA failure loop is not exact and contiguous",
    )
    _require_all_off_bsrr_store(
        main,
        failure,
        known_gpio_register=gpio_register,
        message="post-output GPIOA failure loop does not write ALL_OFF",
    )
    _require_terminal_spin(
        failure,
        "post-output GPIOA failure loop is not terminal",
    )
    _require_exact_branch_entries(
        code,
        gate,
        (),
        "post-output GPIOA ODR gate has an interior bypass entry",
    )
    _require_exact_branch_entries(
        code,
        failure,
        (
            (gate[3].address, failure[0].address),
            (failure[-1].address, failure[0].address),
        ),
        "post-output GPIOA failure loop has an unexpected entry",
    )
    return start + 3


def verify_tim3_initialization(
    main: FunctionDisassembly,
    *,
    timer_register: str,
    zero_register: str,
    prescaler_register: str,
    startup_failure_address: int,
    watchdog_call_index: int,
) -> int:
    code = main.code
    block_start = watchdog_call_index - 23
    _require(block_start >= 0, "TIM3 initialization/readback block is missing")
    block = code[block_start:watchdog_call_index]
    _require(
        len(block) == 23 and _is_contiguous(block),
        "TIM3 initialization/readback block is not exact and contiguous",
    )
    loaded_timer_register, _, _ = _require_memory(
        block[0],
        operation="ldr",
        base_register="pc",
        message="TIM3 peripheral base load is not exact",
    )
    _require(
        loaded_timer_register == timer_register and _literal_value(main, block[0]) == TIM3_BASE,
        "TIM3 initialization is not anchored to the exact TIM3 base",
    )
    prescaler_operands = _require_two_operands(
        block[1],
        operation="adds",
        first=prescaler_register,
        second="#9",
        message="TIM3 prescaler calculation is not exact",
    )
    _require(
        prescaler_operands[0] == prescaler_register,
        "TIM3 prescaler register is not exact",
    )
    _require_memory(
        block[2],
        operation="str",
        value_register=zero_register,
        base_register=timer_register,
        offset=TIM3_CR1_OFFSET,
        message="TIM3 CR1 disable store is not exact",
    )
    _require_memory(
        block[3],
        operation="str",
        value_register=zero_register,
        base_register=timer_register,
        offset=TIM3_DIER_OFFSET,
        message="TIM3 DIER zero store is not exact",
    )
    _require_memory(
        block[4],
        operation="str",
        value_register=prescaler_register,
        base_register=timer_register,
        offset=TIM3_PSC_OFFSET,
        message="TIM3 PSC store is not exact",
    )
    auto_reload_register, _, _ = _require_memory(
        block[5],
        operation="ldr",
        base_register="pc",
        message="TIM3 ARR literal load is not exact",
    )
    _require(
        _literal_value(main, block[5]) == TIM3_AUTO_RELOAD,
        "TIM3 ARR constant is not exactly 65535",
    )
    _require_memory(
        block[6],
        operation="str",
        value_register=auto_reload_register,
        base_register=timer_register,
        offset=TIM3_ARR_OFFSET,
        message="TIM3 ARR store is not exact",
    )
    cen_register = _require_memory(
        block[7],
        operation="str",
        base_register=timer_register,
        offset=TIM3_EGR_OFFSET,
        message="TIM3 update-generation store is not exact",
    )[0]
    cen_seed = _last_register_write(code, cen_register, block_start + 7)
    _require(cen_seed is not None, "TIM3 CEN/UG seed is missing")
    assert cen_seed is not None
    _require_two_operands(
        cen_seed,
        operation="movs",
        first=cen_register,
        second=f"#{TIM3_CR1_CEN}",
        message="TIM3 CEN/UG seed is not exact",
    )
    _require_memory(
        block[8],
        operation="str",
        value_register=zero_register,
        base_register=timer_register,
        offset=TIM3_SR_OFFSET,
        message="TIM3 status clear is not exact",
    )
    _require_memory(
        block[9],
        operation="str",
        value_register=zero_register,
        base_register=timer_register,
        offset=TIM3_CNT_OFFSET,
        message="TIM3 counter clear is not exact",
    )
    _require_memory(
        block[10],
        operation="str",
        value_register=cen_register,
        base_register=timer_register,
        offset=TIM3_CR1_OFFSET,
        message="TIM3 CEN enable store is not exact",
    )

    readback_register, _, _ = _require_memory(
        block[11],
        operation="ldr",
        base_register=timer_register,
        offset=TIM3_PSC_OFFSET,
        message="TIM3 PSC readback is missing",
    )
    _require_two_operands(
        block[12],
        operation="cmp",
        first=readback_register,
        second=f"#{TIM3_PRESCALER}",
        message="TIM3 PSC readback comparison is not exactly 11",
    )
    failure_target = _branch_target(block[13])
    _require(block[13].operation == "bne", "TIM3 PSC mismatch is not fail-closed")
    arr_readback, _, _ = _require_memory(
        block[14],
        operation="ldr",
        base_register=timer_register,
        offset=TIM3_ARR_OFFSET,
        message="TIM3 ARR readback is missing",
    )
    _require_two_operands(
        block[15],
        operation="cmp",
        first=arr_readback,
        second=auto_reload_register,
        message="TIM3 ARR readback comparison is not exact",
    )
    _require(
        block[16].operation == "bne" and _branch_target(block[16]) == failure_target,
        "TIM3 ARR mismatch is not fail-closed",
    )
    dier_readback, _, _ = _require_memory(
        block[17],
        operation="ldr",
        base_register=timer_register,
        offset=TIM3_DIER_OFFSET,
        message="TIM3 DIER readback is missing",
    )
    _require_two_operands(
        block[18],
        operation="cmp",
        first=dier_readback,
        second="#0",
        message="TIM3 DIER readback comparison is not zero",
    )
    _require(
        block[19].operation == "bne" and _branch_target(block[19]) == failure_target,
        "TIM3 DIER mismatch is not fail-closed",
    )
    cr1_readback, _, _ = _require_memory(
        block[20],
        operation="ldr",
        base_register=timer_register,
        offset=TIM3_CR1_OFFSET,
        message="TIM3 CR1 readback is missing",
    )
    _require_binary(
        block[21],
        operation="lsls",
        first=cr1_readback,
        second=cr1_readback,
        third="#31",
        message="TIM3 CEN readback test is not exact",
    )
    _require(
        block[22].operation == "bpl" and _branch_target(block[22]) == failure_target,
        "TIM3 disabled readback is not fail-closed",
    )
    _require(
        failure_target == startup_failure_address,
        "TIM3 readback failure does not use the startup terminal stop",
    )
    address_to_index = {item.address: index for index, item in enumerate(code)}
    _require(
        startup_failure_address in address_to_index,
        "TIM3 readback failure target is invalid",
    )
    failure_index = address_to_index[startup_failure_address]
    failure_block = code[failure_index : failure_index + 2]
    _require_terminal_spin(failure_block, "TIM3 readback failure is not a terminal spin")
    _require_dominates(
        code,
        block_start,
        [watchdog_call_index],
        "exact TIM3 initialization/readback does not dominate watchdog startup",
    )
    return block_start


def _find_tight_poll(main: FunctionDisassembly) -> tuple[int, tuple[Instruction, ...], str, int]:
    code = main.code
    address_to_index = {item.address: index for index, item in enumerate(code)}
    candidates: list[tuple[int, tuple[Instruction, ...], str, int]] = []
    for branch_index, instruction in enumerate(code):
        if instruction.operation != "bmi":
            continue
        target = _branch_target(instruction)
        if target is None or target not in address_to_index:
            continue
        start_index = address_to_index[target]
        if start_index >= branch_index:
            continue
        loop = tuple(code[start_index : branch_index + 1])
        if len(loop) != 5 or not _is_contiguous(loop):
            continue
        timer_load = _memory_operands(loop[0])
        deadline_load = _memory_operands(loop[1])
        subtract = _binary_operands(loop[2])
        shift = _binary_operands(loop[3])
        if (
            loop[0].operation != "ldr"
            or timer_load is None
            or timer_load[2] != TIM3_CNT_OFFSET
            or loop[1].operation != "ldr"
            or deadline_load is None
            or deadline_load[1] != "sp"
            or loop[2].operation != "subs"
            or subtract is None
            or subtract[0] != timer_load[0]
            or subtract[1] != timer_load[0]
            or subtract[2] != deadline_load[0]
            or loop[3].operation != "lsls"
            or shift != (timer_load[0], timer_load[0], "#16")
            or _branch_target(loop[4]) != loop[0].address
        ):
            continue
        timer_register = timer_load[1]
        if timer_register not in {"r4", "r5", "r6", "r7"}:
            continue
        candidates.append((start_index, loop, timer_register, deadline_load[2]))
    _require(len(candidates) == 1, "main lacks one exact five-instruction TIM3 deadline poll")
    return candidates[0]


def _estimate_path_cycles(
    path: Sequence[Instruction],
    *,
    branch_cycles: dict[int, int],
    call_cycles: dict[int, int] | None = None,
    message: str,
) -> int:
    if call_cycles is None:
        call_cycles = {}
    cycles = 0
    for instruction in path:
        operation = instruction.operation
        if operation == "bl":
            _require(
                instruction.address in call_cycles,
                f"{message} contains an unexpected call",
            )
            cycles += call_cycles[instruction.address]
            continue
        if operation in BRANCH_OPERATIONS:
            _require(
                instruction.address in branch_cycles,
                f"{message} contains an unexpected branch",
            )
            cycles += branch_cycles[instruction.address]
        elif operation in {"ldr", "ldrb", "ldrh", "str", "strb", "strh"}:
            cycles += 3
        else:
            cycles += 1
    return cycles


def _maximum_return_path_cycles(
    function: FunctionDisassembly,
    *,
    message: str,
) -> int:
    """Conservatively bound every acyclic entry-to-BX-LR path."""

    code = function.code
    _require(bool(code), f"{message} contains no instructions")

    def visit(index: int, active: frozenset[int]) -> tuple[int, ...]:
        _require(index not in active, f"{message} contains a control-flow cycle")
        instruction = code[index]
        if instruction.operation == "bx":
            _require(
                _operand_text(instruction) == "lr",
                f"{message} has an unexpected indirect return",
            )
            return (3,)
        _require(
            instruction.operation != "bl",
            f"{message} contains an unexpected nested call",
        )
        successors = _successor_indices(code, index)
        _require(bool(successors), f"{message} has a path without an exact return")
        next_active = active | {index}
        path_cycles: list[int] = []
        target = _branch_target(instruction)
        for successor in successors:
            if instruction.operation in BRANCH_OPERATIONS:
                instruction_cycles = 3 if code[successor].address == target else 1
            elif instruction.operation in {
                "ldr",
                "ldrb",
                "ldrh",
                "str",
                "strb",
                "strh",
            }:
                instruction_cycles = 3
            else:
                instruction_cycles = 1
            path_cycles.extend(instruction_cycles + tail for tail in visit(successor, next_active))
        return tuple(path_cycles)

    return max(visit(0, frozenset()))


def verify_high_rate_next_deadline(disassembly: str) -> int:
    helper = parse_function(disassembly, "high_rate_next_deadline")
    code = helper.code
    _require(
        len(code) == 3 and _is_contiguous(code),
        "high_rate_next_deadline instruction sequence is not exact",
    )
    _require_binary(
        code[0],
        operation="adds",
        first="r0",
        second="r0",
        third="r1",
        message="high_rate_next_deadline addition is not exact",
    )
    _require_two_operands(
        code[1],
        operation="uxth",
        first="r0",
        second="r0",
        message="high_rate_next_deadline timer-width truncation is not exact",
    )
    _require(
        code[2].operation == "bx" and _operand_text(code[2]) == "lr",
        "high_rate_next_deadline does not return directly",
    )
    return _maximum_return_path_cycles(helper, message="high_rate_next_deadline")


def _frame_advance_marker_path_cycles(disassembly: str) -> int:
    """Bound the marker-to-guard path reached immediately after a refresh."""

    advance = parse_function(disassembly, "high_rate_frame_advance").code
    _require(
        len(advance) == 46,
        "high_rate_frame_advance marker path cannot be isolated",
    )
    marker_path = (
        *advance[:14],
        advance[24],
        advance[25],
    )
    _require(
        _branch_target(advance[3]) == advance[14].address
        and _branch_target(advance[6]) == advance[26].address
        and _branch_target(advance[8]) == advance[41].address
        and _branch_target(advance[13]) == advance[24].address,
        "high_rate_frame_advance marker path branches are not exact",
    )
    return _estimate_path_cycles(
        marker_path,
        branch_cycles={
            advance[3].address: 1,
            advance[6].address: 1,
            advance[8].address: 1,
            advance[13].address: 3,
            advance[25].address: 3,
        },
        message="high_rate_frame_advance marker-to-guard path",
    )


def verify_main_timing_control_flow(disassembly: str) -> HexcalControlFlowEvidence:
    verify_reset_handler_sram_workaround(disassembly)
    full_rcc_valid_cycles = verify_clock_register_configuration(disassembly)
    control_schedule_address = verify_control_profile_state_machine(disassembly)
    frame_advance_cycles = _maximum_return_path_cycles(
        parse_function(disassembly, "high_rate_frame_advance"),
        message="high_rate_frame_advance",
    )
    frame_advance_marker_cycles = _frame_advance_marker_path_cycles(disassembly)
    next_deadline_cycles = verify_high_rate_next_deadline(disassembly)
    _require(
        frame_advance_cycles == 44 and frame_advance_marker_cycles == 28,
        "frame-advance worst/marker path cycle identities are not exact",
    )
    main = parse_function(disassembly, "main")
    code = main.code
    _require_no_external_function_interior_entries(disassembly, main)
    prologue = code[:3]
    _require(
        len(prologue) == 3
        and _is_contiguous(prologue)
        and prologue[0].operation == "push"
        and _operand_text(prologue[0]) == "{r4, r5, r6, r7, lr}",
        "main entry prologue does not preserve the exact callee-saved register set",
    )
    _require_two_operands(
        prologue[1],
        operation="sub",
        first="sp",
        second="#44",
        message="main entry prologue does not allocate the exact 44-byte stack frame",
    )
    _require(
        prologue[2].operation == "cpsid" and _operand_text(prologue[2]) == "i",
        "main entry prologue does not permanently disable interrupts",
    )
    _require_exact_branch_entries(
        code,
        prologue,
        (),
        "main entry prologue has an alternate branch entry",
    )
    stack_use_indices = []
    for index, instruction in enumerate(code[3:], start=3):
        memory = _memory_operands(instruction)
        binary = _binary_operands(instruction)
        if (memory is not None and memory[1] == "sp") or (
            instruction.operation == "add" and binary is not None and binary[1] == "sp"
        ):
            stack_use_indices.append(index)
    _require(bool(stack_use_indices), "main contains no verifiable stack-region use")
    _require(
        not any(item.operation in {"bx", "blx"} for item in code)
        and not any(item.operation == "pop" and "pc" in item.operands for item in code),
        "main contains an indirect control transfer",
    )
    cpsid_indices = [
        index
        for index, item in enumerate(code)
        if item.operation == "cpsid" and _operand_text(item) == "i"
    ]
    _require(
        len(cpsid_indices) == 1 and not any(item.operation == "cpsie" for item in code),
        "main lacks one permanent explicit interrupt disable",
    )
    verify_watchdog_initialization(disassembly, main)
    watchdog_call_index = next(
        index for index, item in enumerate(code) if _calls(item, "watchdog_initialize")
    )
    boot = verify_boot_hardware_initialization(
        main,
        cpsid_index=cpsid_indices[0],
        watchdog_call_index=watchdog_call_index,
    )
    (
        otyper_store_index,
        moder_store_index,
        output_gpio_register,
        output_pin_mask_register,
    ) = verify_gpio_output_configuration(
        main,
        watchdog_call_index=watchdog_call_index,
    )
    post_output_gate_index = verify_post_output_all_off_gate(
        main,
        moder_store_index=moder_store_index,
        gpio_register=output_gpio_register,
        pin_mask_register=output_pin_mask_register,
    )
    gpio_output_configuration = ((otyper_store_index, 4), (moder_store_index, 0))

    tight_index, tight_loop, timer_register, deadline_stack_offset = _find_tight_poll(main)
    tight_load = _memory_operands(tight_loop[0])
    assert tight_load is not None
    timer_base_load = _last_register_write(code, timer_register, tight_index)
    _require(
        timer_base_load is not None and _literal_value(main, timer_base_load) == TIM3_BASE,
        "tight poll is not anchored to the exact TIM3 base",
    )
    tim3_initialization_start = verify_tim3_initialization(
        main,
        timer_register=timer_register,
        zero_register=boot.tim_zero_register,
        prescaler_register=boot.tim_prescaler_register,
        startup_failure_address=boot.startup_failure_address,
        watchdog_call_index=watchdog_call_index,
    )
    gpio_boot_start = cpsid_indices[0] + 1
    clock_boot_start = gpio_boot_start + 20
    tim_boot_start = clock_boot_start + 9
    startup_failure_index = next(
        index for index, item in enumerate(code) if item.address == boot.startup_failure_address
    )
    startup_failure_block = code[startup_failure_index : startup_failure_index + 2]
    _require_exact_branch_entries(
        code,
        startup_failure_block,
        tuple(
            (code[index].address, startup_failure_block[0].address)
            for index in (
                gpio_boot_start + 10,
                gpio_boot_start + 19,
                clock_boot_start + 3,
                clock_boot_start + 8,
                tim_boot_start + 8,
                tim_boot_start + 16,
                tim_boot_start + 26,
                tim3_initialization_start + 13,
                tim3_initialization_start + 16,
                tim3_initialization_start + 19,
                tim3_initialization_start + 22,
                startup_failure_index + 1,
            )
        ),
        "startup terminal stop has an unexpected or missing failure entry",
    )
    _require(
        deadline_stack_offset == DEADLINE_STACK_OFFSET,
        "deadline is not stored in the exact dedicated stack slot",
    )
    tight_poll_sample_cycles = _estimate_path_cycles(
        tight_loop,
        branch_cycles={tight_loop[-1].address: 3},
        message="tight-poll sample-to-next-sample path",
    )
    _require(
        tight_poll_sample_cycles <= TIGHT_POLL_SAMPLE_MAX_CORE_CYCLES,
        "tight-poll sample-to-next-sample path costs "
        f"{tight_poll_sample_cycles} cycles, exceeds "
        f"{TIGHT_POLL_SAMPLE_MAX_CORE_CYCLES}",
    )

    tight_entry_candidates = [
        index
        for index, item in enumerate(code[:tight_index])
        if item.operation == "bls" and _branch_target(item) == tight_loop[0].address
    ]
    _require(
        len(tight_entry_candidates) == 1,
        "tight poll lacks one exact staging-window admission branch",
    )
    stage_branch_index = tight_entry_candidates[0]
    stage_start_index = stage_branch_index - 10
    _require(stage_start_index >= 0, "tight poll lacks the exact 8 us staging branch")
    stage = code[stage_start_index : stage_branch_index + 1]
    _require(
        len(stage) == 11 and _is_contiguous(stage),
        "8 us staging instructions are not contiguous",
    )
    outer_load = _require_memory(
        stage[0],
        operation="ldr",
        base_register=timer_register,
        offset=TIM3_CNT_OFFSET,
        message="staging does not start with TIM3 CNT",
    )
    now_register = outer_load[0]
    pending_deadline = _require_memory(
        stage[1],
        operation="ldr",
        base_register="sp",
        offset=deadline_stack_offset,
        message="staging does not load the exact deadline stack word",
    )[0]
    _require_two_operands(
        stage[2],
        operation="uxth",
        first=now_register,
        second=now_register,
        message="staging TIM3 truncation is not exact",
    )
    pending_register, _, _ = _require_binary(
        stage[3],
        operation="subs",
        second=now_register,
        third=pending_deadline,
        message="staging pending subtraction is not exact",
    )
    _require_binary(
        stage[4],
        operation="lsls",
        first=pending_register,
        second=pending_register,
        third="#16",
        message="staging pending sign test is not exact",
    )
    _require(stage[5].operation == "bpl", "already-due outer path is not fail-closed")
    distance_deadline = _require_memory(
        stage[6],
        operation="ldr",
        base_register="sp",
        offset=deadline_stack_offset,
        message="staging distance does not reload the exact deadline stack word",
    )[0]
    distance_register, _, _ = _require_binary(
        stage[7],
        operation="subs",
        second=distance_deadline,
        third=now_register,
        message="staging distance is not deadline minus TIM3 CNT",
    )
    _require_two_operands(
        stage[8],
        operation="uxth",
        first=distance_register,
        second=distance_register,
        message="staging distance truncation is not exact",
    )
    _require_two_operands(
        stage[9],
        operation="cmp",
        first=distance_register,
        second=f"#{TIGHT_POLL_WINDOW_US}",
        message="tight-poll staging window is not 8 us",
    )
    _require(
        stage[10].operation == "bls" and _branch_target(stage[10]) == tight_loop[0].address,
        "8 us staging gate does not send only near deadlines to the tight poll",
    )
    outer_start = stage[0].address
    _require(
        watchdog_call_index < stage_start_index,
        "watchdog_initialize must complete before schedule entry",
    )

    address_to_index = {item.address: index for index, item in enumerate(code)}
    prevalidation_candidates = [
        index
        for index, item in enumerate(code[:stage_start_index])
        if item.operation == "bne"
        and _branch_target(item) == outer_start
        and index >= 2
        and _calls(code[index - 2], "clock_register_configuration_valid")
    ]
    _require(
        len(prevalidation_candidates) == 1,
        "outer poll lacks one mandatory full RCC prevalidation gate",
    )
    prevalidation_success_index = prevalidation_candidates[0]
    prevalidation_call_index = prevalidation_success_index - 2
    prevalidation_gate = code[prevalidation_call_index : prevalidation_success_index + 1]
    _require(
        len(prevalidation_gate) == 3
        and _is_contiguous(prevalidation_gate)
        and prevalidation_gate[-1].next_address < stage[0].address,
        "mandatory full RCC prevalidation gate is not exact and contiguous",
    )
    _require(
        _calls(prevalidation_gate[0], "clock_register_configuration_valid"),
        "mandatory pre-poll path lacks a direct full RCC validation call",
    )
    _require_two_operands(
        prevalidation_gate[1],
        operation="cmp",
        first="r0",
        second="#0",
        message="mandatory pre-poll full RCC result check is not exact",
    )
    _require(
        prevalidation_gate[2].operation == "bne"
        and _branch_target(prevalidation_gate[2]) == outer_start,
        "only valid mandatory full RCC prevalidation may enter the outer poll",
    )
    prevalidation_failure = _failure_block_until_spin(
        code,
        prevalidation_success_index + 1,
        stage_start_index,
        "invalid mandatory pre-poll full RCC path does not stop",
    )
    _require_all_off_bsrr_store(
        main,
        prevalidation_failure,
        message="invalid mandatory pre-poll full RCC path is not fail-closed ALL_OFF",
    )

    far_index = stage_branch_index + 1
    far_gate = code[far_index : far_index + 3]
    _require(
        len(far_gate) == 3
        and stage[-1].next_address == far_gate[0].address
        and _is_contiguous(far_gate),
        "far-deadline full RCC gate is not contiguous",
    )
    _require(
        _calls(far_gate[0], "clock_register_configuration_valid"),
        "far-deadline path lacks a direct full RCC validation call",
    )
    _require_two_operands(
        far_gate[1],
        operation="cmp",
        first="r0",
        second="#0",
        message="far-deadline full RCC result check is not exact",
    )
    _require(
        far_gate[2].operation == "bne" and _branch_target(far_gate[2]) == outer_start,
        "only a valid far-deadline full RCC result may return to the outer poll",
    )
    far_failure = _failure_block_until_spin(
        code,
        far_index + 3,
        tight_index,
        "invalid far-deadline full RCC path does not stop",
    )
    _require_all_off_bsrr_store(
        main,
        far_failure,
        message="invalid far-deadline full RCC path is not fail-closed ALL_OFF",
    )
    _require(
        [
            item.address
            for item in code[prevalidation_call_index:tight_index]
            if _calls(item, "clock_register_configuration_valid")
        ]
        == [prevalidation_gate[0].address, far_gate[0].address],
        "deadline polling region does not contain exactly the mandatory and far full RCC calls",
    )
    _require_exact_branch_entries(
        code,
        prevalidation_gate,
        (),
        "mandatory full RCC prevalidation gate has an interior branch entry",
    )
    _require_exact_branch_entries(
        code,
        stage,
        (
            (prevalidation_gate[2].address, stage[0].address),
            (far_gate[2].address, stage[0].address),
        ),
        "outer polling stage has an admission-bypassing branch entry",
    )
    _require_exact_branch_entries(
        code,
        far_gate,
        (),
        "far full RCC validation gate has an interior branch entry",
    )

    incoming_tight = [item for item in code if _branch_target(item) == tight_loop[0].address]
    _require(
        incoming_tight == [stage[10], tight_loop[-1]],
        "staging admission does not dominate every tight-poll entry",
    )
    _require_exact_branch_entries(
        code,
        tight_loop,
        (
            (stage[10].address, tight_loop[0].address),
            (tight_loop[-1].address, tight_loop[0].address),
        ),
        "tight poll has an admission-bypassing interior branch entry",
    )

    full_rcc_call_cycles = DIRECT_CALL_CORE_CYCLES + full_rcc_valid_cycles
    far_outer_sample_path = (*stage, *far_gate, stage[0])
    far_outer_sample_cycles = _estimate_path_cycles(
        far_outer_sample_path,
        branch_cycles={
            stage[5].address: 1,
            stage[10].address: 1,
            far_gate[2].address: 3,
        },
        call_cycles={far_gate[0].address: full_rcc_call_cycles},
        message="far outer-sample-to-next-sample path",
    )
    _require(
        far_outer_sample_cycles <= FAR_OUTER_SAMPLE_MAX_CORE_CYCLES,
        "far outer-sample-to-next-sample path costs "
        f"{far_outer_sample_cycles} cycles, exceeds "
        f"{FAR_OUTER_SAMPLE_MAX_CORE_CYCLES}",
    )
    staging_entry_path = (*stage, tight_loop[0])
    staging_entry_cycles = _estimate_path_cycles(
        staging_entry_path,
        branch_cycles={
            stage[5].address: 1,
            stage[10].address: 3,
        },
        message="staging-entry-sample-to-first-tight-sample path",
    )
    _require(
        staging_entry_cycles <= STAGING_ENTRY_TO_TIGHT_SAMPLE_MAX_CORE_CYCLES,
        "staging-entry-sample-to-first-tight-sample path costs "
        f"{staging_entry_cycles} cycles, exceeds "
        f"{STAGING_ENTRY_TO_TIGHT_SAMPLE_MAX_CORE_CYCLES}",
    )
    _require(
        FAR_OUTER_SAMPLE_MAX_CORE_CYCLES
        < TIGHT_POLL_WINDOW_US * HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK
        and STAGING_ENTRY_TO_TIGHT_SAMPLE_MAX_CORE_CYCLES
        < TIGHT_POLL_WINDOW_US * HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK,
        "staging feed caps do not fit strictly within the staging window",
    )

    hsi_address = tight_loop[-1].next_address
    _require(hsi_address in address_to_index, "tight poll does not fall through to HSI gate")
    hsi_index = address_to_index[hsi_address]
    _require(
        _branch_target(stage[5]) != hsi_address,
        "already-due outer path can reach the final HSI gate",
    )
    incoming_hsi = [item for item in code if _branch_target(item) == hsi_address]
    _require(
        not incoming_hsi,
        "final HSI gate has an unexpected branch entry",
    )

    hsi_gate = code[hsi_index : hsi_index + 10]
    _require(
        len(hsi_gate) == 10 and _is_contiguous(hsi_gate),
        "inline final HSI signature gate is not exact and contiguous",
    )
    expected_register, _ = _require_two_operands(
        hsi_gate[0],
        operation="movs",
        second="#168",
        message="inline HSI expected signature seed is not exact",
    )
    cr_register, rcc_register, _ = _require_memory(
        hsi_gate[1],
        operation="ldr",
        offset=RCC_CR_OFFSET,
        message="inline HSI gate does not read RCC CR exactly once",
    )
    _require(
        rcc_register in {"r4", "r5", "r6", "r7"},
        "inline HSI RCC base is not held in a callee-saved register",
    )
    rcc_base_load = _last_register_write(code, rcc_register, hsi_index + 1)
    _require(
        rcc_base_load is not None and _literal_value(main, rcc_base_load) == RCC_BASE,
        "inline HSI gate is not anchored to the exact RCC base",
    )
    mask_register, _ = _require_two_operands(
        hsi_gate[2],
        operation="movs",
        second="#244",
        message="inline HSI signature mask seed is not exact",
    )
    _require_binary(
        hsi_gate[3],
        operation="lsls",
        first=expected_register,
        second=expected_register,
        third="#5",
        message="inline HSI expected signature shift is not exact",
    )
    _require_two_operands(
        hsi_gate[4],
        operation="eors",
        first=expected_register,
        second=cr_register,
        message="inline HSI signature XOR is not exact",
    )
    gpio_register, _ = _require_two_operands(
        hsi_gate[5],
        operation="movs",
        second="#160",
        message="inline HSI gate does not materialize the exact GPIOA base",
    )
    _require_binary(
        hsi_gate[6],
        operation="lsls",
        first=mask_register,
        second=mask_register,
        third="#6",
        message="inline HSI signature mask shift is not exact",
    )
    _require_binary(
        hsi_gate[7],
        operation="lsls",
        first=gpio_register,
        second=gpio_register,
        third="#23",
        message="inline HSI GPIOA base shift is not exact",
    )
    _require_two_operands(
        hsi_gate[8],
        operation="tst",
        first=expected_register,
        second=mask_register,
        message="inline HSI masked signature test is not exact",
    )
    _require(
        len({expected_register, cr_register, mask_register}) == 3
        and gpio_register not in {expected_register, mask_register},
        "inline HSI signature registers alias unsafely",
    )
    _require(
        HSI_SIGNATURE_EXPECTED == RCC_CLOCK_SIGNATURE == (168 << 5)
        and HSI_SIGNATURE_MASK == RCC_CLOCK_SIGNATURE_MASK == (244 << 6),
        "verifier HSI signature constants are internally inconsistent",
    )
    _require(
        hsi_gate[9].operation == "beq",
        "only an exact HSI ON/RDY/HSIDIV signature may reach final admission",
    )
    final_timer_address = _branch_target(hsi_gate[9])
    _require(final_timer_address in address_to_index, "final HSI valid branch target is invalid")
    assert final_timer_address is not None
    final_timer_index = address_to_index[final_timer_address]
    hsi_failure = _failure_block_until_spin(
        code,
        hsi_index + len(hsi_gate),
        final_timer_index,
        "invalid final HSI signature path does not stop",
    )
    _require_all_off_bsrr_store(
        main,
        hsi_failure,
        known_gpio_register=gpio_register,
        message="invalid final HSI signature path is not fail-closed ALL_OFF",
    )

    incoming_final = [item for item in code if _branch_target(item) == final_timer_address]
    _require(
        incoming_final == [hsi_gate[9]],
        "final timer admission has an unexpected branch entry",
    )
    _require_exact_branch_entries(
        code,
        hsi_gate,
        (),
        "final HSI gate has an admission-bypassing interior branch entry",
    )

    critical = code[final_timer_index : final_timer_index + 6]
    _require(
        len(critical) == 6 and _is_contiguous(critical),
        "final inline lateness gate is not contiguous",
    )
    _require_exact_branch_entries(
        code,
        critical,
        ((hsi_gate[9].address, critical[0].address),),
        "final lateness gate has an admission-bypassing interior branch entry",
    )
    final_load = _require_memory(
        critical[0],
        operation="ldr",
        base_register=timer_register,
        offset=TIM3_CNT_OFFSET,
        message="final gate does not re-read TIM3 CNT",
    )
    final_now = final_load[0]
    final_deadline = _require_memory(
        critical[1],
        operation="ldr",
        base_register="sp",
        offset=deadline_stack_offset,
        message="final gate does not load the exact deadline stack word",
    )[0]
    _require_binary(
        critical[2],
        operation="subs",
        first=final_now,
        second=final_now,
        third=final_deadline,
        message="final lateness subtraction is not exact",
    )
    _require_two_operands(
        critical[3],
        operation="uxth",
        first=final_now,
        second=final_now,
        message="final lateness truncation is not exact",
    )
    _require_two_operands(
        critical[4],
        operation="cmp",
        first=final_now,
        second=f"#{PREWRITE_MAX_LATENESS_US}",
        message="final pre-write lateness threshold is not exactly 2 us",
    )
    _require(
        critical[5].operation == "bhi",
        "final lateness gate is not unsigned reject-only",
    )
    resynchronize_address = _branch_target(critical[5])
    _require(
        resynchronize_address is not None
        and resynchronize_address == _branch_target(stage[5])
        and resynchronize_address in address_to_index,
        "outer-due and excessive-lateness paths do not share fail-closed resynchronization",
    )
    assert resynchronize_address is not None
    resynchronize_index = address_to_index[resynchronize_address]
    resynchronize_block = code[resynchronize_index : resynchronize_index + 5]
    _require(
        len(resynchronize_block) == 5 and _is_contiguous(resynchronize_block),
        "fail-closed resynchronization block is not exact and contiguous",
    )
    _require_all_off_bsrr_store(
        main,
        resynchronize_block,
        message="resynchronization path is not fail-closed ALL_OFF",
    )
    _require(
        not any(item.operation in BRANCH_OPERATIONS for item in resynchronize_block[:-1])
        and resynchronize_block[-1].operation == "b",
        "resynchronization can bypass ALL_OFF",
    )
    restart_target = _branch_target(resynchronize_block[-1])
    _require(
        restart_target is not None
        and restart_target < outer_start
        and restart_target in address_to_index,
        "resynchronization does not restart the frame",
    )
    assert restart_target is not None
    restart_target_index = address_to_index[restart_target]
    _require(
        any(
            _calls(item, "high_rate_frame_init")
            for item in code[restart_target_index : restart_target_index + 3]
        ),
        "resynchronization restart does not reinitialize the full marker",
    )
    incoming_resynchronize = [
        item for item in code if _branch_target(item) == resynchronize_address
    ]
    _require(
        incoming_resynchronize == [stage[5], critical[5]],
        "fail-closed resynchronization has an unexpected branch entry",
    )

    accept_index = final_timer_index + len(critical)
    _require(
        accept_index < len(code) and critical[-1].next_address == code[accept_index].address,
        "accepted GPIO path is not the direct final-gate fallthrough",
    )
    accept = code[accept_index : accept_index + 2]
    _require(
        len(accept) == 2 and _is_contiguous(accept),
        "accepted precomputed-BSRR path is not exact",
    )
    planned_load = _require_memory(
        accept[0],
        operation="ldr",
        base_register="sp",
        message="accepted GPIO value is not a precomputed stack BSRR word",
    )
    planned_register, _, planned_offset = planned_load
    _require(
        planned_register != gpio_register and planned_offset == PLANNED_BSRR_STACK_OFFSET,
        "accepted BSRR value is not in the exact dedicated stack slot",
    )
    _require_memory(
        accept[1],
        operation="str",
        value_register=planned_register,
        base_register=gpio_register,
        offset=GPIO_BSRR_OFFSET,
        message="accepted path does not write the precomputed word directly to GPIOA BSRR",
    )
    accept_addresses = {item.address for item in accept}
    _require(
        not any(_branch_target(item) in accept_addresses for item in code),
        "accepted GPIO path has an admission-bypassing branch entry",
    )
    prior_planned_stores = [
        (index, item)
        for index, item in enumerate(code[:final_timer_index])
        if item.operation == "str"
        and (memory := _memory_operands(item)) is not None
        and memory[1] == "sp"
        and memory[2] == planned_offset
    ]
    _require(bool(prior_planned_stores), "precomputed BSRR stack word has no producer")
    last_planned_index, _ = prior_planned_stores[-1]
    _require(
        last_planned_index < stage_start_index,
        "BSRR word is not precomputed before deadline polling",
    )
    _require(last_planned_index >= 9, "precomputed BSRR producer shape is missing")
    planned_producer = code[last_planned_index - 9 : last_planned_index + 1]
    _require(
        len(planned_producer) == 10 and _is_contiguous(planned_producer),
        "precomputed BSRR producer is not exact and contiguous",
    )
    applied_register, planned_frame_register, _ = _require_memory(
        planned_producer[0],
        operation="ldrb",
        offset=2,
        message="precomputed BSRR does not load planned applied_code",
    )
    mask_register, _ = _require_two_operands(
        planned_producer[1],
        operation="movs",
        second="#240",
        message="precomputed BSRR reset-mask seed is not exact",
    )
    reset_register, _ = _require_two_operands(
        planned_producer[2],
        operation="mvns",
        second=applied_register,
        message="precomputed BSRR complement is not exact",
    )
    _require_binary(
        planned_producer[3],
        operation="lsls",
        first=mask_register,
        second=mask_register,
        third="#12",
        message="precomputed BSRR reset-mask shift is not exact",
    )
    _require_binary(
        planned_producer[4],
        operation="lsls",
        first=reset_register,
        second=reset_register,
        third="#16",
        message="precomputed BSRR reset-bit shift is not exact",
    )
    _require_two_operands(
        planned_producer[5],
        operation="ands",
        first=reset_register,
        second=mask_register,
        message="precomputed BSRR reset-bit mask is not exact",
    )
    _require_two_operands(
        planned_producer[6],
        operation="movs",
        first=mask_register,
        second="#15",
        message="precomputed BSRR set-mask seed is not exact",
    )
    _require_two_operands(
        planned_producer[7],
        operation="ands",
        first=applied_register,
        second=mask_register,
        message="precomputed BSRR set-bit mask is not exact",
    )
    _require_two_operands(
        planned_producer[8],
        operation="orrs",
        first=reset_register,
        second=applied_register,
        message="precomputed BSRR set/reset merge is not exact",
    )
    _require_memory(
        planned_producer[9],
        operation="str",
        value_register=reset_register,
        base_register="sp",
        offset=planned_offset,
        message="precomputed BSRR producer does not store the accepted stack word",
    )
    advance_call_indices = [
        index
        for index, item in enumerate(code[: last_planned_index - 9])
        if _calls(item, "high_rate_frame_advance")
    ]
    _require(bool(advance_call_indices), "planned frame is not advanced before BSRR computation")
    advance_call_index = advance_call_indices[-1]
    advance_argument = _last_register_write(code, "r0", advance_call_index)
    _require(advance_argument is not None, "planned frame advance argument is missing")
    assert advance_argument is not None
    _require_two_operands(
        advance_argument,
        operation="movs",
        first="r0",
        second=planned_frame_register,
        message="planned applied_code does not come from the advanced frame",
    )

    prior_deadline_stores = [
        index
        for index, item in enumerate(code[:stage_start_index])
        if item.operation == "str"
        and (memory := _memory_operands(item)) is not None
        and memory[1] == "sp"
        and memory[2] == deadline_stack_offset
    ]
    _require(bool(prior_deadline_stores), "deadline stack word has no initial producer")
    deadline_store_index = prior_deadline_stores[-1]
    _require(deadline_store_index >= 4, "deadline initial producer shape is not exact")
    deadline_producer = code[deadline_store_index - 4 : deadline_store_index + 1]
    _require(
        len(deadline_producer) == 5 and _is_contiguous(deadline_producer),
        "deadline initial producer is not exact and contiguous",
    )
    deadline_now, _, _ = _require_memory(
        deadline_producer[0],
        operation="ldr",
        base_register=timer_register,
        offset=TIM3_CNT_OFFSET,
        message="deadline producer does not sample TIM3 CNT",
    )
    deadline_duration, frame_register, _ = _require_memory(
        deadline_producer[1],
        operation="ldrh",
        offset=4,
        message="deadline producer does not load the initial phase duration",
    )
    _require_binary(
        deadline_producer[2],
        operation="adds",
        first=deadline_duration,
        second=deadline_duration,
        third=deadline_now,
        message="deadline producer does not add duration to TIM3 CNT",
    )
    _require_two_operands(
        deadline_producer[3],
        operation="uxth",
        first=deadline_duration,
        second=deadline_duration,
        message="deadline producer does not truncate to the timer width",
    )
    _require_memory(
        deadline_producer[4],
        operation="str",
        value_register=deadline_duration,
        base_register="sp",
        offset=deadline_stack_offset,
        message="deadline producer does not store the exact deadline stack word",
    )

    frame_pointer_index = deadline_store_index - 7
    _require(frame_pointer_index >= 0, "initial live-frame initialization sequence is missing")
    frame_initialization = code[frame_pointer_index : deadline_store_index + 1]
    _require(
        len(frame_initialization) == 8 and _is_contiguous(frame_initialization),
        "initial live-frame initialization/deadline sequence is not exact and contiguous",
    )
    _require_binary(
        frame_initialization[0],
        operation="add",
        first=frame_register,
        second="sp",
        third=f"#{LIVE_FRAME_STACK_OFFSET}",
        message="live frame is not materialized at the exact stack object",
    )
    _require_two_operands(
        frame_initialization[1],
        operation="movs",
        first="r0",
        second=frame_register,
        message="high_rate_frame_init argument provenance is not exact",
    )
    _require(
        _calls(frame_initialization[2], "high_rate_frame_init"),
        "live frame is not initialized through the exact helper entry",
    )
    _require(
        tuple(frame_initialization[3:]) == tuple(deadline_producer),
        "initial live-frame initialization is not contiguous with deadline production",
    )
    _require(
        restart_target == frame_initialization[1].address,
        "resynchronization does not re-enter the exact live-frame initialization sequence",
    )
    _require_exact_branch_entries(
        code,
        frame_initialization,
        ((resynchronize_block[-1].address, frame_initialization[1].address),),
        "live-frame initialization/deadline sequence has an unexpected branch entry",
    )

    planned_copy_start = advance_call_index - 8
    _require(
        planned_copy_start >= 0,
        "next planned-frame copy/advance sequence is missing",
    )
    planned_copy = code[planned_copy_start : advance_call_index + 1]
    _require(
        len(planned_copy) == 9 and _is_contiguous(planned_copy),
        "next planned-frame copy/advance sequence is not exact and contiguous",
    )
    copied_applied, copied_frame, _ = _require_memory(
        planned_copy[0],
        operation="ldrb",
        base_register=frame_register,
        offset=2,
        message="planned-frame copy does not load current applied_code",
    )
    copied_phase, _, _ = _require_memory(
        planned_copy[1],
        operation="ldrh",
        base_register=frame_register,
        offset=0,
        message="planned-frame copy does not load current phase",
    )
    _require_binary(
        planned_copy[2],
        operation="add",
        first=planned_frame_register,
        second="sp",
        third=f"#{PLANNED_FRAME_STACK_OFFSET}",
        message="planned-frame stack object is not exact",
    )
    _require_memory(
        planned_copy[3],
        operation="strh",
        value_register=copied_phase,
        base_register=planned_frame_register,
        offset=0,
        message="planned-frame phase copy is not exact",
    )
    _require_memory(
        planned_copy[4],
        operation="strb",
        value_register=copied_applied,
        base_register=planned_frame_register,
        offset=2,
        message="planned-frame applied_code copy is not exact",
    )
    copied_duration, _, _ = _require_memory(
        planned_copy[5],
        operation="ldrh",
        base_register=frame_register,
        offset=4,
        message="planned-frame copy does not load current duration",
    )
    _require_two_operands(
        planned_copy[6],
        operation="movs",
        first="r0",
        second=planned_frame_register,
        message="planned-frame advance argument is not exact",
    )
    _require_memory(
        planned_copy[7],
        operation="strh",
        value_register=copied_duration,
        base_register=planned_frame_register,
        offset=4,
        message="planned-frame duration copy is not exact",
    )
    _require(
        _calls(planned_copy[8], "high_rate_frame_advance"),
        "planned frame is not advanced by the exact helper entry",
    )
    cycle_result_store_index = advance_call_index + 1
    _require(
        cycle_result_store_index < len(code)
        and planned_copy[-1].next_address == code[cycle_result_store_index].address,
        "planned-frame completion result store is missing",
    )
    cycle_result_register, _, cycle_result_offset = _require_memory(
        code[cycle_result_store_index],
        operation="str",
        value_register="r0",
        base_register="sp",
        message="planned-frame completion result is not stored exactly",
    )
    _require(
        cycle_result_register == "r0"
        and cycle_result_offset == CYCLE_RESULT_STACK_OFFSET
        and code[cycle_result_store_index].next_address == planned_producer[0].address,
        "planned-frame result is not in its exact dedicated stack slot",
    )
    stack_regions = (
        (DEADLINE_STACK_OFFSET, DEADLINE_STACK_OFFSET + 4),
        (CYCLE_RESULT_STACK_OFFSET, CYCLE_RESULT_STACK_OFFSET + 4),
        (PLANNED_BSRR_STACK_OFFSET, PLANNED_BSRR_STACK_OFFSET + 4),
        (LIVE_FRAME_STACK_OFFSET, LIVE_FRAME_STACK_OFFSET + FRAME_STACK_SIZE),
        (PLANNED_FRAME_STACK_OFFSET, PLANNED_FRAME_STACK_OFFSET + FRAME_STACK_SIZE),
    )
    _require(
        all(
            left_end <= right_start or right_end <= left_start
            for index, (left_start, left_end) in enumerate(stack_regions)
            for right_start, right_end in stack_regions[index + 1 :]
        ),
        "deadline/result/BSRR/live/planned stack regions overlap",
    )
    _require(
        planned_producer[-1].next_address == prevalidation_gate[0].address,
        "mandatory full RCC validation is not immediately after BSRR preparation",
    )

    commit_start = accept_index + len(accept)
    commit = code[commit_start : commit_start + 8]
    _require(
        len(commit) == 8
        and accept[-1].next_address == commit[0].address
        and _is_contiguous(commit),
        "accepted frame-commit/deadline sequence is not exact and contiguous",
    )
    committed_applied, _, _ = _require_memory(
        commit[0],
        operation="ldrb",
        base_register=planned_frame_register,
        offset=2,
        message="accepted commit does not load planned applied_code",
    )
    committed_phase, _, _ = _require_memory(
        commit[1],
        operation="ldrh",
        base_register=planned_frame_register,
        offset=0,
        message="accepted commit does not load planned phase",
    )
    _require_memory(
        commit[2],
        operation="strb",
        value_register=committed_applied,
        base_register=frame_register,
        offset=2,
        message="accepted applied_code commit is not exact",
    )
    _require_memory(
        commit[3],
        operation="strh",
        value_register=committed_phase,
        base_register=frame_register,
        offset=0,
        message="accepted phase commit is not exact",
    )
    committed_duration, _, _ = _require_memory(
        commit[4],
        operation="ldrh",
        base_register=planned_frame_register,
        offset=4,
        message="accepted commit does not load planned duration",
    )
    _require_memory(
        commit[5],
        operation="ldr",
        value_register="r0",
        base_register="sp",
        offset=deadline_stack_offset,
        message="next-deadline call does not load the current deadline",
    )
    _require_memory(
        commit[6],
        operation="strh",
        value_register=committed_duration,
        base_register=frame_register,
        offset=4,
        message="accepted duration commit is not exact",
    )
    _require(
        committed_duration == "r1" and _calls(commit[7], "high_rate_next_deadline"),
        "next deadline is not produced by the exact helper and duration argument",
    )

    turnover_dispatch = code[commit_start + len(commit) : commit_start + len(commit) + 8]
    _require(
        len(turnover_dispatch) == 8
        and commit[-1].next_address == turnover_dispatch[0].address
        and _is_contiguous(turnover_dispatch),
        "turnover completion/refresh dispatch is not exact and contiguous",
    )
    cycle_result_load, _, _ = _require_memory(
        turnover_dispatch[0],
        operation="ldr",
        base_register="sp",
        offset=cycle_result_offset,
        message="turnover does not reload the exact cycle-completion result",
    )
    _require_memory(
        turnover_dispatch[1],
        operation="str",
        value_register="r0",
        base_register="sp",
        offset=deadline_stack_offset,
        message="turnover does not store the exact next deadline",
    )
    _require_two_operands(
        turnover_dispatch[2],
        operation="cmp",
        first=cycle_result_load,
        second="#0",
        message="turnover cycle-completion check is not exact",
    )
    _require(
        turnover_dispatch[3].operation == "beq"
        and _branch_target(turnover_dispatch[3]) == planned_copy[0].address,
        "non-cycle turnover does not branch directly to next-frame planning",
    )
    refresh_base, _, _ = _require_memory(
        turnover_dispatch[4],
        operation="ldr",
        base_register="pc",
        message="cycle-complete turnover does not load the IWDG base",
    )
    _require(
        _literal_value(main, turnover_dispatch[4]) == IWDG_BASE,
        "cycle-complete turnover IWDG base is not exact",
    )
    refresh_key, _, _ = _require_memory(
        turnover_dispatch[5],
        operation="ldr",
        base_register="pc",
        message="cycle-complete turnover does not load the IWDG refresh key",
    )
    _require(
        _literal_value(main, turnover_dispatch[5]) == IWDG_REFRESH_KEY,
        "cycle-complete turnover IWDG refresh key is not exact",
    )
    _require_memory(
        turnover_dispatch[6],
        operation="str",
        value_register=refresh_key,
        base_register=refresh_base,
        offset=0,
        message="cycle-complete turnover IWDG refresh store is not exact",
    )
    _require(
        turnover_dispatch[7].operation == "b"
        and _branch_target(turnover_dispatch[7]) == planned_copy[0].address,
        "cycle-complete turnover does not return directly to next-frame planning",
    )
    _require_exact_branch_entries(
        code,
        planned_copy,
        (
            (turnover_dispatch[3].address, planned_copy[0].address),
            (turnover_dispatch[7].address, planned_copy[0].address),
        ),
        "next-frame planning has an unexpected branch entry",
    )

    full_rcc_call_cycles = DIRECT_CALL_CORE_CYCLES + full_rcc_valid_cycles
    next_deadline_call_cycles = DIRECT_CALL_CORE_CYCLES + next_deadline_cycles
    frame_advance_call_cycles = DIRECT_CALL_CORE_CYCLES + frame_advance_cycles
    turnover_suffix = (
        *planned_copy,
        code[cycle_result_store_index],
        *planned_producer,
        *prevalidation_gate,
        stage[0],
    )
    turnover_call_cycles = {
        commit[7].address: next_deadline_call_cycles,
        planned_copy[-1].address: frame_advance_call_cycles,
        prevalidation_gate[0].address: full_rcc_call_cycles,
    }
    refresh_turnover_call_cycles = {
        **turnover_call_cycles,
        planned_copy[-1].address: (DIRECT_CALL_CORE_CYCLES + frame_advance_marker_cycles),
    }
    turnover_without_refresh = (
        accept[1],
        *commit,
        *turnover_dispatch[:4],
        *turnover_suffix,
    )
    turnover_with_refresh = (
        accept[1],
        *commit,
        *turnover_dispatch,
        *turnover_suffix,
    )
    turnover_without_refresh_cycles = _estimate_path_cycles(
        turnover_without_refresh,
        branch_cycles={
            turnover_dispatch[3].address: 3,
            prevalidation_gate[2].address: 3,
        },
        call_cycles=turnover_call_cycles,
        message="accepted BSRR-to-next-outer-sample non-refresh turnover",
    )
    turnover_with_refresh_cycles = _estimate_path_cycles(
        turnover_with_refresh,
        branch_cycles={
            turnover_dispatch[3].address: 1,
            turnover_dispatch[7].address: 3,
            prevalidation_gate[2].address: 3,
        },
        call_cycles=refresh_turnover_call_cycles,
        message="accepted BSRR-to-next-outer-sample refresh turnover",
    )
    transition_turnover_cycles = max(
        turnover_without_refresh_cycles,
        turnover_with_refresh_cycles,
    )
    _require(
        transition_turnover_cycles <= TRANSITION_TURNOVER_MAX_CORE_CYCLES,
        "accepted BSRR-to-next-outer-sample turnover costs "
        f"{transition_turnover_cycles} cycles, exceeds "
        f"{TRANSITION_TURNOVER_MAX_CORE_CYCLES}",
    )
    shortest_phase_core_cycles = SHORTEST_PHASE_US * HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK
    _require(
        DEADLINE_TO_GPIO_MAX_CORE_CYCLES
        == (PREWRITE_MAX_LATENESS_US + COUNTER_QUANTIZATION_TICKS)
        * HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK
        + GPIO_WRITE_MAX_CORE_CYCLES,
        "deadline-to-GPIO cap components are internally inconsistent",
    )
    shortest_phase_chain_cycles = (
        DEADLINE_TO_GPIO_MAX_CORE_CYCLES
        + TRANSITION_TURNOVER_MAX_CORE_CYCLES
        + STAGING_ENTRY_TO_TIGHT_SAMPLE_MAX_CORE_CYCLES
        - 2 * ENDPOINT_MEMORY_ACCESS_CORE_CYCLES
    )
    _require(
        transition_turnover_cycles == TRANSITION_TURNOVER_MAX_CORE_CYCLES,
        "transition turnover cap is not bound to the exact longest feasible path",
    )
    _require(
        shortest_phase_chain_cycles == SHORTEST_PHASE_CHAIN_MAX_CORE_CYCLES
        and shortest_phase_chain_cycles < shortest_phase_core_cycles,
        "deadline/turnover/staging chain does not fit strictly within the shortest phase",
    )

    _require_dominates(
        code,
        1,
        stack_use_indices,
        "exact main stack allocation does not dominate every stack-region use",
    )
    _require_dominates(
        code,
        watchdog_call_index,
        [index for index, _ in gpio_output_configuration] + [stage_start_index, accept_index + 1],
        "watchdog initialization does not dominate GPIO output enable and schedule execution",
    )
    boot_runtime_targets = [
        watchdog_call_index,
        otyper_store_index,
        moder_store_index,
        post_output_gate_index,
        frame_pointer_index,
        stage_start_index,
        accept_index + 1,
    ]
    for boot_dominator_index, description in (
        (boot.gpio_clock_store_index, "GPIOA IOPENR enable store"),
        (boot.gpio_clock_readback_index + 1, "GPIOA IOPENR enable readback gate"),
        (boot.gpio_preload_store_index, "GPIOA ALL_OFF preload"),
        (gpio_boot_start + 19, "GPIOA pre-output ODR gate"),
        (boot.reset_clock_gate_index, "reset-clock validation gate"),
        (boot.tim_clock_store_index, "TIM3 APBENR1 enable store"),
        (boot.tim_clock_readback_index + 1, "TIM3 APBENR1 readback gate"),
        (boot.tim_reset_assert_store_index, "TIM3 reset assert store"),
        (boot.tim_reset_assert_readback_index + 1, "TIM3 reset-assert readback gate"),
        (boot.tim_reset_deassert_store_index, "TIM3 reset deassert store"),
        (boot.tim_reset_deassert_readback_index + 1, "TIM3 reset-deassert readback gate"),
        (tim3_initialization_start, "exact TIM3 register initialization"),
        (tim3_initialization_start + 22, "TIM3 register readback gate"),
    ):
        _require_dominates(
            code,
            boot_dominator_index,
            boot_runtime_targets,
            f"{description} does not dominate watchdog/output/runtime entry",
        )
    _require(
        boot.gpio_preload_store_index < moder_store_index
        and boot.tim_clock_store_index
        < boot.tim_reset_assert_store_index
        < boot.tim_reset_deassert_store_index
        < tim3_initialization_start,
        "boot GPIO preload or TIM3 enable/reset ordering is not exact",
    )
    _require_dominates(
        code,
        moder_store_index,
        [post_output_gate_index, frame_pointer_index, stage_start_index, accept_index + 1],
        "GPIOA MODER output enable does not dominate the ODR gate/runtime",
    )
    _require_dominates(
        code,
        post_output_gate_index,
        [frame_pointer_index, deadline_store_index, stage_start_index, accept_index + 1],
        "successful post-output ALL_OFF verification does not dominate runtime",
    )
    _require_dominates(
        code,
        frame_pointer_index,
        [
            frame_pointer_index + 2,
            deadline_store_index,
            planned_copy_start,
            stage_start_index,
            accept_index + 1,
        ],
        "exact live-frame stack materialization does not dominate runtime",
    )
    _require_dominates(
        code,
        frame_pointer_index + 2,
        [deadline_store_index, planned_copy_start, stage_start_index, accept_index + 1],
        "high_rate_frame_init does not dominate deadline/planning/runtime",
    )
    _require_dominates(
        code,
        cpsid_indices[0],
        [watchdog_call_index]
        + [index for index, _ in gpio_output_configuration]
        + [stage_start_index, accept_index + 1],
        "interrupt disable does not dominate watchdog, GPIO enable, and schedule execution",
    )
    _require_dominates(
        code,
        deadline_store_index,
        [stage_start_index],
        "initial deadline production does not dominate polling",
    )
    _require_dominates(
        code,
        prevalidation_call_index,
        [stage_start_index, tight_index, hsi_index, final_timer_index, accept_index],
        "mandatory full RCC prevalidation does not dominate deadline admission",
    )
    _require_dominates(
        code,
        tight_index,
        [hsi_index, final_timer_index, accept_index, accept_index + 1],
        "tight-poll due sample does not dominate final HSI/timer/GPIO admission",
    )
    _require_dominates(
        code,
        hsi_index + len(hsi_gate) - 1,
        [final_timer_index, accept_index, accept_index + 1],
        "valid final HSI signature does not dominate timer/GPIO admission",
    )
    _require_dominates(
        code,
        final_timer_index + len(critical) - 1,
        [accept_index, accept_index + 1],
        "final unsigned lateness gate does not dominate GPIO admission",
    )

    deadline_to_final_sample_path = (*tight_loop[1:], *hsi_gate, critical[0])
    deadline_to_final_sample_cycles = _estimate_path_cycles(
        deadline_to_final_sample_path,
        branch_cycles={tight_loop[-1].address: 1, hsi_gate[-1].address: 3},
        message="deadline-to-final-sample liveness path",
    )
    _require(
        deadline_to_final_sample_cycles <= DEADLINE_TO_FINAL_SAMPLE_MAX_CORE_CYCLES,
        "deadline-to-final-sample liveness path costs "
        f"{deadline_to_final_sample_cycles} cycles, exceeds "
        f"{DEADLINE_TO_FINAL_SAMPLE_MAX_CORE_CYCLES}",
    )

    accepted_path = (*critical, *accept)
    cycles = _estimate_path_cycles(
        accepted_path,
        branch_cycles={critical[-1].address: 1},
        message="accepted timer-to-BSRR path",
    )
    _require(
        cycles <= GPIO_WRITE_MAX_CORE_CYCLES,
        f"accepted timer-to-BSRR path costs {cycles} cycles, exceeds {GPIO_WRITE_MAX_CORE_CYCLES}",
    )

    return HexcalControlFlowEvidence(
        control_schedule_address=control_schedule_address,
        tight_poll_instruction_count=len(tight_loop),
        tight_poll_window_us=TIGHT_POLL_WINDOW_US,
        far_outer_sample_core_cycles=far_outer_sample_cycles,
        far_outer_sample_max_core_cycles=FAR_OUTER_SAMPLE_MAX_CORE_CYCLES,
        staging_entry_to_tight_sample_core_cycles=staging_entry_cycles,
        staging_entry_to_tight_sample_max_core_cycles=(
            STAGING_ENTRY_TO_TIGHT_SAMPLE_MAX_CORE_CYCLES
        ),
        tight_poll_sample_core_cycles=tight_poll_sample_cycles,
        tight_poll_sample_max_core_cycles=TIGHT_POLL_SAMPLE_MAX_CORE_CYCLES,
        prewrite_max_lateness_us=PREWRITE_MAX_LATENESS_US,
        deadline_to_final_sample_core_cycles=deadline_to_final_sample_cycles,
        deadline_to_final_sample_max_core_cycles=DEADLINE_TO_FINAL_SAMPLE_MAX_CORE_CYCLES,
        gpio_write_path_core_cycles=cycles,
        gpio_write_max_core_cycles=GPIO_WRITE_MAX_CORE_CYCLES,
        transition_turnover_core_cycles=transition_turnover_cycles,
        transition_turnover_max_core_cycles=TRANSITION_TURNOVER_MAX_CORE_CYCLES,
        shortest_phase_chain_max_core_cycles=shortest_phase_chain_cycles,
        shortest_phase_core_cycles=shortest_phase_core_cycles,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("elf", type=Path)
    args = parser.parse_args()
    elf = args.elf.resolve(strict=True)

    size_text = output("arm-none-eabi-size", str(elf))
    match = re.search(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+", size_text, re.MULTILINE)
    if match is None:
        raise SystemExit("HEXCAL VERIFY FAIL: cannot parse size output")
    text_size, data_size, bss_size = (int(value) for value in match.groups())
    if text_size + data_size > FLASH_LIMIT or data_size + bss_size > RAM_LIMIT:
        raise SystemExit("HEXCAL VERIFY FAIL: device memory region exceeded")

    if output("arm-none-eabi-nm", "-u", str(elf)).strip():
        raise SystemExit("HEXCAL VERIFY FAIL: undefined symbols present")
    symbols = output("arm-none-eabi-nm", "-a", str(elf))
    required_symbols = (
        "CONTROL_SCHEDULE",
        "Default_Handler",
        "HexcalStartupSystemInit",
        "Reset_Handler",
        "SystemCoreClockUpdate",
        "_estack",
        "_sdata",
        "clock_register_configuration_valid",
        "g_pfnVectors",
        "high_rate_frame_advance",
        "high_rate_next_deadline",
        "main",
        "watchdog_initialize",
    )
    missing = [
        name
        for name in required_symbols
        if not re.search(rf"\b{re.escape(name)}$", symbols, re.MULTILINE)
    ]
    if missing:
        raise SystemExit("HEXCAL VERIFY FAIL: missing symbols " + ", ".join(missing))

    disassembly = output("arm-none-eabi-objdump", "-d", str(elf)).lower()
    try:
        vector_address = _symbol_address(symbols, "g_pfnVectors")
        reset_handler_address = _symbol_address(symbols, "Reset_Handler")
        default_handler_address = _symbol_address(symbols, "Default_Handler")
        _require(
            vector_address == VECTOR_TABLE_ADDRESS,
            "g_pfnVectors is not at the start of flash",
        )
        _require(
            _symbol_address(symbols, "_estack") == SRAM_END,
            "_estack symbol is not the SRAM limit",
        )
        vector_bytes = _section_byte_map(
            output("arm-none-eabi-objdump", "-s", "-j", ".isr_vector", str(elf))
        )
        expected_vector_addresses = set(
            range(VECTOR_TABLE_ADDRESS, VECTOR_TABLE_ADDRESS + VECTOR_TABLE_WORDS * 4)
        )
        _require(
            set(vector_bytes) == expected_vector_addresses,
            ".isr_vector section does not cover exactly the owned vector table",
        )
        verify_vector_table_bytes(
            bytes(vector_bytes[address] for address in sorted(vector_bytes)),
            reset_handler_address=reset_handler_address,
            default_handler_address=default_handler_address,
        )
        verify_default_handler_terminal_loop(
            disassembly,
            default_handler_address=default_handler_address,
        )
        reject_forbidden_peripheral_access(disassembly)
        evidence = verify_main_timing_control_flow(disassembly)
        sized_symbols = output("arm-none-eabi-nm", "-S", str(elf))
        schedule_symbol = re.search(
            r"^([0-9a-f]+)\s+([0-9a-f]+)\s+\w\s+CONTROL_SCHEDULE$",
            sized_symbols,
            re.MULTILINE,
        )
        _require(schedule_symbol is not None, "CONTROL_SCHEDULE sized symbol is missing")
        assert schedule_symbol is not None
        schedule_address = int(schedule_symbol.group(1), 16)
        schedule_size = int(schedule_symbol.group(2), 16)
        verify_control_schedule_symbol_identity(
            evidence,
            schedule_address=schedule_address,
            schedule_size=schedule_size,
        )
        section_bytes = _section_byte_map(
            output("arm-none-eabi-objdump", "-s", "-j", ".text", str(elf))
        )
        expected_text_addresses = set(
            range(TEXT_SECTION_ADDRESS, TEXT_SECTION_ADDRESS + TEXT_SECTION_SIZE)
        )
        _require(
            set(section_bytes) == expected_text_addresses,
            "executable .text section does not cover the exact frozen address range",
        )
        verify_text_section_bytes(
            bytes(section_bytes[address] for address in sorted(section_bytes))
        )
        schedule_values = [
            section_bytes.get(schedule_address + offset) for offset in range(schedule_size)
        ]
        _require(
            all(value is not None for value in schedule_values),
            "CONTROL_SCHEDULE bytes are absent from .text",
        )
        verify_control_schedule_bytes(
            bytes(value for value in schedule_values if value is not None)
        )
    except HexcalElfVerificationError as exc:
        raise SystemExit(f"HEXCAL VERIFY FAIL: {exc}") from exc
    if "400030" not in disassembly:
        raise SystemExit("HEXCAL VERIFY FAIL: independent-watchdog access missing")
    if "400004" not in disassembly:
        raise SystemExit("HEXCAL VERIFY FAIL: TIM3 access missing")
    if re.search(r"\bcpsie\b", disassembly):
        raise SystemExit("HEXCAL VERIFY FAIL: interrupt enable instruction present")

    print(
        "HEXCAL VERIFY PASS: "
        f"flash={text_size + data_size}/{FLASH_LIMIT} "
        f"ram={data_size + bss_size}/{RAM_LIMIT} "
        "profile=hexcal-v1/exact-elf "
        "text_digest=exact "
        "vector_table=exact default_handler=terminal "
        "reset_es0569_sram_first_read=verified "
        "TIM3=exact IWDG=/4x128 full_RCC=exact "
        "maskable_interrupts=explicitly-disabled-and-never-reenabled "
        f"tight_poll={evidence.tight_poll_instruction_count}insn/"
        f"{evidence.tight_poll_window_us}us/"
        f"{evidence.tight_poll_sample_core_cycles}/"
        f"{evidence.tight_poll_sample_max_core_cycles}cycles "
        f"far_outer_feed={evidence.far_outer_sample_core_cycles}/"
        f"{evidence.far_outer_sample_max_core_cycles}cycles "
        f"staging_feed={evidence.staging_entry_to_tight_sample_core_cycles}/"
        f"{evidence.staging_entry_to_tight_sample_max_core_cycles}cycles "
        "inline_hsi_signature=exact "
        f"due_to_final_sample={evidence.deadline_to_final_sample_core_cycles}/"
        f"{evidence.deadline_to_final_sample_max_core_cycles}cycles "
        f"prewrite_lateness<={evidence.prewrite_max_lateness_us}us "
        f"timer_to_bsrr<={evidence.gpio_write_path_core_cycles}/"
        f"{evidence.gpio_write_max_core_cycles}cycles "
        f"transition_turnover={evidence.transition_turnover_core_cycles}/"
        f"{evidence.transition_turnover_max_core_cycles}cycles "
        f"shortest_phase_chain={evidence.shortest_phase_chain_max_core_cycles}/"
        f"{evidence.shortest_phase_core_cycles}cycles "
        "rcc_fail_closed=verified dbgmcu_iwdg_freeze_access=absent "
        "flash_option_base_literal=absent"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
