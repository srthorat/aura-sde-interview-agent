from __future__ import annotations

import importlib
import sys
from types import ModuleType

import pytest


def _install_pipecat_stubs():
    frames_module = ModuleType("pipecat.frames.frames")

    class Frame:
        pass

    class StartFrame(Frame):
        pass

    class EndFrame(Frame):
        pass

    class CancelFrame(Frame):
        pass

    class EndTaskFrame(Frame):
        def __init__(self, reason=None):
            self.reason = reason

    frames_module.Frame = Frame
    frames_module.StartFrame = StartFrame
    frames_module.EndFrame = EndFrame
    frames_module.CancelFrame = CancelFrame
    frames_module.EndTaskFrame = EndTaskFrame

    processor_module = ModuleType("pipecat.processors.frame_processor")

    class FrameDirection:
        UPSTREAM = "upstream"
        DOWNSTREAM = "downstream"

    class FrameProcessor:
        def __init__(self, **_kwargs):
            self.processed = []
            self.pushed = []
            self.created = []
            self.cancelled = []
            self.cleaned = False

        async def process_frame(self, frame, direction):
            self.processed.append((frame, direction))

        async def push_frame(self, frame, direction):
            self.pushed.append((frame, direction))

        def create_task(self, coro):
            self.created.append(coro)
            return "task-token"

        async def cancel_task(self, task):
            self.cancelled.append(task)

        async def cleanup(self):
            self.cleaned = True

    processor_module.FrameDirection = FrameDirection
    processor_module.FrameProcessor = FrameProcessor

    sys.modules["pipecat"] = ModuleType("pipecat")
    sys.modules["pipecat.frames"] = ModuleType("pipecat.frames")
    sys.modules["pipecat.frames.frames"] = frames_module
    sys.modules["pipecat.processors"] = ModuleType("pipecat.processors")
    sys.modules["pipecat.processors.frame_processor"] = processor_module

    return frames_module, processor_module


def _import_module():
    frames_module, processor_module = _install_pipecat_stubs()
    import bot.processors.session_timer as session_timer_module

    module = importlib.reload(session_timer_module)
    return module, frames_module, processor_module


def test_session_timer_default_duration(monkeypatch):
    session_timer_module, _, _ = _import_module()

    monkeypatch.setenv("MAX_CALL_DURATION_SECS", "900")
    processor = session_timer_module.SessionTimerProcessor()

    assert processor._max_duration == 900
    assert processor._session_started is False


@pytest.mark.asyncio
async def test_process_frame_starts_and_stops_timer(monkeypatch):
    session_timer_module, frames_module, processor_module = _import_module()

    processor = session_timer_module.SessionTimerProcessor(max_duration_secs=12)
    start_calls = []
    stop_calls = []

    monkeypatch.setattr(processor, "_start_timer", lambda: start_calls.append(True))

    async def fake_stop_timer():
        stop_calls.append(True)

    monkeypatch.setattr(processor, "_stop_timer", fake_stop_timer)

    await processor.process_frame(frames_module.StartFrame(), processor_module.FrameDirection.DOWNSTREAM)
    await processor.process_frame(frames_module.StartFrame(), processor_module.FrameDirection.DOWNSTREAM)
    await processor.process_frame(frames_module.EndFrame(), processor_module.FrameDirection.DOWNSTREAM)
    await processor.process_frame(frames_module.CancelFrame(), processor_module.FrameDirection.DOWNSTREAM)

    assert len(start_calls) == 1
    assert len(stop_calls) == 2
    assert len(processor.pushed) == 4


@pytest.mark.asyncio
async def test_start_stop_timer_and_cleanup():
    session_timer_module, _, _ = _import_module()

    processor = session_timer_module.SessionTimerProcessor(max_duration_secs=5)
    processor._start_timer()
    assert processor._timer_task == "task-token"
    processor._start_timer()
    assert len(processor.created) == 1

    await processor._stop_timer()
    assert processor._timer_task is None
    assert processor.cancelled == ["task-token"]

    await processor._stop_timer()
    assert processor.cancelled == ["task-token"]

    processor._timer_task = "cleanup-task"
    await processor.cleanup()
    assert processor.cleaned is True
    assert processor.cancelled[-1] == "cleanup-task"

    for created in processor.created:
        created.close()


@pytest.mark.asyncio
async def test_timer_handler_pushes_end_task(monkeypatch):
    session_timer_module, _, processor_module = _import_module()

    processor = session_timer_module.SessionTimerProcessor(max_duration_secs=3)

    async def fast_sleep(_seconds):
        return None

    monkeypatch.setattr(session_timer_module.asyncio, "sleep", fast_sleep)

    await processor._timer_handler()

    pushed_frame, direction = processor.pushed[0]
    assert pushed_frame.reason == "exceeded-max-duration"
    assert direction == processor_module.FrameDirection.UPSTREAM
