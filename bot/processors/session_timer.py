"""Session timer processor for call hard limits."""

import asyncio
import os
from typing import Optional

from loguru import logger
from pipecat.frames.frames import CancelFrame, EndFrame, EndTaskFrame, Frame, StartFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class SessionTimerProcessor(FrameProcessor):
    """Terminate the pipeline when the max call duration is reached."""

    def __init__(self, max_duration_secs: Optional[float] = None, **kwargs):
        super().__init__(**kwargs)
        default_max = int(os.getenv("MAX_CALL_DURATION_SECS", "840"))
        self._max_duration = max_duration_secs if max_duration_secs is not None else default_max
        self._timer_task: Optional[asyncio.Task] = None
        self._session_started = False

        logger.info(f"SessionTimerProcessor: max_duration={self._max_duration}s")

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame) and not self._session_started:
            self._session_started = True
            self._start_timer()

        if isinstance(frame, (EndFrame, CancelFrame)):
            await self._stop_timer()

        await self.push_frame(frame, direction)

    def _start_timer(self):
        if self._timer_task is None:
            logger.info(f"Session timer started: {self._max_duration}s max duration")
            self._timer_task = self.create_task(self._timer_handler())

    async def _stop_timer(self):
        if self._timer_task:
            await self.cancel_task(self._timer_task)
            self._timer_task = None

    async def _timer_handler(self):
        await asyncio.sleep(self._max_duration)
        logger.warning(f"Session max duration ({self._max_duration}s) reached, ending call")
        await self.push_frame(
            EndTaskFrame(reason="exceeded-max-duration"),
            FrameDirection.UPSTREAM,
        )

    async def cleanup(self):
        await super().cleanup()
        await self._stop_timer()