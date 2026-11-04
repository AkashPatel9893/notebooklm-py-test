"""Bounded, feature-owned Android Live Voice sessions."""

from __future__ import annotations

import asyncio
import math
from collections import deque
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from .exceptions import (
    ConfigurationError,
    DecodingError,
    NotebookLMError,
    RPCTimeoutError,
    UnsupportedOperationError,
)
from .live import (
    LiveAudio,
    LiveBufferOverflowError,
    LiveEstablished,
    LiveEvent,
    LiveGoAway,
    LiveInterrupted,
    LiveSessionClosedError,
    LiveTranscript,
    LiveTurnComplete,
)

if TYPE_CHECKING:
    from ._android import live_wire as wire


@dataclass(frozen=True)
class _TranscriptSegment:
    ident: str
    text: str


class LiveSession:
    """One notebook-bound full-duplex stream. Callers own media I/O.

    Use only through ``client.live.connect``. There is one event consumer and
    one audio writer. All transcript events replace their segment's full text.
    No input is replayed and automatic reconnection is not yet supported.
    """

    def __init__(
        self,
        transport: Any,
        *,
        notebook_id: str,
        source_ids: tuple[str, ...] | None,
        transcribe: bool,
        timeout: float,
        lifetime: float | None,
        absolute_deadline: float | None,
        max_input_bytes: int,
        max_audio_bytes: int,
        max_events: int,
        max_response_bytes: int,
    ) -> None:
        from ._android import live_wire

        self._wire = live_wire
        self._transport = transport
        self._setup = live_wire.setup(notebook_id, source_ids, transcribe)
        self._loop = asyncio.get_running_loop()
        self._absolute_deadline = absolute_deadline
        self._establishment_deadline = self._loop.time() + timeout
        if absolute_deadline is not None:
            self._establishment_deadline = min(self._establishment_deadline, absolute_deadline)
        self._lifetime = lifetime
        self._max_input = max_input_bytes
        self._max_audio = max_audio_bytes
        self._max_events = max_events
        self._max_response = max_response_bytes
        self._queue: deque[LiveEvent] = deque()
        self._audio_bytes = 0
        self._text_bytes = 0
        self._wake = asyncio.Event()
        self._ready = asyncio.Event()
        self._closed = False
        self._established = False
        self._error: Exception | None = None
        self._observed = False
        self._consumer = False
        self._sending = False
        self._sequence = 0
        self._segment_number = 0
        self._segments: dict[str, _TranscriptSegment] = {}
        self._token = ""
        self._bootstrap: asyncio.Task[None] | None = None
        self._reader: asyncio.Task[None] | None = None
        self._writer: asyncio.Task[None] | None = None
        self._cleanup: asyncio.Task[None] | None = None

    def _assert_loop(self) -> None:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("Live Voice sessions cannot be used across event loops")

    async def _start(self) -> None:
        if self._closed:
            self._raise_closed()
        self._bootstrap = asyncio.create_task(self._establish(), name="notebooklm-live-establish")
        remaining = max(0.0, self._establishment_deadline - self._loop.time())
        done, _ = await asyncio.wait((self._bootstrap,), timeout=remaining)
        if not done:
            self._fail(RPCTimeoutError("Live Voice establishment timed out"))
            self._raise_closed()
        if self._bootstrap.cancelled():
            self._raise_closed()
        self._bootstrap.result()
        if not self._established or self._error is not None or (self._closed and not self._queue):
            self._raise_closed()

    async def _establish(self) -> None:
        try:
            await self._transport.open(self._absolute_deadline)
            if self._closed:
                return
            await self._transport.write(self._setup)
            self._setup = b""
            self._reader = asyncio.create_task(self._read(), name="notebooklm-live-reader")
            await self._ready.wait()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fail(self._safe_error(error))

    def _safe_error(self, error: Exception) -> Exception:
        if isinstance(error, NotebookLMError):
            return error
        return self._transport.map_error(error)

    def _raise_closed(self) -> None:
        if self._error is not None:
            self._observed = True
            raise self._error.with_traceback(None) from None
        raise LiveSessionClosedError("Live Voice session is closed")

    def _finish(self, *, discard: bool) -> None:
        self._closed = True
        self._segments.clear()
        self._token = ""
        if discard:
            self._queue.clear()
            self._audio_bytes = 0
            self._text_bytes = 0
        self._transport.cancel()
        if self._writer is not None and self._writer is not asyncio.current_task():
            self._writer.cancel()
        self._ready.set()
        self._wake.set()

    def _fail(self, error: Exception) -> None:
        if self._error is None and not self._closed:
            error.__context__ = None
            error.__cause__ = None
            self._error = error.with_traceback(None)
        self._finish(discard=True)
        if self._reader is not None and self._reader is not asyncio.current_task():
            self._reader.cancel()

    def _emit(self, kind: type[LiveEvent], generation: int | None = None, **kwargs: Any) -> None:
        event = kind(sequence=self._sequence, epoch=0, generation_id=generation, **kwargs)
        audio_size = len(event.data) if isinstance(event, LiveAudio) else 0
        text_size = len(event.text.encode()) if isinstance(event, LiveTranscript) else 0
        if (
            len(self._queue) >= self._max_events
            or self._audio_bytes + audio_size > self._max_audio
            or self._text_bytes + text_size > self._max_response
        ):
            raise LiveBufferOverflowError("Live Voice output buffer exceeded its configured limit")
        self._sequence += 1
        self._queue.append(event)
        self._audio_bytes += audio_size
        self._text_bytes += text_size
        self._wake.set()

    def _transcript(
        self, role: Literal["user", "model"], msg: wire.Message, generation: int | None
    ) -> None:
        previous = self._segments.get(role)
        if previous is None:
            self._segment_number += 1
            previous = _TranscriptSegment(f"0:{self._segment_number}", "")
        # Qualified captures: input is snapshots, output is text deltas. Never
        # guess based on string prefixes (which would corrupt real revisions).
        text = msg.text(1) if role == "user" else previous.text + msg.text(1)
        if len(text.encode()) > self._max_response:
            raise LiveBufferOverflowError("Live Voice transcript segment exceeded its limit")
        final = bool(msg.integer(2))
        self._emit(
            LiveTranscript, generation, role=role, segment_id=previous.ident, text=text, final=final
        )
        if final:
            self._segments.pop(role, None)
        else:
            self._segments[role] = _TranscriptSegment(previous.ident, text)

    def _expand(self, data: bytes) -> bool:
        if len(data) > self._max_response:
            raise DecodingError("Live Voice response exceeded its configured limit")
        msg = self._wire.Message(data)
        if msg.has(5):
            self._token = msg.text(5)
        if msg.has(1):
            if self._established:
                raise DecodingError("Live Voice established more than once")
            self._established = True
            self._emit(LiveEstablished)
            self._ready.set()
        if msg.has(2):
            if not self._established:
                raise DecodingError("Live Voice content preceded session establishment")
            content = msg.message(2)
            generation = content.integer(7) if content.has(7) else None
            if generation is not None and generation >= (1 << 63):
                generation -= 1 << 64
            interrupted = bool(content.integer(4))
            if interrupted:
                # Retire invalidated audio before any new event needs queue capacity.
                self._queue = deque(e for e in self._queue if not isinstance(e, LiveAudio))
                self._audio_bytes = 0
                self._segments.pop("model", None)
            if content.has(5):
                self._transcript("user", content.message(5), generation)
            if interrupted:
                self._emit(LiveInterrupted, generation)
            if content.has(6):
                self._transcript("model", content.message(6), generation)
            if not interrupted and content.has(1):
                for part in content.message(1).repeated(2):
                    if part.has(2):
                        blob = part.message(2)
                        if blob.has(2):
                            self._emit(
                                LiveAudio, generation, data=blob.blob(2), mime_type=blob.text(1)
                            )
            if content.integer(3):
                self._segments.pop("model", None)
                self._emit(LiveTurnComplete, generation)
        if msg.has(3):
            self._emit(LiveGoAway)
            return False
        return True

    async def _read(self) -> None:
        try:
            while not self._closed:
                data = await self._transport.read()
                if self._closed:
                    break
                if data is None or not self._expand(data):
                    self._finish(discard=False)
                    break
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fail(self._safe_error(error))
        finally:
            self._ready.set()
            self._wake.set()
            await self._transport.close()

    async def send_audio(self, data: bytes, *, mime_type: str = "audio/pcm;rate=16000") -> None:
        """Write one chunk, without replay. A completed write is not server acknowledgment.

        Only one send can be in flight; its bytes count against the input bound.
        This applies backpressure directly at the gRPC write rather than retaining
        an additional input queue. Canceling an active send closes the session.
        """
        self._assert_loop()
        if self._closed:
            self._raise_closed()
        if self._sending:
            raise RuntimeError("Concurrent Live Voice audio writers are not supported")
        if not isinstance(data, bytes) or not data:
            raise ValueError("data must be nonempty bytes")
        if len(data) > self._max_input:
            raise ValueError("Audio chunk exceeds the configured input limit")
        if not isinstance(mime_type, str) or not mime_type or len(mime_type) > 256:
            raise ValueError("mime_type must be a nonempty string of at most 256 characters")
        self._sending = True
        self._writer = asyncio.create_task(self._transport.write(self._wire.audio(data, mime_type)))
        try:
            try:
                # asyncio.wait reports child cancellation as completion. A
                # CancelledError here therefore always belongs to our caller,
                # including simultaneous shutdown on Python 3.10.
                await asyncio.wait((self._writer,))
            except asyncio.CancelledError:
                await self.aclose()
                raise
            if self._writer.cancelled():
                if not self._closed:
                    self._fail(LiveSessionClosedError("Live Voice audio write was canceled"))
            else:
                try:
                    self._writer.result()
                except Exception as error:
                    if not self._closed:
                        self._fail(self._safe_error(error))
        finally:
            self._writer = None
            self._sending = False
        # Raise outside the raw exception handler so __context__ cannot retain
        # a gRPC exception, its details or transport-owned metadata.
        if self._closed:
            self._raise_closed()

    def events(self) -> _LiveEvents:
        """Return the sole event iterator. Close it explicitly when abandoning consumption."""
        self._assert_loop()
        if self._consumer:
            raise RuntimeError("Live Voice events support only one consumer")
        self._consumer = True
        return _LiveEvents(self)

    async def _next_event(self) -> LiveEvent:
        self._assert_loop()
        while True:
            if self._error is not None:
                self._raise_closed()
            if self._queue:
                event = self._queue.popleft()
                if isinstance(event, LiveAudio):
                    self._audio_bytes -= len(event.data)
                if isinstance(event, LiveTranscript):
                    self._text_bytes -= len(event.text.encode())
                return event
            if self._closed:
                raise StopAsyncIteration
            self._wake.clear()
            await self._wake.wait()

    async def _close(self) -> None:
        self._finish(discard=True)
        tasks = [t for t in (self._bootstrap, self._reader, self._writer) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._transport.close()
        self._setup = b""

    async def aclose(self) -> None:
        """Close idempotently, joining owned workers even if the waiter is canceled."""
        self._assert_loop()
        if self._cleanup is None:
            self._finish(discard=True)
            self._cleanup = asyncio.create_task(self._close(), name="notebooklm-live-cleanup")
        cancellation: asyncio.CancelledError | None = None
        while not self._cleanup.done():
            try:
                await asyncio.shield(self._cleanup)
            except asyncio.CancelledError as error:
                cancellation = error
        self._cleanup.result()
        if cancellation is not None:
            raise cancellation


class _LiveEvents:
    def __init__(self, session: LiveSession) -> None:
        self._owner = session
        self._advancing = False

    def __aiter__(self) -> _LiveEvents:
        return self

    async def __anext__(self) -> LiveEvent:
        if self._advancing:
            raise RuntimeError("Concurrent Live event advances are not supported")
        self._advancing = True
        try:
            return await self._owner._next_event()
        except asyncio.CancelledError:
            await self._owner.aclose()
            raise
        finally:
            self._advancing = False

    async def aclose(self) -> None:
        await self._owner.aclose()


class LiveAPI:
    def __init__(
        self,
        supervisor: Any,
        runtime: Any,
        *,
        backend: str,
        transport_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._supervisor = supervisor
        self._runtime = runtime
        self._backend = backend
        self._factory = transport_factory
        self._sessions: dict[int, LiveSession] = {}

    async def _shutdown(self, epoch: int | None = None) -> None:
        sessions = [
            session
            for generation, session in self._sessions.items()
            if epoch is None or generation == epoch
        ]
        for session in sessions:
            session._finish(discard=True)
        await asyncio.gather(*(session.aclose() for session in sessions))

    @asynccontextmanager
    async def connect(
        self,
        notebook_id: str,
        *,
        source_ids: Sequence[str] | None = None,
        transcribe: bool = True,
        reconnect: bool = False,
        establishment_timeout: float = 30,
        max_input_bytes: int = 256 * 1024,
        max_audio_bytes: int = 512 * 1024,
        max_events: int = 256,
        max_response_bytes: int = 1024 * 1024,
    ) -> AsyncIterator[LiveSession]:
        """Open Android Live Voice. Web and unqualified reconnect fail before dispatch."""
        if self._backend != "android":
            raise UnsupportedOperationError("Notebook Live Voice requires the Android backend")
        if reconnect:
            raise UnsupportedOperationError("Automatic Live Voice reconnection is not qualified")
        if not isinstance(notebook_id, str) or not notebook_id.strip():
            raise ValueError("notebook_id must be a nonempty string")
        if isinstance(source_ids, (str, bytes)):
            raise ValueError("source_ids must be a sequence of source ID strings")
        sources = None if source_ids is None else tuple(source_ids)
        if sources is not None and (
            not sources or any(not isinstance(s, str) or not s for s in sources)
        ):
            raise ValueError("An explicit source filter must contain nonempty source IDs")
        if not isinstance(transcribe, bool):
            raise ValueError("transcribe must be a bool")
        if not math.isfinite(establishment_timeout) or establishment_timeout <= 0:
            raise ValueError("establishment_timeout must be finite and positive")
        for value in (max_input_bytes, max_audio_bytes, max_events, max_response_bytes):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError("Live Voice buffer limits must be positive integers")
        session: LiveSession | None = None
        epoch: int | None = None
        body_error = False
        try:
            # Admission covers registration only, not the lifetime stream. There is
            # no await between checking the registry and publishing the new owner.
            async with self._supervisor.operation_scope("live.connect", timeout=None) as lease:
                epoch = lease.epoch
                if epoch in self._sessions:
                    raise ConfigurationError("Only one Live Voice session is allowed per client")
                factory = self._factory
                if factory is None:
                    from ._android.live_transport import LiveTransport

                    factory = LiveTransport
                lifetime = lease.context.remaining()
                timeout = (
                    establishment_timeout
                    if lifetime is None
                    else min(establishment_timeout, lifetime)
                )
                session = LiveSession(
                    factory(self._runtime, epoch, max_response_bytes),
                    notebook_id=notebook_id,
                    source_ids=sources,
                    transcribe=transcribe,
                    timeout=timeout,
                    lifetime=lifetime,
                    absolute_deadline=lease.context.absolute_deadline,
                    max_input_bytes=max_input_bytes,
                    max_audio_bytes=max_audio_bytes,
                    max_events=max_events,
                    max_response_bytes=max_response_bytes,
                )
                self._sessions[epoch] = session
            await session._start()
            yield session
        except BaseException:
            body_error = True
            raise
        finally:
            if session is not None:
                try:
                    await session.aclose()
                finally:
                    if epoch is not None and self._sessions.get(epoch) is session:
                        del self._sessions[epoch]
                if not body_error and session._error is not None and not session._observed:
                    session._raise_closed()
