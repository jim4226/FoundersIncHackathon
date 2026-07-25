"""Which of the things on the table are you looking at?

This is the one measurement in the system that needs no calibration, and the
reason is worth stating plainly: every quantity here is a CONTRAST BETWEEN TWO
ELECTRODES, not a level. Levels drift with fit, skin, sweat and session time,
which is why absolute band power needs a per-subject baseline. A left-minus-
right difference divides that drift out, because both sides drift together.

Two signals, and they are complementary rather than redundant:

  Horizontal EOG (AF7 minus AF8) is the FAST one. The eye is a standing
  electrical dipole -- cornea positive, retina negative -- so rotating it right
  drives AF8 positive and AF7 negative. The excursion is enormous next to
  cortical activity and it lands within a few tens of milliseconds of the
  saccade. But the headband is AC-coupled, so a sustained gaze decays back
  toward zero over a couple of seconds: EOG tells you the eyes MOVED, and which
  way, but it will not hold the answer.

  Alpha lateralisation (TP9 vs TP10) is the SLOW one, and it holds. Posterior
  alpha desynchronises over the hemisphere CONTRALATERAL to attended space --
  attend right, left-hemisphere alpha drops. As (left - right) / (left + right)
  it is unitless and self-normalising, it persists for as long as attention
  does, and it tracks COVERT attention, so it still reports when the eyes are
  still. It is also slower and noisier on two dry electrodes.

So the EOG catches the switch and the alpha holds the state. Neither needs a
per-subject calibration step, because both are ratios.

Output is a position on [-1, +1], left to right, plus a dwell timer per zone.
The consumer maps zones onto whatever Prism reports is physically on the table.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

import numpy as np
from scipy import signal as sps

from .metrics import BANDS, CH_AF7, CH_AF8, CH_TP9, CH_TP10, SAMPLE_RATE, _bandpower

HEOG_TAU_S = 0.30               # smoothing on the raw difference
HEOG_SCALE_FLOOR_UV = 40.0      # keeps noise from being scaled up into "gaze"
SACCADE_STEP_UV = 25.0          # step in the smoothed difference that counts as one
SACCADE_REFRACTORY_S = 0.15

ALPHA_SCALE_FLOOR = 0.06        # keeps a flat alpha ratio from being amplified
OCULAR_WEIGHT = 0.55            # fast, transient
ALPHA_WEIGHT = 0.45             # slow, sustained

DWELL_SELECT_S = 1.2            # rest this long on a zone and it counts as intent


def _robust_scale(history: deque[float], floor: float) -> float:
    """85th percentile of recent magnitude. Adaptive, so no calibration step."""
    if len(history) < 32:
        return floor
    return max(floor, float(np.percentile(np.asarray(history, dtype=float), 85)))


@dataclass
class AttentionReading:
    timestamp: float
    position: float               # -1 left .. +1 right
    zone: str | None
    dwell: float
    selected: str | None
    confidence: float
    heog: float                   # smoothed AF7-AF8 difference, microvolts
    ocular_position: float
    alpha_lateralisation: float   # (left - right) / (left + right)
    alpha_position: float
    saccades: int

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "position": round(self.position, 3),
            "zone": self.zone,
            "dwell": round(self.dwell, 2),
            "selected": self.selected,
            "confidence": round(self.confidence, 3),
            "heog": round(self.heog, 1),
            "ocularPosition": round(self.ocular_position, 3),
            "alphaLateralisation": round(self.alpha_lateralisation, 3),
            "alphaPosition": round(self.alpha_position, 3),
            "saccades": self.saccades,
        }


class LateralAttention:
    """Tracks left/right attention across zones laid out on the table.

    Zones are ``{id: centre}`` with centre on [-1, +1]. Prism knows where the
    physical objects actually are; this only needs their rough placement, so a
    two-object layout is just ``{"a": -0.65, "b": 0.65}``.
    """

    def __init__(self, zones: dict[str, float] | None = None):
        self.zones = dict(zones) if zones else {"left": -0.65, "right": 0.65}
        self._heog = 0.0
        self._last_ts = time.time()
        self._heog_history: deque[float] = deque(maxlen=int(10 * SAMPLE_RATE))
        self._alpha_history: deque[float] = deque(maxlen=400)
        self._alpha_li = 0.0
        self._recent: deque[float] = deque(maxlen=max(2, int(0.06 * SAMPLE_RATE)))
        self._last_saccade = 0.0
        self.saccade_count = 0
        self._zone: str | None = None
        self._zone_since = time.time()
        self._selected: str | None = None

    def set_zones(self, zones: dict[str, float]) -> None:
        self.zones = dict(zones)
        self._zone = None
        self._zone_since = time.time()
        self._selected = None

    # ---------------------------------------------------------------- ingest

    def push_sample(self, sample, ts: float | None = None) -> None:
        """One four-channel sample. Maintains the smoothed gaze difference."""
        ts = time.time() if ts is None else ts
        try:
            differential = float(sample[CH_AF7]) - float(sample[CH_AF8])
        except (IndexError, TypeError, ValueError):
            return

        dt = max(1e-4, min(0.25, ts - self._last_ts))
        self._last_ts = ts

        alpha = 1.0 - float(np.exp(-dt / HEOG_TAU_S))
        self._heog += alpha * (differential - self._heog)
        self._heog_history.append(abs(self._heog))
        self._recent.append(self._heog)

        # Measure the step across ~60 ms rather than between adjacent samples:
        # a saccade takes tens of milliseconds, so a single-sample derivative of
        # a smoothed signal is far below any sensible threshold.
        if len(self._recent) == self._recent.maxlen:
            step = self._heog - self._recent[0]
            if abs(step) >= SACCADE_STEP_UV and ts - self._last_saccade >= SACCADE_REFRACTORY_S:
                self._last_saccade = ts
                self.saccade_count += 1

    # --------------------------------------------------------------- compute

    def update_alpha(self, buffers) -> float:
        """Alpha lateralisation index from the two posterior electrodes."""

        def power(ch: int) -> float:
            arr = np.asarray(buffers[ch], dtype=float)
            if arr.size < 128:
                return 0.0
            return _bandpower(sps.detrend(arr, type="constant"), *BANDS["alpha"])

        left, right = power(CH_TP9), power(CH_TP10)
        total = left + right
        self._alpha_li = (left - right) / total if total > 1e-9 else 0.0
        self._alpha_history.append(abs(self._alpha_li))
        return self._alpha_li

    def compute(self, buffers=None) -> AttentionReading:
        now = time.time()
        if buffers is not None:
            self.update_alpha(buffers)

        # Looking right drives AF7 negative and AF8 positive, so the difference
        # goes negative -- hence the sign flip onto a left-negative axis.
        ocular = float(np.clip(
            -self._heog / _robust_scale(self._heog_history, HEOG_SCALE_FLOOR_UV), -1.0, 1.0
        ))
        # Attending right suppresses LEFT-hemisphere alpha, making the index
        # negative, so this flips too.
        alpha_pos = float(np.clip(
            -self._alpha_li / _robust_scale(self._alpha_history, ALPHA_SCALE_FLOOR), -1.0, 1.0
        ))

        # Weight each channel by how much it is currently saying. The EOG term
        # decays toward zero within a couple of seconds of a saccade because the
        # headband is AC-coupled, so a fixed blend would drag a sustained look
        # back toward centre and un-highlight whatever the user is still staring
        # at. Alpha lateralisation has no such decay, so as the ocular evidence
        # fades the cortical evidence simply takes over.
        w_ocular = OCULAR_WEIGHT * float(np.clip(abs(ocular) / 0.5, 0.0, 1.0))
        w_alpha = ALPHA_WEIGHT * float(np.clip(abs(alpha_pos) / 0.5, 0.0, 1.0))
        total_w = w_ocular + w_alpha
        blended = (
            float(np.clip((w_ocular * ocular + w_alpha * alpha_pos) / total_w, -1.0, 1.0))
            if total_w > 1e-6 else 0.0
        )

        zone, distance = self._nearest_zone(blended)
        if zone != self._zone:
            self._zone = zone
            self._zone_since = now
            self._selected = None

        dwell = now - self._zone_since
        if zone is not None and dwell >= DWELL_SELECT_S:
            self._selected = zone

        confidence = float(np.clip(1.0 - distance / 0.7, 0.0, 1.0)) * float(
            np.clip(dwell / DWELL_SELECT_S, 0.15, 1.0)
        )

        return AttentionReading(
            timestamp=now,
            position=blended,
            zone=zone,
            dwell=dwell,
            selected=self._selected,
            confidence=confidence,
            heog=self._heog,
            ocular_position=ocular,
            alpha_lateralisation=self._alpha_li,
            alpha_position=alpha_pos,
            saccades=self.saccade_count,
        )

    def _nearest_zone(self, position: float) -> tuple[str | None, float]:
        if not self.zones:
            return None, 1.0
        best = min(self.zones.items(), key=lambda kv: abs(kv[1] - position))
        return best[0], abs(best[1] - position)
