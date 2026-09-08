# Switched source-muted control — 2026-09-08

The 5.811 GHz/25 us record contains a weak consistent harmonic family near
2229 Hz, alongside stronger competing peaks. Those peaks are consistent with a
frequency-shifted switching family; the separately measured RX1 tone is near
100.77 kHz. This observation alone does not identify RF interference, a baseband
switch transient, PCB coupling or a receiver artifact.

Before further model changes, collect three independently restarted four-second
source-muted records at each of 25/200/1000 us, A/2 MS/s, 1.6 MHz bandwidth,
5.811 GHz and RX60 dB. Use only the established receiver/source serials and
verified selector profiles. Randomize within rounds; restore the exact original
full selector image on completion or failure. Both source TX channels stay at
zero DDS scale and -80 dB gain. No radio firmware or wiring change.

The new `fast-ambient` mode records raw IQ but cannot enable the source or report
a TX1 static reference. It still requires verified selector-flash evidence and
sample continuity/clipping checks. These are switched artifact controls, not
calibration holdouts or bearing measurements. Retain every failed attempt.

Compare RX2's direct low-frequency spectrum and switch-synchronous waveform with
the earlier source-enabled record. Persistence with the source muted supports a
source-independent component; it does not alone prove the PCB or receiver is
responsible, because other environmental signals remain present.
