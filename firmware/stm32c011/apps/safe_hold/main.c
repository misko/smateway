#include <stdint.h>

#include "control_profile.h"
#include "stm32c0xx.h"

#define CONTROL_PIN_MASK UINT32_C(0x0F)
#define CONTROL_MODE_MASK UINT32_C(0xFF)
#define CONTROL_OUTPUT_MODES UINT32_C(0x55)

_Static_assert(CONTROL_ALL_OFF_CODE == 0x8u, "hardware ALL_OFF contract changed");

static uint32_t control_bsrr_word(uint32_t code)
{
    const uint32_t set_bits = code & CONTROL_PIN_MASK;
    const uint32_t reset_bits = (~code) & CONTROL_PIN_MASK;

    return set_bits | (reset_bits << 16u);
}

int main(void)
{
    const uint32_t all_off = (uint32_t)CONTROL_ALL_OFF_CODE;
    volatile uint32_t clock_readback;

    RCC->IOPENR |= RCC_IOPENR_GPIOAEN;
    clock_readback = RCC->IOPENR;
    (void)clock_readback;

    /* One atomic write while PA0..PA3 remain reset-state inputs. */
    GPIOA->BSRR = control_bsrr_word(all_off);

    /* PA0..PA3 only: push-pull, reset low speed, no internal pulls. */
    GPIOA->OTYPER &= ~CONTROL_PIN_MASK;
    GPIOA->MODER = (GPIOA->MODER & ~CONTROL_MODE_MASK) | CONTROL_OUTPUT_MODES;

    if ((GPIOA->ODR & CONTROL_PIN_MASK) != all_off) {
        for (;;) {
            GPIOA->BSRR = control_bsrr_word(all_off);
            __NOP();
        }
    }

    for (;;) {
        __NOP();
    }
}
