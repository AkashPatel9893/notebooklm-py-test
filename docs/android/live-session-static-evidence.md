# Live Voice static and wire evidence for #2509

Investigation date: 2026-10-24. Evidence combines static Android AOT inspection with
authenticated direct gRPC probes. These are host-side probes, not an interception
of a running current Android app. The proposed API is recorded separately in
[ADR-0040](../adr/0040-live-voice-session-api.md).

## Result

The inspected app and the probed backend implement **Live Voice with bidirectional
gRPC carrying audio bytes**, on **`labstailwind.pa.googleapis.com:443`**. Its
WebRTC ICE/SDP implementation belongs to a separate interactivity
path. Combining the three RPCs into one mandatory WebRTC lifecycle is not supported
by these call sites.

| Question | Result | Evidence |
|---|---|---|
| Is `StreamLiveSession` bidirectional? | Yes: one open RPC accepted setup and successive PCM requests while returning audio and events. | L1, W1 |
| What links SDP to the notebook Live session? | No SDP is needed: setup identifies the project. The response correctly used synthetic facts from that notebook. | L2, L3, L4, W1 |
| Does the audio-data field exist? | Yes: `LiveBlob.data`, bytes tag 2, was populated in both directions. | L2, W1 |

This changes the proposed scope from WebRTC signaling to a possible **audio-byte
session API**, with callers still responsible for recording, playback and codecs.
It does not require adding device audio or native WebRTC dependencies to the library.

## Artifact identity and reproducibility

The repository's `notebooklm.apk/` directory contains `1.46.7.940945420`, code
`138238`, not `1.55.10`. Its ARM64 AOT library hashes to
`082d75e36eb03aea7ea5a8c252029c48b964177311ca4ebac6392814b8e6f81f`.
That matches the existing local Blutter `out/full` analysis workspace.

The retained `out/1.55.10` analysis existed, but its staging symlinks pointed to
a missing extraction. An exact-version package was downloaded from the
[APKPure version page](https://apkpure.net/gemini-notebook/com.google.android.apps.labs.language.tailwind/download/1.55.10.971450265),
using its linked download with `versionCode=153888`.

| Item | Value |
|---|---|
| XAPK version metadata | `1.55.10.971450265`, code `153888`, ARM64 |
| Downloaded XAPK SHA-256 | `67fafb471ffa50379b36e8a33879292a7ff52eb04f4460d33f54db28dc51ad78` |
| AOT member | `config.arm64_v8a.apk!lib/arm64-v8a/libNotebookLM_prod_android_library_flutter_artifacts.so` |
| AOT SHA-256 | `77bff7507e393c092b78ff1756bb3d726881050b22728dcc8c46cf0fecd7cda7` |
| Persistent extraction | `/Users/blackmyth/src/notebooklm-apk-1.55.10/` |
| Existing analysis | `/Users/blackmyth/src/blutter-nblm/out/1.55.10/` |

The AOT hash exactly matches the previously documented regeneration identity in
[README.md](README.md#current-regeneration-identity). The existing decompilation
was reused; it was not regenerated during this investigation. `apksigner verify`
passed for the base and both splits. The base signer SHA-256 is
`ba49176908275f83be9ae1034968f0b18e65177a64e5a40b3a621f148dfb6fa2`, matching
the earlier [signed-package audit](grpc-capability-and-signature-evidence.md).
The hash match binds
the downloaded AOT member to the previously inspected artifact, not to a current
Play Store release.

Evidence below uses paths relative to `out/1.55.10/asm/` unless stated otherwise.
Addresses are Blutter code addresses, not APK ZIP offsets. They are scoped to
the hash above. REA's Android tool reported a missing `REA_JADX_MCP_JAR`; the
analysis therefore used existing Blutter assembly and object-store output.
Shared AOT setters can have misleading names from code folding (for example,
`Any::typeUrl=` called on another message). Receiver types, field slots and
BuilderInfo registrations must corroborate those labels.

## Finding ledger

### L1: Explicit bidirectional gRPC declaration

`google.internal.labs.tailwind.api.v1/live_session_rpc_client.dart`,
`ProtoLiveSessionService::streamLiveSessionRequest`:

```text
0x140f334  loads Obj!StreamingType@358bd71
0x140f33c  stores it in ProtoRpcRequest.field_33
```

`out/1.55.10/objs.txt:101278` resolves that exact object:

```text
Obj!StreamingType@358bd71
  off_8: int(0x3)
  off_10: "biDiStreaming"
```

The request path is
`/google.internal.labs.tailwind.api.v1.LiveSessionService/StreamLiveSession`.
The resource factory in
`labs.language.tailwind.mobile.app.services.live_session_stream/live_session_stream_service.dart`
selects binary serialization (`0x19cc078`), `Transport@358be31` (`0x19cc098`,
resolved to `grpc` in `objs.txt:101320`), and a `GrpcHttp2Channel` binding
(`0x19cc170`). This is stronger evidence than inferring cardinality from a name.

The older hash-matched `out/full` client also stores
`StreamingType@3302191` at `0x14c5300`; `out/full/objs.txt:97983` resolves it to
`biDiStreaming`. The server-streaming statement in `endpoints.md` was incorrect.

### L2: Audio bytes exist and the caller supplies them

`google.internal.labs.tailwind.api.v1/live_session.pb.dart`, `LiveBlob::_i`:

```text
0x16e72a4..0x16e72b0  aOS(1, "mimeType")
0x16e72b4              TypeArguments: <List<int>>
0x16e72c0              field number 2
0x16e72c4              "data"
0x16e72cc              field type 32
0x16e72d8              BuilderInfo::a
```

The recovered fragment is:

```proto
message LiveBlob {
  string mimeType = 1;
  bytes data = 2;
}
```

`labs.language.tailwind.mobile.app.pages.live_voice/live_voice_component.dart`:
`_createAudioInputStream` selects `audio/pcm;rate=16000` at `0x140fa14`;
its `Uint8List` mapping closure (`0x140fbec`) constructs `LiveRealtimeInput`
and `LiveBlob`, calls `GeneratedMessage::$_setBytes` at `0x140fc44`, and
attaches the blob. `LiveSessionStreamService` wraps successive inputs as
`LiveSessionClientMessage.realtimeInput` and adds them to its request controller
(closure `0x140f728`). Setup is enqueued before those inputs.

The older build also registers `data=2` at `0x1483654..0x148366c`.
The omission is in the recovered schema, not an absent field in either binary.
`scripts/parse_pbschema.py` recognizes specialized bytes adders but its `ADDERS`
table does not include generic `BuilderInfo::a`. A generic-adder recovery fix
and schema regeneration should be a separate, tested change; blindly treating
every `a()` call as bytes would be incorrect.

### L3: Live Voice binds the notebook in setup and receives audio

`LiveSessionStreamService::streamLiveSession` (`0x140ee38`) constructs setup
with `projects/<id>`, input/output transcription settings, source IDs,
`liveUseCount`, and a reconnection token. Its input is a stream, not one request.
`LiveVoiceComponent::_subscribeToLiveStream` (`0x140e124`) builds the audio
input stream and invokes this service at `0x140e210`.

On the response side, `_onServerContent` invokes `_handleModelTurn` at
`0x14102e4`. Its playback closure (`0x1410d5c`) reads inline blob byte slot 1
(tag 2), converts a list to `Uint8List` if needed (`0x1411024`), and queues
playback. `LiveVoiceAudioManager` calls `LiveVoiceAudioService::playChunk`
at `0x1411344`; the latter calls `enqueuePlaybackPcm` at `0x1411438`.
This is compiled audio handling in both directions, not just transcript handling.

### L4: ICE/SDP is a distinct interactivity path

`labs.language.tailwind.mobile.app.services.rpc/interactivity_service.dart`:

- `_sendSdpOffer` (`0x145f99c`) builds an SDP request and request context,
  then calls orchestration `sendSdpOffer` at `0x145fa34`.
- `createSession` (`0x14600d4`) calls `_getIceConfig` at `0x14601d4`,
  supplies `_sendSdpOffer` to `Webrtc::createSession` at `0x1460250`, and
  configures a `BasicAgentModule` with captions, conversation and custom messages.
- Callers include `services/interactive_audio_player.dart` and
  `services/tailwind_service.dart`. No `InteractivityService` reference was found
  in the inspected Live Voice component or Live session stream service.

These are bounded static call-site observations. They do not prove that no
other build or dynamically selected path could combine the transports. They do
show that an SDP-to-`StreamLiveSession` binding is not a prerequisite in the
Live Voice path inspected here. The Web counterpart's artifact binding was later
recovered statically; see [Web Interactive Audio Overviews](#web-interactive-audio-overviews).
An Android interactivity runtime capture remains separate from these findings.

### L5: Interruption, resumption, usage and refusal handling

In `live_voice_component.dart`:

- `_onServerContent` tests `LiveServerContent.interrupted` at `0x1410574`
  and calls `clearPlayback` at `0x1410670`. A Python consumer needs an
  interruption event so its own playback queue can be cleared.
- The server-message handler reads `reconnectionToken` at `0x14100c0`.
  `_onGoAway` (`0x1411cf4`) initiates the reconnection loop at `0x1411e3c`.
  Setup writes the reconnection token at `0x140f054` in the stream service.
  This supports internal resumption management, but does not establish
  lossless replay or audio-delivery guarantees.
- `_subscribeToLiveStream` reads `UserPreferences::liveUseCount`
  (`0x140e1f4`), and `_onSessionEstablished` calls
  `UserPreferences::incrementLiveUseCount` (`0x14119e8`). There is direct
  compiled evidence of client-managed counting; its backend purpose is unknown.
- `_mapGrpcError` compares code 8 at `0x140d90c` and maps it to
  "Usage limit reached" at `0x140d914`. This is a client error mapping,
  stronger than banner IDs but still not an observed server refusal/status detail.

## API implications and remaining gates

Keep the context-managed session and typed events proposal, but redesign it
around a bidirectional audio stream: an input byte method/iterator, output audio
events, transcripts, interruption and lifecycle events. Do not require
`ice_servers` or `negotiate()` for this path. Keep codecs/device I/O outside the
library. Treat automatic reconnection as policy with bounded retries and an
opt-out; specify what happens to queued audio and playback on reconnect.

The direct probes below validate basic setup, authentication, bidirectional audio,
transcripts, interruption, cancellation and token acceptance. Remaining gates are
natural go-away/resumption, precise resume continuity, token expiry, cross-account
availability and actual quota refusal details. A current-app interception remains
useful for defaults and metadata; `1.55.10` predates the release cited in the issue.
No microphone recording, issue comments or public API implementation were performed.

## Direct backend evidence

### W1: Successful notebook-bound bidirectional audio

No device or AVD was configured. The host probe used an isolated owner-only copy of
the default auth profile, minted the existing NotebookLM Android OAuth bearer, and
created a disposable notebook containing only synthetic facts. It deleted that
notebook after the call. No real notebook content was used.

The first request to `notebooklm-pa.googleapis.com:443` returned gRPC
`UNIMPLEMENTED`, detail `GRPC target method can't be resolved.` The AOT factory's
`ServerEnvironment::getFirstPartyApiHost` is important: at `0x19ccd30` it returns
the production object's first-party field, whose value is
`labstailwind.pa.googleapis.com` (`objs.txt:103032`), rather than the normal
orchestration host. Retrying that exact compiled host resolved the route.

The successful call used:

```text
target: labstailwind.pa.googleapis.com:443
method: /google.internal.labs.tailwind.api.v1.LiveSessionService/StreamLiveSession
transport: TLS, HTTP/2, grpc.aio channel.stream_stream
application metadata: authorization: Bearer <redacted>
first message: setup(project="projects/<disposable-id>",
                     enableInputTranscription=true,
                     enableOutputTranscription=true,
                     sourceIds=[{id: "<synthetic-source-id>"}])
later messages: realtimeInput(audio={mimeType: "audio/pcm;rate=16000", data: <PCM>})
```

No request context, SDP, ICE config, explicit `liveUseCount`, custom user-agent,
or extra routing metadata was supplied. gRPC's own transport headers were used.
Omitting `liveUseCount` worked in this probe; its backend purpose remains unknown.

The client generated a question using macOS speech synthesis and converted it to
mono signed 16-bit little-endian 16 kHz PCM with ffmpeg. It sent 3,200-byte chunks
at 100 ms intervals, followed by three seconds of digital silence, while reading
responses concurrently. The question asked for the notebook's synthetic animal
and city. The model's output transcript contained both correct facts, "blue otter"
and "Cedar Bay".

| Observation | Result |
|---|---|
| Server messages | 54 |
| Established messages | 1 |
| Messages with reconnection token | 54 |
| Input/output transcription | Both observed; input `finished=true` observed |
| Output blob MIME | `audio/pcm;rate=24000` |
| Output blob payload bytes | 359,044 across 38 parts |
| `interrupted=true` | 1 message, during overlap with synthetic speech |
| `turnComplete=true` | 3 messages |
| Stop | Client canceled after final turn completion |
| Disposable notebook | Deleted successfully |

The byte total includes two 2-byte blobs; do not assume fixed-size output chunks.
`outputTranscription.finished=true` was not observed, so consumers cannot require
it before recognizing the server's turn-complete signal.

The server also populated fields omitted by the recovered schema:

- `LiveContent` tag 1, wire type 2, carried `model` in 53 responses. Its semantic
  role is apparent, but the exact protobuf field name was not recovered here.
- `LiveSessionEstablished` tag 1 contained 39 bytes. Its meaning is unqualified.
- `LiveServerContent` tag 2, wire type 0, carried `1` once. Do not invent its name
  or conflate it with the known turn-complete/interruption fields.

### W2: Reconnection token acceptance and half-close

A second disposable notebook was used for three bounded calls:

| Call | Result |
|---|---|
| Initial setup without sources | Established; returned a token; client canceled |
| Same project with that token in setup tag 8 | Established; 45 messages returned |
| Same project with deliberately invalid token | No messages; gRPC `INTERNAL`, `Internal error encountered.` |

On the valid-token call, the client half-closed its send side immediately after
establishment. Responses continued until the 12-second local deadline returned
`DEADLINE_EXCEEDED`. Half-close alone therefore did not promptly terminate this
session. Explicit cancellation is needed for bounded cleanup.

This proves token acceptance, not lossless resumption or conversation continuity.
The valid-token call did not wait for a natural go-away. Invalid-token `INTERNAL`
must not be described as a quota error or blindly retried indefinitely. The
second notebook was also deleted successfully.

### Local evidence retention

Private request/response protobufs and probe scripts are retained under
`/tmp/notebooklm-live-evidence-2509/`, with a mode-0700 parent and mode-0600 raw
message files. The `resume/` subdirectory holds W2. They include session tokens
and are intentionally outside Git. Safe aggregate observations are in
`wire-observations.json`; W1/W2 summaries record successful cleanup. The temporary
credential copy was removed after the investigation. No bearer headers were saved.

## Web Interactive Audio Overviews

A follow-up authenticated notebook-route GET recovered a Web feature bundle with
a complete static Interactive Audio Overviews implementation. This extends the
homepage-only sample, which contained signaling registrations and telemetry but
no media call sites. A subsequent [wire spike](#web-interactive-audio-wire-spike)
qualified that separate Web implementation; neither establishes general Web
notebook Live Voice through `StreamLiveSession`.

| Identity | Value |
|---|---|
| Build | `boq-labs-tailwind.LabsTailwindUi.en.bFdio5LWkVM.es6.O` |
| Acquired | 2026-10-25 UTC (2026-10-24 local) |
| Notebook feature bundle | `notebook-1.js`, 5,073,188 bytes |
| SHA-256 | `5db0d07c432be4f1ddd441f5baf46a8e643a6c20f8198a5a889799819375f2cc` |
| Last-Modified | 2026-10-22 22:31:23 UTC |
| Local evidence | `/tmp/notebooklm-web-live-2509/report.md` and `manifest.json` |

The call chain is `interactive-audio` Join UI → `InteractiveAudioService.connect`
→ `GetIceConfig` (`Of0kDd`) → parsed JSON ICE configuration → `RTCPeerConnection`
and microphone tracks → JSON SDP offer through `SendSdpOffer` (`eyWvXc`) → remote
description, remote audio tracks and a protobuf data channel. The concrete media
endpoint comes dynamically from the signaling responses; no media host is qualified.

Signaling does not carry the audio artifact ID. The UI passes its current
`artifactId` as `showId`; service `i3.lfa` packs that value into field 1 of
`gemfm.agents.GroundedAgentConfig`, with field 2 empty and field 4 true. It sends
the packed Any to `grounded_agent` through the configured data-channel stream.
The outer envelope is `c3` field 1 → `UHe` field 1 → `SHe`, whose agent name is
field 1 and packed Any is field 2. Other config field semantics remain unknown.
This binds an existing Audio Overview artifact, rather than the Android
`projects/<id>` LiveSession setup.

Reproducible zero-based UTF-8 offsets in the hash-pinned feature bundle:

| Location | Evidence |
|---|---|
| 2,098,436 | Current `artifactId` passed as `showId` |
| 2,275,190 | `navigator.mediaDevices.getUserMedia({audio:true})` |
| 2,275,960 | `new RTCPeerConnection(config)` |
| 2,277,493 | Data channel creation and offer construction |
| 2,277,842 | Signaling callback and remote description |
| 2,288,175 | Consumer GetIceConfig/SendSdpOffer adapter |
| 2,290,485–2,290,632 | Grounded agent setup and packed configuration |
| 2,594,490 | `interactive-audio` UI registration and Join controls |

No `StreamLiveSession`, `realtimeInput`, `audio/pcm` or reconnection-token path was
recovered in this feature bundle. These bounded negative searches do not prove
absence from other modules. That bundle investigation did not test runtime
availability; the later spike below did. All bundle fetches were read-only; no notebook content,
personalized HTML or credential headers were retained, and isolated credential
copies were removed. Web has a concrete shipped interactive-audio implementation;
an equivalent Web LiveSession SDK path remains unqualified.

## Web Interactive Audio wire spike

**Verified:** 2026-10-25 UTC (2026-10-24 local). The consumer Web backend supports
interactive voice for an **existing Audio Overview artifact**, on the tested
account. This is a successful protocol-level test using a headless Chromium
harness, not an execution of the product's Join UI or a qualification of all cohorts.

The user authorized replicating an existing notebook. The saved generation-test
ID was stale; a current notebook with an Audio Overview was located by read-only
listing. Each of three bounded trials used its own notebook copy, including copied
artifacts. No new Audio Overview was generated and the original notebook was not
mutated. Each copy was deleted after its trial. Final list read-back confirmed all
three copies absent, the original present, and the notebook count unchanged at 16.

Signaling used a Web-selected client and the usual cookie-authenticated
`batchexecute` executor, with temporary process-local bindings and retries disabled:

| Operation | Request | Observed result |
|---|---|---|
| GetIceConfig (`Of0kDd`) | `[]` | JSON config: `NOT_BLOCKED`, two ICE servers, policy `all` |
| SendSdpOffer (`eyWvXc`) | `[JSON.stringify({type:"offer", sdp:...})]` | JSON SDP answer accepted by Chromium |
| WebRTC negotiation | Synthetic audio track plus `data-channel` | Connection state `connected`, remote audio track and remote data channel |
| Grounded agent setup | Copied artifact ID in config, as recovered above | Overview agent transcripts, TTS and playback events |
| Join request | AgentComms client field 3 → MicrophoneEvent `{1:5}` | Server microphone event sequence `3 → 4 → 3`, user transcription and subsequent agent output |

The data-channel transport wraps protobuf payloads in chunk envelopes: outer field
2 contains a chunk with message ID tag 1, total size tag 2, offset tag 3 and bytes
tag 4; outer tag 3 indicates compression. The probe sent uncompressed setup and
Join payloads. This is distinct from gRPC framing and `LiveSessionClientMessage`.

The first trial established playback/control with a synthetic AudioContext track
but recorded zero outbound RTP. The second used Chromium's fake microphone and
verified outbound RTP, but did not request a listener turn. The final trial also
sent the app's separate Join control, recovered from `i3.yDa()`, and qualified
speech input. Its microphone was explicitly `Fake Default Audio Input`, sourced
from a generated WAV; no physical microphone or audible speaker output was used.

Final 45-second trial observations:

| Observation | Result |
|---|---|
| Inbound audio RTP | 87,509 bytes / 1,155 packets |
| Outbound audio RTP | 105,657 bytes / 2,279 packets |
| User utterance messages | 26; synthetic question recognized |
| Agent utterance messages | 145, including 96 after user speech |
| TTS / send-audio events | 11 / 46 |
| Playback / microphone events | 3 / 3 |
| Status / custom messages | 97 / 1 |
| AgentComms error events | 0 |

The synthetic question requested a one-sentence introduction to the overview's
main topic. An offline check confirmed the recognized input contained the expected
phrases without publishing the copied notebook's text or the model's answer.
Inbound RTP was observed, but headless decoded-audio sample/energy counters stayed
zero; waveform quality and audible playback were not verified. Subsequent agent
transcripts and TTS events establish response activity independently of that counter.

This qualifies the Web signaling, artifact setup, Join control, speech recognition
and response-event paths. It does not establish general notebook Live Voice,
automatic reconnection, quota errors, or parity with the Android LiveSession API.
Keep these as separate features rather than substituting one transport silently.

Scripts, private SDP/protobuf captures and safe summaries are retained under
`/tmp/notebooklm-web-live-spike-2509/`; trials 1 and 2 are in `run1/` and `run2/`.
The parent directory is owner-only, raw captures are mode 0600, and the isolated
credential copy was removed after cleanup verification. No headers were captured.

## Android Interactive Audio Overview wire spike

**Verified:** 2026-10-25 UTC. A separate 45-second live trial qualified Android's
bearer-authenticated ICE/SDP path against an existing Audio Overview copied into a
disposable notebook. This used Android gRPC signaling and a headless Chromium
WebRTC media harness, not an Android device or execution of the app UI.

Both unary calls used `client.raw.unary(...)`, replay disabled, at
`notebooklm-pa.googleapis.com:443` under
`google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService`:

| RPC | Protobuf request | Observed response |
|---|---|---|
| `GetIceConfig` | Android RequestContext in field 1 | Field 1 JSON config: `NOT_BLOCKED`, two ICE servers |
| `SendSdpOffer` | Field 1 JSON `{type:"offer",sdp:...}`, RequestContext field 2 | Field 1 JSON SDP answer; accepted by Chromium |

The same artifact-bound GroundedAgentConfig, chunked AgentComms setup and Join
control described in the Web spike worked over the negotiated data channel.
The copied artifact ID binds the session after SDP negotiation; neither signaling
request carries a notebook ID. This provides runtime evidence for the separate
Android Interactive Audio Overview path, not a link to `StreamLiveSession`.

| Observation | Result |
|---|---|
| WebRTC | Connected; remote audio track and data channel |
| Inbound audio RTP | 93,539 bytes / 1,256 packets |
| Outbound audio RTP | 101,523 bytes / 2,262 packets |
| User utterance messages | 31; synthetic question recognized |
| Agent utterance messages | 97, including 48 after user speech |
| TTS / send-audio events | 13 / 50 |
| Microphone event types | `3 → 4 → 3` after Join |
| AgentComms error events | 0 |

Only Chromium's WAV-backed fake microphone was used. No physical microphone or
audible speaker output was used. As in the Web trial, decoded-audio sample and
energy counters were zero despite inbound RTP; audible output quality remains
unqualified. Transcript and TTS events demonstrate subsequent response activity.

The first construction attempt failed at the legacy `from_storage` Web homepage
bootstrap because Web authentication had expired, before any notebook mutation.
Direct client construction with an empty Web auth shell and the isolated profile's
Android master token then succeeded; all notebook and signaling operations used
Android. No Web fallback was used in the successful trial.

The copy was deleted in `finally`; a fresh Android list confirmed it absent, the
original present and the notebook count unchanged at 16. The temporary credential
copy was removed. Owner-only scripts, private captures and safe summaries are in
`/tmp/notebooklm-android-interactive-spike-2509/`. This trial does not qualify
reconnection, quota handling, other accounts or the native app's media stack.

## Android Live Voice SDK validation

**Verified:** 2026-10-25 UTC. The implemented `client.live.connect(...)` path was
exercised with Android master-token authentication, an isolated credential copy
and a disposable notebook containing only the synthetic blue-otter/Cedar-Bay facts.
No raw gRPC call was made by the harness: setup, reads, audio writes and teardown
used the SDK session. No physical microphone or speaker was used.

An initial trial stopped at the model's early greeting before input had finished;
it qualified establishment and teardown but not the intended question/answer.
The corrected trial waited for finalized input and a completed response containing
both synthetic facts. It observed one established event, 37 audio events totaling
328,322 bytes, 14 transcript events, one interruption and three turn-complete
events. Sequence numbers increased, input `final=True` was delivered, and both
synthetic facts were present in normalized model text. All owned worker tasks
settled after session exit.

Both trial notebooks were deleted. List read-back after each trial confirmed the
notebook set unchanged. Temporary credential copies were removed. The safe final
summary and private notebook identifier are retained owner-only under
`/tmp/notebooklm-android-sdk-spike-2509/`; the harness is `/tmp/sdk_live_probe.py`.

Review of the original synthetic raw capture also qualified the framing used by
the decoder: eight input transcript messages were snapshots (lengths 4, 11, 16,
31, 36, 42, 42, 42; final marker on the last), whereas six output messages were
deltas (lengths 11, 16, 19, 13, 18, 14). No runtime prefix heuristic is used.
Controlled streams additionally test input revisions and combined-field ordering;
these are SDK contract tests, not evidence of naturally occurring server revisions
or every possible combined-field message.

The schema parser now recovers generic `a<List<int>>(..., PbFieldType.OY=32)`
fields without confusing the type-code immediate for a field tag. Regeneration
adds `LiveBlob.data=2` and `FileArtifact.fileContent=5` to the evidence schema.
Reconnection, all account cohorts, natural quota details and media waveform quality
remain outside this validation.
