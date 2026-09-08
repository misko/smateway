# Amendment 04 — source-muted selected-port diagnostic

The 2.450 GHz 40 dB switching block failed on round 3's 200 µs acquisition with
20 full-scale RX2 samples, despite its earlier complete static headroom screen.
Preserve all eight attempted switched captures; the failed holdout is not replaced.
Full original selector restoration and exact source mute passed. No after-static
reference bracket was collected, so the interrupted block cannot qualify a recipe.

Investigate the strong approximately 100 ms pattern observed in RX2 power with
the source muted and ANT1 statically selected. This differs from the previous
muted preflights, which kept the selector at ALL_OFF. Use 2.450 GHz, 2 MS/s,
RX30 dB, and independently restarted two-second captures with 100000, 50000 and
200000 samples per transport block. Keep every raw stream and per-block gain
telemetry. These are ambient selected-port diagnostics, never TX1 reference rows.

The lower receive gain reduces clipping risk; it is not substituted into the
40 dB block. No transmitter is enabled for these controls. A physical RF pattern
should retain its acquired-time period when host block boundaries change;
a change tied to block size merits acquisition-path investigation. Neither
outcome alone identifies a particular interferer, firmware fault or PCB component.
