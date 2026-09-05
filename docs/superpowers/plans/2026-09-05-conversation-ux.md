# Conversation UX Implementation Plan

> **For agentic workers:** Use executing-plans for implementation. The user subsequently authorized polishment, ai-slop-cleaner, PR creation, and merge on 2026-09-06.

**Goal:** Make the next voice action clear and preserve the position of a reader browsing earlier conversation.

**Architecture:** Extend the existing TranscriptView and wire its guidance to existing connection/control events. Keep the backend and media lifecycle unchanged. Use existing CSS tokens and native DOM controls.

**Tech Stack:** Vanilla JavaScript, CSS, Node test with jsdom, Playwright Test, Python through uv/just.

## Task 1: Define behavior with failing tests

- [x] Add `tests/js/conversation.test.js` for TranscriptView scroll preservation, resume, clear, stable timestamps, safe text, and state-aware empty guidance.
- [x] Add a connecting-state test for `setConnectionAction` in `tests/js/app.test.js`.
- [x] Run `just test-frontend 'TranscriptView|connection action'`; verify failures describe missing behavior.

## Task 2: Implement conversation UX

- [x] Extend `TranscriptView(container, { latestButton, now } = {})` in `src/moco/web/static/app.js`. Preserve scrollTop while browsing, expose a latest button, create a time element once per utterance, and render empty guidance with `setGuidance(state)`.
- [x] Wire guidance into connection attempts, successful connection, disconnect, server state reconciliation, and local microphone controls. Use actual microphone state after reconciliation.
- [x] Update `src/moco/web/static/index.html`: add `transcript-latest`, empty guidance, named focusable logs, and a microphone-stop explanation.
- [x] Update `src/moco/web/static/styles.css`: wrap header actions, give time metadata a readable rail, bound mobile pane heights, wrap long text, and give logs visible focus.
- [x] Run `just test-frontend`; verify all existing media/control tests remain green.

## Task 3: Verify browser integration and complete

- [x] Extend browser coverage with long transcripts, scroll preservation, latest-button focus, empty-state lifecycle, and narrow-width layout. Keep fixtures synthetic and independent of credentials and live media services.
- [x] Run `just test-browser`; inspect screenshots at mobile and desktop widths with existing themes.
- [x] Run `just format`, then `just check`. Inspect the diff and newly introduced identifiers with `rg`; resolve issues inside this scope.
- [x] Report implementation, verification, and unverified live-audio behavior separately. The initial implementation was left uncommitted pending the user's publication request.

## Completion evidence

- `just check`: exit 0. Python: 2747 passed, 4 skipped, 7 deselected; coverage 90.27%. JavaScript: 229 passed. Chromium/WebKit: 36 passed. Static checks, secret scan, and package build passed.
- Visual checks: light, dark, high contrast; 320/390/430/820/1280px. Long-state header regression additionally covers 1121/1180px.
- Fable provided design feedback through the authenticated Claude CLI, model `fable`, with read-only tools. Native code review found and reverified fixes for microphone guidance during delayed device setup and long-state header overflow.
- Live microphone, Codex, and Irodori connections were not exercised. All browser transcript fixtures are synthetic.
- Initial implementation branch: `codex/conversation-ux`. Publication was subsequently authorized on 2026-09-06.
