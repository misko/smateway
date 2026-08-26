#ifndef SMATEWAY_HIGH_RATE_AUTONOMOUS_CORE_H
#define SMATEWAY_HIGH_RATE_AUTONOMOUS_CORE_H

#include <stdbool.h>
#include <stdint.h>

#include "control_profile.h"

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

void high_rate_frame_init(high_rate_frame_t *frame);
bool high_rate_frame_advance(high_rate_frame_t *frame);
high_rate_deadline_action_t high_rate_deadline_action(
    uint16_t now,
    uint16_t deadline
);

#endif
