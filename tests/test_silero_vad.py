from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace

import numpy as np


def _import_module():
    import bot.audio.silero_vad as silero_vad_module

    return importlib.reload(silero_vad_module)


def test_silero_model_resets_and_tracks_context(monkeypatch):
    silero_vad_module = _import_module()

    calls = []

    class FakeSessionOptions:
        def __init__(self):
            self.inter_op_num_threads = None
            self.intra_op_num_threads = None

    class FakeInferenceSession:
        def __init__(self, model_path, providers, sess_options):
            calls.append((model_path, tuple(providers), sess_options))

        def run(self, _output_names, feed_dict):
            return np.array([[0.7]], dtype=np.float32), np.ones((2, 1, 128), dtype=np.float32)

    fake_ort = SimpleNamespace(SessionOptions=FakeSessionOptions, InferenceSession=FakeInferenceSession)
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)

    model = silero_vad_module._SileroModel("/tmp/silero.onnx")
    first = model(np.ones(512, dtype=np.float32), 16000)
    second = model(np.ones((2, 512), dtype=np.float32), 8000)

    assert calls[0][0] == "/tmp/silero.onnx"
    assert first.shape == (1, 1)
    assert second.shape == (1, 1)
    assert model._last_sr == 8000
    assert model._last_batch_size == 2
    assert model._context.shape[1] == 32


def test_silero_model_reuses_and_resets_state_for_shape_changes(monkeypatch):
    silero_vad_module = _import_module()

    class FakeSessionOptions:
        def __init__(self):
            self.inter_op_num_threads = None
            self.intra_op_num_threads = None

    class FakeInferenceSession:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, _output_names, _feed_dict):
            return np.array([[0.4]], dtype=np.float32), np.ones((2, 1, 128), dtype=np.float32)

    fake_ort = SimpleNamespace(SessionOptions=FakeSessionOptions, InferenceSession=FakeInferenceSession)
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)

    model = silero_vad_module._SileroModel("/tmp/silero.onnx")
    reset_calls = []
    original_reset = model.reset_states

    def tracking_reset(batch_size=1):
        reset_calls.append(batch_size)
        original_reset(batch_size)

    model.reset_states = tracking_reset
    model(np.ones((1, 512), dtype=np.float32), 16000)
    model(np.ones((1, 512), dtype=np.float32), 16000)
    model(np.ones((2, 512), dtype=np.float32), 16000)

    assert reset_calls == [1, 2]


def test_vad_analyzer_process_and_state_machine(monkeypatch):
    silero_vad_module = _import_module()

    class FakeModel:
        def __init__(self, model_path):
            self.model_path = model_path
            self.reset_calls = 0

        def __call__(self, _audio, _sample_rate):
            return np.array([[0.9]], dtype=np.float32)

        def reset_states(self):
            self.reset_calls += 1

    monotonic_values = iter([0.0, 0.0, 0.1, 6.2, 6.3])
    monkeypatch.setattr(silero_vad_module, "_SileroModel", FakeModel)
    monkeypatch.setattr(silero_vad_module.time, "monotonic", lambda: next(monotonic_values))

    analyzer = silero_vad_module.SileroVADAnalyzer(
        silero_vad_module.VADParams(confidence=0.75, start_secs=0.064, stop_secs=0.064, min_volume=0.6)
    )
    analyzer._smooth_volume = lambda _frame: 0.8

    speaking_frame = (np.ones(512, dtype=np.int16) * 16000).tobytes()
    quiet_frame = (np.zeros(512, dtype=np.int16)).tobytes()

    assert analyzer.process(speaking_frame[:100]) == []
    assert analyzer.process(speaking_frame[100:]) == [silero_vad_module.VADState.STARTING]
    assert analyzer.process(speaking_frame) == [silero_vad_module.VADState.SPEAKING]

    analyzer._smooth_volume = lambda _frame: 0.0
    assert analyzer.process(quiet_frame) == [silero_vad_module.VADState.STOPPING]
    assert analyzer.process(quiet_frame) == [silero_vad_module.VADState.QUIET]

    assert analyzer._model.reset_calls == 1


def test_confidence_error_and_volume_smoothing(monkeypatch):
    silero_vad_module = _import_module()

    class FakeModel:
        def __init__(self, _model_path):
            pass

        def __call__(self, _audio, _sample_rate):
            raise RuntimeError("boom")

        def reset_states(self):
            return None

    class FakeMeter:
        def __init__(self, sample_rate, block_size):
            self.sample_rate = sample_rate
            self.block_size = block_size

        def integrated_loudness(self, audio_float):
            assert audio_float.dtype == np.float64
            return 30.0

    monkeypatch.setattr(silero_vad_module, "_SileroModel", FakeModel)
    monkeypatch.setattr(silero_vad_module.pyln, "Meter", FakeMeter)
    monkeypatch.setattr(silero_vad_module.time, "monotonic", lambda: 0.0)

    analyzer = silero_vad_module.SileroVADAnalyzer()
    frame = (np.ones(512, dtype=np.int16) * 4000).tobytes()

    assert analyzer._confidence(frame) == 0.0
    volume = analyzer._smooth_volume(frame)
    assert round(volume, 3) == 0.1


def test_step_covers_remaining_state_transitions(monkeypatch):
    silero_vad_module = _import_module()

    class FakeModel:
        def __init__(self, _model_path):
            self.reset_calls = 0

        def __call__(self, _audio, _sample_rate):
            return np.array([[0.95]], dtype=np.float32)

        def reset_states(self):
            self.reset_calls += 1

    monkeypatch.setattr(silero_vad_module, "_SileroModel", FakeModel)
    monkeypatch.setattr(silero_vad_module.time, "monotonic", lambda: 0.0)

    analyzer = silero_vad_module.SileroVADAnalyzer(
        silero_vad_module.VADParams(confidence=0.75, start_secs=0.064, stop_secs=0.064, min_volume=0.6)
    )

    analyzer._confidence = lambda _frame: 0.9
    analyzer._smooth_volume = lambda _frame: 0.8
    assert analyzer._step(b"x" * analyzer.BYTES_PER_CHUNK) == silero_vad_module.VADState.STARTING

    analyzer._confidence = lambda _frame: 0.0
    analyzer._smooth_volume = lambda _frame: 0.0
    assert analyzer._step(b"x" * analyzer.BYTES_PER_CHUNK) == silero_vad_module.VADState.QUIET

    analyzer._state = silero_vad_module.VADState.STOPPING
    analyzer._stopping_count = 1
    analyzer._confidence = lambda _frame: 0.9
    analyzer._smooth_volume = lambda _frame: 0.8
    assert analyzer._step(b"x" * analyzer.BYTES_PER_CHUNK) == silero_vad_module.VADState.SPEAKING


def test_step_logs_and_retains_stopping_state(monkeypatch):
    silero_vad_module = _import_module()

    class FakeModel:
        def __init__(self, _model_path):
            pass

        def __call__(self, _audio, _sample_rate):
            return np.array([[0.0]], dtype=np.float32)

        def reset_states(self):
            return None

    debug_calls = []
    monkeypatch.setattr(silero_vad_module, "_SileroModel", FakeModel)
    monkeypatch.setattr(silero_vad_module.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(silero_vad_module.logger, "debug", lambda message: debug_calls.append(message))

    analyzer = silero_vad_module.SileroVADAnalyzer(
        silero_vad_module.VADParams(confidence=0.75, start_secs=0.064, stop_secs=0.096, min_volume=0.6)
    )
    analyzer._state = silero_vad_module.VADState.STOPPING
    analyzer._stopping_count = 0
    analyzer._frame_count = 29
    analyzer._confidence = lambda _frame: 0.0
    analyzer._smooth_volume = lambda _frame: 0.0

    assert analyzer._step(b"x" * analyzer.BYTES_PER_CHUNK) == silero_vad_module.VADState.STOPPING
    assert analyzer._stopping_count == 1
    assert debug_calls
