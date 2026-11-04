# ADR-0040: Android Live Voice session API

## Status

Accepted for an initial experimental Android implementation. Reconnection and broader protocol
qualifications remain gated. SDK policies below are choices, not recovered server guarantees.

## Context

Issue #2509 proposes real-time voice; its WebRTC-signaling sketch combines distinct app paths.

[Static analysis and authenticated probes](../android/live-session-static-evidence.md) establish
bidirectional `google.internal.labs.tailwind.api.v1.LiveSessionService/StreamLiveSession` on
`labstailwind.pa.googleapis.com:443`. Setup identifies `projects/<notebook-id>`; subsequent protobuf
messages carry audio. Responses contain audio, transcripts, interruption, turn completion and
reconnection tokens. ICE/SDP is not required.
Web ships a separate WebRTC Interactive Audio Overviews path, bound to an existing audio artifact
([evidence](../android/live-session-static-evidence.md#web-interactive-audio-overviews)). This ADR scopes SDK LiveSession support to Android. Interactive Audio Overview
signaling and its public API design are deferred to a separate proposal.

Android needs bidirectional transport; lifetime RPC permits would obstruct unrelated calls.

## Decision

### Scope and public surface

Expose an Android-only, context-managed session. Callers own recording, playback, resampling and
codecs. The SDK transports bytes and typed events without native media dependencies. Web rejects
`connect()` immediately with the existing `UnsupportedOperationError`, before credentials, optional
imports or network access.

This Python 3.10-compatible sketch shows concurrent ownership; spelling remains illustrative.
`microphone_chunks()` is caller-owned recording code. Behavioral contracts follow below.

```python
async def send_microphone(live):
    async for chunk in microphone_chunks():  # caller owns capture and its cleanup
        try:
            await live.send_audio(chunk, mime_type="audio/pcm;rate=16000")
        except LiveSessionClosedError:
            return


async def consume_events(live):
    async for event in live.events():
        match event:
            case LiveAudio(data=data, mime_type=mime, epoch=epoch):
                ...  # caller queues playback, retaining its epoch
            case LiveTranscript(segment_id=segment, text=text, final=final):
                ...  # replace this segment's displayed text
            case LiveInterrupted():
                ...  # caller clears queued playback


async with client.live.connect(
    notebook_id,
    source_ids=None,
    transcribe=True,
    reconnect=False,
) as live:
    tasks = [asyncio.create_task(send_microphone(live)), asyncio.create_task(consume_events(live))]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()  # propagate failure; either task ending stops this example
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
```

`connect()` completes entry only after `sessionEstablished`. An establishment failure cleans up
before raising. `events()` has one consumer; a second consumer raises `RuntimeError`. Serialize
audio writes; concurrent `send_audio` calls are rejected with `RuntimeError` rather than inventing
microphone ordering. A successful send means the local gRPC write completed, not that the server
acknowledged the chunk or generated a turn. Stopping the producer does not end the session. Context
exit cancels the RPC explicitly; half-close is not the teardown mechanism.

### Parent-client ownership and admission

Use a dedicated first-party channel under the existing Android credential owner, with an epoch-bound
Live session registry. Never retarget the orchestration channel. Admit and atomically register
creation before dispatch, then release the ordinary RPC permit; registration transfers lifetime
ownership to the feature. A Live session does not hold an ordinary RPC permit for its lifetime. The
initial policy permits one Live session per client; another concurrent connect fails with
`ConfigurationError`.

Admission/registration and drain must be serialized: once draining starts, no new session can
register. Add an idempotent, epoch-specific feature shutdown operation that marks sessions closing,
fences sends/reconnects, cancels their RPCs and joins their worker tasks before the parent waits for
idle admissions or closes channels. Graceful close can invoke it through the existing feature hook.
Today's `ClientLifecycle.drain()` does not execute those hooks: explicitly extend its orchestration
with a Live shutdown step after stopping admission and before waiting for idle. Do not
indiscriminately run unrelated close-only hooks during drain. Forced close must invoke the same Live
shutdown operation before channel teardown. Drain returns successfully only after Live workers
settle. Cleanup must not require a fresh admitted RPC or the caller to consume queued events. Do not
retain lifetime admission in the application task, which would conflict with the shutdown guard.

A session belongs to one client generation. Parent drain/close permanently closes it; client reopen
never revives it. Close, drain and session exit are idempotent, and all shutdown paths wake blocked
operations. A drain/close timeout must follow the parent's existing cancellation/settlement contract
rather than orphan a Live worker; test `max_concurrent_rpcs=1` and close/reopen races.

### Events, transcripts and ordering

Provide established, audio, transcript, interrupted, turn-complete and go-away events. Reserve
reconnecting/reconnected events for the gated reconnection implementation. Every event carries a monotonically increasing local sequence number and
connection epoch. These are SDK identities, not asserted server fields. Preserve an observed
`generationId` as optional correlation data, without assuming its presence or continuity.

`LiveTranscript` has role `"user"` for input transcription or `"model"` for output, an opaque
session-local `segment_id`, full replacement `text`, and `final`. Successive events with the same
segment ID replace text; consumers never append those events. The decoder must normalize the
qualified wire framing into this contract, whether the wire uses deltas or snapshots. No heuristic
prefix detection is permitted. Qualifying input/output framing, revisions and segment boundaries is
a release gate.

`final=True` means the server explicitly marked that transcription finished; turn completion does
not synthesize it. A role has at most one open segment per epoch. An explicit finished marker closes
its segment; model turn-complete or interruption also retires the model segment, which may remain
non-final. A model boundary does not retire an input segment. Reconnection retires all open segments
without inventing finality. A later segment gets a new ID. Transcript closure caused by
interruption, turn completion or epoch replacement is conveyed by that lifecycle event. Output
`finished=True` was not observed and is not required for progress.

Define expansion of one server message explicitly, since it may contain several fields. First update
the private token and emit established if present. For its content, emit input transcription,
interruption, output transcription, audio and turn-complete in that order, then go-away. This is a
chosen public ordering, not an assertion that protobuf fields have an intrinsic order. An
interrupted content message invalidates queued output audio for its current epoch/generation before
publishing interruption; audio in that same content message is discarded. Subsequent content can
begin a new output segment. The exact combined-message cases must be qualified and tested before
release. Unknown fields are retained internally and never assigned guessed event meanings.

### Flow control and terminal outcomes

Use independent reader and serialized writer workers. Bound queued input to 256 KiB, output audio
to 512 KiB, pending events to 256, and one wire response to 1 MiB. These are SDK limits; make tuning
explicit in the implementation API. Count pending writer bytes against the input bound, and reject a
chunk larger than that bound before copying or queueing it. Awaiting capacity applies backpressure
to the producer. Do not copy unlimited caller buffers while waiting for capacity.

The reader never waits for space in the consumer's event queue. It first removes invalidated queued
audio on interruption. If an otherwise valid event still cannot fit its audio/event bound, fail the
session with a `LiveBufferOverflowError(NotebookLMError)`, cancel the call and clear
buffered output. Do not silently drop valid audio, reorder controls ahead of still-valid received
events or promise bounded interruption latency when the application stops consuming. Terminal
failure wakes the consumer outside the full queue; its caller must clear playback on failure as well
as interruption. Bounds also apply to non-audio payloads and processing: reject an oversized wire
response before decoding.

A permanent read/write error, decoding/overflow failure or exhausted reconnect policy publishes one
sanitized terminal failure, cancels its peer worker and wakes all blocked calls. Discard buffered
events on failure; the next `events()` advance and all pending/future sends raise that terminal
failure. Subsequent iterator advances retain the failure. Session exit joins workers and raises an
unobserved terminal failure when the body has no exception; it does not mask a body exception or
caller cancellation. Mark the failure observed when delivered by either public operation, avoiding a
second surprise raise at exit after the caller handled it. Do not retain credentials or raw
protobufs in errors, reprs or telemetry.

Explicit session/parent close is normal termination: discard buffers, end the event iterator and
reject sends with the `LiveSessionClosedError(NotebookLMError)`. A clean server EOF does
the same after delivering already buffered events; do not invent a turn-complete event. Caller
cancellation propagates `CancelledError` after owned cleanup. An individual canceled send with
uncertain dispatch terminates the session rather than leaving an orphan write; canceling/closing an
active event consumer closes the session too. Worker cleanup must complete without depending on
queue space or user tasks.

### Reconnection and credentials

Default `reconnect=False`: go-away is delivered, then closes the session; a transport failure
terminates it. `reconnect=True` currently raises `UnsupportedOperationError` before admission. It cannot ship
until natural go-away and continuity qualifications pass. Keep tokens private and update them when supplied.

With reconnect enabled, natural go-away or `UNAVAILABLE` starts a replacement connection using the
latest token and the same notebook/source/transcription settings. No token means terminal failure;
do not silently start a fresh conversation. Use at most three attempts within 30 seconds per outage,
with cancelable backoff; reset that budget only after an established connection makes progress.
`INTERNAL`, invalid-token failures, auth refusal and quota refusal are terminal, not generic
reconnect triggers. On `UNAUTHENTICATED`, invalidate the bearer for the next explicit session; do
not replay the failed session as an authentication retry.

Entering reconnect discards unsent input and queued output audio, retires transcript segments,
increments the connection epoch and emits `LiveReconnecting` before any replacement output. A send
started in an earlier epoch is never replayed and raises the proposed
`LiveAudioUnavailableError(NetworkError)` with `delivery="unknown"`; this is a per-send failure
while reconnection is pending, not a terminal session failure. Calls begun during reconnect raise
that same type immediately with `delivery="not_sent"`, so a microphone producer can discard the
chunk and continue without buffering stale input or treating recovery as a fatal producer error.
After establishment, publish `LiveReconnected` and accept new input. Old-generation callbacks cannot
write, enqueue events or update current tokens. A replacement failure that exhausts policy follows
the shared terminal contract. Callers clear already handed-off playback at the reconnect event.

Acquire a fresh valid bearer from the existing provider for each new connection. Do not assume token
lifetime or mutate authentication headers of an established RPC. Session runtime has no inherited
short unary deadline; use explicit bounded establishment/reconnect/cleanup windows and honor an
enclosing operation's earlier absolute deadline. Reconnect never resets that outer deadline.
Expiration handling for an established stream remains a qualification gate.

### Compatibility and error mapping

Keep raw Live bindings private and omit client-managed `liveUseCount`, whose omission succeeded.
ICE/SDP is separate; MCP and REST are out of scope.
Use existing Android error classes and sanitized status mapping, including `RateLimitError` for
`RESOURCE_EXHAUSTED`; there is no existing `QuotaExceededError` hierarchy. A more specific quota
classification requires observed server details and a separate explicit API decision. Never
interpret invalid-token `INTERNAL` as quota exhaustion. `LiveBufferOverflowError`,
`LiveSessionClosedError` are public error types. `LiveAudioUnavailableError` remains a proposed
reconnection-only type and is not exposed by the initial implementation. `RuntimeError` is reserved for programmer concurrency misuse (multiple consumers
or simultaneous writers), consistent with existing client lifecycle guards. Normal closure and
reconnect refusals remain catchable through `NotebookLMError`, following
[ADR-0019](0019-error-and-return-contract.md). Terminal transport errors retain existing mapped
types and never use the non-terminal audio-unavailable type.

### Initial implementation and remaining gates

`client.live.connect(...)` implements the non-reconnecting path. A dedicated writer
operation and reader task run independently. One serialized audio write is allowed
in flight, bounded by the input byte limit; the write itself supplies backpressure
without a separate input queue. Additional concurrent writers raise `RuntimeError`.
Queued transcript text also has an aggregate byte bound equal to the wire-response
limit. The event iterator has explicit `aclose()` and retains terminal failure on
later advances. Context exit remains the final owner when an iterator is abandoned.

Captured input transcription is full snapshots, including repeated text and a
finished marker; output transcription is deltas. The decoder replaces input text
and concatenates output deltas by segment. Controlled tests qualify revisions and
combined-message expansion without inferring framing from text prefixes. Natural
combined-field cases and additional cohorts remain qualification work.

The SDK live trial established a session, sent synthetic PCM, received input
`finished=true`, verified the model repeated both synthetic source facts, observed
328,322 output audio bytes, and settled all workers. Its disposable notebook was
deleted and the original notebook set preserved. These results do not qualify
natural reconnection or native media quality.


Before declaring the API stable:

1. Completed: fix generic protobuf-adder recovery with focused tests and regenerate schema
   evidence, including `LiveBlob.data=2`. Preserve unknown fields without guessing names.
2. Qualify transcript delta/snapshot/revision behavior and input/output boundaries; qualify
   combined-field messages, interruption correlation and generation handling.
3. Qualify natural go-away, token expiry, resume continuity, deadlines and cleanup before exposing
   opt-in reconnect. Token acceptance alone is insufficient.
4. Verify `source_ids=None`, explicit filters and another account/cohort with synthetic disposable
   notebooks. A one-source success does not prove filtering.
5. Capture naturally occurring quota errors before adding a specific classification; do not
   deliberately exhaust account quotas.
6. Initial controlled tests cover concurrent reads/writes, queue limits and overflow, blocked
   operations, failure fan-out, cancellation, EOF, event expansion, transcript normalization,
   no replay, parent drain/close/reopen, establishment/drain races and `max_concurrent_rpcs=1`,
   plus immediate Web rejection. Add reconnect retry-budget and continuity tests when enabled.
   The authenticated SDK trial used a synthetic disposable notebook.

## Consequences

Callers can build interruptible voice without WebRTC while retaining media I/O ownership. The
library owns bidirectional flow control, explicit terminal outcomes and epoch-bound session cleanup.
The bounded overflow policy favors stopping a stalled session over silent audio loss. Reconnect
remains disabled pending separate qualification; it provides no exactly-once or lossless-audio guarantee.

## Alternatives considered

**WebRTC signaling and a shared orchestration target.** The inspected Live path needs neither ICE
nor SDP; its method is unresolved on the orchestration host.

**Bundled recording, playback and codecs.** Callers already own media I/O; native media dependencies
would expand the library beyond the qualified transport contract.

**Lifetime RPC permits.** Feature ownership avoids monopolizing ordinary RPC capacity.

**Unbounded queues, silent drops or input replay.** These hide latency or risk lost/duplicate audio.
Bounded failure and explicit per-send delivery uncertainty make those costs observable.
