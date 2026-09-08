# Fresh readiness record — 2026-09-08

The coordinating task relayed the user's confirmations on 2026-09-08:

- Wiring is unchanged from the current tracking fixture.
- Installed antennas support both 2.4 and 5.8 GHz.
- The lab is in the United States.

The current connection is TX1 through a splitter to RX1 and the first OTA test
antenna, TX2 to a separate test antenna, and the C6 selector common to RX2.
The campaign uses only its pinned receiver, source and selector identities.
Neither the wiring confirmation nor the ISM allocation constitutes a new
emissions certification; retain the previously authorized bounded low-power
settings and the US limits recorded in the protocol.

No matching STL or other CAD model of the installed C6 fixture was located by the
coordinating task in this checkout, remote main or local all-ref history.
The older [HexRay design](../hexray_tx_in_middle_calibration/README.md) records a
user-confirmed **51 mm circle**, but explicitly calls its coordinates design
coordinates and uses ANT1–ANT6 with a centred transmitter. That is not the current
port map or transmitter placement. It is a useful hypothesis, not a survey of
the installed C6 fixture. Do not silently substitute either 25.5 mm or the old
band-dependent nominal radii as verified current coordinates.

Current phase-centre positions and surveyed transmitter angles remain unconfirmed.
Geometry-independent diagnostics can establish acquisition continuity, settled
complex references and phase/gain repeatability. They cannot establish surveyed
bearing accuracy. Any acquisition before geometry confirmation must explicitly
carry unknown positions and diagnostic-only scope.
