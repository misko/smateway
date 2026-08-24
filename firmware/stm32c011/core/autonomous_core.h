#ifndef SMATEWAY_AUTONOMOUS_CORE_H
#define SMATEWAY_AUTONOMOUS_CORE_H

#include <stdint.h>

#include "control_profile.h"

typedef enum {
    AUTONOMOUS_MARKER,
    AUTONOMOUS_GUARD,
    AUTONOMOUS_DWELL,
} autonomous_phase_t;

typedef struct {
    autonomous_phase_t phase;
    uint8_t state_index;
    uint8_t applied_code;
    uint16_t phase_ms_remaining;
} autonomous_frame_t;

void autonomous_frame_init(autonomous_frame_t *frame);
void autonomous_frame_tick_ms(autonomous_frame_t *frame, uint32_t elapsed_ms);

#endif
