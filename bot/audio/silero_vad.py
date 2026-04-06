"""Standalone Silero VAD for Gemini Live manual activity detection.

Mirrors the SileroVADAnalyzer from Pipecat but without the Pipecat
dependency.  The ONNX model is bundled at bot/audio/silero_vad.onnx.

Usage
-----
    vad = SileroVADAnalyzer()
    speech_active = False

    for pcm_chunk in audio_stream:
        states = vad.process(pcm_chunk)
        for state in states:
            if state == VADState.SPEAKING and not speech_active:
                speech_active = True
                await session.send_realtime_input(activity_start=ActivityStart())
            elif state == VADState.QUIET and speech_active:
                speech_active = False
                await session.send_realtime_input(activity_end=ActivityEnd())
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

import numpy as np
import pyloudnorm as pyln
from loguru import logger

# Reset internal ONNX model state every N seconds (prevents memory growth).
_MODEL_RESET_INTERVAL = 5.0

# Exponential smoothing factor for volume calculation.
_SMOOTHING_FACTOR = 0.2

# How often (in frames) to emit periodic debug stats.
_LOG_EVERY_N_FRAMES = 30  # ~1 second at 32ms/frame


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class VADState(Enum):
    QUIET = 1
    STARTING = 2
    SPEAKING = 3
    STOPPING = 4


@dataclass
class VADParams:
    """Configuration for the Silero VAD state machine.

    These match the values used in Solution 2 / Pipecat's recommended
    SmartTurn-compatible settings.
    """
    confidence: float = 0.75  # Minimum model confidence to count as speech
    start_secs: float = 0.2   # Seconds of speech before confirming turn start
    stop_secs: float = 0.2    # Seconds of silence before confirming turn end
    min_volume: float = 0.6   # Minimum RMS volume (filters breath/background)


# ---------------------------------------------------------------------------
# Internal ONNX model wrapper
# ---------------------------------------------------------------------------

class _SileroModel:
    """Thin ONNX-runtime wrapper around the pre-trained Silero VAD model."""

    def __init__(self, model_path: str) -> None:
        import onnxruntime as ort  # noqa: PLC0415

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self._session = ort.InferenceSession(
            model_path, providers=["CPUExecutionProvider"], sess_options=opts
        )
        self.reset_states()

    def reset_states(self, batch_size: int = 1) -> None:
        self._h = np.zeros((2, batch_size, 128), dtype="float32")
        self._context = np.zeros((batch_size, 0), dtype="float32")
        self._last_sr = 0
        self._last_batch_size = 0

    def __call__(self, audio_float32: np.ndarray, sr: int) -> np.ndarray:
        x = audio_float32
        if x.ndim == 1:
            x = np.expand_dims(x, 0)

        batch_size = x.shape[0]
        context_size = 64 if sr == 16000 else 32

        if not self._last_batch_size:
            self.reset_states(batch_size)
        if self._last_sr and self._last_sr != sr:
            self.reset_states(batch_size)
        if self._last_batch_size and self._last_batch_size != batch_size:
            self.reset_states(batch_size)

        if not self._context.shape[1]:
            self._context = np.zeros((batch_size, context_size), dtype="float32")

        x = np.concatenate((self._context, x), axis=1)
        ort_out, h_new = self._session.run(
            None, {"input": x, "state": self._h, "sr": np.array(sr, dtype="int64")}
        )
        self._h = h_new
        self._context = x[..., -context_size:]
        self._last_sr = sr
        self._last_batch_size = batch_size
        return ort_out


# ---------------------------------------------------------------------------
# Public analyzer
# ---------------------------------------------------------------------------

class SileroVADAnalyzer:
    """Client-side Silero VAD for driving Gemini Live manual activity signals.

    Processes raw PCM16 @ 16 kHz audio and returns VADState transitions that
    the caller uses to send ``ActivityStart`` / ``ActivityEnd`` to Gemini Live.

    The VAD runs in-process (no asyncio executor needed for lightweight calls).
    For higher-volume streams, wrap ``process()`` in ``loop.run_in_executor``.
    """

    # Silero requires exactly 512 samples (32 ms) at 16 kHz.
    FRAMES_PER_CHUNK: int = 512
    SAMPLE_RATE: int = 16_000
    BYTES_PER_CHUNK: int = FRAMES_PER_CHUNK * 2  # PCM16 = 2 bytes/sample = 1024

    def __init__(self, params: Optional[VADParams] = None) -> None:
        self._params = params or VADParams()

        model_path = str(Path(__file__).parent / "silero_vad.onnx")
        logger.debug(f"[vad] Loading Silero model from {model_path}")
        self._model = _SileroModel(model_path)
        self._last_reset_ts = time.monotonic()

        # State machine
        self._state = VADState.QUIET
        self._starting_count = 0
        self._stopping_count = 0
        self._prev_volume = 0.0

        # How many 32ms frames are needed to confirm start / end
        frames_per_sec = self.FRAMES_PER_CHUNK / self.SAMPLE_RATE  # 0.032
        self._start_frames = round(self._params.start_secs / frames_per_sec)
        self._stop_frames = round(self._params.stop_secs / frames_per_sec)

        # Internal buffer for accumulating partial chunks
        self._buf = b""

        # Debug counters
        self._frame_count = 0
        self._speech_frame_count = 0

        logger.info(
            f"[vad] Ready (start_frames={self._start_frames}, "
            f"stop_frames={self._stop_frames}, chunk={self.BYTES_PER_CHUNK}B, "
            f"min_volume={self._params.min_volume}, confidence={self._params.confidence})"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, pcm16: bytes) -> list[VADState]:
        """Process a PCM16 audio chunk and return states for each full frame.

        The caller should scan the returned list for SPEAKING / QUIET
        transitions to decide when to send activity_start / activity_end.

        Args:
            pcm16: Any-length PCM16 @ 16 kHz mono bytes.

        Returns:
            One VADState per complete 512-sample (32ms) frame processed.
            May be empty if the input is shorter than one frame.
        """
        self._buf += pcm16
        results: list[VADState] = []
        while len(self._buf) >= self.BYTES_PER_CHUNK:
            frame, self._buf = self._buf[:self.BYTES_PER_CHUNK], self._buf[self.BYTES_PER_CHUNK:]
            results.append(self._step(frame))
        return results

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _step(self, frame: bytes) -> VADState:
        """Run one 32ms frame through the model and advance the state machine."""
        confidence = self._confidence(frame)
        volume = self._smooth_volume(frame)
        is_speech = confidence >= self._params.confidence and volume >= self._params.min_volume

        self._frame_count += 1
        if is_speech:
            self._speech_frame_count += 1

        # Periodic debug logging (~every 1 second)
        if self._frame_count % _LOG_EVERY_N_FRAMES == 0:
            logger.debug(
                f"[vad] frame={self._frame_count} vol={volume:.3f} conf={confidence:.3f} "
                f"is_speech={is_speech} state={self._state.name} "
                f"speech_frames={self._speech_frame_count}/{self._frame_count}"
            )

        # Advance state machine
        if is_speech:
            if self._state == VADState.QUIET:
                self._state = VADState.STARTING
                self._starting_count = 1
            elif self._state == VADState.STARTING:
                self._starting_count += 1
            elif self._state == VADState.STOPPING:  # pragma: no branch
                self._state = VADState.SPEAKING
                self._stopping_count = 0
        else:
            if self._state == VADState.STARTING:
                self._state = VADState.QUIET
                self._starting_count = 0
            elif self._state == VADState.SPEAKING:
                self._state = VADState.STOPPING
                self._stopping_count = 1
            elif self._state == VADState.STOPPING:  # pragma: no branch
                self._stopping_count += 1

        # Threshold promotions
        if self._state == VADState.STARTING and self._starting_count >= self._start_frames:
            self._state = VADState.SPEAKING
            self._starting_count = 0

        if self._state == VADState.STOPPING and self._stopping_count >= self._stop_frames:
            self._state = VADState.QUIET
            self._stopping_count = 0

        return self._state

    def _confidence(self, frame: bytes) -> float:
        try:
            audio = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
            # Model returns shape (batch, 1) — flatten to get scalar
            conf = float(self._model(audio, self.SAMPLE_RATE).flat[0])
            # Periodic model reset to prevent memory growth
            now = time.monotonic()
            if now - self._last_reset_ts >= _MODEL_RESET_INTERVAL:
                self._model.reset_states()
                self._last_reset_ts = now
            return conf
        except Exception as exc:
            logger.warning(f"[vad] confidence error: {exc}")
            return 0.0

    def _smooth_volume(self, frame: bytes) -> float:
        """Calculate smoothed volume matching Pipecat's EBU R128 approach.

        Pipecat's calculate_audio_volume() uses pyloudnorm with raw int16 data
        (not normalized to [-1,1]), producing values in [0,1] where normal
        speech ≈ 0.8-1.0.  Our previous simple RMS/32768 gave ~0.05-0.15,
        which never reached min_volume=0.26 — that's why Silero never fired.
        """
        audio_np = np.frombuffer(frame, dtype=np.int16)
        audio_float = audio_np.astype(np.float64)

        block_size = audio_np.size / self.SAMPLE_RATE
        meter = pyln.Meter(self.SAMPLE_RATE, block_size=block_size)
        loudness = meter.integrated_loudness(audio_float)

        # Normalize loudness from [-20, 80] range to [0, 1]
        # (matches Pipecat's normalize_value)
        volume = max(0.0, min(1.0, (loudness - (-20)) / (80 - (-20))))

        smoothed = _SMOOTHING_FACTOR * volume + (1.0 - _SMOOTHING_FACTOR) * self._prev_volume
        self._prev_volume = smoothed
        return smoothed
