SHELL := /bin/sh

CC ?= cc
PYTHON ?= python3
UV ?= uv
BUILD_DIR ?= build
HOST_TEST := $(BUILD_DIR)/host/control_core_test
PHASE_HOST_TEST := $(BUILD_DIR)/host/phase20_core_test
HEXCAL_HOST_TEST := $(BUILD_DIR)/host/hexcal_core_test
CORE_SOURCES := \
	firmware/stm32c011/core/control_core.c \
	firmware/stm32c011/core/autonomous_core.c
CORE_HEADERS := \
	firmware/stm32c011/core/control_core.h \
	firmware/stm32c011/core/autonomous_core.h \
	profiles/fast20-v1/control_profile.h

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
FAST_DIR := $(BUILD_DIR)/$(MCU)/fast20
FAST_ELF := $(FAST_DIR)/pluto_fast20.elf
FAST_BIN := $(FAST_DIR)/pluto_fast20.bin
FAST_MAP := $(FAST_DIR)/pluto_fast20.map
FAST_LST := $(FAST_DIR)/pluto_fast20.lst
PHASE_DIR := $(BUILD_DIR)/$(MCU)/phase20
PHASE_ELF := $(PHASE_DIR)/pluto_phase20.elf
PHASE_BIN := $(PHASE_DIR)/pluto_phase20.bin
PHASE_MAP := $(PHASE_DIR)/pluto_phase20.map
PHASE_LST := $(PHASE_DIR)/pluto_phase20.lst
HEXCAL_DIR := $(BUILD_DIR)/$(MCU)/hexcal
HEXCAL_ELF := $(HEXCAL_DIR)/pluto_hexcal.elf
HEXCAL_BIN := $(HEXCAL_DIR)/pluto_hexcal.bin
HEXCAL_MAP := $(HEXCAL_DIR)/pluto_hexcal.map
HEXCAL_LST := $(HEXCAL_DIR)/pluto_hexcal.lst
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
PHASE_CPPFLAGS := \
	-DSTM32C011xx \
	-I$(DEVICE_ROOT)/Include \
	-I$(CMSIS_ROOT)/Include \
	-Iprofiles/phase20-v1
HEXCAL_CPPFLAGS := \
	-DSTM32C011xx \
	-I$(DEVICE_ROOT)/Include \
	-I$(CMSIS_ROOT)/Include \
	-Iprofiles/hexcal-v1
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

.PHONY: all test test-c test-phase20-core test-hexcal-core test-python \
	profile-check phase-profile-check hexcal-profile-check safe-hold bench \
	fast20 phase20 hexcal clean

all: test

test: profile-check phase-profile-check hexcal-profile-check test-c \
	test-phase20-core test-hexcal-core test-python

profile-check:
	$(PYTHON) scripts/sync_control_profile.py \
		--circuits-root /home/pi/gits/circuits --check

phase-profile-check:
	$(PYTHON) scripts/generate_phase20_profile.py --check

hexcal-profile-check:
	$(PYTHON) scripts/generate_hexcal_profile.py --check

$(HOST_TEST): tests/firmware_core/control_core_test.c $(CORE_SOURCES) $(CORE_HEADERS)
	mkdir -p $(dir $@)
	$(CC) -std=c11 -O2 -Wall -Wextra -Werror -Wconversion -Wshadow \
		-pedantic -Ifirmware/stm32c011/core -Iprofiles/fast20-v1 \
		$(CORE_SOURCES) tests/firmware_core/control_core_test.c -o $@

test-c: $(HOST_TEST)
	$(HOST_TEST)

$(PHASE_HOST_TEST): tests/firmware_core/control_core_test.c $(CORE_SOURCES) \
		firmware/stm32c011/core/control_core.h \
		firmware/stm32c011/core/autonomous_core.h \
		profiles/phase20-v1/control_profile.h
	mkdir -p $(dir $@)
	$(CC) -std=c11 -O2 -Wall -Wextra -Werror -Wconversion -Wshadow \
		-pedantic -Ifirmware/stm32c011/core -Iprofiles/phase20-v1 \
		$(CORE_SOURCES) tests/firmware_core/control_core_test.c -o $@

test-phase20-core: $(PHASE_HOST_TEST)
	$(PHASE_HOST_TEST)

$(HEXCAL_HOST_TEST): tests/firmware_core/hexcal_core_test.c \
		firmware/stm32c011/core/high_rate_autonomous_core.c \
		firmware/stm32c011/core/high_rate_autonomous_core.h \
		profiles/hexcal-v1/control_profile.h
	mkdir -p $(dir $@)
	$(CC) -std=c11 -O2 -Wall -Wextra -Werror -Wconversion -Wshadow \
		-pedantic -Ifirmware/stm32c011/core -Iprofiles/hexcal-v1 \
		firmware/stm32c011/core/high_rate_autonomous_core.c \
		tests/firmware_core/hexcal_core_test.c -o $@

test-hexcal-core: $(HEXCAL_HOST_TEST)
	$(HEXCAL_HOST_TEST)

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

$(FAST_DIR)/main.o: firmware/stm32c011/apps/fast20/main.c $(CORE_HEADERS)
	mkdir -p $(dir $@)
	$(TARGET_CC) $(TARGET_CPPFLAGS) -Ifirmware/stm32c011/core \
		$(TARGET_CFLAGS) -c $< -o $@

$(FAST_DIR)/autonomous_core.o: firmware/stm32c011/core/autonomous_core.c $(CORE_HEADERS)
	mkdir -p $(dir $@)
	$(TARGET_CC) $(TARGET_CPPFLAGS) -Ifirmware/stm32c011/core \
		$(TARGET_CFLAGS) -c $< -o $@

$(FAST_DIR)/safe_runtime.o: firmware/stm32c011/apps/safe_hold/safe_runtime.c
	mkdir -p $(dir $@)
	$(TARGET_CC) $(TARGET_CPPFLAGS) $(TARGET_CFLAGS) -c $< -o $@

$(FAST_DIR)/system_stm32c0xx.o: $(DEVICE_ROOT)/Source/Templates/system_stm32c0xx.c
	mkdir -p $(dir $@)
	$(TARGET_CC) $(TARGET_CPPFLAGS) $(TARGET_CFLAGS) -c $< -o $@

$(FAST_DIR)/startup_stm32c011xx.o: $(DEVICE_ROOT)/Source/Templates/gcc/startup_stm32c011xx.s
	mkdir -p $(dir $@)
	$(TARGET_CC) $(TARGET_CPPFLAGS) -mcpu=cortex-m0plus -mthumb -g3 -c $< -o $@

$(FAST_ELF): \
		$(FAST_DIR)/main.o \
		$(FAST_DIR)/autonomous_core.o \
		$(FAST_DIR)/safe_runtime.o \
		$(FAST_DIR)/system_stm32c0xx.o \
		$(FAST_DIR)/startup_stm32c011xx.o \
		firmware/stm32c011/linker/stm32c011f4p6.ld
	$(TARGET_CC) $(filter %.o,$^) $(TARGET_LDFLAGS_COMMON) \
		-Wl,-Map,$(FAST_MAP) -o $@
	$(TARGET_SIZE) $@ | tee $(FAST_DIR)/pluto_fast20.size.txt
	sha256sum $@ > $@.sha256

$(FAST_BIN): $(FAST_ELF)
	$(TARGET_OBJCOPY) -O binary $< $@
	sha256sum $@ > $@.sha256

$(FAST_LST): $(FAST_ELF)
	$(TARGET_OBJDUMP) -d -S -h $< > $@

fast20: profile-check test-c $(FAST_BIN) $(FAST_LST)
	$(PYTHON) scripts/verify_fast20_elf.py $(FAST_ELF)

$(PHASE_DIR)/main.o: firmware/stm32c011/apps/fast20/main.c \
		profiles/phase20-v1/control_profile.h
	mkdir -p $(dir $@)
	$(TARGET_CC) $(PHASE_CPPFLAGS) -Ifirmware/stm32c011/core \
		$(TARGET_CFLAGS) -c $< -o $@

$(PHASE_DIR)/autonomous_core.o: firmware/stm32c011/core/autonomous_core.c \
		profiles/phase20-v1/control_profile.h
	mkdir -p $(dir $@)
	$(TARGET_CC) $(PHASE_CPPFLAGS) -Ifirmware/stm32c011/core \
		$(TARGET_CFLAGS) -c $< -o $@

$(PHASE_DIR)/safe_runtime.o: firmware/stm32c011/apps/safe_hold/safe_runtime.c
	mkdir -p $(dir $@)
	$(TARGET_CC) $(PHASE_CPPFLAGS) $(TARGET_CFLAGS) -c $< -o $@

$(PHASE_DIR)/system_stm32c0xx.o: $(DEVICE_ROOT)/Source/Templates/system_stm32c0xx.c
	mkdir -p $(dir $@)
	$(TARGET_CC) $(PHASE_CPPFLAGS) $(TARGET_CFLAGS) -c $< -o $@

$(PHASE_DIR)/startup_stm32c011xx.o: \
		$(DEVICE_ROOT)/Source/Templates/gcc/startup_stm32c011xx.s
	mkdir -p $(dir $@)
	$(TARGET_CC) $(PHASE_CPPFLAGS) -mcpu=cortex-m0plus -mthumb -g3 -c $< -o $@

$(PHASE_ELF): \
		$(PHASE_DIR)/main.o \
		$(PHASE_DIR)/autonomous_core.o \
		$(PHASE_DIR)/safe_runtime.o \
		$(PHASE_DIR)/system_stm32c0xx.o \
		$(PHASE_DIR)/startup_stm32c011xx.o \
		firmware/stm32c011/linker/stm32c011f4p6.ld
	$(TARGET_CC) $(filter %.o,$^) $(TARGET_LDFLAGS_COMMON) \
		-Wl,-Map,$(PHASE_MAP) -o $@
	$(TARGET_SIZE) $@ | tee $(PHASE_DIR)/pluto_phase20.size.txt
	sha256sum $@ > $@.sha256

$(PHASE_BIN): $(PHASE_ELF)
	$(TARGET_OBJCOPY) -O binary $< $@
	sha256sum $@ > $@.sha256

$(PHASE_LST): $(PHASE_ELF)
	$(TARGET_OBJDUMP) -d -S -h $< > $@

phase20: profile-check phase-profile-check test-phase20-core $(PHASE_BIN) $(PHASE_LST)
	$(PYTHON) scripts/verify_fast20_elf.py $(PHASE_ELF)

$(HEXCAL_DIR)/main.o: firmware/stm32c011/apps/hexcal/main.c \
		firmware/stm32c011/core/high_rate_autonomous_core.h \
		profiles/hexcal-v1/control_profile.h
	mkdir -p $(dir $@)
	$(TARGET_CC) $(HEXCAL_CPPFLAGS) -Ifirmware/stm32c011/core \
		$(TARGET_CFLAGS) -c $< -o $@

$(HEXCAL_DIR)/high_rate_autonomous_core.o: \
		firmware/stm32c011/core/high_rate_autonomous_core.c \
		firmware/stm32c011/core/high_rate_autonomous_core.h \
		profiles/hexcal-v1/control_profile.h
	mkdir -p $(dir $@)
	$(TARGET_CC) $(HEXCAL_CPPFLAGS) -Ifirmware/stm32c011/core \
		$(TARGET_CFLAGS) -c $< -o $@

$(HEXCAL_DIR)/safe_runtime.o: firmware/stm32c011/apps/safe_hold/safe_runtime.c
	mkdir -p $(dir $@)
	$(TARGET_CC) $(HEXCAL_CPPFLAGS) $(TARGET_CFLAGS) -c $< -o $@

$(HEXCAL_DIR)/system_stm32c0xx.o: \
		$(DEVICE_ROOT)/Source/Templates/system_stm32c0xx.c
	mkdir -p $(dir $@)
	$(TARGET_CC) $(HEXCAL_CPPFLAGS) $(TARGET_CFLAGS) -c $< -o $@

$(HEXCAL_DIR)/startup_stm32c011xx.o: \
		firmware/stm32c011/apps/hexcal/startup_stm32c011xx.S \
		$(DEVICE_ROOT)/Source/Templates/gcc/startup_stm32c011xx.s
	mkdir -p $(dir $@)
	$(TARGET_CC) $(HEXCAL_CPPFLAGS) -mcpu=cortex-m0plus -mthumb -g3 -c $< -o $@

$(HEXCAL_ELF): \
		$(HEXCAL_DIR)/main.o \
		$(HEXCAL_DIR)/high_rate_autonomous_core.o \
		$(HEXCAL_DIR)/safe_runtime.o \
		$(HEXCAL_DIR)/system_stm32c0xx.o \
		$(HEXCAL_DIR)/startup_stm32c011xx.o \
		firmware/stm32c011/linker/stm32c011f4p6.ld
	$(TARGET_CC) $(filter %.o,$^) $(TARGET_LDFLAGS_COMMON) \
		-Wl,-Map,$(HEXCAL_MAP) -o $@
	$(TARGET_SIZE) $@ | tee $(HEXCAL_DIR)/pluto_hexcal.size.txt
	sha256sum $@ > $@.sha256

$(HEXCAL_BIN): $(HEXCAL_ELF)
	$(TARGET_OBJCOPY) -O binary $< $@
	sha256sum $@ > $@.sha256

$(HEXCAL_LST): $(HEXCAL_ELF)
	$(TARGET_OBJDUMP) -d -S -h $< > $@

hexcal: profile-check hexcal-profile-check test-hexcal-core \
		$(HEXCAL_BIN) $(HEXCAL_LST)
	$(PYTHON) scripts/verify_hexcal_elf.py $(HEXCAL_ELF)

clean:
	test "$(BUILD_DIR)" = "build"
	rm -rf build
