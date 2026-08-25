# Smateway

Firmware, host control and qualification tooling for the Pluto RX2 eight-way
v5 RF selector.

The hardware and timing authorities remain in the `circuits` repository:

- `projects/pluto-rx2-8way-v5/01_docs/ARCHITECTURE.md` defines the electrical
  interface and truth table;
- `projects/pluto-rx2-8way-v5/03_src/rules/control_protocol.yaml` defines the
  autonomous timing profile; and
- `projects/pluto-rx2-8way-v5/05_firmware/README.md` defines the gated
  first-article procedure.

This repository contains every hand-written STM32 firmware source and every
board-specific host tool. It imports generated control-profile consumers with
provenance instead of hand-copying codes or dwell values.

## Safety boundary

- Power the target independently through exactly one of J1 or J12. Never power
  it from a Raspberry Pi GPIO, 3V3, 5V or J11.1.
- Keep Pluto TX1 and TX2 muted until RF-port DC, fixed attenuation and the
  below-0-dBm board input limit have been verified.
- Do not write target flash until read-only SWD, a factory-flash backup and
  connect-under-reset recovery have passed and been recorded.
- Do not change option bytes, BOR, readout protection or watchdog policy during
  the first writable-image gate.

## Repository layout

```text
firmware/stm32c011/   STM32C011 sources and host-testable control core
openocd/              normal and connect-under-reset Raspberry Pi configs
profiles/             generated profile snapshots plus source provenance
scripts/              profile sync and bench automation
src/smateway/         board-specific Python host tools
tests/                host-only and explicitly selected hardware tests
```

Build outputs are ignored under `build/`. Per-board dumps, logs and RF captures
belong outside Git under `~/.local/state/smateway/boards/<board-id>/`.

## Host-only bootstrap

From this repository:

```sh
python3 scripts/sync_control_profile.py \
  --circuits-root /home/pi/gits/circuits --write
make test
```

The first target build is `pluto_safe_hold`. Flashing is deliberately not part
of `make`; hardware writes require an explicit, recorded bench command after
the read-only gates pass.

```sh
make safe-hold
make bench
make fast20
make phase20
```

`pluto_bench` is a separate, leased static-selector image. Its generated JSON
manifest carries the reviewed RAM-mailbox address, ABI offsets and exact ELF
hash, so host commands do not embed an unreviewed address. It accepts only the
generated eight antenna codes plus `ALL_OFF`, inserts the generated 5 ms
break-before-make guard, and returns to `ALL_OFF` when a lease expires.

After that image has independently passed its flash and electrical gates, use:

```sh
smateway bench --board-id <recorded-uid> status
smateway bench --board-id <recorded-uid> all-off
smateway bench --board-id <recorded-uid> select ANT4 --lease-ms 5000
```

Each invocation appends a UTC JSON event to that article's private state
directory and uses a per-board process lock. SWD reads and writes use the
memory access port without halting the core, so debugger loss cannot freeze a
selected state with its lease timer stopped. Commands write code and lease
first and the sequence word last; firmware publishes status before
acknowledging that sequence. A select command waits until the generated guard
has ended and the requested code is observed; when `--lease-ms` is omitted it
uses the maximum lease from the reviewed firmware manifest.

`pluto_fast20` consumes the same generated schedule and is independently
host-tested for its 80 ms marker body, every 5 ms guard, all eight unique
dwells, and the 386 ms cycle. It starts and refreshes the independent watchdog
without enabling interrupts or accessing flash/option registers. The separate
BOR-level-4 option-byte gate is intentionally not hidden in this image or its
build; it must be reviewed, recorded and recovered independently before the
autonomous image can be qualified.

`pluto_phase20` is a separate, temporary phase-comparison image. Its generated
profile preserves the qualified truth table and 5 ms `ALL_OFF` guard while
using a 20 ms marker followed by ANT1 through ANT8 with equal 20 ms dwells. The
cycle is 220 ms. Regenerate or check it with
`scripts/generate_phase20_profile.py --write|--check`; the generator imports
all GPIO codes from `fast20-v1` rather than duplicating the switch table.

After the image is flashed and its GPIO sequence is verified, one bounded
3-second, 5 MS/s transmission can be captured and analyzed with:

```sh
uv run python scripts/capture_phase20.py --tx-channel 0
uv run python scripts/capture_phase20.py --tx-channel 1
```

Each command persists 60 contiguous 250,000-sample dual-RX frames, refines the
pilot frequency, performs a 65,536-point FFT inside every complete dwell, and
writes both ANT1-relative and pairwise phase differences. The capture helper
restores both transmitters to its fail-muted state on normal return or a
cooperative exception.

## Continuous phase-sensitive OTA qualification

The development dependency pins `pluto-plus-utils` at commit
`f495a1c1191f4b6e323c5dc1e3d0c4e6c8eaa920`. That revision adds a bounded
dual-RX DDS capture using the exact tandem-V7 metadata runtime, more than two
kernel buffers, a fresh buffer generation, and a persisted continuity ledger.
At 1 MS/s, 100 refills of 100,000 samples form one 10-second capture.

Use `estimate_coherent_pilot_offset()` followed by
`analyze_fast20_phase_sensitive()` to refine the coherent pilot, align the
generated Fast20 schedule, subtract the local `ALL_OFF` leakage reference and
measure one complex phasor per antenna state. Supply a continuity ledger derived
from the persisted metadata. Buffer sequence and FPGA first-sample sequence are
the authoritative continuity proof; host realtime and monotonic values are
uncertain affine estimates and must not be used to splice phase records.

The reported phase is an uncalibrated fingerprint within one capture. It
contains selector, unequal PCB-path, antenna, mutual-coupling and receiver-path
phase, so it is not an emitter coordinate and is not directly comparable
between independently started captures. Geometric localization requires a
complex calibration at every RF path and an in-situ antenna calibration,
preferably at several frequencies. Analysis confidence measures schedule
alignment and cycle-to-cycle repeatability, not position probability.

The powered first-article audit from 2026-08-25 is retained outside Git under
`~/.local/state/smateway/boards/stm32c011-4c0055000950313950363920/fast20-5238fbd/`.
Its 10-second TX1 and TX2 captures each contain 100 contiguous frames and exactly
10,000,000 FPGA-counted samples with no gaps or failure flags. TX1 was strongest
through ANT4 and TX2 through ANT5. These are strong coupling fingerprints, not
calibrated position fixes.

The host decoder consumes the generated duration windows and fails closed to
`unknown` for no signal, truncation, ambiguous duration, missed/extra
transitions, bad ordering or a missing marker. BOR planning is likewise pure
and non-mutating:

```sh
uv run python scripts/plan_bor4.py --observed-optr 0xfffffeaa
```

For the first article this proves that BOR4 requires only `BOR_EN` bit 8 to
change: the factory rising and falling threshold fields already encode level
4. The planner refuses any option word whose RDP byte is not exactly `0xAA` and
prints `executed: false`; applying and re-reading that mask remains a separate
hardware gate.
