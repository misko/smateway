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

The user subsequently confirmed that the **current six-antenna circle is 51 mm
in diameter measured between centres of opposite antennas**. Record radius
25.5 mm as current user-confirmed geometry, without asserting surveyed precision,
frequency-independent RF phase centres or CAD evidence. The clockwise current
port order remains the pending confirmation; the coordinating task is asking
one question at a time at the user's request.

Full port-to-position binding and surveyed transmitter angles remain unconfirmed.
Geometry-independent diagnostics can establish acquisition continuity, settled
complex references and phase/gain repeatability. They cannot establish surveyed
bearing accuracy. Any acquisition before geometry confirmation must explicitly
carry unknown positions and diagnostic-only scope.

## Completed nominal geometry confirmation

The user then confirmed installation according to the recommended C6 layout in
the PCB calibration/tracking reports: ANT1 is forward; ANT1, ANT2, ANT4, ANT8,
ANT7, ANT5 proceed clockwise at 60° increments. Combined with the independently
confirmed current 51 mm diameter, this resolves nominal port-to-position binding.
The user also confirmed that transmitter angles are approximate, not surveyed.
No further geometry question is needed to proceed with model-based repeatability
diagnostics. Absolute angular accuracy remains unavailable.

The new [v2 fixture record](data/fixture-c6-51mm-v2.json) binds those answers for
subsequent captures. Earlier v1 diagnostic records remain immutable and retain
their original unknown-geometry acquisition scope. Use the same 25.5 mm radius
in both bands. The physical layout must not be confused with the report's separate
pair-first temporal scanning recommendation; historical reproduction is clockwise.
