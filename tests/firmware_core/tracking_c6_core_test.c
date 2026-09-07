#include "high_rate_autonomous_core.h"

#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

static void test_profile(void)
{
    static const uint8_t expected_codes[] = {0x0u, 0x4u, 0x6u, 0x7u, 0x3u, 0x1u};
    const uint16_t dwell_us = CONTROL_SCHEDULE[0].dwell_us;
    uint32_t cycle_us = CONTROL_MARKER_BODY_US;
    size_t index;

    assert(CONTROL_STATE_COUNT == 6u);
    assert(CONTROL_ALL_OFF_CODE == 0x8u);
    assert(CONTROL_TIMER_HZ == 1000000u);
    assert(CONTROL_MARKER_BODY_US == 180u);
    assert(CONTROL_GUARD_US == 20u);
    assert(CONTROL_MAX_LATENESS_US == 5u);
    assert(dwell_us >= 25u && dwell_us <= 200u);
    for (index = 0u; index < CONTROL_STATE_COUNT; ++index) {
        assert(CONTROL_SCHEDULE[index].gpio_code_pa3_pa0 == expected_codes[index]);
        assert(CONTROL_SCHEDULE[index].dwell_us == dwell_us);
        assert(CONTROL_SCHEDULE[index].gpio_code_pa3_pa0 != CONTROL_ALL_OFF_CODE);
        cycle_us += CONTROL_GUARD_US + dwell_us;
    }
    assert(cycle_us == CONTROL_NOMINAL_CYCLE_US);
}

static void test_frame(void)
{
    high_rate_frame_t frame;
    size_t index;

    high_rate_frame_init(&frame);
    assert(frame.phase == HIGH_RATE_MARKER);
    assert(frame.applied_code == CONTROL_ALL_OFF_CODE);
    assert(frame.phase_duration_us == CONTROL_MARKER_BODY_US);
    assert(!high_rate_frame_advance(&frame));
    assert(frame.phase == HIGH_RATE_GUARD);
    for (index = 0u; index < CONTROL_STATE_COUNT; ++index) {
        assert(!high_rate_frame_advance(&frame));
        assert(frame.phase == HIGH_RATE_DWELL);
        assert(frame.state_index == index);
        assert(frame.applied_code == CONTROL_SCHEDULE[index].gpio_code_pa3_pa0);
        if (index + 1u == CONTROL_STATE_COUNT) {
            assert(high_rate_frame_advance(&frame));
            assert(frame.phase == HIGH_RATE_MARKER);
        } else {
            assert(!high_rate_frame_advance(&frame));
            assert(frame.phase == HIGH_RATE_GUARD);
            assert(frame.applied_code == CONTROL_ALL_OFF_CODE);
        }
    }
}

int main(void)
{
    test_profile();
    test_frame();
    return 0;
}
