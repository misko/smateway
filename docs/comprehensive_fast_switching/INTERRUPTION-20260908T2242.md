# Interrupted B block — 2026-09-08 22:42 UTC

`block-20260908T223136472639Z.json` received an interrupt while collecting the
A-after ANT7 reference. The handler maps SIGINT/SIGTERM to `KeyboardInterrupt`;
the available traceback does not establish which signal, sender, or cause.
Do not classify this as sample loss, RF clipping, switch failure, or a user stop.

All 21 switched captures passed acquisition checks. The full selector image was
already restored exactly, and all six B-after references completed. Only one
A-after reference completed. The interrupted ANT7 capture retains its own failed
record and verified final source mute. The block remains failed/incomplete;
after-reference replacement and block promotion are not authorized by this note.

At 22:43 UTC both exact serial-pinned radios were again verified at TX gains
[-80, -80] dB and eight zero DDS scales. The bench status was command/ack 46,
applied ALL_OFF code 8, zero lease duration and inactive lease. No hardware
capture, selector programming or conflicting owner process remained active.
The available kernel journal showed no matching OOM, killed-process, shutdown or
reboot event during 22:40–22:44 UTC; the current shell CPU-time limit is unlimited.
Those negative checks do not identify the interrupt sender or exclude an
application-level limit.

The coordinating thread explicitly confirmed it had issued no stop, process
signal or ownership change, and relayed authorization to continue independent
campaign work after those checks. D/5 MS/s, 4 MHz bandwidth testing resumes as a
new block, not a retry or replacement of B's interrupted reference bracket.
Offline B analysis retains the incomplete A bracket and complete B bracket
separately; no full-block qualification is claimed.
