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
smateway bench --board-id <recorded-uid> select ANT4 --lease-ms 1000
```

Each invocation appends a UTC JSON event to that article's private state
directory. Connecting the debugger briefly halts the lease timer; commands
write code and lease first and the sequence word last, while the target is
halted, then immediately resume it.

`pluto_fast20` consumes the same generated schedule and is independently
host-tested for its 80 ms marker body, every 5 ms guard, all eight unique
dwells, and the 386 ms cycle. It starts and refreshes the independent watchdog
without enabling interrupts or accessing flash/option registers. The separate
BOR-level-4 option-byte gate is intentionally not hidden in this image or its
build; it must be reviewed, recorded and recovered independently before the
autonomous image can be qualified.
