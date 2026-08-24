#include <stddef.h>
#include <stdint.h>

#include "bench_protocol.h"
#include "control_core.h"
#include "stm32c0xx.h"

#define CONTROL_PIN_MASK UINT32_C(0x0F)
#define CONTROL_MODE_MASK UINT32_C(0xFF)
#define CONTROL_OUTPUT_MODES UINT32_C(0x55)
#define RESET_CORE_CLOCK_HZ UINT32_C(12000000)
#define TICKS_PER_SECOND UINT32_C(1000)
#define SYSTICK_RELOAD ((RESET_CORE_CLOCK_HZ / TICKS_PER_SECOND) - 1u)

_Static_assert(offsetof(bench_mailbox_t, magic) == BENCH_OFFSET_MAGIC, "mailbox ABI");
_Static_assert(offsetof(bench_mailbox_t, version) == BENCH_OFFSET_VERSION, "mailbox ABI");
_Static_assert(
    offsetof(bench_mailbox_t, command_sequence) == BENCH_OFFSET_COMMAND_SEQUENCE,
    "mailbox ABI"
);
_Static_assert(offsetof(bench_mailbox_t, command_code) == BENCH_OFFSET_COMMAND_CODE, "mailbox ABI");
_Static_assert(
    offsetof(bench_mailbox_t, command_lease_ms) == BENCH_OFFSET_COMMAND_LEASE_MS,
    "mailbox ABI"
);
_Static_assert(
    offsetof(bench_mailbox_t, acknowledged_sequence) == BENCH_OFFSET_ACKNOWLEDGED_SEQUENCE,
    "mailbox ABI"
);
_Static_assert(offsetof(bench_mailbox_t, applied_code) == BENCH_OFFSET_APPLIED_CODE, "mailbox ABI");
_Static_assert(
    offsetof(bench_mailbox_t, remaining_lease_ms) == BENCH_OFFSET_REMAINING_LEASE_MS,
    "mailbox ABI"
);
_Static_assert(offsetof(bench_mailbox_t, status_flags) == BENCH_OFFSET_STATUS_FLAGS, "mailbox ABI");
_Static_assert(sizeof(bench_mailbox_t) == BENCH_MAILBOX_SIZE, "mailbox ABI");

__attribute__((section(".bench_mailbox"), used, aligned(4)))
volatile bench_mailbox_t smateway_bench_mailbox;

static uint32_t control_bsrr_word(uint8_t code)
{
    const uint32_t set_bits = (uint32_t)code & CONTROL_PIN_MASK;
    const uint32_t reset_bits = (~(uint32_t)code) & CONTROL_PIN_MASK;

    return set_bits | (reset_bits << 16u);
}

static void gpio_apply(uint8_t code)
{
    GPIOA->BSRR = control_bsrr_word(code);
}

static void gpio_initialize_all_off(void)
{
    volatile uint32_t clock_readback;

    RCC->IOPENR |= RCC_IOPENR_GPIOAEN;
    clock_readback = RCC->IOPENR;
    (void)clock_readback;
    gpio_apply(CONTROL_ALL_OFF_CODE);
    GPIOA->OTYPER &= ~CONTROL_PIN_MASK;
    GPIOA->MODER = (GPIOA->MODER & ~CONTROL_MODE_MASK) | CONTROL_OUTPUT_MODES;
}

static void mailbox_publish(const control_selector_t *selector, uint32_t command_status)
{
    uint32_t flags = command_status;

    if (selector->lease_active) {
        flags |= BENCH_STATUS_LEASE_ACTIVE;
    }
    if (selector->guard_ms_remaining > 0u) {
        flags |= BENCH_STATUS_GUARD_ACTIVE;
    }
    smateway_bench_mailbox.applied_code = selector->applied_code;
    smateway_bench_mailbox.remaining_lease_ms = selector->lease_ms_remaining;
    smateway_bench_mailbox.status_flags = flags;
}

static uint32_t process_command(control_selector_t *selector)
{
    const uint32_t sequence = smateway_bench_mailbox.command_sequence;
    const uint32_t raw_code = smateway_bench_mailbox.command_code;
    const uint32_t lease_ms = smateway_bench_mailbox.command_lease_ms;
    bool accepted;

    if (sequence == smateway_bench_mailbox.acknowledged_sequence) {
        return smateway_bench_mailbox.status_flags
            & (BENCH_STATUS_COMMAND_VALID | BENCH_STATUS_INVALID_COMMAND);
    }

    if (raw_code > UINT8_MAX || lease_ms > BENCH_MAX_LEASE_MS) {
        control_selector_force_all_off(selector);
        accepted = false;
    } else {
        accepted = control_selector_request(selector, (uint8_t)raw_code, lease_ms);
    }
    gpio_apply(selector->applied_code);
    smateway_bench_mailbox.acknowledged_sequence = sequence;
    return accepted ? BENCH_STATUS_COMMAND_VALID : BENCH_STATUS_INVALID_COMMAND;
}

int main(void)
{
    control_selector_t selector;
    uint32_t command_status = 0u;

    control_selector_init(&selector);
    gpio_initialize_all_off();

    smateway_bench_mailbox.magic = BENCH_MAILBOX_MAGIC;
    smateway_bench_mailbox.version = BENCH_MAILBOX_VERSION;
    smateway_bench_mailbox.command_sequence = 0u;
    smateway_bench_mailbox.command_code = CONTROL_ALL_OFF_CODE;
    smateway_bench_mailbox.command_lease_ms = 0u;
    smateway_bench_mailbox.acknowledged_sequence = 0u;
    mailbox_publish(&selector, command_status);

    if (SystemCoreClock != RESET_CORE_CLOCK_HZ) {
        for (;;) {
            gpio_apply(CONTROL_ALL_OFF_CODE);
        }
    }

    SysTick->LOAD = SYSTICK_RELOAD;
    SysTick->VAL = 0u;
    SysTick->CTRL = SysTick_CTRL_CLKSOURCE_Msk | SysTick_CTRL_ENABLE_Msk;

    for (;;) {
        command_status = process_command(&selector);
        if ((SysTick->CTRL & SysTick_CTRL_COUNTFLAG_Msk) != 0u) {
            const uint8_t previous_code = selector.applied_code;

            control_selector_tick_ms(&selector, 1u);
            if (selector.applied_code != previous_code) {
                gpio_apply(selector.applied_code);
            }
        }
        mailbox_publish(&selector, command_status);
    }
}
