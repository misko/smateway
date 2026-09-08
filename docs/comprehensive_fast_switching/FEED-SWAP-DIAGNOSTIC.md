# Pending ANT1/ANT2 feed-swap experiment

Update: the user declined this intervention because they are away. **No cables
were swapped.** This remains a future discriminating experiment; the temporary
hardware hold was released under the existing unchanged-fixture authorization.

This is a predeclared intervention, **not a claim that the operator has done it**.
Hardware was muted and the bench selector verified ALL_OFF before requesting it.
No hardware/fixture-dependent capture may resume until the user confirms or declines.

Swap only the two antenna feed connections at PCB ANT1 and ANT2. Leave the
physical antenna locations, transmitter positions, RX1 reference and common/RX2
path fixed. The physical antenna formerly observed through ANT1 then reaches PCB
ANT2, and vice versa. The next fixture record must swap those two coordinate rows;
it must not pretend the physical antennas moved or silently retain the old mapping.

At 5.800 GHz (RX60 dB) and 2.475 GHz (RX40 dB), obtain three independently restarted
two-second static captures each of ANT1, ANT2 and ANT4, with order varied by round.
ANT4 is an unchanged control. Keep TX1 at the existing −35 dB/0.25 DDS settings,
mute between captures, and retain continuity/clipping failures without replacing
them as independent validation. These are separate intervention data, not additions
to the unchanged-fixture switching holdouts. No selector firmware flash is needed.

The 5.8 GHz pre-swap block has repeated independent before/after static references
for all ports. Compare per-port complex-transfer amplitude and phase statistics
with that baseline, reporting uncertainty and ANT4 drift. The 2.475 GHz pre-swap
evidence is only one ANT1 diagnostic; do not invent an ANT2 baseline at that frequency.

Interpret amplitude movement cautiously:

- A large increase at PCB ANT1 and decrease at PCB ANT2 would support the weakness
  following an antenna/feed assembly rather than staying with one PCB input.
- Little movement, with PCB ANT1 still weak, would prioritize the PCB input path
  or its connector; this is not proof of a particular component failure.
- Broad changes including ANT4 would make source/fixture drift or the intervention
  itself a competing explanation.

Connector reseating, cable bends, antenna phase-centre uncertainty and coupling can
change phase. A swap alone is not a perfect PCB de-embedding measurement. Request
return to the original connections as a separate one-question-at-a-time step after
the swapped measurements; do not assume that return was performed.
