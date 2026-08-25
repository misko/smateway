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
450 ms, 5 MS/s transmission can be captured and analyzed with:

```sh
uv run python scripts/capture_phase20.py --tx-channel 0
uv run python scripts/capture_phase20.py --tx-channel 1
```

For phase-slope calibration, `--center-frequency-hz` selects a bounded center
frequency from 2.30 through 2.50 GHz; the emitted pilot remains 100 kHz above
that center. Alternate TX1 and TX2 at every frequency without moving any
antenna.

Position solving uses `profiles/phase20-v1/array_geometry.json`. Coordinates
come from the released PCB SMA mating planes plus the antenna image's nominal
30 mm mating-face-to-whip-axis dimension. Before localization, retain the
unknown-position IQ captures, then make one known-position OTA calibration:
keep the receive array fixed, stand TX1 vertically on the same plane, align its
whip axis with the board centerline (`x = 65 mm`), and place that axis exactly
300 mm beyond the south PCB edge (`y = 385 mm` in the geometry file). Three
2.400 GHz captures at that unchanged point provide per-channel phase offsets
and a repeatability estimate. `smateway.localization` then reports distinct
wrapped-phase position candidates and their residual errors rather than hiding
spatial ambiguity.

Each command first retains nine contiguous 250,000-sample dual-RX frames in RAM,
allowing the real-time refill loop to avoid CI16 conversion and disk latency.
After TX is muted it persists the 2.25-million-sample capture, refines the pilot
frequency, performs a 65,536-point FFT inside every complete dwell, and writes
both ANT1-relative and pairwise phase differences. The capture helper restores
both transmitters to its fail-muted state on normal return or a cooperative
exception. This bounded duration stays within the verified continuous prefix of
the present USB path while spanning more than two 220 ms selector cycles. Run
several independent captures to measure phase repeatability; a run with any
metadata counter discontinuity is rejected rather than analyzed.

## Continuous phase-sensitive OTA qualification

The development dependency pins `pluto-plus-utils` at commit
`5551d29bc6c326f26285670efd20fc149caef474`. That revision adds a bounded
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

For a repeated transition-level proof of the unique-dwell firmware, run:

```bash
PYTHONPATH=src /home/pi/pluto-plus-utils/.venv/bin/python \
  scripts/capture_fast20_dwell.py --tx-channel 0
```

This emits one bounded TX1 pilot and defaults to the qualified Pi USB rate:
100 timestamped dual-RX frames (10 seconds at 1 MS/s) into a SHA-256-verified
artifact. It fails closed on a
buffer sequence, FPGA sample-counter, metadata, or overflow error. It derives
1 ms signal-presence transitions from the recording, decodes every complete
`ALL_OFF, ANT1, ..., ANT8` cycle, checks all eight measured dwell distributions
against the disjoint profile windows, and repeats the decode at three SNR
thresholds. A passing result requires at least 20 complete cycles with no
non-edge marker rejection and the same cycle count throughout the threshold
sweep. Both TX paths are muted during setup and in cooperative cleanup.
One 1 ms decision bin contains 1,000 IQ samples, and the 20–50 ms antenna
dwells contain 20,000–50,000 samples. A `--sample-rate-hz 5000000` option is
retained for faster host paths; sustained dual-RX 5 MS/s on this Pi/hub was
measured to overflow after 1.4–2.0 seconds and is never reported as continuous.

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
