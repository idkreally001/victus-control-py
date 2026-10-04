"""Software AUTO: a temperature/RPM curve that replaces firmware AUTO.

Firmware AUTO (pwm1_enable=2) was measured twice on this hardware to leave
fans at 0 RPM through 83-97C package temp under real sustained load -- it
does not respond to Linux-visible thermal state at all. This curve runs
entirely in software, on top of hardware MANUAL mode, the same approach the
original victus-control project used (it called this "Better Auto" and
made it the actual default -- true firmware AUTO was essentially never
exercised in practice there either).

Thresholds are in P-CORE-MEAN terms, not package temp. A ~1hr usage trace
(daemon/trace.csv) showed package temp reads 14C hotter than P-core mean
on average and up to 29C hotter -- package tracks the single hottest core's
instant spike, which is noisy and would cause fan level "surging" if used
directly (this was diagnosed as the likely cause of the old system's
"too loud" complaint). P-core mean of the 8 P-cores (Core 0/4/8/12/16/20/
24/28 on this 8P+12E i7-14700HX) is the smoothed signal fed into level
selection, with a light EMA on top for additional tick-to-tick stability.

The 75C P-core-mean max-fan threshold is not arbitrary: trace samples where
package hit 95-97C (right at the 100C throttle floor) showed P-core mean at
75-80C. Fans must be at max well before that point, not exactly at it.
"""

# --- EMA smoothing -----------------------------------------------------

EMA_ALPHA = 0.35


class Ema:
    def __init__(self, alpha=EMA_ALPHA):
        self.alpha = alpha
        self.value = None

    def update(self, sample):
        if sample is None:
            return self.value
        if self.value is None:
            self.value = float(sample)
        else:
            self.value = self.alpha * sample + (1 - self.alpha) * self.value
        return self.value


# --- CPU curve (P-core mean, C) -----------------------------------------
#
# Bands derived from real usage trace: the bulk of light/moderate desktop
# use (browsing, editing, this conversation) sat 40-50C P-core-mean, so
# that range gets finer gradation to avoid one big jump dominating normal
# use. Above 55C the curve is deliberately steep -- that's where trace data
# is thinnest (only synthetic load reached it) and under-cooling is the
# dangerous direction, so bias aggressive rather than guess conservative.
#
# (threshold_c, level)
CPU_LEVELS = [
    (0, 1),    # idle/silent, matches the 35-40C bulk of real usage
    (40, 2),
    (45, 3),
    (50, 4),
    (55, 5),
    (62, 6),
    (68, 7),
    (75, 8),   # max fan -- anchored to the 75-80C P-core-mean seen at
               # package 95-97C (right at the 100C throttle floor) in trace
]

# --- GPU curve (C) -------------------------------------------------------
#
# No loaded-GPU trace data exists yet (all logged sessions were CPU-only
# synthetic load) -- these are conservative defaults based on the RTX 4060
# Max-Q's known ~87C throttle point, not trace-derived. Revisit once a GPU
# load trace exists.
GPU_LEVELS = [
    (0, 1),
    (50, 2),
    (60, 3),
    (68, 4),
    (75, 5),
    (80, 6),
    (84, 7),
    (87, 8),
]

MAX_LEVEL = 8
MIN_RPM = 2000

# Drop at most one level per tick (matches the old system's proven anti-
# oscillation approach) -- only falls are damped this way, so a brief dip
# doesn't cause the fan to instantly drop and re-climb.
MAX_LEVEL_DROP_PER_TICK = 1

TICK_INTERVAL_S = 2.0

# A rise must persist for this many consecutive ticks before the level is
# actually allowed to increase. Not derived from a specific observed burst
# duration (fitting the threshold to one sample just moves the same
# problem to a slightly longer burst) -- derived from thermal physics
# instead: the heatsink's thermal time constant is far longer than a brief
# CPU spike, so increased airflow can't meaningfully remove heat from a
# transient shorter than roughly 10-15s anyway. The 100C hardware throttle,
# not the fan, is what actually protects the chip during a short burst.
# So holding for ~12s costs effectively nothing thermally on genuine
# sustained load (heat soaks in over tens of seconds regardless) while
# filtering out app-launch/background-task-style bursts that a fan
# couldn't have helped with even if it reacted instantly. Bursts that
# outlast this hold are, by construction, long enough that fan response
# starts to matter -- accept that deliberately rather than chasing every
# possible burst length. Falls are unaffected; the drop limiter above
# already handles those correctly.
RISE_HOLD_TICKS = 6  # ~12s at TICK_INTERVAL_S=2.0

# Re-apply fan targets at least this often even if the level hasn't
# changed, and independently re-assert MANUAL mode -- both ported from the
# old system's defensive periodic reassertion. Critical: without the mode
# reassertion specifically, a long steady-state period (level unchanged for
# a while) would never rewrite pwm1_enable, and the EC would silently
# revert to firmware AUTO -- which is fans-to-0, the exact failure mode
# this curve exists to prevent.
REAPPLY_INTERVAL_S = 90.0
MODE_REASSERT_INTERVAL_S = 80.0


def level_from_temp(temp_c, levels):
    if temp_c is None:
        return None
    level = levels[0][1]
    for threshold, lvl in levels:
        if temp_c >= threshold:
            level = lvl
    return level


def rpm_for_level(level, max_rpm):
    level = max(1, min(MAX_LEVEL, level))
    if max_rpm <= MIN_RPM:
        return max_rpm
    step = (max_rpm - MIN_RPM) / (MAX_LEVEL - 1)
    return round(MIN_RPM + (level - 1) * step)


class AutoCurveState:
    """Tracks the smoothed/leveled state across ticks. One instance per
    curve run (created fresh each time software AUTO starts)."""

    def __init__(self):
        self.cpu_ema = Ema()
        self.gpu_ema = Ema()
        self.level = 1
        self.last_applied_level = None
        self.last_apply_time = 0.0
        self.last_mode_assert_time = 0.0
        self._rise_candidate_ticks = 0

    def compute_target_level(self, cpu_temp_c, gpu_temp_c):
        cpu_smoothed = self.cpu_ema.update(cpu_temp_c)
        gpu_smoothed = self.gpu_ema.update(gpu_temp_c)

        cpu_level = level_from_temp(cpu_smoothed, CPU_LEVELS)
        gpu_level = level_from_temp(gpu_smoothed, GPU_LEVELS)

        candidates = [l for l in (cpu_level, gpu_level) if l is not None]
        raw_target = max(candidates) if candidates else self.level

        if raw_target > self.level:
            # A rise must persist RISE_HOLD_TICKS in a row before it's
            # allowed through -- filters brief bursts (app launches, short
            # background tasks) without slowing reaction to load that's
            # actually still there several ticks later. Persistence is
            # "still above current level", not "still exactly this value"
            # -- a genuinely ramping load (raw_target climbing 6, 7, 8
            # tick over tick) must count as continuously rising, not reset
            # the hold counter on every step.
            self._rise_candidate_ticks += 1

            if self._rise_candidate_ticks >= RISE_HOLD_TICKS:
                target = raw_target
            else:
                target = self.level
        else:
            self._rise_candidate_ticks = 0
            target = raw_target
            if target < self.level:
                target = max(target, self.level - MAX_LEVEL_DROP_PER_TICK)

        self.level = target
        return target

    def should_reapply(self, now, level):
        # Level change must apply within one tick, not wait for the
        # periodic timer -- REAPPLY_INTERVAL_S is a steady-state defensive
        # reassert floor (guards against the hardware silently forgetting
        # the target), not the primary trigger. Without the level != last
        # term, the curve computes a correct new level every 2s but can go
        # up to 90s without writing it -- inert to real temperature change
        # for up to a minute and a half, reintroducing "fans don't ramp
        # under load" one layer above where it was actually fixed.
        return (
            level != self.last_applied_level
            or now - self.last_apply_time >= REAPPLY_INTERVAL_S
            or self.last_apply_time == 0.0
        )

    def should_reassert_mode(self, now):
        return (
            now - self.last_mode_assert_time >= MODE_REASSERT_INTERVAL_S
            or self.last_mode_assert_time == 0.0
        )

    def mark_applied(self, now, level):
        self.last_apply_time = now
        self.last_applied_level = level

    def mark_mode_asserted(self, now):
        self.last_mode_assert_time = now
