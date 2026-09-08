# Protocol v1 amendment 02 — geometry-independent diagnostics

Recorded after the fresh wiring, dual-band antenna and US-location confirmations,
and before any new OTA capture. The protocol's bearing qualification gate remains
unchanged: actual installed geometry must be established for that claim.

Allow bounded acquisition and phase/gain diagnostics before that geometry is
established. Such fixture records must explicitly set `positions_m` to `null`,
`geometry_status` to `unconfirmed`, and `capture_scope` to
`diagnostic_no_geometry`. Persist that restriction in each run. Do not populate
unknown coordinates from the old band profiles or the older 51 mm design.

Serial identity, fresh readiness, current wiring, source power, US frequency
limits, occupied-signal margin, continuity and final muting gates still apply.
Use US candidate intervals 2400–2483.5 MHz and 5725–5875 MHz with the frozen
1 MHz provisional edge guard. This amendment does not increase RF power, certify
emissions compliance, relax analysis thresholds, or authorize a physical change.

Results collected in this scope may establish transport/headroom and independent
RX1-referenced phase/gain closure. Bearing analysis must remain unavailable or be
explicitly separated as a sensitivity study using stated geometry hypotheses;
it cannot qualify a calibrated array or surveyed angular accuracy. Later geometry
evidence must be linked separately without rewriting original acquisition records.
