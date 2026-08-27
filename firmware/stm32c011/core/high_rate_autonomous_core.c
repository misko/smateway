#include "high_rate_autonomous_core.h"

void high_rate_frame_init(high_rate_frame_t *frame)
{
    frame->phase = HIGH_RATE_MARKER;
    frame->state_index = 0u;
    frame->applied_code = CONTROL_ALL_OFF_CODE;
    frame->phase_duration_us = CONTROL_MARKER_BODY_US;
}

bool high_rate_frame_advance(high_rate_frame_t *frame)
{
    switch (frame->phase) {
    case HIGH_RATE_MARKER:
        frame->phase = HIGH_RATE_GUARD;
        frame->state_index = 0u;
        frame->applied_code = CONTROL_ALL_OFF_CODE;
        frame->phase_duration_us = CONTROL_GUARD_US;
        return false;
    case HIGH_RATE_GUARD:
        frame->phase = HIGH_RATE_DWELL;
        frame->applied_code = CONTROL_SCHEDULE[frame->state_index].gpio_code_pa3_pa0;
        frame->phase_duration_us = CONTROL_SCHEDULE[frame->state_index].dwell_us;
        return false;
    case HIGH_RATE_DWELL:
        frame->applied_code = CONTROL_ALL_OFF_CODE;
        ++frame->state_index;
        if (frame->state_index == CONTROL_STATE_COUNT) {
            high_rate_frame_init(frame);
            return true;
        }
        frame->phase = HIGH_RATE_GUARD;
        frame->phase_duration_us = CONTROL_GUARD_US;
        return false;
    }

    high_rate_frame_init(frame);
    return false;
}

high_rate_deadline_action_t high_rate_deadline_action(
    uint16_t now,
    uint16_t deadline
)
{
    if (high_rate_deadline_pending(now, deadline)) {
        return HIGH_RATE_DEADLINE_WAIT;
    }
    if (!high_rate_deadline_advance_allowed(now, deadline)) {
        return HIGH_RATE_DEADLINE_RESYNCHRONIZE;
    }
    return HIGH_RATE_DEADLINE_ADVANCE;
}

uint16_t high_rate_next_deadline(
    uint16_t previous_deadline,
    uint16_t phase_duration_us
)
{
    return (uint16_t)(previous_deadline + phase_duration_us);
}
