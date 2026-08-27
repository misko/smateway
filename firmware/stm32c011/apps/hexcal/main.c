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
#define IWDG_PRESCALER_DIV4_REGISTER_VALUE UINT32_C(0)

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
_Static_assert(
    RESET_CORE_CLOCK_HZ / CONTROL_TIMER_HZ
        == HIGH_RATE_CORE_CYCLES_PER_TIMER_TICK,
    "GPIO cycle proof does not match the configured timer"
);
_Static_assert(CONTROL_MAX_LATENESS_US < CONTROL_GUARD_US, "lateness exceeds guard");
_Static_assert(
    CONTROL_TIGHT_POLL_WINDOW_US < CONTROL_GUARD_US,
    "tight-poll window exceeds guard"
);
_Static_assert(
    CONTROL_TIGHT_POLL_WINDOW_US > CONTROL_PREWRITE_MAX_LATENESS_US,
    "tight-poll window must cover the accepted pre-write lateness"
);
_Static_assert(
    IWDG_PRESCALER_DIVIDER == 4u,
    "IWDG register value is valid only for the /4 prescaler"
);
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

static bool gpio_preload_all_off(void)
{
    volatile uint32_t clock_readback;

    RCC->IOPENR |= RCC_IOPENR_GPIOAEN;
    clock_readback = RCC->IOPENR;
    if ((clock_readback & RCC_IOPENR_GPIOAEN) == 0u) {
        return false;
    }
    gpio_apply(CONTROL_ALL_OFF_CODE);
    __COMPILER_BARRIER();

    return (GPIOA->ODR & CONTROL_PIN_MASK) == CONTROL_ALL_OFF_CODE;
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

__attribute__((always_inline)) static inline bool
clock_hsi_signature_valid(void)
{
    /*
     * A full CR/CFGR validation is performed before entering the final 8 us
     * staging window.  This compact late check detects loss of HSI48 or a
     * divider change without consuming the deadline admission window.  The
     * ELF verifier bounds its exact accepted path to the final timer sample.
     */
    const uint32_t required_hsi_bits = RCC_CR_HSION | RCC_CR_HSIRDY;
    const uint32_t hsi_mismatch =
        (RCC->CR ^ (required_hsi_bits | RESET_HSI_DIVIDER_BITS))
        & (required_hsi_bits | RCC_CR_HSIDIV);

    return hsi_mismatch == 0u;
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
    if ((clock_readback & RCC_APBENR1_TIM3EN) == 0u) {
        return false;
    }
    RCC->APBRSTR1 |= RCC_APBRSTR1_TIM3RST;
    clock_readback = RCC->APBRSTR1;
    if ((clock_readback & RCC_APBRSTR1_TIM3RST) == 0u) {
        return false;
    }
    RCC->APBRSTR1 &= ~RCC_APBRSTR1_TIM3RST;
    clock_readback = RCC->APBRSTR1;
    if ((clock_readback & RCC_APBRSTR1_TIM3RST) != 0u) {
        return false;
    }

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

__attribute__((noinline)) static void watchdog_initialize(void)
{
    IWDG->KR = IWDG_KEY_ENABLE;
    IWDG->KR = IWDG_KEY_WRITE_ACCESS;
    IWDG->PR = IWDG_PRESCALER_DIV4_REGISTER_VALUE;
    IWDG->RLR = IWDG_RELOAD_VALUE;
    while (IWDG->SR != 0u) {
    }
    IWDG->KR = IWDG_KEY_REFRESH;
}

int main(void)
{
    __disable_irq();

    high_rate_frame_t frame;
    uint16_t deadline;

    if (!gpio_preload_all_off()
        || !reset_clock_configuration_valid()
        || !timer_initialize()) {
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
        high_rate_frame_t planned_frame;

        planned_frame.phase = frame.phase;
        planned_frame.state_index = frame.state_index;
        planned_frame.applied_code = frame.applied_code;
        planned_frame.phase_duration_us = frame.phase_duration_us;
        const bool cycle_completed = high_rate_frame_advance(&planned_frame);
        const volatile uint32_t planned_bsrr_word =
            control_bsrr_word(planned_frame.applied_code);
        uint16_t now;

        /*
         * Establish a full CR/CFGR validation before the first poll.  The far
         * path repeats it while waiting; the staging path then enters the
         * tight loop directly so two consecutive full checks cannot consume
         * the complete 8 us approach window.
         */
        if (!clock_register_configuration_valid()) {
            gpio_apply(CONTROL_ALL_OFF_CODE);
            for (;;) {
                __NOP();
            }
        }

        /*
         * Keep the tight deadline poll itself free of function calls.  A
         * compact inline HSI signature check follows the due sample.  The
         * former polling loop took several timer ticks per iteration and made
         * the 20 us guards alternate between early and late RF edges.  The
         * frame and atomic BSRR word are prepared before this wait so every
         * accepted deadline has the same mechanically bounded GPIO path.
         */
        for (;;) {
            now = timer_now();
            if (!high_rate_deadline_pending(now, deadline)) {
                /*
                 * Only the staged tight poll is allowed to admit an edge.
                 * Reaching the deadline in this outer path means the bounded
                 * admission proof was not established for this transition.
                 */
                goto resynchronize;
            }
            if (high_rate_deadline_within_staging_window(
                now,
                deadline,
                CONTROL_TIGHT_POLL_WINDOW_US
            )) {
                do {
                    now = timer_now();
                } while (high_rate_deadline_pending(now, deadline));
                break;
            }
            if (!clock_register_configuration_valid()) {
                gpio_apply(CONTROL_ALL_OFF_CODE);
                for (;;) {
                    __NOP();
                }
            }
        }

        if (!clock_hsi_signature_valid()) {
            gpio_apply(CONTROL_ALL_OFF_CODE);
            for (;;) {
                __NOP();
            }
        }

        /*
         * Re-read after the compact HSI check.  Admission reserves a separately
         * verified write-path budget, so the physical BSRR edge remains
         * within CONTROL_MAX_LATENESS_US.  The independent watchdog bounds a
         * normal runtime stall below the timer half-range; debugger-controlled
         * watchdog freezing is outside that runtime guarantee.
         */
        now = timer_now();
        if (!high_rate_deadline_advance_allowed(now, deadline)) {
            goto resynchronize;
        }
        GPIOA->BSRR = planned_bsrr_word;
        __COMPILER_BARRIER();

        frame.phase = planned_frame.phase;
        frame.state_index = planned_frame.state_index;
        frame.applied_code = planned_frame.applied_code;
        frame.phase_duration_us = planned_frame.phase_duration_us;
        deadline = high_rate_next_deadline(deadline, frame.phase_duration_us);
        if (cycle_completed) {
            IWDG->KR = IWDG_KEY_REFRESH;
        }
        continue;

resynchronize:
        gpio_apply(CONTROL_ALL_OFF_CODE);
        high_rate_frame_init(&frame);
        deadline = (uint16_t)(timer_now() + frame.phase_duration_us);
    }
}
