#ifndef SMATEWAY_CONTROL_CORE_H
#define SMATEWAY_CONTROL_CORE_H

#include <stdbool.h>
#include <stdint.h>

#include "control_profile.h"

typedef struct {
    uint8_t applied_code;
    uint8_t requested_code;
    uint16_t guard_ms_remaining;
    uint32_t lease_ms_remaining;
    bool lease_active;
} control_selector_t;

bool control_code_is_legal(uint8_t code);
void control_selector_init(control_selector_t *selector);
bool control_selector_request(
    control_selector_t *selector,
    uint8_t requested_code,
    uint32_t lease_ms
);
void control_selector_tick_ms(control_selector_t *selector, uint32_t elapsed_ms);
void control_selector_force_all_off(control_selector_t *selector);

#endif
