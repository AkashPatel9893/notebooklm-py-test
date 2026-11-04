"""Controlled full-duplex streams qualify lifecycle, ordering and backpressure."""

import asyncio
from types import SimpleNamespace

import pytest

from notebooklm._android.live_wire import Message, field
from notebooklm._client_metrics import ClientMetrics
from notebooklm._live import LiveAPI
from notebooklm._runtime.call_supervisor import CallSupervisor
from notebooklm._runtime.lifecycle import ClientLifecycle
from notebooklm.exceptions import (
    ConfigurationError,
    DecodingError,
    ServerError,
    UnsupportedOperationError,
)
from notebooklm.live import (
    LiveAudio,
    LiveBufferOverflowError,
    LiveEstablished,
    LiveGoAway,
    LiveInterrupted,
    LiveSessionClosedError,
    LiveTranscript,
    LiveTurnComplete,
)


class Transport:
    def __init__(self, *args):
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait(field(1, b""))
        self.writes = []
        self.closed = False
        self.open_gate = None
        self.write_gate = None
        self.write_started = asyncio.Event()
        self.read_started = asyncio.Event()
        self.open_started = asyncio.Event()

    async def open(self, deadline):
        self.deadline = deadline
        self.open_started.set()
        if self.open_gate:
            await self.open_gate.wait()

    async def write(self, data):
        self.write_started.set()
        if self.write_gate:
            await self.write_gate.wait()
        self.writes.append(data)

    async def read(self):
        self.read_started.set()
        value = await self.incoming.get()
        if isinstance(value, Exception):
            raise value
        return value

    def cancel(self):
        pass

    async def close(self):
        self.closed = True

    def map_error(self, error):
        return ServerError("Sanitized Live failure")


@pytest.fixture
async def harness():
    supervisor = CallSupervisor(metrics=ClientMetrics(), max_concurrent_rpcs=1)
    instances = []

    def factory(*args):
        transport = Transport(*args)
        instances.append(transport)
        return transport

    api = LiveAPI(supervisor, None, backend="android", transport_factory=factory)
    supervisor.register_drain_hook("live", api._shutdown)
    lifecycle = ClientLifecycle(
        supervisor=supervisor,
        transports=(),
        loop_participants=(supervisor,),
        session_shutdown=api._shutdown,
    )
    await lifecycle.open()
    try:
        yield SimpleNamespace(api=api, owner=lifecycle, supervisor=supervisor, instances=instances)
    finally:
        await lifecycle.close(drain=False)


async def next_event(events):
    return await asyncio.wait_for(anext(events), 1)


def content(value):
    return field(2, value)


def transcription(role, text, final=False):
    return content(
        field(5 if role == "user" else 6, field(1, text) + (b"\x10\x01" if final else b""))
    )


def audio(data=b"audio"):
    return content(field(1, field(2, field(2, field(1, "audio/pcm;rate=24000") + field(2, data)))))


@pytest.mark.asyncio
async def test_setup_audio_and_single_consumer(harness):
    async with harness.api.connect("nb", source_ids=["source"]) as session:
        transport = harness.instances[0]
        setup = Message(transport.writes[0]).message(1)
        assert setup.text(1) == "projects/nb"
        assert setup.integer(2) == setup.integer(3) == 1
        assert setup.repeated(4)[0].text(1) == "source"
        assert not setup.has(5)
        events = session.events()
        assert isinstance(await next_event(events), LiveEstablished)
        with pytest.raises(RuntimeError):
            session.events()
        await session.send_audio(b"pcm")
        blob = Message(transport.writes[1]).message(2).message(1)
        assert blob.blob(2) == b"pcm"
        assert blob.text(1) == "audio/pcm;rate=16000"
        transport.incoming.put_nowait(audio())
        event = await next_event(events)
        assert isinstance(event, LiveAudio)
        assert event.data == b"audio"
        assert event.sequence == 1
        assert "data=" not in repr(event)
    assert transport.closed
    with pytest.raises(LiveSessionClosedError):
        await session.send_audio(b"x")


@pytest.mark.asyncio
async def test_snapshot_input_delta_output_segments(harness):
    async with harness.api.connect("nb") as session:
        events = session.events()
        await next_event(events)
        transport = harness.instances[0]
        observed = []
        for role, text, final in [
            ("user", "hello", False),
            ("user", "revised", True),
            ("model", "one", False),
            ("model", " two", False),
        ]:
            transport.incoming.put_nowait(transcription(role, text, final))
            observed.append(await next_event(events))
        assert [e.text for e in observed] == ["hello", "revised", "one", "one two"]
        assert observed[0].segment_id == observed[1].segment_id
        assert observed[2].segment_id == observed[3].segment_id
        assert observed[1].final
        transport.incoming.put_nowait(content(b"\x18\x01"))
        assert isinstance(await next_event(events), LiveTurnComplete)
        transport.incoming.put_nowait(transcription("model", "fresh"))
        event = await next_event(events)
        assert event.text == "fresh"
        assert event.segment_id != observed[2].segment_id
        assert not event.final


@pytest.mark.asyncio
async def test_combined_order_and_interruption_discards_audio(harness):
    async with harness.api.connect("nb") as session:
        transport = harness.instances[0]
        events = session.events()
        await next_event(events)
        transport.incoming.put_nowait(audio(b"stale"))
        combined = (
            field(5, field(1, "input"))
            + b"\x20\x01"
            + field(6, field(1, "output"))
            + b"\x18\x01\x38\x05"
            + field(1, field(2, field(2, field(2, b"discard"))))
        )
        transport.incoming.put_nowait(content(combined))
        # A control frame behind the combined message proves reader processing completed.
        transport.incoming.put_nowait(field(3, b""))
        await session._reader
        observed = [event async for event in events]
        assert [type(e) for e in observed] == [
            LiveTranscript,
            LiveInterrupted,
            LiveTranscript,
            LiveTurnComplete,
            LiveGoAway,
        ]
        assert [e.generation_id for e in observed[:4]] == [5] * 4
        assert not any(isinstance(e, LiveAudio) for e in observed)


@pytest.mark.asyncio
async def test_clean_eof_drains_received_events(harness):
    async with harness.api.connect("nb") as session:
        events = session.events()
        transport = harness.instances[0]
        transport.incoming.put_nowait(audio())
        transport.incoming.put_nowait(None)
        observed = [e async for e in events]
        assert [type(e) for e in observed] == [LiveEstablished, LiveAudio]
        with pytest.raises(LiveSessionClosedError):
            await session.send_audio(b"x")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limits,payload",
    [
        ({"max_events": 1}, audio()),
        ({"max_audio_bytes": 2}, audio()),
        ({"max_response_bytes": 16}, b"x" * 17),
    ],
)
async def test_overflow_and_wire_limit_wake_consumer(harness, limits, payload):
    async with harness.api.connect("nb", **limits) as session:
        events = session.events()
        harness.instances[0].incoming.put_nowait(payload)
        await session._reader
        expected = DecodingError if "max_response_bytes" in limits else LiveBufferOverflowError
        for _ in range(2):
            with pytest.raises(expected):
                await next_event(events)
        with pytest.raises(expected):
            await session.send_audio(b"x")
        assert not session._queue


@pytest.mark.asyncio
async def test_unobserved_failure_raised_at_exit_and_body_not_masked(harness):
    with pytest.raises(ServerError):
        async with harness.api.connect("nb") as session:
            harness.instances[0].incoming.put_nowait(RuntimeError("secret"))
            await session._reader
    with pytest.raises(ValueError, match="body"):
        async with harness.api.connect("nb") as session:
            harness.instances[-1].incoming.put_nowait(RuntimeError("secret"))
            await session._reader
            raise ValueError("body")


@pytest.mark.asyncio
async def test_simultaneous_write_rejected_and_parent_drain_wakes_writer(harness):
    async with harness.api.connect("nb") as session:
        transport = harness.instances[0]
        transport.write_gate = asyncio.Event()
        transport.write_started.clear()
        writer = asyncio.create_task(session.send_audio(b"first"))
        await transport.write_started.wait()
        with pytest.raises(RuntimeError):
            await session.send_audio(b"second")
        await harness.owner.drain()
        with pytest.raises(LiveSessionClosedError):
            await writer
        assert transport.closed
        assert session._reader.done()
        assert session._bootstrap.done()
        with pytest.raises(RuntimeError):
            async with harness.api.connect("nb"):
                pytest.fail("drained generation admitted a session")


@pytest.mark.asyncio
async def test_no_lifetime_rpc_permit_and_one_session(harness):
    async with harness.api.connect("nb"):
        async with harness.supervisor.call_scope("ordinary", "ordinary", None):
            pass
        with pytest.raises(ConfigurationError):
            async with harness.api.connect("nb"):
                pytest.fail("second session admitted")


@pytest.mark.asyncio
async def test_forced_close_reopen_does_not_revive_old_session(harness):
    async with harness.api.connect("nb") as old:
        await harness.owner.close(drain=False)
        await harness.owner.open()
        async with harness.api.connect("new") as new:
            assert new is not old
            with pytest.raises(LiveSessionClosedError):
                await old.send_audio(b"old")
            await new.send_audio(b"new")


@pytest.mark.asyncio
async def test_cancel_writer_closes_stream_without_orphan(harness):
    async with harness.api.connect("nb") as session:
        transport = harness.instances[0]
        transport.write_gate = asyncio.Event()
        transport.write_started.clear()
        writer = asyncio.create_task(session.send_audio(b"x"))
        await transport.write_started.wait()
        writer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writer
        assert transport.closed
        assert session._reader.done()


@pytest.mark.asyncio
async def test_cancel_consumer_and_explicit_iterator_close(harness):
    async with harness.api.connect("nb") as session:
        events = session.events()
        await next_event(events)
        task = asyncio.create_task(anext(events))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert harness.instances[0].closed
    async with harness.api.connect("nb") as session:
        await session.events().aclose()
        assert harness.instances[-1].closed


@pytest.mark.asyncio
async def test_web_and_reconnect_refused_before_admission():
    api = LiveAPI(None, None, backend="web")
    with pytest.raises(UnsupportedOperationError):
        async with api.connect("nb"):
            pytest.fail("entered")
    api = LiveAPI(None, None, backend="android")
    with pytest.raises(UnsupportedOperationError):
        async with api.connect("nb", reconnect=True):
            pytest.fail("entered")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"notebook_id": ""},
        {"source_ids": "id"},
        {"source_ids": []},
        {"source_ids": [""]},
        {"transcribe": "yes"},
        {"establishment_timeout": 0},
        {"establishment_timeout": float("nan")},
        {"max_events": 0},
        {"max_input_bytes": True},
    ],
)
async def test_invalid_configuration_before_admission(kwargs):
    api = LiveAPI(None, None, backend="android")
    with pytest.raises(ValueError):
        async with api.connect(**{"notebook_id": "nb", **kwargs}):
            pytest.fail("entered")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data,mime", [(b"", "audio/pcm"), ("bad", "audio/pcm"), (b"oversize", "audio/pcm"), (b"x", "")]
)
async def test_invalid_audio_does_not_dispatch(harness, data, mime):
    async with harness.api.connect("nb", max_input_bytes=4) as session:
        with pytest.raises(ValueError):
            await session.send_audio(data, mime_type=mime)
        assert len(harness.instances[0].writes) == 1


@pytest.mark.parametrize("data", [b"\x80", b"\x00", b"\x0b", b"\x0a\x02x", b"\x08" + b"\xff" * 10])
def test_invalid_protobuf_sanitized(data):
    with pytest.raises(DecodingError):
        Message(data)


@pytest.mark.asyncio
async def test_drain_during_establishment_leaves_no_worker(harness):
    gate, started = asyncio.Event(), asyncio.Event()
    original = harness.api._factory

    def factory(*args):
        transport = original(*args)
        transport.open_gate = gate
        started.set()
        return transport

    harness.api._factory = factory

    async def connect():
        async with harness.api.connect("nb"):
            pytest.fail("should not establish")

    task = asyncio.create_task(connect())
    await started.wait()
    await harness.owner.drain()
    with pytest.raises(LiveSessionClosedError):
        await task
    assert harness.instances[0].closed
    assert not harness.api._sessions


@pytest.mark.asyncio
async def test_establishment_timeout_cleans_owned_transport(harness):
    from notebooklm.exceptions import RPCTimeoutError

    original = harness.api._factory

    def factory(*args):
        transport = original(*args)
        transport.open_gate = asyncio.Event()
        return transport

    harness.api._factory = factory
    with pytest.raises(RPCTimeoutError):
        async with harness.api.connect("nb", establishment_timeout=0.01):
            pytest.fail("entered")
    assert harness.instances[0].closed
    assert not harness.api._sessions


@pytest.mark.asyncio
async def test_eof_before_establishment_is_entry_failure(harness):
    original = harness.api._factory

    def factory(*args):
        transport = original(*args)
        transport.incoming.get_nowait()
        transport.incoming.put_nowait(None)
        return transport

    harness.api._factory = factory
    with pytest.raises(LiveSessionClosedError):
        async with harness.api.connect("nb"):
            pytest.fail("entered")


@pytest.mark.asyncio
async def test_writer_failure_fans_out_and_clears_queue(harness):
    async with harness.api.connect("nb") as session:

        async def fail(data):
            raise RuntimeError("secret wire details")

        harness.instances[0].write = fail
        with pytest.raises(ServerError, match="Sanitized") as failure:
            await session.send_audio(b"x")
        assert failure.value.__context__ is None
        with pytest.raises(ServerError):
            await next_event(session.events())
        assert not session._queue


@pytest.mark.asyncio
async def test_parent_deadline_reaches_stream_and_no_transcription_flags(harness):
    async with (
        harness.supervisor.operation_scope("outer", timeout=10),
        harness.api.connect("nb", transcribe=False) as session,
    ):
        assert 0 < session._lifetime <= 10
        setup = Message(harness.instances[0].writes[0]).message(1)
        assert not setup.has(2) and not setup.has(3)


@pytest.mark.asyncio
async def test_transcript_limits_and_new_input_after_explicit_final(harness):
    async with harness.api.connect("nb", max_response_bytes=100) as session:
        events = session.events()
        await next_event(events)
        transport = harness.instances[0]
        transport.incoming.put_nowait(transcription("user", "first", True))
        first = await next_event(events)
        transport.incoming.put_nowait(transcription("user", "second"))
        second = await next_event(events)
        assert first.segment_id != second.segment_id
        transport.incoming.put_nowait(transcription("model", "x" * 70))
        await next_event(events)
        transport.incoming.put_nowait(transcription("model", "x" * 70))
        with pytest.raises(LiveBufferOverflowError):
            await next_event(events)


@pytest.mark.asyncio
async def test_cancellation_during_admission_settlement_cleans_registration(harness, monkeypatch):
    finish = harness.supervisor._finish_generation_token
    owner = asyncio.current_task()
    canceled = False

    async def cancel_during_settlement(generation, task):
        nonlocal canceled
        if not canceled:
            canceled = True
            owner.cancel()
        await finish(generation, task)

    monkeypatch.setattr(harness.supervisor, "_finish_generation_token", cancel_during_settlement)
    with pytest.raises(asyncio.CancelledError):
        async with harness.api.connect("nb"):
            pytest.fail("Canceled admission must not establish a session")
    assert canceled
    assert not harness.api._sessions
    assert harness.instances[0].closed
    async with harness.api.connect("nb") as session:
        assert isinstance(await next_event(session.events()), LiveEstablished)


@pytest.mark.asyncio
async def test_absolute_deadline_survives_admission_settlement(harness, monkeypatch):
    from notebooklm._runtime.operation_context import current_operation_context

    finish = harness.supervisor._finish_generation_token
    factory = harness.api._factory
    settling, release = asyncio.Event(), asyncio.Event()
    expected = None
    first = True

    def capture_deadline(*args):
        nonlocal expected
        expected = current_operation_context(harness.supervisor).absolute_deadline
        return factory(*args)

    async def settle(generation, task):
        nonlocal first
        if first:
            first = False
            settling.set()
            await release.wait()
        await finish(generation, task)

    monkeypatch.setattr(harness.api, "_factory", capture_deadline)
    monkeypatch.setattr(harness.supervisor, "_finish_generation_token", settle)

    async def connect():
        async with (
            harness.supervisor.operation_scope("outer", timeout=10),
            harness.api.connect("nb") as session,
        ):
            assert harness.instances[0].deadline == expected
            assert session._establishment_deadline <= expected

    task = asyncio.create_task(connect())
    try:
        await settling.wait()
        assert expected is not None
        release.set()
        await asyncio.wait_for(task, 1)
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cross_loop_session_misuse_is_rejected_before_dispatch(harness):
    async with harness.api.connect("nb") as session:

        async def misuse():
            with pytest.raises(RuntimeError, match="event loops"):
                await session.send_audio(b"cross-loop")
            with pytest.raises(RuntimeError, match="event loops"):
                session.events()
            with pytest.raises(RuntimeError, match="event loops"):
                await session.aclose()

        await asyncio.to_thread(lambda: asyncio.run(misuse()))
        assert len(harness.instances[0].writes) == 1
        await session.send_audio(b"same-loop")


@pytest.mark.asyncio
async def test_simultaneous_parent_close_and_writer_cancellation_preserves_caller(harness):
    async with harness.api.connect("nb") as session:
        transport = harness.instances[0]
        transport.write_gate = asyncio.Event()
        transport.write_started.clear()
        sender = asyncio.create_task(session.send_audio(b"x"))
        await transport.write_started.wait()
        # Both cancellations in the same scheduler turn exercise the Python
        # 3.10 ambiguity: its Task has no cancelling() count to inspect.
        session._finish(discard=True)
        sender.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sender
        assert transport.closed


@pytest.mark.asyncio
async def test_simultaneous_bootstrap_and_caller_cancellation_preserves_caller(harness):
    original = harness.api._factory
    created = asyncio.Event()

    def factory(*args):
        transport = original(*args)
        transport.open_gate = asyncio.Event()
        created.set()
        return transport

    harness.api._factory = factory

    async def connect():
        async with harness.api.connect("nb"):
            pytest.fail("entered")

    caller = asyncio.create_task(connect())
    await created.wait()
    await harness.instances[0].open_started.wait()
    session = next(iter(harness.api._sessions.values()))
    session._bootstrap.cancel()
    session._finish(discard=True)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert harness.instances[0].closed
    assert not harness.api._sessions


@pytest.mark.asyncio
async def test_interruption_reclaims_full_audio_queue_before_input_transcription(harness):
    async with harness.api.connect("nb", max_events=2) as session:
        events = session.events()
        await next_event(events)
        transport = harness.instances[0]
        transport.incoming.put_nowait(audio(b"first"))
        transport.incoming.put_nowait(audio(b"second"))
        transport.incoming.put_nowait(content(field(5, field(1, "question")) + b"\x20\x01"))
        transport.incoming.put_nowait(None)
        await session._reader
        observed = [event async for event in events]
        assert [type(event) for event in observed] == [LiveTranscript, LiveInterrupted]
        assert observed[0].text == "question"
        assert session._audio_bytes == 0


@pytest.mark.asyncio
async def test_client_default_operation_timeout_does_not_limit_stream(harness, monkeypatch):
    monkeypatch.setattr(harness.supervisor, "_operation_timeout", 10)
    async with harness.api.connect("nb") as session:
        assert session._lifetime is None
        assert harness.instances[0].deadline is None
        assert isinstance(await next_event(session.events()), LiveEstablished)
