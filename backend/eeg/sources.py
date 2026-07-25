"""Ways to get samples out of a Muse, in descending order of how much can go wrong.

  osc        Mind Monitor on a phone, streaming OSC over UDP. No BLE stack on the
             laptop, and the app hands us blink, jaw-clench and per-channel fit
             quality for free. It also shortens the Bluetooth hop from ~3 m
             (headband to laptop) to ~30 cm (headband to pocket), which is worth
             roughly 20 dB of link budget in a venue where a few hundred radios
             are fighting over an 83 MHz band. Fastest to first signal, and the
             right choice on stage even if you develop on something else.
  brainflow  Direct BLE from this machine. Cleanest data, most ways to fail.
             Native BLE on macOS/Windows; on Linux you are compiling from source.
  sim        A synthetic subject. Not an apology -- it is how the UI gets built
             before the hardware is on anyone's head, how the demo survives a
             dead headband, and how a scripted stage run stays repeatable.

Every source pushes into the same estimator and attention tracker, so nothing
downstream knows which one is running.
"""

from __future__ import annotations

import math
import os
import random
import threading
import time
from typing import Callable

import numpy as np

from .attention import LateralAttention
from .metrics import SAMPLE_RATE, EffortEstimator


class EEGSource:
    name = "base"

    def __init__(self, estimator: EffortEstimator, attention: LateralAttention | None = None):
        self.estimator = estimator
        self.attention = attention
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.status = "idle"
        self.error: str | None = None

    def _emit(self, sample, ts: float | None = None) -> None:
        self.estimator.push_sample(sample, ts)
        if self.attention is not None:
            self.attention.push_sample(sample, ts)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_guarded, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run_guarded(self) -> None:
        try:
            self.status = "connecting"
            self._run()
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI, never fatal
            self.error = f"{type(exc).__name__}: {exc}"
            self.status = "error"

    def _run(self) -> None:
        raise NotImplementedError


class SimulatedSource(EEGSource):
    """A synthetic subject who looks around a table and drifts in and out of focus.

    Generates real oscillations at real frequencies and amplitudes, real blink
    deflections on the frontal leads, real anti-symmetric saccade steps, and real
    alpha lateralisation -- so the entire downstream pipeline runs exactly as it
    would with a headband on. Drive `gaze_target` and `target_effort` from the UI
    to script a demo beat by beat.
    """

    name = "sim"

    def __init__(self, estimator: EffortEstimator, attention: LateralAttention | None = None):
        super().__init__(estimator, attention)
        self.target_effort = 0.55      # 0-1
        self.gaze_target = 0.0         # -1 left .. +1 right
        self.auto_drift = False
        self._gaze = 0.0
        self._blink_request = threading.Event()
        self._clench_request = threading.Event()

    def request_blink(self) -> None:
        self._blink_request.set()

    def request_clench(self) -> None:
        self._clench_request.set()

    def set_effort(self, value: float) -> None:
        self.target_effort = max(0.0, min(1.0, value))

    def look_at(self, position: float) -> None:
        self.gaze_target = max(-1.0, min(1.0, position))

    def _run(self) -> None:
        self.status = "streaming"
        dt = 1.0 / SAMPLE_RATE
        t = 0.0
        phase = {b: random.random() * math.tau for b in ("theta", "alpha", "beta")}
        blink_samples_left = 0
        pending_second_blink = 0
        next_natural_blink = time.time() + 3.0
        saccade_samples_left = 0
        saccade_step = 0.0
        heog_level = 0.0

        while not self._stop.is_set():
            batch_start = time.time()
            eff = self.target_effort
            if self.auto_drift:
                eff = min(1.0, max(0.0, eff + 0.12 * math.sin(batch_start * 0.05)))

            # A gaze change fires a saccade: a fast step in AF7 vs AF8.
            if abs(self.gaze_target - self._gaze) > 0.08 and saccade_samples_left == 0:
                delta = self.gaze_target - self._gaze
                self._gaze += delta
                saccade_step = -delta * 190.0   # leftward gaze -> AF7 positive
                saccade_samples_left = int(0.035 * SAMPLE_RATE)

            # Focused subjects blink less: ~5/min absorbed, ~26/min diffuse.
            blink_interval = 60.0 / max(4.0, 26.0 - 21.0 * eff)

            for _ in range(int(SAMPLE_RATE * 0.02)):
                t += dt
                theta_amp = 8.0 + 6.0 * eff
                alpha_amp = 24.0 - 14.0 * eff
                beta_amp = 4.0 + 7.0 * eff

                for key, freq in (("theta", 6.0), ("alpha", 10.0), ("beta", 20.0)):
                    phase[key] = (phase[key] + math.tau * freq * dt) % math.tau

                if saccade_samples_left > 0:
                    heog_level += saccade_step / (0.035 * SAMPLE_RATE)
                    saccade_samples_left -= 1
                heog_level *= math.exp(-dt / 2.6)   # AC coupling leaks to centre

                sample = []
                for ch in range(4):
                    frontal = ch in (1, 2)
                    # Attend right -> left-hemisphere (TP9) alpha suppresses.
                    lat = 1.0
                    if ch == 0:
                        lat = 1.0 - 0.45 * max(0.0, self._gaze)
                    elif ch == 3:
                        lat = 1.0 - 0.45 * max(0.0, -self._gaze)

                    value = (
                        (1.2 if frontal else 0.8) * theta_amp * math.sin(phase["theta"] + ch * 0.3)
                        + (0.5 if frontal else 1.5) * lat * alpha_amp * math.sin(phase["alpha"] + ch * 0.2)
                        + beta_amp * math.sin(phase["beta"] + ch * 0.4)
                        + random.gauss(0, 5.0)
                    )
                    if frontal:
                        # AF7 and AF8 see the eye dipole with opposite sign.
                        value += heog_level * (1.0 if ch == 1 else -1.0)
                        if blink_samples_left > 0:
                            progress = 1.0 - (blink_samples_left / (0.15 * SAMPLE_RATE))
                            value += 250.0 * math.sin(math.pi * progress)
                    sample.append(value)

                if blink_samples_left > 0:
                    blink_samples_left -= 1
                    if blink_samples_left == 0 and pending_second_blink > 0:
                        pending_second_blink -= 1
                        # 250 ms gap: inside the deliberate double-blink window.
                        blink_samples_left = -int(0.10 * SAMPLE_RATE)
                elif blink_samples_left < 0:
                    blink_samples_left += 1
                    if blink_samples_left == 0:
                        blink_samples_left = int(0.15 * SAMPLE_RATE)

                self._emit(sample)

            now = time.time()
            if self._blink_request.is_set():
                self._blink_request.clear()
                blink_samples_left = int(0.15 * SAMPLE_RATE)
                pending_second_blink = 1        # deliberate flag = two blinks
                next_natural_blink = now + blink_interval
            elif now >= next_natural_blink and blink_samples_left == 0:
                blink_samples_left = int(0.15 * SAMPLE_RATE)
                next_natural_blink = now + blink_interval * random.uniform(0.7, 1.3)

            if self._clench_request.is_set():
                self._clench_request.clear()
                self.estimator.note_external_clench(now)

            time.sleep(max(0.0, 0.02 - (time.time() - batch_start)))


class OSCSource(EEGSource):
    """Mind Monitor (iOS/Android) streaming OSC over UDP.

    Point the app's OSC target at this machine on MUSE_OSC_PORT. Note that most
    venue WiFi enables AP client isolation, which silently drops phone-to-laptop
    UDP with no error anywhere -- if packets never arrive, put the laptop on the
    phone's hotspot. Bind 0.0.0.0, never 127.0.0.1.

    The app's own blink and jaw-clench detectors are better tuned than anything
    we would write before the deadline, so we defer to them when they fire.
    """

    name = "osc"

    def __init__(self, estimator, attention=None, port: int | None = None):
        super().__init__(estimator, attention)
        self.port = port or int(os.environ.get("MUSE_OSC_PORT", "5000"))
        self.last_packet = 0.0
        self.horseshoe: list[float] = [4.0, 4.0, 4.0, 4.0]

    def _run(self) -> None:
        from pythonosc.dispatcher import Dispatcher
        from pythonosc.osc_server import BlockingOSCUDPServer

        def on_eeg(_addr, *values):
            self.last_packet = time.time()
            self.status = "streaming"
            if len(values) >= 4:
                self._emit([float(v) for v in values[:4]])

        def on_blink(_addr, *values):
            if values and float(values[0]) > 0:
                self.estimator.note_external_blink()

        def on_clench(_addr, *values):
            if values and float(values[0]) > 0:
                self.estimator.note_external_clench()

        def on_horseshoe(_addr, *values):
            if len(values) >= 4:                      # 1 good, 2 ok, >=3 bad
                self.horseshoe = [float(v) for v in values[:4]]
                self.estimator.set_contact(self.horseshoe)

        def on_acc(_addr, *values):
            if len(values) >= 3:
                self.estimator.push_motion(accel=tuple(float(v) for v in values[:3]))

        def on_gyro(_addr, *values):
            if len(values) >= 3:
                self.estimator.push_motion(gyro=tuple(float(v) for v in values[:3]))

        dispatcher = Dispatcher()
        dispatcher.map("/muse/eeg", on_eeg)
        dispatcher.map("/muse/elements/blink", on_blink)
        dispatcher.map("/muse/elements/jaw_clench", on_clench)
        dispatcher.map("/muse/elements/horseshoe", on_horseshoe)
        dispatcher.map("/muse/acc", on_acc)
        dispatcher.map("/muse/gyro", on_gyro)

        server = BlockingOSCUDPServer(("0.0.0.0", self.port), dispatcher)
        self.status = "listening"
        while not self._stop.is_set():
            server.handle_request()
        server.server_close()


class BrainFlowSource(EEGSource):
    """Direct BLE via BrainFlow.

    Board IDs: MUSE_2_BOARD 38, MUSE_S_BOARD 39, MUSE_2016_BOARD 41,
    MUSE_S_ATHENA_BOARD 67. The *_BLED_BOARD dongle variants were deprecated in
    BrainFlow 5.22.0 -- native BLE is the supported path now. Pin >= 5.22.2.
    """

    name = "brainflow"

    def __init__(self, estimator, attention=None, board_id: int | None = None):
        super().__init__(estimator, attention)
        self.board_id = board_id if board_id is not None else int(os.environ.get("MUSE_BOARD_ID", "39"))
        self.serial_number = os.environ.get("MUSE_NAME", "")
        self.mac = os.environ.get("MUSE_MAC", "")

    def _run(self) -> None:
        from brainflow.board_shim import BoardShim, BrainFlowInputParams, BrainFlowPresets

        params = BrainFlowInputParams()
        if self.serial_number:
            params.serial_number = self.serial_number   # advertised name, not a serial
        if self.mac:
            params.mac_address = self.mac
        params.timeout = 20                             # crowded RF room

        BoardShim.disable_board_logger()
        board = BoardShim(self.board_id, params)
        board.prepare_session()
        board.start_stream(45000)
        self.status = "streaming"

        eeg_channels = BoardShim.get_eeg_channels(self.board_id)[:4]
        try:
            accel_channels = BoardShim.get_accel_channels(
                self.board_id, BrainFlowPresets.AUXILIARY_PRESET
            )
            gyro_channels = BoardShim.get_gyro_channels(
                self.board_id, BrainFlowPresets.AUXILIARY_PRESET
            )
        except Exception:  # noqa: BLE001 - IMU is a bonus, not a requirement
            accel_channels, gyro_channels = [], []

        try:
            while not self._stop.is_set():
                data = board.get_board_data()
                if data.size:
                    block = np.asarray([data[ch] for ch in eeg_channels])
                    for i in range(block.shape[1]):
                        self._emit(block[:, i].tolist())

                if accel_channels or gyro_channels:
                    try:
                        aux = board.get_board_data(preset=BrainFlowPresets.AUXILIARY_PRESET)
                        if aux.size:
                            accel = tuple(float(aux[c][-1]) for c in accel_channels) if accel_channels else None
                            gyro = tuple(float(aux[c][-1]) for c in gyro_channels) if gyro_channels else None
                            self.estimator.push_motion(accel=accel, gyro=gyro)
                    except Exception:  # noqa: BLE001
                        pass
                time.sleep(0.02)
        finally:
            try:
                board.stop_stream()
                board.release_session()
            except Exception:  # noqa: BLE001 - teardown must not mask the real error
                pass


def build_source(kind: str, estimator: EffortEstimator, attention: LateralAttention | None = None) -> EEGSource:
    kinds: dict[str, Callable[..., EEGSource]] = {
        "sim": SimulatedSource,
        "osc": OSCSource,
        "brainflow": BrainFlowSource,
    }
    if kind not in kinds:
        raise ValueError(f"unknown EEG source {kind!r}; expected one of {sorted(kinds)}")
    return kinds[kind](estimator, attention)
