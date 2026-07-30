# moco First Usable Release Design

## Goal

Deliver a public, production-shaped macOS agent that the user can launch and
use for push-to-talk conversation today:

- The configured push-to-talk control enables the microphone only while held.
- The configured cancel control immediately stops local capture, queued speech, and the current agent
  response or delegated Codex turn.
- GPT-Live owns the conversation and may delegate work to Codex.
- Assistant text is synthesized by Irodori on the existing Windows GPU host.
- An inactive conversation closes after a configurable timeout, while the moco
  process remains available for the next conversation.
- A local operator page and terminal output expose state, latency, and safe
  diagnostic events.

Long-term memory and Mem0 are deliberately excluded from this release and will
be tracked as a separate GitHub issue.

## Recovered Product Decisions

The previous design discussions established these user-visible decisions:

- moco is the long-lived product repository, not a disposable spike.
- macOS is the first supported client platform.
- ChatGPT's existing UI is not the product shell because it has no supported
  extension point for replacing speech output with local Irodori audio.
- The daemon remains alive while GPT-Live and Codex conversation sessions are
  short-lived.
- Push-to-talk may interrupt current speech.
- Cancel stops current activity without intentionally deleting conversation
  context.
- Operationally adjustable values live in strictly validated YAML. Secrets do
  not live in YAML; YAML may name environment variables or OS-managed
  credentials.
- Configuration changes apply after restart rather than through hot reload.
- The terminal is an operator console, not the owner of the daemon.
- Audio, full transcripts, prompts, and future memory content are not exported
  as telemetry.

## Approaches Considered

### 1. Productize the proven WebRTC bridge — selected

Reuse the tested Codex App Server WebRTC, transcript, Irodori, and browser audio
path from `codex-irodori-voice`. Add moco's lifecycle, global hotkeys, YAML
configuration, observability, packaging, and launchd controls around it.

This is selected because the real ChatGPT subscription authentication, Codex
Realtime connection, Windows Irodori service, microphone, interruption, and
WAV playback have already completed multi-turn live tests on the target
hardware. The first release therefore concentrates new risk in moco-specific
behavior.

### 2. Headless App Server audio transport

Use `thread/realtime/appendAudio` over the experimental App Server websocket
transport and capture/play audio entirely in Python. This can eventually remove
the browser, but it would introduce a new microphone stack, macOS audio
permissions, VAD/end-of-turn behavior, and an unproven Realtime transport in
the same milestone.

The architecture keeps the media/session boundary replaceable so this can be
implemented later without changing orchestration or Irodori.

### 3. Native macOS application

Build a Swift menu-bar application with native hotkeys and audio. This offers
the best eventual desktop integration but delays the first usable release and
duplicates already proven browser media behavior.

## Runtime Architecture

```text
macOS launchd
  └─ moco runtime
      ├─ configurable global hotkey source
      ├─ local operator HTTP/WebSocket server
      ├─ conversation lifecycle controller
      ├─ Codex App Server adapter
      ├─ Irodori HTTP adapter
      └─ safe telemetry/logging
             │
             ├─ local browser media companion
             │    ├─ microphone WebRTC track
             │    └─ Irodori WAV playback
             │
             ├─ ChatGPT.app Codex App Server
             │    └─ GPT-Live ↔ delegated Codex work
             │
             └─ Tailscale HTTP
                  └─ Windows Irodori API
```

The local server binds only to loopback. A per-process capability token is
passed in the URL fragment and WebSocket subprotocol, following the proven PoC
boundary. The token is never placed in HTTP paths, query strings, or persistent
logs.

## Components

### Configuration

`moco config init` creates a YAML file at the standard macOS application
support location. `moco config validate` reports field-level errors without
starting external services.

The initial schema contains:

- server loopback host and port;
- push-to-talk/cancel key bindings and whether global hotkeys are enabled;
- idle timeout;
- ChatGPT.app Codex binary and working directory;
- Irodori base URL, portable speaker name, synthesis parameters, timeout, and
  maximum WAV size;
- transcript segmentation limit;
- telemetry console/OTLP settings and safe service metadata.

The schema rejects unknown keys. URLs with embedded credentials, non-loopback
operator binds, invalid timeout/range values, and missing absolute paths fail
closed.

### Codex adapter

The adapter starts the ChatGPT.app-bundled `codex app-server` over stdio and
uses the binary's experimental API capability. Each conversation receives an
ephemeral, read-only, no-approval thread and a Realtime v3 WebRTC session.
Startup context is disabled; moco supplies a short speech-oriented developer
prompt.

The adapter consumes user/assistant transcripts and Realtime errors. It also
tracks delegated turn identifiers so cancel can issue `turn/interrupt` where
possible. Realtime methods remain isolated behind a protocol so a future
stable transport can replace them.

### Browser media companion

The page is a minimal operator surface, not a separate chat product. The user
performs one explicit enable action so the browser can grant microphone and
audio playback permission.

After enablement:

- the microphone track starts disabled;
- push-to-talk down stops current local playback, enables the track, and displays
  `recording`;
- push-to-talk up disables the track and displays `working`;
- disabled WebRTC tracks continue sending silence, allowing server VAD to
  finalize the utterance;
- cancel disables the track, clears pending/playing WAV audio, suppresses stale
  assistant speech, and requests cancellation from the backend;
- the next completed user transcript ends cancellation suppression.

The page shows connection state, current lifecycle state, safe error codes,
user/assistant transcript for the current process lifetime, and latency
metrics. It does not persist transcript content.

### Hotkeys

A process-local hotkey service listens for configured global key events and broadcasts
typed control messages to the connected operator page. Duplicate key-down
events caused by key repeat are ignored. Loss of the hotkey listener is
reported as a degraded state; the page retains equivalent visible controls.

macOS may require the user to grant Input Monitoring permission to the process
that launches moco. The doctor command detects the listener failure and gives a
specific remediation message.

### Conversation lifecycle

The lifecycle states are:

```text
disabled → ready → connecting → listening/recording
                             → working → speaking
                             → cancelling
                             → idle-expired → ready
                             → error
```

Activity is refreshed by push-to-talk, finalized user input, assistant output, delegated
work progress, synthesis, and playback. The idle timer runs only when no
recording, delegated work, synthesis, or playback is active.

On expiry, moco closes the Realtime session and ephemeral Codex thread
connection but keeps the operator WebSocket and hotkey service alive. The next
Push-to-talk creates a fresh media and conversation session. No prior transcript is
automatically injected.

### Irodori adapter

The first release intentionally pins the API contract already proven by
`codex-irodori-voice`, rather than adopting the unverified VoiceDesign v3 work
in the dirty local infrastructure checkout.

The adapter:

- checks `/health` and requires `model_loaded=true`;
- sends portable speaker names to `/synthesize`;
- never sends Windows-local embedding paths;
- limits response bytes before and after decoding;
- validates complete RIFF/WAVE framing;
- cancels local delivery and discards stale generations on interruption.

GPU inference may continue after HTTP cancellation; stale audio is never
played.

### Observability

The runtime emits structured, redacted events and OpenTelemetry spans for:

- process and operator connection lifecycle;
- hotkey down/up/cancel;
- conversation start/stop/idle expiry;
- Codex connection and delegated work;
- Irodori health and synthesis;
- playback, barge-in, timeout, retry, and error boundaries.

Events include duration, state, stable error code, and trace ID. They exclude
audio bytes, transcript text, prompts, URLs containing credentials, account
identity, capability tokens, and future memory content. Console export is
available for foreground operation; optional OTLP export is configured in
YAML.

## CLI and Installation

The public CLI surface is:

```text
moco config init
moco config validate
moco doctor
moco run
moco open
moco service install
moco service start
moco service stop
moco service status
moco service uninstall
```

`moco run` is the foreground path used for first permissions and live
verification. `service install` writes a user LaunchAgent pointing at the
repository's installed moco executable, with logs in the standard macOS user
log directory. Uninstall removes only the exact moco LaunchAgent created by
this CLI.

The README presents one golden path: clone, sync, initialize YAML, set the
Irodori Tailscale URL, run doctor, run moco, click enable once, then hold the configured push-to-talk key.

## Error Handling

- Configuration errors stop before any process or network connection starts.
- Codex startup, account, feature, and Realtime failures use stable,
  user-facing doctor codes.
- Irodori not-ready is distinct from unreachable and synthesis-failed.
- A failed conversation returns to `ready` without terminating the long-lived
  runtime.
- Repeated invalid browser messages close only that capability-bound client.
- Cleanup is idempotent and cancels pending synthesis, notification consumers,
  and child processes.

## Test Strategy

Tests are written before changed behavior and use fakes at every external
boundary.

- Unit tests: strict YAML parsing, state transitions, idle eligibility,
  hotkey de-duplication, cancellation suppression, transcript segmentation,
  response limits, and redaction.
- Contract tests: fake Codex JSON-RPC process and fake Irodori HTTP server.
- Browser tests: Node DOM/Web API fakes for semantic controls, idle restart, audio
  generation invalidation, and WebRTC track enablement.
- Integration test: fake Codex SDP/transcripts → fake Irodori WAV → browser
  WebSocket delivery.
- Live doctor: real ChatGPT account/features/voices and real Irodori health.
- Live acceptance: Chrome microphone, a temporary F1/F2 feature-test mapping, multi-turn conversation, idle
  session renewal, Irodori playback, and a delegated Codex request.

Normal CI never requires a real account, microphone, Tailscale, GPU, or
StackChan.

## Repository and Quality Baseline

- Python 3.13, uv, a single typed package, and MIT license.
- `AGENTS.md` is canonical; `CLAUDE.md` is a symlink.
- Ruff, mypy strict, vulture, deptry, pytest branch coverage, Biome for browser
  assets, secretlint, and ast-grep structural checks.
- `just check` is the common local and CI quality gate.
- GitHub Actions use minimal permissions, immutable action SHAs, concurrency
  cancellation, Ubuntu quality checks, macOS mocked integration, and tag-based
  wheel/sdist artifacts.
- The GitHub repository is public.

## Explicit Non-goals

- Mem0, memory retrieval, retention, decay, consolidation, or deletion.
- StackChan device integration.
- VoiceDesign v3 migration.
- A native macOS UI.
- Windows or Linux client support.
- Cloud hosting, accounts, mobile UI, transcript persistence, or analytics.
- Automatic publication to PyPI.

## Acceptance Criteria

1. A new checkout completes the documented setup with no undocumented manual
   file edits.
2. `moco doctor` confirms the local Codex account/features and Windows Irodori
   readiness without exposing sensitive values.
3. After one browser permission action, holding the configured push-to-talk key records and releasing it
   produces an Irodori-spoken GPT-Live response.
4. Push-to-talk during playback stops old audio before accepting new speech.
5. Cancel stops capture and local output, invalidates stale synthesis, and sends
   cancellation to active backend work.
6. The configured idle timeout closes only the conversation; the next push-to-talk starts
   a new conversation without restarting moco.
7. Foreground terminal output shows safe state/latency/trace events without
   audio or transcript content.
8. The complete local quality gate and mocked integration tests pass.
9. A live multi-turn smoke test succeeds on the target Mac and Windows GPU
   service.
10. The public GitHub repository contains setup, troubleshooting, security, and
    experimental-API caveats, plus a separate long-term-memory issue.
