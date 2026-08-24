#include "control_core.h"

#include <stddef.h>

bool control_code_is_legal(uint8_t code)
{
    size_t index;

    if (code == CONTROL_ALL_OFF_CODE) {
        return true;
    }
    for (index = 0u; index < CONTROL_STATE_COUNT; ++index) {
        if (CONTROL_SCHEDULE[index].gpio_code_pa3_pa0 == code) {
            return true;
        }
    }
    return false;
}

void control_selector_force_all_off(control_selector_t *selector)
{
    selector->applied_code = CONTROL_ALL_OFF_CODE;
    selector->requested_code = CONTROL_ALL_OFF_CODE;
    selector->guard_ms_remaining = 0u;
    selector->lease_ms_remaining = 0u;
    selector->lease_active = false;
}

void control_selector_init(control_selector_t *selector)
{
    control_selector_force_all_off(selector);
}

bool control_selector_request(
    control_selector_t *selector,
    uint8_t requested_code,
    uint32_t lease_ms
)
{
    if (!control_code_is_legal(requested_code)) {
        control_selector_force_all_off(selector);
        return false;
    }
    if (requested_code == CONTROL_ALL_OFF_CODE) {
        control_selector_force_all_off(selector);
        return true;
    }
    if (lease_ms == 0u) {
        control_selector_force_all_off(selector);
        return false;
    }

    selector->lease_active = true;
    selector->lease_ms_remaining = lease_ms;
    if (selector->requested_code == requested_code
        && selector->applied_code == requested_code) {
        return true;
    }
    if (selector->requested_code == requested_code
        && selector->applied_code == CONTROL_ALL_OFF_CODE
        && selector->guard_ms_remaining > 0u) {
        return true;
    }

    selector->requested_code = requested_code;
    selector->applied_code = CONTROL_ALL_OFF_CODE;
    selector->guard_ms_remaining = CONTROL_GUARD_MS;
    return true;
}

void control_selector_tick_ms(control_selector_t *selector, uint32_t elapsed_ms)
{
    if (elapsed_ms == 0u) {
        return;
    }
    if (!selector->lease_active
        || elapsed_ms >= selector->lease_ms_remaining) {
        control_selector_force_all_off(selector);
        return;
    }
    selector->lease_ms_remaining -= elapsed_ms;

    if (selector->requested_code == CONTROL_ALL_OFF_CODE) {
        selector->applied_code = CONTROL_ALL_OFF_CODE;
        selector->guard_ms_remaining = 0u;
        return;
    }
    if (selector->guard_ms_remaining == 0u) {
        selector->applied_code = selector->requested_code;
        return;
    }
    if (elapsed_ms >= selector->guard_ms_remaining) {
        selector->guard_ms_remaining = 0u;
        selector->applied_code = selector->requested_code;
        return;
    }
    selector->guard_ms_remaining = (uint16_t)(
        selector->guard_ms_remaining - elapsed_ms
    );
}
