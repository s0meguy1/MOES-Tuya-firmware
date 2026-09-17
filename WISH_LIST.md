> **Status, build 36 (2026-09-02):** every Tier 1 and Tier 2 item is implemented
> except item 7, plus items 8, 10, 11, 12 and the timeline-drift finding (F14).
> Reference: `work/tuyaZigbee/docs/light_show.md`. Per item:
> 1 ✅ `light_show_index` (persisted) + `light_show_spread`, and phase now offsets
> every periodic effect · 2 ✅ `delay` in a one-frame `light_show_cue` · 3 ✅
> `light_show_cue_list` (32 entries) + `light_show_cue_run` · 4 ✅
> `light_show_level` · 5 ✅ `light_show_takeover: hold` · 6 ✅ `light_show_duration`
> · 7 ✅ **resolved in build 38** — it was the output stage, not the
> MoveToLevel path; see the item itself · 8 ✅ show colour lives in
> the engine; every stop returns to the fixture's own state · 9 ✅ documented
> (`wave` does follow hue; the other three own their colour) · 10 ✅
> `light_show_density` · 11 ✅ real reports after unicast writes and on `/get`
> (group frames deliberately silent) · 12 ✅ hue/saturation/level can be staged
> on a dark fixture. Built as v1.36s3.3, host suites green, bench pass pending.

# Moes custom firmware — capability wishlist from building a real show

**Written 2026-09-02, after building `script.self_destruct_sequence` v3 through five
revisions against build v1.35s3.3 on 19 live fixtures.** Every item below is something that
cost design time or forced a workaround, and every claim traces to a measurement in
[MOES_CUSTOM_FIRMWARE_LIGHT_SHOW_FIELD_NOTES_2026-09-02.md](./MOES_CUSTOM_FIRMWARE_LIGHT_SHOW_FIELD_NOTES_2026-09-02.md)
(F-numbers below) or the reporting report of 2026-08-31.

**The one-line summary:** the engine renders beautifully on a single fixture, but it has no
concept of *where a fixture is* and no concept of *when* — so everything multi-fixture or
time-critical has to be driven frame-by-frame from Home Assistant, against a measured ceiling
of ~1.55 group commands/s. That ceiling, not the LEDs, is what shapes this show.

---

## Tier 1 — these change what is possible at all

### 1. A persistent fixture INDEX, and effects that use it
**Problem:** `light_show_phase` is a no-op (F13). Two bulbs given phases 0 and 180, both
running `pulse`, produced the same 5.2 W swing as two bulbs both at phase 0; anti-phase would
have read flat. Staggering the effect *starts* does create an offset but it decays back to
unison within ~5 s (F14). **There is currently no way to make a chase, a wave, or a bolt.**
That killed the feature the user most wanted and forced a fallback to one Zigbee frame per
fixture, which he then correctly rejected as "just turning it on and off fast".

**Ask:** a `light_show_index` datapoint (u8), **persisted in NV**, set once per fixture at
commissioning — plus a `light_show_spread` (degrees of offset per index step, or a member
count). Spatial effects then compute their own offset from `index`, and **one group broadcast
becomes a chase across the whole house**.

Why index rather than fixing phase: phase-as-a-per-show-write means 19 unicasts before every
run (~7 s of transport) and it is stateless, so any resync loses it. An index in NV is written
once, survives reboots, and makes the group broadcast do all the work. It is also the natural
home for "which fixture am I" that a lighting product wants anyway.

### 2. Deferred execution — "start this in N milliseconds"
**Problem:** a command starts an effect the instant it arrives. Group frames arrive at
~1.55/s with real jitter, so nothing can be tightly synchronised, and every cue in the show
had to be published early with a skip-if-late guard and an absolute wall-clock target. The two
explosions are timed against a ±0.3 s fudge factor (`det_lead`) that exists purely to cover
publish-to-photons latency.

**Ask:** an optional `delay_ms` on the effect/level/colour commands. A group broadcast reaches
all members within a few milliseconds; if each schedules the action at `receipt + delay_ms`,
group sync collapses from ~100 ms of jitter to sub-10 ms, and cues can be armed slightly ahead
of when they are needed. Cheap to implement, and it improves *every* multi-fixture moment.

### 3. An on-chip cue list
**Problem:** the whole show is 36 group frames against a ~1.55 cmd/s budget — the transport is
~85 % saturated for 33 seconds, and the schedule is built by a 200-line Jinja template whose
main job is spacing frames far enough apart. Roughly half the design effort in this project has
gone into rationing Zigbee airtime rather than deciding what the room should look like.

**Ask:** upload a small sequence — say 32 entries of
`{t_ms, effect, speed, hue, sat, level}` — and trigger it with one command (ideally with
`delay_ms` from item 2). The chip then runs the sequence autonomously at 50 fps.

This is the highest-leverage item on the list. A 33-second show would cost **two frames instead
of thirty-six**, it would be frame-perfect against the audio, and it would be immune to mesh
congestion. Memory cost is trivial (a few hundred bytes).

---

## Tier 2 — these remove workarounds already in the show

### 4. Effect brightness independent of the ZCL level
**Problem:** colour-following effects render scaled by the fixture's *current output level*
(F2) — OFF or level 3 renders essentially nothing, ON at 254 renders full. Combined with item
5, there is no invisible way to arm an effect: the fixture must be driven to full first, which
is a visible flash. The show's entire opening beat ("the grid slams to full white") exists
because of this, not because anyone designed it.

**Ask:** a `light_show_level` datapoint the renderer uses instead of `currentLevel`
(default: follow `currentLevel`, so nothing changes for existing users). Then an effect can be
started on a dark fixture and come up *as the effect*.

### 5. Stop being implicit — let an effect survive a Level command
**Problem:** any Level command, and `genOnOff.off`, silently stops a running effect (F3). It is
genuinely useful as a one-frame cut to black, and the show uses it that way — but it also means
you cannot dim an effect, cannot flash one fixture without losing its effect, and cannot black
out one room and bring it back without spending three frames re-arming it.

**Ask:** a policy datapoint, e.g. `light_show_takeover`: `0` = today's behaviour (any command
takes the output back — correct when a user grabs the colour picker), `1` = the effect persists
and Level/colour commands update its parameters instead. Keep `0` as the default.

### 6. Run-for-N-then-stop, and one-shot effects
**Problem:** stopping an effect always costs a second frame at a precise time. `explode` already
holds until told otherwise; `burst` and friends loop forever.

**Ask:** a `light_show_duration` (ms, 0 = forever) so an effect self-clears, exactly as
`genOnOff.onWithTimedOff` already does for on/off — and that one **works well** on this firmware
(F15; stock ignored it entirely), so the pattern is proven. A `one_shot` flag on the looping
effects would cover "a single spark" cleanly.

### 7. Fix decreasing transitions
**Problem:** `moveToLevel` with a transition time does not step the output downward — it holds
the old level for the whole transition and snaps at the end (F7). Rate-based
`genLevelCtrl.move` down *is* a correct smooth ramp (F16), which localises the bug to the
MoveToLevel-with-`transtime` path rather than the output stage. Every fade-out in this stack
avoids the broken path.

**Verdict (2026-09-04, build 38): it was the output stage, and the F16 localisation was wrong.**

Two separate findings, only one of which survives.

*The whole-transition hold does not reproduce.* A live test on 13 kitchen fixtures all running
`v1.37s3.3` ramped in both directions and stayed in sync. Whatever produced F7 was fixed by
build 36 or 37; the level arithmetic itself ramps down correctly on the host, which the
previous session had already established.

*The hold-then-snap at the bottom was real, and it was the render chain.* Measured over the
build-37 code in `tools/level_curve_hosttest`, at 230 mireds: ZCL levels 3, 4, 5 and 6 all
produce PWM compare tick 236, and levels 1 and 2 both produce 94. Ten adjacent level pairs
below level 16 collapse onto one output. So the last second of a fade-out genuinely holds for
four levels and then snaps 94 → 0. That is F7's description exactly, arriving from the output
stage rather than from the command path — which is why the rate-based move looked no better in
principle and only looked better in practice, being usually run over a wider range.

Build 38 removes it. The same measurement on the new chain: every level below 16 moves the
output, a 5 s fade resolves 250 distinct PWM values instead of 50, and the last twelve compare
ticks are an even staircase (`562 525 488 452 415 378 342 305 268 232 195 158`) where build 37
gave `566 472 236 94 0`. **Hardware confirmation is still outstanding** — the host cannot prove
PWM, and no fixture has been flashed.

---

## Tier 3 — correctness and consistency

### 8. Make the two stop paths agree on colour
`light_show:stop` adopts the effect's hue as the fixture's ZCL colour (a red strobe leaves a red
bulb — F4, re-measured at 4.0 W against a 4.0 W full-red reference). A **Level** command that
stops the same effect restores the colour the fixture had *before* (F10). Two stop paths, two
different colours. The show exploits the difference deliberately, but it should be a documented
choice rather than an accident.

### 9. Effects documented as hue-following that are not
`wave`, `chase`, `color_step` and `snow` all draw **more than the solid set colour** on the
meter (110–198 % — F17), i.e. they are not honouring `light_show_hue`, and three of the four are
documented as colour-following. Either fix them or mark them as owning their colour.

### 10. Separate `burst` density from speed, and calibrate it
`burst` is documented as landing in ~35 % of one-second slots at speed 100. Measured, a fixture
running it averages ~9 % duty (F18) — an occasional spark rather than a texture. That gap caused
a live-run complaint ("the sparking stops and all lights sit blue"). Density and rate are
independent creative parameters and want independent controls.

### 11. Make the effect state readable
`light_show*` values are optimistic only — what was last commanded, not what the fixture is
doing. Combined with the reporting gaps of 2026-08-31 (commands executing with no state report,
`/get` timing out ~1/3 of the time), **every single measurement in this project was taken with a
mains wattmeter on the porch circuit**, because no readback could be trusted. That is a lot of
friction for a device that already mirrors these values internally on `0xEF00`.

### 12. Allow colour to be staged while off
A colour command to an OFF fixture is ignored (standard ZCL, and confirmed here), so a show
cannot pre-set a colour during a blackout — it must light the fixture, colour it, and re-dark it.
A "stage colour without applying" path would make blackout-to-colour transitions free.

---

## Things that are already right — please don't regress them

- **`genOnOff.onWithTimedOff` works** (F15). One frame = a self-clearing flash. It is the only
  reason a per-fixture chain was affordable at all.
- **Chaotic effects are seeded independently per fixture** (F18). Six bulbs running `burst` from
  one broadcast averaged 3.8 W against 18.5 W solid and never lit together, while a `pulse`
  control on the same group swung the full range in unison. Scatter for free from a single
  broadcast is genuinely valuable — the current show's opening act is built on it.
- **Datapoint writes apply to a running effect without restarting it** (F11) — hue, saturation,
  speed and phase all land live. Recolouring a running effect mid-scene is exactly right.
- **Re-sending an effect restarts it**, which is what makes "unify every fixture into lockstep"
  a single frame.
- **`explode` and `fire` render at full from any state**, ignoring the ZCL level. Both blasts
  depend on this.

---

## If only one thing gets built

**The cue list (item 3), with deferred execution (item 2).** Together they move choreography
from the network to the chip, which is where it belongs — the engine already renders at 50 fps
and the coordinator manages 1.55 commands per second. Item 1 (fixture index) is a close second,
because it is the difference between "nineteen fixtures" and "a lighting rig".

---

## Hardening (added 2026-09-17)

### H1. Sanity-check the calibration area at boot
**Problem:** the SDK loads RF/ADC calibration values from the identity block at
`0xFB000+` and trusts them. pvvx's point in doctor64/tuyaZigbee#23: manufacturers
don't actually calibrate these, and a module that arrives with garbage there
would run the firmware with a detuned radio while looking perfectly healthy.
Every unit converted so far shipped with plausible values, which proves nothing
about the next batch.

**Ask:** validate the calibration fields against plausible ranges before the
stack uses them and fall back to the SDK defaults (logging a boot-reason
breadcrumb) when they are out of range. Never write to that block.
