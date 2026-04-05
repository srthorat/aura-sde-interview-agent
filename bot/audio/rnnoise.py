"""RNNoise noise suppression for raw PCM16 audio.

Wraps pyrnnoise to clean background noise from the user's microphone
before streaming to Gemini Live. Handles buffering and 16kHz↔48kHz
resampling internally.
"""

from __future__ import annotations

import numpy as np
from loguru import logger

try:
    from pyrnnoise import RNNoise as _RNNoise
    _PYRNNOISE_AVAILABLE = True
except ModuleNotFoundError:
    _PYRNNOISE_AVAILABLE = False
    logger.warning("[rnnoise] pyrnnoise not installed — noise suppression disabled")

_INPUT_RATE = 16_000   # our pipeline rate
_RNN_RATE   = 48_000   # RNNoise requires 48 kHz internally


def _resample(data: bytes, from_rate: int, to_rate: int) -> bytes:
    if from_rate == to_rate or len(data) == 0:
        return data
    samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    n_out = int(len(samples) * to_rate / from_rate)
    if n_out == 0:
        return b""
    indices = np.linspace(0, len(samples) - 1, n_out)
    return np.interp(indices, np.arange(len(samples)), samples).astype(np.int16).tobytes()


class RNNoiseFilter:
    """Apply RNNoise noise suppression to 16 kHz mono PCM16 bytes.

    Usage::

        filt = RNNoiseFilter()
        clean_pcm = filt.process(raw_pcm_bytes)
    """

    def __init__(self) -> None:
        if not _PYRNNOISE_AVAILABLE:
            self._rnn = None
            return
        self._rnn = _RNNoise(sample_rate=_RNN_RATE)
        logger.info("[rnnoise] RNNoise noise suppression loaded")

    @property
    def enabled(self) -> bool:
        return self._rnn is not None

    def process(self, pcm16_bytes: bytes) -> bytes:
        """Filter one chunk of 16 kHz mono PCM16. Returns filtered bytes (may be shorter)."""
        if not self.enabled or len(pcm16_bytes) == 0:
            return pcm16_bytes

        # Upsample 16 kHz → 48 kHz for RNNoise
        up = _resample(pcm16_bytes, _INPUT_RATE, _RNN_RATE)
        samples = np.frombuffer(up, dtype=np.int16)

        filtered_frames: list[np.ndarray] = []
        for _speech_prob, frame in self._rnn.denoise_chunk(samples):
            if np.issubdtype(frame.dtype, np.floating):
                frame = (frame * 32767).astype(np.int16)
            else:
                frame = frame.astype(np.int16)
            if frame.ndim > 1:
                frame = frame.squeeze()
            filtered_frames.append(frame)

        if not filtered_frames:
            return b""  # still buffering

        filtered_48k = np.concatenate(filtered_frames).tobytes()

        # Downsample 48 kHz → 16 kHz
        return _resample(filtered_48k, _RNN_RATE, _INPUT_RATE)
