SHELL := /bin/sh

CC ?= cc
PYTHON ?= python3
UV ?= uv
BUILD_DIR ?= build
HOST_TEST := $(BUILD_DIR)/host/control_core_test
CORE_SOURCES := firmware/stm32c011/core/control_core.c
CORE_HEADERS := firmware/stm32c011/core/control_core.h profiles/fast20-v1/control_profile.h

MCU ?= STM32C011F4P6
CROSS ?= arm-none-eabi-
TARGET_CC := $(CROSS)gcc
TARGET_OBJCOPY := $(CROSS)objcopy
TARGET_OBJDUMP := $(CROSS)objdump
TARGET_SIZE := $(CROSS)size
TARGET_DIR := $(BUILD_DIR)/$(MCU)/safe_hold
TARGET_ELF := $(TARGET_DIR)/pluto_safe_hold.elf
TARGET_BIN := $(TARGET_DIR)/pluto_safe_hold.bin
TARGET_MAP := $(TARGET_DIR)/pluto_safe_hold.map
TARGET_LST := $(TARGET_DIR)/pluto_safe_hold.lst
BENCH_DIR := $(BUILD_DIR)/$(MCU)/bench
BENCH_ELF := $(BENCH_DIR)/pluto_bench.elf
BENCH_BIN := $(BENCH_DIR)/pluto_bench.bin
BENCH_MAP := $(BENCH_DIR)/pluto_bench.map
BENCH_LST := $(BENCH_DIR)/pluto_bench.lst
BENCH_MANIFEST := $(BENCH_DIR)/pluto_bench.manifest.json
BENCH_PROTOCOL := firmware/stm32c011/apps/bench/bench_protocol.h
DEVICE_ROOT := firmware/stm32c011/vendor/cmsis-device-c0
CMSIS_ROOT := firmware/stm32c011/vendor/CMSIS_5/CMSIS/Core
TARGET_SOURCES := \
	firmware/stm32c011/apps/safe_hold/main.c \
	firmware/stm32c011/apps/safe_hold/safe_runtime.c \
	$(DEVICE_ROOT)/Source/Templates/system_stm32c0xx.c \
	$(DEVICE_ROOT)/Source/Templates/gcc/startup_stm32c011xx.s
TARGET_OBJECTS := \
	$(TARGET_DIR)/main.o \
	$(TARGET_DIR)/safe_runtime.o \
	$(TARGET_DIR)/system_stm32c0xx.o \
	$(TARGET_DIR)/startup_stm32c011xx.o
TARGET_CPPFLAGS := \
	-DSTM32C011xx \
	-I$(DEVICE_ROOT)/Include \
	-I$(CMSIS_ROOT)/Include \
	-Iprofiles/fast20-v1
TARGET_CFLAGS := \
	-mcpu=cortex-m0plus -mthumb -std=c11 -Os -g3 \
	-ffreestanding -ffunction-sections -fdata-sections -fno-common \
	-Wall -Wextra -Werror -Wconversion -Wshadow -pedantic
TARGET_LDFLAGS_COMMON := \
	-mcpu=cortex-m0plus -mthumb \
	-Tfirmware/stm32c011/linker/stm32c011f4p6.ld \
	-Wl,--gc-sections -Wl,--build-id=none \
	-Wl,--print-memory-usage -nostdlib
TARGET_LDFLAGS := $(TARGET_LDFLAGS_COMMON) -Wl,-Map,$(TARGET_MAP)

.PHONY: all test test-c test-python profile-check safe-hold bench clean

all: test

test: profile-check test-c test-python

profile-check:
	$(PYTHON) scripts/sync_control_profile.py \
		--circuits-root /home/pi/gits/circuits --check

$(HOST_TEST): tests/firmware_core/control_core_test.c $(CORE_SOURCES) $(CORE_HEADERS)
	mkdir -p $(dir $@)
	$(CC) -std=c11 -O2 -Wall -Wextra -Werror -Wconversion -Wshadow \
		-pedantic -Ifirmware/stm32c011/core -Iprofiles/fast20-v1 \
		$(CORE_SOURCES) tests/firmware_core/control_core_test.c -o $@

test-c: $(HOST_TEST)
	$(HOST_TEST)

test-python:
	$(UV) run pytest

ifeq ($(MCU),STM32C011F4P6)
else
$(error Unsupported MCU '$(MCU)'; reviewed target is STM32C011F4P6)
endif

$(TARGET_DIR)/main.o: firmware/stm32c011/apps/safe_hold/main.c profiles/fast20-v1/control_profile.h
	mkdir -p $(dir $@)
	$(TARGET_CC) $(TARGET_CPPFLAGS) $(TARGET_CFLAGS) -c $< -o $@

$(TARGET_DIR)/safe_runtime.o: firmware/stm32c011/apps/safe_hold/safe_runtime.c
	mkdir -p $(dir $@)
	$(TARGET_CC) $(TARGET_CPPFLAGS) $(TARGET_CFLAGS) -c $< -o $@

$(TARGET_DIR)/system_stm32c0xx.o: $(DEVICE_ROOT)/Source/Templates/system_stm32c0xx.c
	mkdir -p $(dir $@)
	$(TARGET_CC) $(TARGET_CPPFLAGS) $(TARGET_CFLAGS) -c $< -o $@

$(TARGET_DIR)/startup_stm32c011xx.o: $(DEVICE_ROOT)/Source/Templates/gcc/startup_stm32c011xx.s
	mkdir -p $(dir $@)
	$(TARGET_CC) $(TARGET_CPPFLAGS) -mcpu=cortex-m0plus -mthumb -g3 -c $< -o $@

$(TARGET_ELF): $(TARGET_OBJECTS) firmware/stm32c011/linker/stm32c011f4p6.ld
	$(TARGET_CC) $(TARGET_OBJECTS) $(TARGET_LDFLAGS) -o $@
	$(TARGET_SIZE) $@ | tee $(TARGET_DIR)/pluto_safe_hold.size.txt
	sha256sum $@ > $@.sha256

$(TARGET_BIN): $(TARGET_ELF)
	$(TARGET_OBJCOPY) -O binary $< $@
	sha256sum $@ > $@.sha256

$(TARGET_LST): $(TARGET_ELF)
	$(TARGET_OBJDUMP) -d -S -h $< > $@

safe-hold: profile-check $(TARGET_BIN) $(TARGET_LST)
	$(PYTHON) scripts/verify_safe_hold_elf.py $(TARGET_ELF)

$(BENCH_DIR)/main.o: firmware/stm32c011/apps/bench/main.c $(BENCH_PROTOCOL) $(CORE_HEADERS)
	mkdir -p $(dir $@)
	$(TARGET_CC) $(TARGET_CPPFLAGS) -Ifirmware/stm32c011/core \
		-Ifirmware/stm32c011/apps/bench $(TARGET_CFLAGS) -c $< -o $@

$(BENCH_DIR)/control_core.o: firmware/stm32c011/core/control_core.c $(CORE_HEADERS)
	mkdir -p $(dir $@)
	$(TARGET_CC) $(TARGET_CPPFLAGS) -Ifirmware/stm32c011/core \
		$(TARGET_CFLAGS) -c $< -o $@

$(BENCH_DIR)/safe_runtime.o: firmware/stm32c011/apps/safe_hold/safe_runtime.c
	mkdir -p $(dir $@)
	$(TARGET_CC) $(TARGET_CPPFLAGS) $(TARGET_CFLAGS) -c $< -o $@

$(BENCH_DIR)/system_stm32c0xx.o: $(DEVICE_ROOT)/Source/Templates/system_stm32c0xx.c
	mkdir -p $(dir $@)
	$(TARGET_CC) $(TARGET_CPPFLAGS) $(TARGET_CFLAGS) -c $< -o $@

$(BENCH_DIR)/startup_stm32c011xx.o: $(DEVICE_ROOT)/Source/Templates/gcc/startup_stm32c011xx.s
	mkdir -p $(dir $@)
	$(TARGET_CC) $(TARGET_CPPFLAGS) -mcpu=cortex-m0plus -mthumb -g3 -c $< -o $@

$(BENCH_ELF): \
		$(BENCH_DIR)/main.o \
		$(BENCH_DIR)/control_core.o \
		$(BENCH_DIR)/safe_runtime.o \
		$(BENCH_DIR)/system_stm32c0xx.o \
		$(BENCH_DIR)/startup_stm32c011xx.o \
		firmware/stm32c011/linker/stm32c011f4p6.ld
	$(TARGET_CC) $(filter %.o,$^) $(TARGET_LDFLAGS_COMMON) \
		-Wl,-Map,$(BENCH_MAP) -o $@
	$(TARGET_SIZE) $@ | tee $(BENCH_DIR)/pluto_bench.size.txt
	sha256sum $@ > $@.sha256

$(BENCH_BIN): $(BENCH_ELF)
	$(TARGET_OBJCOPY) -O binary $< $@
	sha256sum $@ > $@.sha256

$(BENCH_LST): $(BENCH_ELF)
	$(TARGET_OBJDUMP) -d -S -h $< > $@

$(BENCH_MANIFEST): $(BENCH_ELF) $(BENCH_PROTOCOL)
	$(PYTHON) scripts/generate_bench_manifest.py \
		--elf $(BENCH_ELF) --protocol $(BENCH_PROTOCOL) --output $@

bench: profile-check test-c $(BENCH_BIN) $(BENCH_LST) $(BENCH_MANIFEST)
	$(PYTHON) scripts/verify_bench_elf.py $(BENCH_ELF)

clean:
	test "$(BUILD_DIR)" = "build"
	rm -rf build
