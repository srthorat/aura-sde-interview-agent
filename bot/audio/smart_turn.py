"""Deprecated SmartTurn placeholder.

Aura no longer uses SmartTurn in the runtime path. Gemini server-side VAD and
the local Silero gate replaced this experiment, so the heavy model dependency
and ONNX artifact were intentionally removed from the active project surface.

This file stays in the repo only as an explicit marker that SmartTurn is
deprecated and unused.
"""

from __future__ import annotations

from pathlib import Path
import warnings


class SmartTurnV3:
    """Deprecated placeholder for the removed SmartTurn experiment."""

    def __init__(self, model_path: str | Path | None = None) -> None:
        if model_path is not None:
            self.model_path = str(model_path)
        else:
            self.model_path = str(Path(__file__).with_name("smart_turn_v3.onnx"))

        warnings.warn(
            "SmartTurnV3 is deprecated and unused in Aura. The runtime path no longer "
            "loads this model.",
            DeprecationWarning,
            stacklevel=2,
        )
        raise RuntimeError(
            "SmartTurnV3 is deprecated and not available in this project. "
            "Aura uses Gemini server-side VAD plus Silero instead."
        )
