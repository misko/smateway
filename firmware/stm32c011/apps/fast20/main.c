#include <stdint.h>

#include "autonomous_core.h"
#include "stm32c0xx.h"

#define CONTROL_PIN_MASK UINT32_C(0x0F)
#define CONTROL_MODE_MASK UINT32_C(0xFF)
#define CONTROL_OUTPUT_MODES UINT32_C(0x55)
#define RESET_CORE_CLOCK_HZ UINT32_C(12000000)
#define TICKS_PER_SECOND UINT32_C(1000)
#define SYSTICK_RELOAD ((RESET_CORE_CLOCK_HZ / TICKS_PER_SECOND) - 1u)

#define IWDG_KEY_ENABLE UINT32_C(0xCCCC)
#define IWDG_KEY_WRITE_ACCESS UINT32_C(0x5555)
#define IWDG_KEY_REFRESH UINT32_C(0xAAAA)
#define IWDG_PRESCALER_DIV32 UINT32_C(3)
#define IWDG_RELOAD_TICKS UINT32_C(999)

static uint32_t control_bsrr_word(uint8_t code)
{
    const uint32_t set_bits = (uint32_t)code & CONTROL_PIN_MASK;
    const uint32_t reset_bits = (~(uint32_t)code) & CONTROL_PIN_MASK;

    return set_bits | (reset_bits << 16u);
}

static void gpio_apply(uint8_t code)
{
    GPIOA->BSRR = control_bsrr_word(code);
}

static void gpio_preload_all_off(void)
{
    volatile uint32_t clock_readback;

    RCC->IOPENR |= RCC_IOPENR_GPIOAEN;
    clock_readback = RCC->IOPENR;
    (void)clock_readback;
    gpio_apply(CONTROL_ALL_OFF_CODE);
}

static void gpio_enable_control_outputs(void)
{
    GPIOA->OTYPER &= ~CONTROL_PIN_MASK;
    GPIOA->MODER = (GPIOA->MODER & ~CONTROL_MODE_MASK) | CONTROL_OUTPUT_MODES;
}

static void watchdog_initialize(void)
{
    IWDG->KR = IWDG_KEY_ENABLE;
    IWDG->KR = IWDG_KEY_WRITE_ACCESS;
    IWDG->PR = IWDG_PRESCALER_DIV32;
    IWDG->RLR = IWDG_RELOAD_TICKS;
    while (IWDG->SR != 0u) {
    }
    IWDG->KR = IWDG_KEY_REFRESH;
}

int main(void)
{
    autonomous_frame_t frame;

    gpio_preload_all_off();
    if (SystemCoreClock != RESET_CORE_CLOCK_HZ) {
        gpio_enable_control_outputs();
        for (;;) {
            gpio_apply(CONTROL_ALL_OFF_CODE);
        }
    }

    SysTick->LOAD = SYSTICK_RELOAD;
    SysTick->VAL = 0u;
    SysTick->CTRL = SysTick_CTRL_CLKSOURCE_Msk | SysTick_CTRL_ENABLE_Msk;
    watchdog_initialize();
    gpio_enable_control_outputs();
    autonomous_frame_init(&frame);

    for (;;) {
        if ((SysTick->CTRL & SysTick_CTRL_COUNTFLAG_Msk) != 0u) {
            const uint8_t previous_code = frame.applied_code;

            autonomous_frame_tick_ms(&frame, 1u);
            if (frame.applied_code != previous_code) {
                gpio_apply(frame.applied_code);
            }
        }
        IWDG->KR = IWDG_KEY_REFRESH;
    }
}
