"""Turn a Muse's four electrodes into an effort score and a flag gesture.

The Muse sits at TP9, AF7, AF8, TP10, referenced to FPz. All dry. That montage
decides what is honestly measurable, and it rules out the obvious approach:

  Frontal theta at AF7/AF8 is NOT a workload index here. The workload
  literature measures frontal *midline* theta at Fz/FCz; Muse has no midline
  electrode and references to FPz, which cancels spatially-broad frontal
  potentials rather than merely attenuating them. Worse, the blink artifact is
  a sub-10 Hz deflection 50-200x larger than theta, so a "workload index" built
  this way is a blink counter wearing a lab coat. Blink rate itself falls under
  load, so it even correlates the right way -- which makes it dangerous, not
  useful. Theta/beta "focus" is the same trap plus a discredited literature.

So effort here is built from three things the hardware actually does well:

  blink rate      Spontaneous blinking drops to ~4.5/min while reading and
                  absorbed, sits near ~17/min at rest, and rises to ~26/min in
                  conversation. That is a 5x effect measured on AF7/AF8, the
                  two electrodes with the best contact and no hair over them.
                  This carries most of the score.
  alpha           Posterior alpha at TP9/TP10 suppresses with engagement. Real,
                  but slow and confounded by eye state, so it gets a small
                  weight and a long window.
  stillness       IMU variance. Settled at the bench versus fidgeting. Not a
                  brain signal at all, and we label it as such.

Every component is normalised against a rolling per-session baseline, because
absolute values mean nothing across people or across a session as dry
electrodes settle. `components` is published alongside the score so the UI can
show which signal is driving it -- being legible about that is the difference
between a demo that survives a knowledgeable judge and one that doesn't.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np
from scipy import signal as sps

SAMPLE_RATE = 256.0

# Muse channel order as delivered by both BrainFlow and Mind Monitor.
CH_TP9, CH_AF7, CH_AF8, CH_TP10 = 0, 1, 2, 3
FRONTAL = (CH_AF7, CH_AF8)
POSTERIOR = (CH_TP9, CH_TP10)

BANDS = {
    "delta": (1.0, 4.0),
    "theta": (4.0, 8.0),
    "alpha": (8.0, 13.0),
    "beta": (13.0, 30.0),
    "gamma": (30.0, 44.0),
}

# Facial EMG from talking and clenching lives here. Used as a guard, never as
# a "focus" signal.
SPEECH_BAND = (20.0, 45.0)

WINDOW_SECONDS = 2.0
WINDOW_SAMPLES = int(WINDOW_SECONDS * SAMPLE_RATE)
# 30 s trades statistical resolution for responsiveness. A full minute is the
# textbook window for blink rate, but a meter that takes a minute to move reads
# as broken to anyone watching, and 30 s still spans ~13 blinks at the diffuse
# end of the range.
BLINK_RATE_WINDOW_S = 30.0
# Only the alpha term needs a per-subject baseline; blink rate is anchored to
# published rates, so a usable score exists long before alpha is primed.
CALIBRATION_SECONDS = 10.0
ALPHA_BASELINE_SECONDS = 30.0

# Blink detection. Threshold is adaptive (median + k*MAD); this floor only
# stops the detector from chasing noise on a dead channel.
BLINK_FLOOR_UV = 45.0
BLINK_MAD_K = 5.0
BLINK_REFRACTORY_S = 0.28
BLINK_SYMMETRY_TOLERANCE = 0.30   # AF7 and AF8 amplitudes must agree within 30%
ARTIFACT_REJECT_UV = 350.0

# A deliberate double-blink clusters at 150-400 ms. Natural consecutive blinks
# at 26/min average ~2.3 s apart, so this window is what separates intent from
# physiology.
DOUBLE_BLINK_MIN_S = 0.15
DOUBLE_BLINK_MAX_S = 0.40
FLAG_COOLDOWN_S = 1.4

# Blink rate anchors, blinks/min, from the ocular literature.
BLINK_RATE_ABSORBED = 5.0
BLINK_RATE_DIFFUSE = 26.0


def _bandpower(window: np.ndarray, low: float, high: float) -> float:
    """Mean PSD in [low, high) via Welch, for one channel."""
    if window.size < 64:
        return 0.0
    nperseg = min(256, window.size)
    freqs, psd = sps.welch(window, fs=SAMPLE_RATE, nperseg=nperseg)
    mask = (freqs >= low) & (freqs < high)
    if not mask.any():
        return 0.0
    return float(np.mean(psd[mask]))


class _RollingBaseline:
    """Adaptive mean/std in log space. Gates the pipeline until primed.

    Readiness is wall-clock based, not sample-count based: callers poll at
    whatever rate suits them, and a baseline that silently required a particular
    tick rate would leave the score stuck at None on any caller that ticked
    slower than assumed.
    """

    def __init__(self, seconds: float = 120.0, hz: float = 8.0, min_seconds: float | None = None):
        self.values: deque[float] = deque(maxlen=max(16, int(seconds * hz)))
        self.min_seconds = min_seconds or CALIBRATION_SECONDS
        self._first_ts: float | None = None

    def push(self, value: float) -> None:
        if value > 0 and math.isfinite(value):
            if self._first_ts is None:
                self._first_ts = time.time()
            self.values.append(math.log(value))

    @property
    def ready(self) -> bool:
        if self._first_ts is None or len(self.values) < 8:
            return False
        return (time.time() - self._first_ts) >= self.min_seconds

    def z(self, value: float) -> float:
        if not self.ready or value <= 0 or not math.isfinite(value):
            return 0.0
        arr = np.asarray(self.values, dtype=float)
        std = float(arr.std())
        if std < 1e-6:
            return 0.0
        return float(np.clip((math.log(value) - float(arr.mean())) / std, -3.0, 3.0))


@dataclass
class EffortReading:
    timestamp: float
    effort: float | None              # 0-100, None while calibrating
    components: dict[str, float] = field(default_factory=dict)
    blink_rate: float = 0.0           # blinks/min, rolling
    bands: dict[str, float] = field(default_factory=dict)
    contact_quality: float = 1.0
    speaking: bool = False
    moving: bool = False
    calibrating: bool = True
    calibration_progress: float = 0.0
    artifact: bool = False

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "effort": self.effort,
            "components": self.components,
            "blinkRate": round(self.blink_rate, 1),
            "bands": self.bands,
            "contactQuality": self.contact_quality,
            "speaking": self.speaking,
            "moving": self.moving,
            "calibrating": self.calibrating,
            "calibrationProgress": self.calibration_progress,
            "artifact": self.artifact,
        }


class EffortEstimator:
    """Samples in; effort readings and flag events out.

    Blink detection runs per-sample so a deliberate flag is caught the instant
    it happens. `compute` collapses the current window into a reading and is
    meant to be called on a timer at roughly 4 Hz.
    """

    def __init__(self, smoothing: float = 0.2):
        self.buffers = [deque(maxlen=WINDOW_SAMPLES) for _ in range(4)]
        self.smoothing = smoothing
        self._smoothed: float | None = None
        self._started = time.time()

        # Adaptive blink threshold, tracked over ~20 s of frontal amplitude.
        self._frontal_history: deque[float] = deque(maxlen=int(20 * SAMPLE_RATE))
        self._blink_times: deque[float] = deque(maxlen=64)
        self._interval_ewma: float | None = None
        self._last_blink = 0.0
        self._last_flag = 0.0
        self._last_clench = 0.0
        self.pending_flags: deque[dict] = deque()

        self._alpha_baseline = _RollingBaseline(min_seconds=ALPHA_BASELINE_SECONDS)

        # Guards, updated by the source layer.
        self.speaking = False
        self.moving = False
        self.contact_ok = True
        self._motion_energy = 0.0
        self._motion_baseline: deque[float] = deque(maxlen=200)

    # ---------------------------------------------------------------- ingest

    def push_sample(self, sample: list[float] | np.ndarray, ts: float | None = None) -> None:
        """One sample = four channel values in microvolts, TP9/AF7/AF8/TP10."""
        ts = time.time() if ts is None else ts
        for ch in range(4):
            try:
                self.buffers[ch].append(float(sample[ch]))
            except (IndexError, TypeError, ValueError):
                self.buffers[ch].append(0.0)
        self._detect_blink(ts)

    def push_motion(self, accel: tuple[float, float, float] | None = None,
                    gyro: tuple[float, float, float] | None = None) -> None:
        """IMU update. Feeds the stillness component and the motion guard."""
        energy = 0.0
        if gyro:
            energy += float(np.linalg.norm(gyro))
        if accel:
            # Subtract gravity so a stationary head reads near zero.
            energy += abs(float(np.linalg.norm(accel)) - 1.0) * 40.0
        self._motion_energy = energy
        self._motion_baseline.append(energy)
        if len(self._motion_baseline) > 20:
            median = float(np.median(self._motion_baseline))
            self.moving = energy > max(4.0, median * 2.5)

    def _blink_threshold(self) -> float:
        """median + k*MAD over recent frontal amplitude.

        Absolute blink amplitude varies 3-5x across people with fit and skin, and
        drifts across a session as dry electrodes settle, so a fixed microvolt
        threshold that works in testing will miss every blink on stage.
        """
        if len(self._frontal_history) < int(2 * SAMPLE_RATE):
            return BLINK_FLOOR_UV * 2.5
        arr = np.asarray(self._frontal_history, dtype=float)
        median = float(np.median(arr))
        mad = float(np.median(np.abs(arr - median))) or 1.0
        return max(BLINK_FLOOR_UV, median + BLINK_MAD_K * mad * 1.4826)

    def _detect_blink(self, ts: float) -> None:
        """Blinks live in the common mode of AF7 and AF8; gaze lives in the difference.

        Both eyelids move together, so a blink drives both frontal electrodes the
        same way and shows up in the SUM. Horizontal gaze rotates the corneo-
        retinal dipole toward one side, driving the two electrodes oppositely, so
        it shows up in the DIFFERENCE. Splitting the pair this way separates the
        two signals by physics rather than by a symmetry threshold -- which
        matters because a blink taken while looking off to one side is genuinely
        asymmetric, and any tolerance-based test throws it away.
        """
        if len(self.buffers[CH_AF7]) < 8:
            return
        af7 = self.buffers[CH_AF7][-1]
        af8 = self.buffers[CH_AF8][-1]
        common = af7 + af8            # blink
        differential = af7 - af8      # gaze
        self._frontal_history.append(abs(common))

        if ts - self._last_blink < BLINK_REFRACTORY_S:
            return
        # Guards: never fire on a channel the device says is bad, on a moving
        # head, or while the wearer is talking (facial EMG mimics the shape).
        if not self.contact_ok or self.moving or self.speaking:
            return

        if abs(common) < self._blink_threshold():
            return
        # Differential dominating means the eyes swung sideways, not shut.
        if abs(differential) > abs(common):
            return
        if abs(common) > 2 * ARTIFACT_REJECT_UV:
            return

        self._note_blink(ts)
        self._maybe_flag(ts)

    def _note_blink(self, ts: float) -> None:
        """Record a blink and fold its interval into the rate estimate.

        The deliberate double-blink is excluded from the interval statistics --
        its ~250 ms gap is intent, not physiology, and letting it in would spike
        the rate estimate and read as a sudden loss of focus every time someone
        flags something.
        """
        if self._blink_times:
            gap = ts - self._blink_times[-1]
            if gap > DOUBLE_BLINK_MAX_S:
                self._interval_ewma = (
                    gap if self._interval_ewma is None
                    else self._interval_ewma + 0.35 * (gap - self._interval_ewma)
                )
        self._last_blink = ts
        self._blink_times.append(ts)

    def _maybe_flag(self, ts: float) -> None:
        """Two blinks 150-400 ms apart is a deliberate marker, not physiology."""
        if ts - self._last_flag < FLAG_COOLDOWN_S:
            return
        if len(self._blink_times) < 2:
            return
        gap = ts - self._blink_times[-2]
        if DOUBLE_BLINK_MIN_S <= gap <= DOUBLE_BLINK_MAX_S:
            self._last_flag = ts
            self.pending_flags.append(
                {"timestamp": ts, "source": "double_blink", "gapMs": round(gap * 1000)}
            )

    def note_external_blink(self, ts: float | None = None) -> None:
        """For sources like Mind Monitor that run their own blink detector."""
        ts = time.time() if ts is None else ts
        if ts - self._last_blink < BLINK_REFRACTORY_S:
            return
        self._note_blink(ts)
        self._maybe_flag(ts)

    def note_external_clench(self, ts: float | None = None) -> None:
        """Jaw clench: temporalis EMG over 1 mV. Near-impossible to false-fire."""
        ts = time.time() if ts is None else ts
        if ts - self._last_clench < FLAG_COOLDOWN_S:
            return
        self._last_clench = ts
        self._last_flag = ts
        self.pending_flags.append({"timestamp": ts, "source": "jaw_clench"})

    def set_contact(self, horseshoe: list[float]) -> None:
        """Mind Monitor fit indicator, 1 good / 2 ok / >=3 bad, per channel."""
        if len(horseshoe) >= 4:
            self.contact_ok = horseshoe[CH_AF7] < 3 and horseshoe[CH_AF8] < 3

    def drain_flags(self) -> list[dict]:
        flags, self.pending_flags = list(self.pending_flags), deque()
        return flags

    def blink_rate(self, now: float | None = None) -> float:
        """Blinks per minute, estimated from inter-blink intervals rather than a
        fixed counting window.

        A 30 s window needs the full 30 s to register that someone has started
        blinking rapidly, which is far too slow to watch. An EWMA over intervals
        reacts within two or three blinks -- about 7 s at the diffuse end, where
        responsiveness actually matters. Going the other way, the guard against
        a stale estimate is time-since-last-blink: if nothing has happened for
        20 s, the rate cannot still be 26/min regardless of history.
        """
        now = time.time() if now is None else now
        cutoff = now - BLINK_RATE_WINDOW_S
        while self._blink_times and self._blink_times[0] < cutoff:
            self._blink_times.popleft()

        since_last = now - self._last_blink if self._last_blink else (now - self._started)
        if self._interval_ewma is None:
            # Not enough blinks yet to have an interval; fall back to counting.
            elapsed = max(2.0, min(BLINK_RATE_WINDOW_S, now - self._started))
            counted = len(self._blink_times) * 60.0 / elapsed
            return min(counted, 60.0 / max(since_last, 1.0))

        return float(np.clip(60.0 / max(self._interval_ewma, since_last, 0.4), 0.0, 60.0))

    # --------------------------------------------------------------- compute

    def _channel(self, ch: int) -> np.ndarray:
        arr = np.asarray(self.buffers[ch], dtype=float)
        return sps.detrend(arr, type="constant") if arr.size else arr

    def compute(self) -> EffortReading | None:
        now = time.time()
        if len(self.buffers[0]) < WINDOW_SAMPLES // 2:
            return None

        channels = [self._channel(ch) for ch in range(4)]
        amplitudes = [float(np.percentile(np.abs(c), 95)) if c.size else 0.0 for c in channels]
        good = [1e-3 < a < ARTIFACT_REJECT_UV for a in amplitudes]
        contact_quality = sum(good) / 4.0
        artifact = contact_quality < 0.5

        bands: dict[str, float] = {}
        for name, (low, high) in BANDS.items():
            powers = [_bandpower(channels[ch], low, high) for ch in range(4) if good[ch]]
            bands[name] = float(np.mean(powers)) if powers else 0.0

        # Talking and clenching both light up 20-45 Hz on the frontal leads.
        # Used only to suppress detectors -- never as a "focus" measure.
        speech_powers = [_bandpower(channels[ch], *SPEECH_BAND) for ch in FRONTAL if good[ch]]
        speech_power = float(np.mean(speech_powers)) if speech_powers else 0.0
        broadband = bands["alpha"] + bands["beta"] + 1e-9
        self.speaking = speech_power > 1.5 * broadband

        alpha_powers = [_bandpower(channels[ch], *BANDS["alpha"]) for ch in POSTERIOR if good[ch]]
        alpha_p = float(np.mean(alpha_powers)) if alpha_powers else 0.0

        rate = self.blink_rate(now)
        if not artifact:
            self._alpha_baseline.push(alpha_p)

        elapsed = now - self._started
        progress = min(1.0, elapsed / CALIBRATION_SECONDS)
        calibrating = elapsed < CALIBRATION_SECONDS

        if calibrating or artifact:
            return EffortReading(
                timestamp=now, effort=None, components={}, blink_rate=rate, bands=bands,
                contact_quality=contact_quality, speaking=self.speaking, moving=self.moving,
                calibrating=calibrating, calibration_progress=progress, artifact=artifact,
            )

        # --- component 1: blink rate, inverted. Absorbed people blink less.
        span = BLINK_RATE_DIFFUSE - BLINK_RATE_ABSORBED
        ocular = 1.0 - (np.clip(rate, BLINK_RATE_ABSORBED, BLINK_RATE_DIFFUSE)
                        - BLINK_RATE_ABSORBED) / span
        ocular = float(np.clip(ocular, 0.0, 1.0))

        # --- component 2: posterior alpha suppression, small weight, slow.
        # Contributes only once its baseline is primed; until then its weight is
        # handed to the ocular term rather than defaulting to a neutral 50, which
        # would drag every score toward the middle for the first half minute.
        alpha_ready = self._alpha_baseline.ready
        cortical = (
            float(np.clip(0.5 - 0.25 * self._alpha_baseline.z(alpha_p), 0.0, 1.0))
            if alpha_ready else 0.0
        )

        # --- component 3: stillness from the IMU. Not a brain signal, and the
        # UI labels it as such.
        motion_ref = float(np.median(self._motion_baseline)) if self._motion_baseline else 0.0
        stillness = float(np.clip(1.0 - (self._motion_energy / (motion_ref * 3.0 + 8.0)), 0.0, 1.0))

        weights = {"ocular": 0.6, "cortical": 0.25, "stillness": 0.15}
        if not alpha_ready:
            weights = {"ocular": 0.85, "cortical": 0.0, "stillness": 0.15}
        raw = 100.0 * (
            weights["ocular"] * ocular
            + weights["cortical"] * cortical
            + weights["stillness"] * stillness
        )

        if self._smoothed is None:
            self._smoothed = raw
        else:
            self._smoothed += self.smoothing * (raw - self._smoothed)

        return EffortReading(
            timestamp=now,
            effort=round(self._smoothed, 1),
            components={
                "ocular": round(ocular * 100, 1),
                "cortical": round(cortical * 100, 1) if alpha_ready else None,
                "stillness": round(stillness * 100, 1),
            },
            blink_rate=rate,
            bands=bands,
            contact_quality=contact_quality,
            speaking=self.speaking,
            moving=self.moving,
            calibrating=False,
            calibration_progress=1.0,
            artifact=False,
        )
