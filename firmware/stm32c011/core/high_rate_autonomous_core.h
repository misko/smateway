#ifndef SMATEWAY_HIGH_RATE_AUTONOMOUS_CORE_H
#define SMATEWAY_HIGH_RATE_AUTONOMOUS_CORE_H

#include <stdbool.h>
#include <stdint.h>

#include "control_profile.h"

#define HIGH_RATE_TIMER_HALF_RANGE_US UINT16_C(0x8000)

/*
 * Reserve one timer tick for counter quantization and the final two
 * microseconds of the profile's end-to-end lateness allowance for the atomic
 * GPIO write.  Deadline admission occurs before the write, so it must reject
 * a sample that is already more than two timer ticks late.  The ELF verifier
 * caps the compiled TIM3-CNT-to-GPIO-BSRR path at 16 Cortex-M0+ cycles,
 * including its accepted-path branch and memory accesses; GPIOA uses the
 * core's 0x50000000 single-cycle I/O mapping.
 */
#define CONTROL_COUNTER_QUANTIZATION_TICKS UINT16_C(1)
#define CONTROL_GPIO_WRITE_BUDGET_US UINT16_C(2)
#define CONTROL_PREWRITE_MAX_LATENESS_US \
    (CONTROL_MAX_LATENESS_US \
        - CONTROL_COUNTER_QUANTIZATION_TICKS \
        - CONTROL_GPIO_WRITE_BUDGET_US)
#define HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK UINT32_C(12)
#define CONTROL_TIGHT_POLL_WINDOW_US UINT16_C(8)
#define CONTROL_FAR_POLL_SAMPLE_MAX_CORE_CYCLES UINT32_C(54)
#define CONTROL_STAGING_TO_TIGHT_SAMPLE_MAX_CORE_CYCLES UINT32_C(22)
#define CONTROL_TIGHT_POLL_SAMPLE_MAX_CORE_CYCLES UINT32_C(11)
#define CONTROL_DUE_SAMPLE_TO_FINAL_SAMPLE_MAX_CORE_CYCLES UINT32_C(23)
#define CONTROL_GPIO_WRITE_MAX_CORE_CYCLES UINT32_C(16)
#define CONTROL_ENDPOINT_MEMORY_ACCESS_CORE_CYCLES UINT32_C(3)
#define CONTROL_TRANSITION_TURNOVER_MAX_CORE_CYCLES UINT32_C(165)
#define CONTROL_DEADLINE_TO_GPIO_MAX_CORE_CYCLES \
    ((uint32_t)(CONTROL_PREWRITE_MAX_LATENESS_US \
            + CONTROL_COUNTER_QUANTIZATION_TICKS) \
            * HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK \
        + CONTROL_GPIO_WRITE_MAX_CORE_CYCLES)
#define CONTROL_SHORTEST_PHASE_CHAIN_MAX_CORE_CYCLES \
    (CONTROL_DEADLINE_TO_GPIO_MAX_CORE_CYCLES \
        + CONTROL_TRANSITION_TURNOVER_MAX_CORE_CYCLES \
        + CONTROL_STAGING_TO_TIGHT_SAMPLE_MAX_CORE_CYCLES \
        - UINT32_C(2) * CONTROL_ENDPOINT_MEMORY_ACCESS_CORE_CYCLES)

/*
 * STM32C011 datasheet DS13866 Rev 5 Table 40 gives the HSI48 initial-frequency
 * endpoints and -2.5/+2% drift over the full -40--125 C range.  The 97--103%
 * envelope conservatively covers their combined extremes.  Table 41 gives
 * the LSI electrical-characteristic limits as 29.5--34 kHz.
 * IWDG timeout is (reload + 1) * prescaler / LSI.  Floor the minimum timeout
 * and ceil the maximum timeout so both exported bounds remain conservative.
 */
#define HSI48_MIN_RATE_PERCENT UINT32_C(97)
#define HSI48_NOMINAL_RATE_PERCENT UINT32_C(100)
#define HSI48_MAX_RATE_PERCENT UINT32_C(103)
#define IWDG_LSI_MIN_HZ UINT32_C(29500)
#define IWDG_LSI_MAX_HZ UINT32_C(34000)
#define IWDG_PRESCALER_DIVIDER UINT32_C(4)
#define IWDG_RELOAD_VALUE UINT32_C(127)
#define IWDG_TIMEOUT_NUMERATOR_US \
    ((IWDG_RELOAD_VALUE + UINT32_C(1)) \
        * IWDG_PRESCALER_DIVIDER * UINT32_C(1000000))
#define IWDG_MAX_TIMEOUT_US_CEIL \
    ((IWDG_TIMEOUT_NUMERATOR_US + IWDG_LSI_MIN_HZ - UINT32_C(1)) \
        / IWDG_LSI_MIN_HZ)
#define IWDG_MIN_TIMEOUT_US_FLOOR \
    (IWDG_TIMEOUT_NUMERATOR_US / IWDG_LSI_MAX_HZ)
#define HIGH_RATE_TIMER_HALF_RANGE_MIN_US_FLOOR \
    ((uint32_t)(HIGH_RATE_TIMER_HALF_RANGE_US) \
        * HSI48_NOMINAL_RATE_PERCENT / HSI48_MAX_RATE_PERCENT)
#define CONTROL_CYCLE_MAX_WALL_US_CEIL \
    (((uint32_t)(CONTROL_NOMINAL_CYCLE_US) \
        * HSI48_NOMINAL_RATE_PERCENT + HSI48_MIN_RATE_PERCENT - UINT32_C(1)) \
        / HSI48_MIN_RATE_PERCENT)
#define IWDG_PROVEN_REFRESH_OPPORTUNITIES UINT32_C(9)

_Static_assert(
    CONTROL_MAX_LATENESS_US
        >= CONTROL_COUNTER_QUANTIZATION_TICKS + CONTROL_GPIO_WRITE_BUDGET_US,
    "quantization and GPIO budgets exceed maximum lateness"
);
_Static_assert(
    CONTROL_PREWRITE_MAX_LATENESS_US
        + CONTROL_COUNTER_QUANTIZATION_TICKS
        + CONTROL_GPIO_WRITE_BUDGET_US
        == CONTROL_MAX_LATENESS_US,
    "pre-write, quantization and GPIO budgets must cover maximum lateness"
);
_Static_assert(
    CONTROL_FAR_POLL_SAMPLE_MAX_CORE_CYCLES
        < CONTROL_TIGHT_POLL_WINDOW_US
            * HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK,
    "far poll can skip the complete staging window"
);
_Static_assert(
    CONTROL_STAGING_TO_TIGHT_SAMPLE_MAX_CORE_CYCLES
        < CONTROL_TIGHT_POLL_WINDOW_US
            * HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK,
    "staging path can reach the deadline before the tight poll"
);
_Static_assert(
    CONTROL_FAR_POLL_SAMPLE_MAX_CORE_CYCLES
        + CONTROL_STAGING_TO_TIGHT_SAMPLE_MAX_CORE_CYCLES
        < (CONTROL_TIGHT_POLL_WINDOW_US - UINT16_C(1))
            * HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK,
    "far and staging paths together cannot guarantee pre-deadline tight polling"
);
_Static_assert(
    CONTROL_TIGHT_POLL_SAMPLE_MAX_CORE_CYCLES
        < HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK,
    "tight poll can skip a timer count at the deadline"
);
_Static_assert(
    CONTROL_DUE_SAMPLE_TO_FINAL_SAMPLE_MAX_CORE_CYCLES
        < CONTROL_PREWRITE_MAX_LATENESS_US
            * HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK,
    "deadline recheck path can consume the complete admission window"
);
_Static_assert(
    CONTROL_GPIO_WRITE_MAX_CORE_CYCLES
        <= CONTROL_GPIO_WRITE_BUDGET_US
            * HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK,
    "verified GPIO path exceeds its nominal write budget"
);
_Static_assert(
    CONTROL_TRANSITION_TURNOVER_MAX_CORE_CYCLES
        < CONTROL_GUARD_US * HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK,
    "transition turnover can miss the shortest scheduled phase"
);
_Static_assert(
    CONTROL_SHORTEST_PHASE_CHAIN_MAX_CORE_CYCLES
        < CONTROL_GUARD_US * HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK,
    "prior edge lateness and turnover can miss the next shortest phase"
);
_Static_assert(
    CONTROL_DEADLINE_TO_GPIO_MAX_CORE_CYCLES
        * HSI48_NOMINAL_RATE_PERCENT
        <= (uint32_t)(CONTROL_MAX_LATENESS_US)
            * HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK
            * HSI48_MIN_RATE_PERCENT,
    "deadline and GPIO path exceed wall-time lateness at slowest HSI48"
);
_Static_assert(
    CONTROL_NOMINAL_CYCLE_US < HIGH_RATE_TIMER_HALF_RANGE_US,
    "control cycle exceeds the wrap-safe timer half-range"
);
_Static_assert(
    IWDG_LSI_MIN_HZ < IWDG_LSI_MAX_HZ,
    "watchdog LSI frequency bounds are reversed"
);
_Static_assert(
    IWDG_MAX_TIMEOUT_US_CEIL < HIGH_RATE_TIMER_HALF_RANGE_MIN_US_FLOOR,
    "maximum watchdog timeout exceeds the fastest timer half-range"
);
_Static_assert(
    IWDG_MIN_TIMEOUT_US_FLOOR < IWDG_MAX_TIMEOUT_US_CEIL,
    "watchdog timeout bounds are reversed"
);
_Static_assert(
    IWDG_MIN_TIMEOUT_US_FLOOR
        > IWDG_PROVEN_REFRESH_OPPORTUNITIES * CONTROL_CYCLE_MAX_WALL_US_CEIL,
    "minimum watchdog timeout lacks the required worst-case refresh margin"
);

typedef enum {
    HIGH_RATE_MARKER,
    HIGH_RATE_GUARD,
    HIGH_RATE_DWELL,
} high_rate_phase_t;

typedef enum {
    HIGH_RATE_DEADLINE_WAIT,
    HIGH_RATE_DEADLINE_ADVANCE,
    HIGH_RATE_DEADLINE_RESYNCHRONIZE,
} high_rate_deadline_action_t;

typedef struct {
    high_rate_phase_t phase;
    uint8_t state_index;
    uint8_t applied_code;
    uint16_t phase_duration_us;
} high_rate_frame_t;

static inline bool high_rate_deadline_pending(
    uint16_t now,
    uint16_t deadline
)
{
    return (uint16_t)(now - deadline) >= HIGH_RATE_TIMER_HALF_RANGE_US;
}

static inline bool high_rate_deadline_within_staging_window(
    uint16_t now,
    uint16_t deadline,
    uint16_t staging_window_us
)
{
    return staging_window_us < HIGH_RATE_TIMER_HALF_RANGE_US
        && high_rate_deadline_pending(now, deadline)
        && (uint16_t)(deadline - now) <= staging_window_us;
}

static inline bool high_rate_deadline_advance_allowed(
    uint16_t now,
    uint16_t deadline
)
{
    const uint16_t elapsed_since_deadline = (uint16_t)(now - deadline);

    return !high_rate_deadline_pending(now, deadline)
        && elapsed_since_deadline <= CONTROL_PREWRITE_MAX_LATENESS_US;
}

void high_rate_frame_init(high_rate_frame_t *frame);
bool high_rate_frame_advance(high_rate_frame_t *frame);
high_rate_deadline_action_t high_rate_deadline_action(
    uint16_t now,
    uint16_t deadline
);
uint16_t high_rate_next_deadline(
    uint16_t previous_deadline,
    uint16_t phase_duration_us
);

#endif
