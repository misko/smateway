# 1 ms control implementation

The campaign's 1 ms control uses a separate profile specification; the original
25/50/100/200 µs profile bytes remain unchanged. Its six-port cycle is 6300 µs,
with the same 20 µs guards, 180 µs marker, GPIO path and watchdog register values.

The previous compile-time proof required nine complete cycles before the minimum
watchdog timeout. That is inappropriate for a longer cycle even when individual
deadlines remain safe. The new profile proves **two** refresh opportunities instead:
two worst-case 6495 µs cycles total 12990 µs, below the 15058 µs minimum watchdog
timeout. The maximum watchdog timeout remains 17356 µs, below the fastest timer's
31813 µs half-range. The 6300 µs cycle also remains below the 32768-tick half-range.
No runtime watchdog timeout or lateness threshold has been increased.

Host tests and the compiled profile/schedule/memory/static checks passed. Rebuilding
the existing 200 µs image preserved its SHA-256 exactly:
`505ddd97abee775f65dd5766a3c124787a26d379122a22740df9a468b6a1ddbb`.
The 1156-byte 1 ms image differs from that baseline **only** in the six schedule
entries' 16-bit dwell values, changing 200 to 1000. This establishes unchanged
executable instructions, not measured RF settling or live latency.

The proposed 20 ms continuous TX2 reference is **not implemented or admitted** by
this change. Its 120300 µs cycle violates the existing whole-cycle watchdog/timer
proof and requires a separately designed reference acquisition path. Do not add
20000 to the allowlist or call the 1 ms control a settled TX2 reference.

Deployment still requires exact selector backup/readback/restoration and bounded
captures. Build verification alone is not a deployed or RF-qualified control.
