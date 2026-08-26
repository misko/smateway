#include <stdbool.h>
#include <stdint.h>

#include "high_rate_autonomous_core.h"
#include "stm32c0xx.h"

#define CONTROL_PIN_MASK UINT32_C(0x0F)
#define CONTROL_MODE_MASK UINT32_C(0xFF)
#define CONTROL_OUTPUT_MODES UINT32_C(0x55)
#define RESET_CORE_CLOCK_HZ UINT32_C(12000000)
#define RESET_HSI_DIVIDER_BITS RCC_CR_HSIDIV_1
#define TIM3_PRESCALER ((RESET_CORE_CLOCK_HZ / CONTROL_TIMER_HZ) - 1u)

#define IWDG_KEY_ENABLE UINT32_C(0xCCCC)
#define IWDG_KEY_WRITE_ACCESS UINT32_C(0x5555)
#define IWDG_KEY_REFRESH UINT32_C(0xAAAA)
#define IWDG_PRESCALER_DIV32 UINT32_C(3)
#define IWDG_RELOAD_TICKS UINT32_C(999)

_Static_assert(CONTROL_ALL_OFF_CODE == 0x8u, "hardware ALL_OFF contract changed");
_Static_assert(
    CONTROL_EXPERIMENTAL_GUARD_WAIVER == 1u,
    "hexcal guard must remain explicitly experimental"
);
_Static_assert(CONTROL_RELEASED_GUARD_US == 5000u, "released guard contract changed");
_Static_assert(CONTROL_STATE_COUNT == 6u, "hexcal must select exactly six states");
_Static_assert(CONTROL_TIMER_HZ == 1000000u, "hexcal timer contract changed");
_Static_assert(
    RESET_CORE_CLOCK_HZ % CONTROL_TIMER_HZ == 0u,
    "timer frequency must divide the reset clock"
);
_Static_assert(CONTROL_MAX_LATENESS_US < CONTROL_GUARD_US, "lateness exceeds guard");
_Static_assert(CONTROL_NOMINAL_CYCLE_US < 0x8000u, "cycle exceeds timer half-range");

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

static bool clock_register_configuration_valid(void)
{
    const uint32_t required_hsi_bits = RCC_CR_HSION | RCC_CR_HSIRDY;
    const uint32_t clock_control = RCC->CR;
    const uint32_t clock_configuration = RCC->CFGR;

    return (clock_control & required_hsi_bits) == required_hsi_bits
        && (clock_control & RCC_CR_HSIDIV) == RESET_HSI_DIVIDER_BITS
        && (clock_configuration & RCC_CFGR_SW) == 0u
        && (clock_configuration & RCC_CFGR_SWS) == RCC_CFGR_SWS_HSI
        && (clock_configuration & RCC_CFGR_HPRE) == 0u
        && (clock_configuration & RCC_CFGR_PPRE) == 0u;
}

static bool reset_clock_configuration_valid(void)
{
    SystemCoreClockUpdate();
    return clock_register_configuration_valid()
        && SystemCoreClock == RESET_CORE_CLOCK_HZ;
}

static bool timer_initialize(void)
{
    volatile uint32_t clock_readback;

    RCC->APBENR1 |= RCC_APBENR1_TIM3EN;
    clock_readback = RCC->APBENR1;
    (void)clock_readback;
    RCC->APBRSTR1 |= RCC_APBRSTR1_TIM3RST;
    RCC->APBRSTR1 &= ~RCC_APBRSTR1_TIM3RST;

    TIM3->CR1 = 0u;
    TIM3->DIER = 0u;
    TIM3->PSC = TIM3_PRESCALER;
    TIM3->ARR = UINT16_MAX;
    TIM3->EGR = TIM_EGR_UG;
    TIM3->SR = 0u;
    TIM3->CNT = 0u;
    TIM3->CR1 = TIM_CR1_CEN;

    return TIM3->PSC == TIM3_PRESCALER
        && TIM3->ARR == UINT16_MAX
        && TIM3->DIER == 0u
        && (TIM3->CR1 & TIM_CR1_CEN) != 0u;
}

static uint16_t timer_now(void)
{
    return (uint16_t)(TIM3->CNT & UINT16_MAX);
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
    __disable_irq();

    high_rate_frame_t frame;
    uint16_t deadline;

    gpio_preload_all_off();
    if (!reset_clock_configuration_valid() || !timer_initialize()) {
        for (;;) {
            __NOP();
        }
    }

    watchdog_initialize();
    gpio_enable_control_outputs();
    if ((GPIOA->ODR & CONTROL_PIN_MASK) != CONTROL_ALL_OFF_CODE) {
        for (;;) {
            gpio_apply(CONTROL_ALL_OFF_CODE);
        }
    }

    high_rate_frame_init(&frame);
    deadline = (uint16_t)(timer_now() + frame.phase_duration_us);

    for (;;) {
        const uint16_t now = timer_now();
        const high_rate_deadline_action_t action = high_rate_deadline_action(now, deadline);

        if (!clock_register_configuration_valid()) {
            gpio_apply(CONTROL_ALL_OFF_CODE);
            for (;;) {
                __NOP();
            }
        }
        if (action == HIGH_RATE_DEADLINE_WAIT) {
            continue;
        }
        if (action == HIGH_RATE_DEADLINE_RESYNCHRONIZE) {
            gpio_apply(CONTROL_ALL_OFF_CODE);
            high_rate_frame_init(&frame);
            deadline = (uint16_t)(timer_now() + frame.phase_duration_us);
            continue;
        }

        {
            const uint8_t previous_code = frame.applied_code;
            const bool cycle_completed = high_rate_frame_advance(&frame);

            if (frame.applied_code != previous_code) {
                gpio_apply(frame.applied_code);
            }
            deadline = high_rate_next_deadline(deadline, frame.phase_duration_us);
            if (cycle_completed) {
                IWDG->KR = IWDG_KEY_REFRESH;
            }
        }
    }
}
