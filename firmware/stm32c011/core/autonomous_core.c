#include "autonomous_core.h"

static void advance_phase(autonomous_frame_t *frame)
{
    switch (frame->phase) {
    case AUTONOMOUS_MARKER:
        frame->phase = AUTONOMOUS_GUARD;
        frame->state_index = 0u;
        frame->applied_code = CONTROL_ALL_OFF_CODE;
        frame->phase_ms_remaining = CONTROL_GUARD_MS;
        break;
    case AUTONOMOUS_GUARD:
        frame->phase = AUTONOMOUS_DWELL;
        frame->applied_code = CONTROL_SCHEDULE[frame->state_index].gpio_code_pa3_pa0;
        frame->phase_ms_remaining = CONTROL_SCHEDULE[frame->state_index].dwell_ms;
        break;
    case AUTONOMOUS_DWELL:
        frame->applied_code = CONTROL_ALL_OFF_CODE;
        ++frame->state_index;
        if (frame->state_index == CONTROL_STATE_COUNT) {
            frame->phase = AUTONOMOUS_MARKER;
            frame->state_index = 0u;
            frame->phase_ms_remaining = CONTROL_MARKER_BODY_MS;
        } else {
            frame->phase = AUTONOMOUS_GUARD;
            frame->phase_ms_remaining = CONTROL_GUARD_MS;
        }
        break;
    }
}

void autonomous_frame_init(autonomous_frame_t *frame)
{
    frame->phase = AUTONOMOUS_MARKER;
    frame->state_index = 0u;
    frame->applied_code = CONTROL_ALL_OFF_CODE;
    frame->phase_ms_remaining = CONTROL_MARKER_BODY_MS;
}

void autonomous_frame_tick_ms(autonomous_frame_t *frame, uint32_t elapsed_ms)
{
    while (elapsed_ms > 0u) {
        const uint32_t step = elapsed_ms < frame->phase_ms_remaining
            ? elapsed_ms
            : frame->phase_ms_remaining;

        frame->phase_ms_remaining = (uint16_t)(frame->phase_ms_remaining - step);
        elapsed_ms -= step;
        if (frame->phase_ms_remaining == 0u) {
            advance_phase(frame);
        }
    }
}
