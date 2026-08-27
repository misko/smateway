#include "high_rate_autonomous_core.h"

#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

static void test_exact_profile(void)
{
    static const uint8_t expected_codes[] = {0x0u, 0x4u, 0x2u, 0x6u, 0x1u, 0x5u};
    size_t index;

    assert(CONTROL_ALL_OFF_CODE == 0x8u);
    assert(CONTROL_TIMER_HZ == 1000000u);
    assert(CONTROL_MARKER_BODY_US == 180u);
    assert(CONTROL_GUARD_US == 20u);
    assert(CONTROL_MAX_LATENESS_US == 5u);
    assert(CONTROL_NOMINAL_CYCLE_US == 1500u);
    assert(CONTROL_STATE_COUNT == sizeof(expected_codes) / sizeof(expected_codes[0]));
    for (index = 0u; index < CONTROL_STATE_COUNT; ++index) {
        assert(CONTROL_SCHEDULE[index].gpio_code_pa3_pa0 == expected_codes[index]);
        assert(CONTROL_SCHEDULE[index].dwell_us == 200u);
        assert(CONTROL_SCHEDULE[index].gpio_code_pa3_pa0 != CONTROL_ALL_OFF_CODE);
        assert(CONTROL_SCHEDULE[index].gpio_code_pa3_pa0 != 0x3u);
        assert(CONTROL_SCHEDULE[index].gpio_code_pa3_pa0 != 0x7u);
    }
}

static void test_watchdog_bounds_and_refresh_margin(void)
{
    assert(IWDG_LSI_MIN_HZ == 29500u);
    assert(IWDG_LSI_MAX_HZ == 34000u);
    assert(IWDG_PRESCALER_DIVIDER == 4u);
    assert(IWDG_RELOAD_VALUE == 127u);
    assert(IWDG_TIMEOUT_NUMERATOR_US == 512000000u);
    assert(IWDG_MAX_TIMEOUT_US_CEIL == 17356u);
    assert(IWDG_MIN_TIMEOUT_US_FLOOR == 15058u);
    assert(HSI48_MIN_RATE_PERCENT == 97u);
    assert(HSI48_MAX_RATE_PERCENT == 103u);
    assert(HIGH_RATE_TIMER_HALF_RANGE_MIN_US_FLOOR == 31813u);
    assert(CONTROL_CYCLE_MAX_WALL_US_CEIL == 1547u);
    assert(IWDG_PROVEN_REFRESH_OPPORTUNITIES == 9u);

    assert(
        IWDG_MAX_TIMEOUT_US_CEIL * IWDG_LSI_MIN_HZ
        >= IWDG_TIMEOUT_NUMERATOR_US
    );
    assert(
        (IWDG_MAX_TIMEOUT_US_CEIL - 1u) * IWDG_LSI_MIN_HZ
        < IWDG_TIMEOUT_NUMERATOR_US
    );
    assert(
        IWDG_MIN_TIMEOUT_US_FLOOR * IWDG_LSI_MAX_HZ
        <= IWDG_TIMEOUT_NUMERATOR_US
    );
    assert(
        (IWDG_MIN_TIMEOUT_US_FLOOR + 1u) * IWDG_LSI_MAX_HZ
        > IWDG_TIMEOUT_NUMERATOR_US
    );
    assert(
        IWDG_MAX_TIMEOUT_US_CEIL
        < HIGH_RATE_TIMER_HALF_RANGE_MIN_US_FLOOR
    );
    assert(
        IWDG_MIN_TIMEOUT_US_FLOOR
        > IWDG_PROVEN_REFRESH_OPPORTUNITIES
            * CONTROL_CYCLE_MAX_WALL_US_CEIL
    );
}

static void test_one_transition_per_deadline(void)
{
    high_rate_frame_t frame;
    size_t state_index;
    uint32_t derived_cycle_us = CONTROL_MARKER_BODY_US;

    high_rate_frame_init(&frame);
    assert(frame.phase == HIGH_RATE_MARKER);
    assert(frame.applied_code == CONTROL_ALL_OFF_CODE);
    assert(frame.phase_duration_us == CONTROL_MARKER_BODY_US);
    assert(!high_rate_frame_advance(&frame));
    assert(frame.phase == HIGH_RATE_GUARD);
    assert(frame.applied_code == CONTROL_ALL_OFF_CODE);
    assert(frame.phase_duration_us == CONTROL_GUARD_US);

    for (state_index = 0u; state_index < CONTROL_STATE_COUNT; ++state_index) {
        assert(!high_rate_frame_advance(&frame));
        assert(frame.phase == HIGH_RATE_DWELL);
        assert(frame.state_index == state_index);
        assert(frame.applied_code == CONTROL_SCHEDULE[state_index].gpio_code_pa3_pa0);
        assert(frame.phase_duration_us == CONTROL_SCHEDULE[state_index].dwell_us);
        derived_cycle_us += CONTROL_GUARD_US + frame.phase_duration_us;

        if (state_index + 1u == CONTROL_STATE_COUNT) {
            assert(high_rate_frame_advance(&frame));
            assert(frame.phase == HIGH_RATE_MARKER);
            assert(frame.state_index == 0u);
            assert(frame.applied_code == CONTROL_ALL_OFF_CODE);
            assert(frame.phase_duration_us == CONTROL_MARKER_BODY_US);
        } else {
            assert(!high_rate_frame_advance(&frame));
            assert(frame.phase == HIGH_RATE_GUARD);
            assert(frame.applied_code == CONTROL_ALL_OFF_CODE);
            assert(frame.phase_duration_us == CONTROL_GUARD_US);
        }
    }
    assert(derived_cycle_us == CONTROL_NOMINAL_CYCLE_US);
}

static void test_deadline_classification_and_wrap(void)
{
    assert(CONTROL_COUNTER_QUANTIZATION_TICKS == 1u);
    assert(CONTROL_GPIO_WRITE_BUDGET_US == 2u);
    assert(CONTROL_PREWRITE_MAX_LATENESS_US == 2u);
    assert(HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK == 12u);
    assert(CONTROL_TIGHT_POLL_WINDOW_US == 8u);
    assert(CONTROL_FAR_POLL_SAMPLE_MAX_CORE_CYCLES == 54u);
    assert(CONTROL_STAGING_TO_TIGHT_SAMPLE_MAX_CORE_CYCLES == 22u);
    assert(CONTROL_TIGHT_POLL_SAMPLE_MAX_CORE_CYCLES == 11u);
    assert(CONTROL_DUE_SAMPLE_TO_FINAL_SAMPLE_MAX_CORE_CYCLES == 23u);
    assert(CONTROL_GPIO_WRITE_MAX_CORE_CYCLES == 16u);
    assert(CONTROL_ENDPOINT_MEMORY_ACCESS_CORE_CYCLES == 3u);
    assert(CONTROL_TRANSITION_TURNOVER_MAX_CORE_CYCLES == 165u);
    assert(CONTROL_DEADLINE_TO_GPIO_MAX_CORE_CYCLES == 52u);
    assert(CONTROL_SHORTEST_PHASE_CHAIN_MAX_CORE_CYCLES == 233u);
    assert(
        CONTROL_FAR_POLL_SAMPLE_MAX_CORE_CYCLES
        < CONTROL_TIGHT_POLL_WINDOW_US
            * HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK
    );
    assert(
        CONTROL_STAGING_TO_TIGHT_SAMPLE_MAX_CORE_CYCLES
        < CONTROL_TIGHT_POLL_WINDOW_US
            * HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK
    );
    assert(
        CONTROL_FAR_POLL_SAMPLE_MAX_CORE_CYCLES
            + CONTROL_STAGING_TO_TIGHT_SAMPLE_MAX_CORE_CYCLES
        < (CONTROL_TIGHT_POLL_WINDOW_US - 1u)
            * HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK
    );
    assert(
        CONTROL_TIGHT_POLL_SAMPLE_MAX_CORE_CYCLES
        < HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK
    );
    assert(
        CONTROL_DUE_SAMPLE_TO_FINAL_SAMPLE_MAX_CORE_CYCLES
        < CONTROL_PREWRITE_MAX_LATENESS_US
            * HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK
    );
    assert(
        CONTROL_TRANSITION_TURNOVER_MAX_CORE_CYCLES
        < CONTROL_GUARD_US * HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK
    );
    assert(
        CONTROL_SHORTEST_PHASE_CHAIN_MAX_CORE_CYCLES
        < CONTROL_GUARD_US * HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK
    );
    assert(
        ((CONTROL_PREWRITE_MAX_LATENESS_US
                + CONTROL_COUNTER_QUANTIZATION_TICKS)
            * HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK
            + CONTROL_GPIO_WRITE_MAX_CORE_CYCLES)
            * HSI48_NOMINAL_RATE_PERCENT
        <= CONTROL_MAX_LATENESS_US
            * HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK
            * HSI48_MIN_RATE_PERCENT
    );

    assert(high_rate_deadline_pending(999u, 1000u));
    assert(!high_rate_deadline_pending(1000u, 1000u));
    assert(!high_rate_deadline_pending(1002u, 1000u));
    assert(!high_rate_deadline_advance_allowed(999u, 1000u));
    assert(high_rate_deadline_advance_allowed(1000u, 1000u));
    assert(high_rate_deadline_advance_allowed(1002u, 1000u));
    assert(!high_rate_deadline_advance_allowed(1003u, 1000u));

    assert(high_rate_deadline_action(999u, 1000u) == HIGH_RATE_DEADLINE_WAIT);
    assert(high_rate_deadline_action(1000u, 1000u) == HIGH_RATE_DEADLINE_ADVANCE);
    assert(high_rate_deadline_action(1002u, 1000u) == HIGH_RATE_DEADLINE_ADVANCE);
    assert(
        high_rate_deadline_action(1003u, 1000u)
        == HIGH_RATE_DEADLINE_RESYNCHRONIZE
    );

    assert(high_rate_deadline_pending(UINT16_MAX, 2u));
    assert(!high_rate_deadline_pending(2u, 2u));
    assert(!high_rate_deadline_pending(4u, 2u));
    assert(!high_rate_deadline_advance_allowed(UINT16_MAX, 2u));
    assert(high_rate_deadline_advance_allowed(2u, 2u));
    assert(high_rate_deadline_advance_allowed(4u, 2u));
    assert(!high_rate_deadline_advance_allowed(5u, 2u));

    assert(high_rate_deadline_action(UINT16_MAX, 2u) == HIGH_RATE_DEADLINE_WAIT);
    assert(high_rate_deadline_action(2u, 2u) == HIGH_RATE_DEADLINE_ADVANCE);
    assert(high_rate_deadline_action(4u, 2u) == HIGH_RATE_DEADLINE_ADVANCE);
    assert(
        high_rate_deadline_action(5u, 2u) == HIGH_RATE_DEADLINE_RESYNCHRONIZE
    );

    assert(!high_rate_deadline_pending(UINT16_C(32769), UINT16_C(2)));
    assert(
        high_rate_deadline_action(UINT16_C(32769), UINT16_C(2))
        == HIGH_RATE_DEADLINE_RESYNCHRONIZE
    );
    assert(high_rate_deadline_pending(UINT16_C(32770), UINT16_C(2)));
    assert(
        high_rate_deadline_action(UINT16_C(32770), UINT16_C(2))
        == HIGH_RATE_DEADLINE_WAIT
    );
}

static void test_deadline_staging_window_and_half_range(void)
{
    const uint16_t staging_window_us = UINT16_C(8);

    assert(!high_rate_deadline_within_staging_window(
        991u, 1000u, staging_window_us
    ));
    assert(high_rate_deadline_within_staging_window(
        992u, 1000u, staging_window_us
    ));
    assert(high_rate_deadline_within_staging_window(
        999u, 1000u, staging_window_us
    ));
    assert(!high_rate_deadline_within_staging_window(
        1000u, 1000u, staging_window_us
    ));
    assert(!high_rate_deadline_within_staging_window(
        1001u, 1000u, staging_window_us
    ));

    assert(!high_rate_deadline_within_staging_window(
        65529u, 2u, staging_window_us
    ));
    assert(high_rate_deadline_within_staging_window(
        65530u, 2u, staging_window_us
    ));
    assert(high_rate_deadline_within_staging_window(
        UINT16_MAX, 2u, staging_window_us
    ));
    assert(!high_rate_deadline_within_staging_window(
        2u, 2u, staging_window_us
    ));

    assert(high_rate_deadline_pending(UINT16_C(32770), UINT16_C(2)));
    assert(!high_rate_deadline_within_staging_window(
        UINT16_C(32770), UINT16_C(2), UINT16_MAX
    ));
}

static void test_resynchronization_returns_to_full_marker(void)
{
    high_rate_frame_t frame;

    high_rate_frame_init(&frame);
    assert(!high_rate_frame_advance(&frame));
    assert(!high_rate_frame_advance(&frame));
    assert(frame.phase == HIGH_RATE_DWELL);
    assert(frame.applied_code == CONTROL_SCHEDULE[0].gpio_code_pa3_pa0);

    high_rate_frame_init(&frame);
    assert(frame.phase == HIGH_RATE_MARKER);
    assert(frame.state_index == 0u);
    assert(frame.applied_code == CONTROL_ALL_OFF_CODE);
    assert(frame.phase_duration_us == CONTROL_MARKER_BODY_US);
}

static void test_late_polling_does_not_accumulate_into_the_schedule(void)
{
    high_rate_frame_t frame;
    uint16_t deadline = UINT16_C(1000);
    const uint16_t frame_start = deadline;
    size_t transition;

    high_rate_frame_init(&frame);
    for (transition = 0u; transition < 13u; ++transition) {
        const uint16_t observed_late = (uint16_t)(deadline + UINT16_C(2));

        assert(
            high_rate_deadline_action(observed_late, deadline)
            == HIGH_RATE_DEADLINE_ADVANCE
        );
        (void)high_rate_frame_advance(&frame);
        deadline = high_rate_next_deadline(deadline, frame.phase_duration_us);
    }

    assert((uint16_t)(deadline - frame_start) == CONTROL_NOMINAL_CYCLE_US);
    assert(high_rate_next_deadline(UINT16_C(65530), UINT16_C(20)) == UINT16_C(14));
}

int main(void)
{
    test_exact_profile();
    test_watchdog_bounds_and_refresh_margin();
    test_one_transition_per_deadline();
    test_deadline_classification_and_wrap();
    test_deadline_staging_window_and_half_range();
    test_resynchronization_returns_to_full_marker();
    test_late_polling_does_not_accumulate_into_the_schedule();
    puts("hexcal_core_test: PASS");
    return 0;
}
