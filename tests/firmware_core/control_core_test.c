#include "control_core.h"

#include <assert.h>
#include <stdint.h>
#include <stdio.h>

static void test_truth_table(void)
{
    uint8_t code;
    size_t legal_count = 0u;

    for (code = 0u; code < 16u; ++code) {
        bool expected = code == CONTROL_ALL_OFF_CODE;
        size_t state_index;

        for (state_index = 0u; state_index < CONTROL_STATE_COUNT; ++state_index) {
            if (CONTROL_SCHEDULE[state_index].gpio_code_pa3_pa0 == code) {
                expected = true;
            }
        }
        assert(control_code_is_legal(code) == expected);
        if (expected) {
            ++legal_count;
        }
    }
    assert(legal_count == CONTROL_STATE_COUNT + 1u);
}

static void test_reset_and_invalid_fail_safe(void)
{
    control_selector_t selector;

    control_selector_init(&selector);
    assert(selector.applied_code == CONTROL_ALL_OFF_CODE);
    assert(!selector.lease_active);

    assert(!control_selector_request(&selector, 0xFu, 100u));
    assert(selector.applied_code == CONTROL_ALL_OFF_CODE);
    assert(selector.requested_code == CONTROL_ALL_OFF_CODE);
    assert(!selector.lease_active);
}

static void test_guard_and_lease(void)
{
    control_selector_t selector;
    const uint8_t first = CONTROL_SCHEDULE[0].gpio_code_pa3_pa0;

    control_selector_init(&selector);
    assert(control_selector_request(&selector, first, 20u));
    assert(selector.applied_code == CONTROL_ALL_OFF_CODE);
    control_selector_tick_ms(&selector, CONTROL_GUARD_MS - 1u);
    assert(selector.applied_code == CONTROL_ALL_OFF_CODE);
    control_selector_tick_ms(&selector, 1u);
    assert(selector.applied_code == first);
    control_selector_tick_ms(&selector, 14u);
    assert(selector.applied_code == first);
    control_selector_tick_ms(&selector, 1u);
    assert(selector.applied_code == CONTROL_ALL_OFF_CODE);
    assert(!selector.lease_active);
}

static void test_same_state_refresh_does_not_break(void)
{
    control_selector_t selector;
    const uint8_t code = CONTROL_SCHEDULE[2].gpio_code_pa3_pa0;

    control_selector_init(&selector);
    assert(control_selector_request(&selector, code, 20u));
    control_selector_tick_ms(&selector, CONTROL_GUARD_MS);
    assert(selector.applied_code == code);
    assert(control_selector_request(&selector, code, 100u));
    assert(selector.applied_code == code);
    assert(selector.guard_ms_remaining == 0u);
    assert(selector.lease_ms_remaining == 100u);
}

static void test_every_selected_transition_breaks_before_make(void)
{
    size_t from_index;
    size_t to_index;

    for (from_index = 0u; from_index < CONTROL_STATE_COUNT; ++from_index) {
        for (to_index = 0u; to_index < CONTROL_STATE_COUNT; ++to_index) {
            control_selector_t selector;
            const uint8_t from = CONTROL_SCHEDULE[from_index].gpio_code_pa3_pa0;
            const uint8_t to = CONTROL_SCHEDULE[to_index].gpio_code_pa3_pa0;

            control_selector_init(&selector);
            assert(control_selector_request(&selector, from, 100u));
            control_selector_tick_ms(&selector, CONTROL_GUARD_MS);
            assert(selector.applied_code == from);
            assert(control_selector_request(&selector, to, 100u));
            if (from == to) {
                assert(selector.applied_code == from);
                continue;
            }
            assert(selector.applied_code == CONTROL_ALL_OFF_CODE);
            control_selector_tick_ms(&selector, CONTROL_GUARD_MS - 1u);
            assert(selector.applied_code == CONTROL_ALL_OFF_CODE);
            control_selector_tick_ms(&selector, 1u);
            assert(selector.applied_code == to);
        }
    }
}

static void test_all_off_and_zero_lease(void)
{
    control_selector_t selector;
    const uint8_t code = CONTROL_SCHEDULE[4].gpio_code_pa3_pa0;

    control_selector_init(&selector);
    assert(!control_selector_request(&selector, code, 0u));
    assert(selector.applied_code == CONTROL_ALL_OFF_CODE);
    assert(control_selector_request(&selector, code, 100u));
    control_selector_tick_ms(&selector, CONTROL_GUARD_MS);
    assert(control_selector_request(&selector, CONTROL_ALL_OFF_CODE, 0u));
    assert(selector.applied_code == CONTROL_ALL_OFF_CODE);
}

int main(void)
{
    test_truth_table();
    test_reset_and_invalid_fail_safe();
    test_guard_and_lease();
    test_same_state_refresh_does_not_break();
    test_every_selected_transition_breaks_before_make();
    test_all_off_and_zero_lease();
    puts("control_core_test: PASS");
    return 0;
}
