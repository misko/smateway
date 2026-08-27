/*
 * Keep the upstream STM32C011 vector table and reset implementation byte-for-
 * byte, but interpose the ES0569 section 2.2.5 SRAM workaround before
 * SystemInit.  Reset_Handler has only set SP and executed BL at that point;
 * neither instruction accesses SRAM.
 */
#define SystemInit HexcalStartupSystemInit
#include "../../vendor/cmsis-device-c0/Source/Templates/gcc/startup_stm32c011xx.s"
#undef SystemInit

  .syntax unified
  .cpu cortex-m0plus
  .fpu softvfp
  .thumb

  .section .text.HexcalStartupSystemInit,"ax",%progbits
  .global HexcalStartupSystemInit
  .type HexcalStartupSystemInit, %function
  .thumb_func
HexcalStartupSystemInit:
  ldr   r0, =_sdata
  ldr   r1, [r0]
  b     SystemInit
  .size HexcalStartupSystemInit, .-HexcalStartupSystemInit
