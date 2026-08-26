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
    assert(high_rate_deadline_action(999u, 1000u) == HIGH_RATE_DEADLINE_WAIT);
    assert(high_rate_deadline_action(1000u, 1000u) == HIGH_RATE_DEADLINE_ADVANCE);
    assert(high_rate_deadline_action(1005u, 1000u) == HIGH_RATE_DEADLINE_ADVANCE);
    assert(
        high_rate_deadline_action(1006u, 1000u)
        == HIGH_RATE_DEADLINE_RESYNCHRONIZE
    );

    assert(high_rate_deadline_action(UINT16_MAX, 2u) == HIGH_RATE_DEADLINE_WAIT);
    assert(high_rate_deadline_action(2u, 2u) == HIGH_RATE_DEADLINE_ADVANCE);
    assert(high_rate_deadline_action(7u, 2u) == HIGH_RATE_DEADLINE_ADVANCE);
    assert(
        high_rate_deadline_action(8u, 2u) == HIGH_RATE_DEADLINE_RESYNCHRONIZE
    );
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
        const uint16_t observed_late = (uint16_t)(deadline + UINT16_C(3));

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
    test_one_transition_per_deadline();
    test_deadline_classification_and_wrap();
    test_resynchronization_returns_to_full_marker();
    test_late_polling_does_not_accumulate_into_the_schedule();
    puts("hexcal_core_test: PASS");
    return 0;
}
