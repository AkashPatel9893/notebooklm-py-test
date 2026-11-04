"""Typed events for Android notebook-bound Live Voice (not WebRTC)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .exceptions import LiveBufferOverflowError as LiveBufferOverflowError
from .exceptions import LiveSessionClosedError as LiveSessionClosedError


@dataclass(frozen=True)
class LiveEvent:
    sequence: int
    epoch: int
    generation_id: int | None = None


@dataclass(frozen=True)
class LiveEstablished(LiveEvent):
    pass


@dataclass(frozen=True)
class LiveAudio(LiveEvent):
    data: bytes = field(default=b"", repr=False)
    mime_type: str = ""


@dataclass(frozen=True)
class LiveTranscript(LiveEvent):
    """Full replacement text for a session-local segment; never append events."""

    role: Literal["user", "model"] = "user"
    segment_id: str = ""
    text: str = field(default="", repr=False)
    final: bool = False


@dataclass(frozen=True)
class LiveInterrupted(LiveEvent):
    pass


@dataclass(frozen=True)
class LiveTurnComplete(LiveEvent):
    pass


@dataclass(frozen=True)
class LiveGoAway(LiveEvent):
    pass


__all__ = [
    "LiveEvent",
    "LiveAudio",
    "LiveEstablished",
    "LiveTranscript",
    "LiveInterrupted",
    "LiveTurnComplete",
    "LiveGoAway",
    "LiveSessionClosedError",
    "LiveBufferOverflowError",
]
