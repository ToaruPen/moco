# AGENTS.md

## Why

moco is a macOS-first push-to-talk voice agent. Codex Realtime owns the
conversation and work, while Irodori supplies the spoken Japanese voice. The
first release must remain usable, observable, and privacy-preserving without
adding the deferred long-term-memory layer.

## What

- `src/moco/`: typed Python runtime, integrations, CLI, and loopback web server
- `src/moco/web/static/`: browser microphone and audio-playback companion
- `tests/`: unit tests and explicitly marked boundary/live tests
- `config/moco.example.yaml`: complete non-secret configuration reference
- `docs/`: accepted design and implementation plans

`docs/` is authoritative for product behavior. Do not implement Mem0 or other
long-term memory before its tracked issue is accepted.

## How

Use Python 3.13 through `uv`. Run repository commands through `just`:

- `just sync`: install Python and browser tooling
- `just format`: apply deterministic formatting
- `just test`: run the normal test suite
- `just check`: run all local and CI quality gates
- `just doctor`: inspect local Codex, Irodori, and hotkey readiness
- `just serve`: run the foreground operator service

For behavior changes, work test-first: demonstrate RED, implement the smallest
complete change, then refactor while GREEN. Keep boundary contracts typed and
strict. Reject unknown configuration keys.

Never commit credentials, transcripts, audio, generated speech, or memory
contents. Telemetry may contain bounded metadata and stable error codes only.
Bind the operator server to loopback. Treat changes to remote Irodori hosts,
launchd services, GitHub state, and releases as externally visible operations.

Before completion, run `just check`. Use `bash scripts/check-dotfiles.sh` only
when changing the separate dotfiles repository.
