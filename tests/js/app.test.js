import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { describe, it } from "node:test";

import { JSDOM } from "jsdom";

import { ActivityBuffer, ProgressTracker } from "../../src/moco/web/static/activity.js";
import * as appModule from "../../src/moco/web/static/app.js";
import { EDITABLE_TOKENS } from "../../src/moco/web/static/theme.js";

const {
  AudioPlaybackQueue,
  beginAudioActivation,
  BrowserHotkeyMapper,
  closeAudioContext,
  connectionCloseErrorCode,
  connectionSetupErrorCode,
  closeDisconnectedMedia,
  closeSocketForFailure,
  ConversationHandshake,
  MocoController,
  OperatorStatus,
  PairingPanel,
  renderPresetChoices,
  resetConnectionAttempt,
  setConnectionAction,
  setTransportOffline,
  shouldHandleHotkey,
  ThemePanel,
  TranscriptView,
  VoiceModelController,
  waitForIce,
  waitForSocketOpen,
  watchPeerFailure,
} = appModule;

describe("TranscriptView", () => {
  it("replaces an active utterance with each authoritative text update", () => {
    assert.equal(typeof TranscriptView, "function", "TranscriptView must be exported");
    const dom = new JSDOM(`
      <section id="transcript"><p class="transcript-empty">Empty</p></section>
    `);
    const container = dom.window.document.querySelector("#transcript");
    const view = new TranscriptView(container);

    view.update("user", "きょは", false);
    view.update("user", "今日は", true);

    assert.equal(container.querySelectorAll(".utterance").length, 1);
    assert.equal(container.querySelector(".utterance-text").textContent, "今日は");
  });
});

describe("AudioPlaybackQueue", () => {
  function createPlaybackQueue(context, onState, onError = () => {}) {
    const timers = {
      set(callback, delayMs) {
        context.currentTime += delayMs / 1000;
        callback();
        return 0;
      },
      clear() {},
    };
    return new AudioPlaybackQueue(context, onState, onError, timers);
  }

  it("resumes a suspended context before decoding and tracks correlated playback", async () => {
    const order = [];
    const states = [];
    const source = new EventTarget();
    source.connect = () => {};
    source.start = () => order.push("start");
    const context = {
      state: "suspended",
      currentTime: 0,
      destination: {},
      async resume() {
        order.push("resume");
        this.state = "running";
      },
      async decodeAudioData() {
        order.push("decode");
        return { duration: 0.1 };
      },
      createBufferSource: () => source,
    };
    const metadata = { audioId: 7, generation: 3 };
    const queue = createPlaybackQueue(context, (active, correlated, contextState, phase) =>
      states.push({ active, metadata: correlated, contextState, phase }),
    );

    queue.enqueue(new ArrayBuffer(1), metadata);
    await assert.doesNotReject(queue.chain);

    assert.deepEqual(order, ["resume", "decode", "start"]);
    assert.equal(queue.sources.has(source), true);
    assert.deepEqual(states, [
      { active: true, metadata, contextState: "running", phase: "started" },
    ]);

    source.dispatchEvent(new Event("ended"));

    assert.equal(queue.sources.has(source), false);
    assert.deepEqual(states.at(-1), {
      active: false,
      metadata,
      contextState: "running",
      phase: "completed",
    });
  });

  it("reports every source completion while aggregate playback remains active", async () => {
    const events = [];
    const activity = [];
    const sources = [new EventTarget(), new EventTarget()];
    for (const source of sources) {
      source.connect = () => {};
      source.start = () => {};
    }
    const context = {
      state: "running",
      currentTime: 0,
      destination: {},
      async decodeAudioData() {
        return { duration: 0.1 };
      },
      createBufferSource: () => sources.shift(),
    };
    let playbackActive = false;
    const queue = createPlaybackQueue(context, (active, metadata, contextState, phase) => {
      events.push({ active, metadata, contextState, phase });
      if (playbackActive !== active) {
        playbackActive = active;
        activity.push(active ? "started" : "completed");
      }
    });
    const first = { audioId: 1, generation: 0 };
    const second = { audioId: 2, generation: 0 };
    const [firstSource, secondSource] = [...sources];

    queue.enqueue(new ArrayBuffer(1), first);
    queue.enqueue(new ArrayBuffer(1), second);
    await assert.doesNotReject(queue.chain);
    firstSource.dispatchEvent(new Event("ended"));

    assert.deepEqual(events.at(-1), {
      active: true,
      metadata: first,
      contextState: "running",
      phase: "completed",
    });
    assert.deepEqual(activity, ["started"]);

    secondSource.dispatchEvent(new Event("ended"));

    assert.deepEqual(events.at(-1), {
      active: false,
      metadata: second,
      contextState: "running",
      phase: "completed",
    });
    assert.deepEqual(activity, ["started", "completed"]);
  });

  it("reports resume failure without playing and recovers on a later enqueue", async () => {
    const errors = [];
    const events = [];
    let resumeAttempts = 0;
    const context = {
      state: "suspended",
      currentTime: 0,
      destination: {},
      async resume() {
        resumeAttempts += 1;
        if (resumeAttempts === 1) {
          throw new Error("activation denied");
        }
        this.state = "running";
      },
      async decodeAudioData() {
        return { duration: 0.1 };
      },
      createBufferSource() {
        const source = new EventTarget();
        source.connect = () => {};
        source.start = () => {};
        return source;
      },
    };
    const queue = createPlaybackQueue(
      context,
      (active, metadata, contextState, phase) =>
        events.push({ active, metadata, contextState, phase }),
      (code) => errors.push(code),
    );

    const failed = { audioId: 8, generation: 3 };
    queue.enqueue(new ArrayBuffer(1), failed);
    await assert.doesNotReject(queue.chain);
    assert.equal(queue.isPlaying, false);
    assert.deepEqual(events, [
      {
        active: false,
        metadata: failed,
        contextState: "suspended",
        phase: "failed",
      },
    ]);

    const recovered = { audioId: 9, generation: 3 };
    queue.enqueue(new ArrayBuffer(1), recovered);
    await assert.doesNotReject(queue.chain);

    assert.equal(resumeAttempts, 2);
    assert.deepEqual(errors, ["audio_resume_failed"]);
    assert.equal(queue.isPlaying, true);
    assert.deepEqual(events.at(-1), {
      active: true,
      metadata: recovered,
      contextState: "running",
      phase: "started",
    });
  });

  it("reports resume failure when a suspended context stays suspended", async () => {
    const errors = [];
    const events = [];
    let decodeCalls = 0;
    const context = {
      state: "suspended",
      currentTime: 0,
      async resume() {},
      async decodeAudioData() {
        decodeCalls += 1;
        return { duration: 0.1 };
      },
    };
    const queue = createPlaybackQueue(
      context,
      (active, metadata, contextState, phase) =>
        events.push({ active, metadata, contextState, phase }),
      (code) => errors.push(code),
    );

    const metadata = { audioId: 10, generation: 3 };
    queue.enqueue(new ArrayBuffer(1), metadata);
    await assert.doesNotReject(queue.chain);

    assert.equal(decodeCalls, 0);
    assert.equal(queue.isPlaying, false);
    assert.deepEqual(errors, ["audio_resume_failed"]);
    assert.deepEqual(events, [
      {
        active: false,
        metadata,
        contextState: "suspended",
        phase: "failed",
      },
    ]);
  });

  it("reports a decode error and continues with later audio", async () => {
    const errors = [];
    const events = [];
    let attempts = 0;
    const context = {
      state: "running",
      currentTime: 0,
      destination: {},
      async decodeAudioData() {
        attempts += 1;
        if (attempts === 1) {
          throw new Error("invalid wav");
        }
        return { duration: 0.1 };
      },
      createBufferSource() {
        const source = new EventTarget();
        source.connect = () => {};
        source.start = () => {};
        return source;
      },
    };
    const queue = createPlaybackQueue(
      context,
      (active, metadata, contextState, phase) =>
        events.push({ active, metadata, contextState, phase }),
      (code) => errors.push(code),
    );

    const failed = { audioId: 11, generation: 3 };
    queue.enqueue(new ArrayBuffer(1), failed);
    await assert.doesNotReject(queue.chain);
    const recovered = { audioId: 12, generation: 3 };
    queue.enqueue(new ArrayBuffer(1), recovered);
    await assert.doesNotReject(queue.chain);

    assert.equal(attempts, 2);
    assert.deepEqual(errors, ["audio_decode_failed"]);
    assert.deepEqual(events, [
      {
        active: false,
        metadata: failed,
        contextState: "running",
        phase: "failed",
      },
      {
        active: true,
        metadata: recovered,
        contextState: "running",
        phase: "started",
      },
    ]);
  });

  it("recovers when an audio source cannot start", async () => {
    const errors = [];
    const events = [];
    const source = new EventTarget();
    source.connect = () => {};
    source.start = () => {
      throw new Error("output unavailable");
    };
    const context = {
      state: "running",
      currentTime: 0,
      destination: {},
      async decodeAudioData() {
        return { duration: 0.1 };
      },
      createBufferSource: () => source,
    };
    const queue = createPlaybackQueue(
      context,
      (active, metadata, contextState, phase) =>
        events.push({ active, metadata, contextState, phase }),
      (code) => errors.push(code),
    );

    const metadata = { audioId: 13, generation: 3 };
    queue.enqueue(new ArrayBuffer(1), metadata);
    await assert.doesNotReject(queue.chain);

    assert.equal(queue.isPlaying, false);
    assert.deepEqual(events, [
      {
        active: false,
        metadata,
        contextState: "running",
        phase: "failed",
      },
    ]);
    assert.deepEqual(errors, ["audio_start_failed"]);
  });

  it("reports an uncorrelated stopped phase for invalidation", async () => {
    const events = [];
    const source = new EventTarget();
    source.connect = () => {};
    source.start = () => {};
    source.stop = () => source.dispatchEvent(new Event("ended"));
    const context = {
      state: "running",
      currentTime: 0,
      destination: {},
      async decodeAudioData() {
        return { duration: 0.1 };
      },
      createBufferSource: () => source,
    };
    const queue = createPlaybackQueue(context, (active, metadata, contextState, phase) =>
      events.push({ active, metadata, contextState, phase }),
    );

    queue.enqueue(new ArrayBuffer(1), { audioId: 14, generation: 3 });
    await assert.doesNotReject(queue.chain);
    events.length = 0;
    queue.stop();

    assert.deepEqual(events, [
      {
        active: false,
        metadata: undefined,
        contextState: undefined,
        phase: "stopped",
      },
    ]);
  });

  it("skips stale queued audio and suppresses an in-flight stale failure", async () => {
    const decodeCalls = [];
    const errors = [];
    const events = [];
    const starts = [];
    let rejectStale;
    const context = {
      state: "running",
      currentTime: 0,
      destination: {},
      decodeAudioData(bytes) {
        const id = new Uint8Array(bytes)[0];
        decodeCalls.push(id);
        if (id === 15) {
          return new Promise((_resolve, reject) => {
            rejectStale = reject;
          });
        }
        return Promise.resolve({ duration: 0.1 });
      },
      createBufferSource() {
        const source = new EventTarget();
        source.connect = () => {};
        source.start = () => starts.push(true);
        return source;
      },
    };
    const queue = createPlaybackQueue(
      context,
      (active, metadata, contextState, phase) =>
        events.push({ active, metadata, contextState, phase }),
      (code) => errors.push(code),
    );
    const staleInFlight = { audioId: 15, generation: 3 };
    const staleQueued = { audioId: 16, generation: 3 };
    const current = { audioId: 17, generation: 4 };

    queue.enqueue(Uint8Array.of(15).buffer, staleInFlight);
    await Promise.resolve();
    queue.enqueue(Uint8Array.of(16).buffer, staleQueued);
    queue.stop();
    queue.enqueue(Uint8Array.of(17).buffer, current);
    rejectStale(new Error("stale decode failed"));
    await assert.doesNotReject(queue.chain);

    assert.deepEqual(decodeCalls, [15, 17]);
    assert.deepEqual(errors, []);
    assert.deepEqual(events, [
      {
        active: false,
        metadata: undefined,
        contextState: undefined,
        phase: "stopped",
      },
      {
        active: true,
        metadata: current,
        contextState: "running",
        phase: "started",
      },
    ]);
    assert.equal(starts.length, 1);
  });

  it("skips stale audio after an in-flight resume completes", async () => {
    const decodeCalls = [];
    const events = [];
    let finishResume;
    const context = {
      state: "suspended",
      currentTime: 0,
      destination: {},
      resume() {
        return new Promise((resolve) => {
          finishResume = () => {
            this.state = "running";
            resolve();
          };
        });
      },
      async decodeAudioData(bytes) {
        decodeCalls.push(new Uint8Array(bytes)[0]);
        return { duration: 0.1 };
      },
      createBufferSource() {
        const source = new EventTarget();
        source.connect = () => {};
        source.start = () => {};
        return source;
      },
    };
    const queue = createPlaybackQueue(context, (active, metadata, contextState, phase) =>
      events.push({ active, metadata, contextState, phase }),
    );
    const current = { audioId: 19, generation: 4 };

    queue.enqueue(Uint8Array.of(18).buffer, { audioId: 18, generation: 3 });
    await Promise.resolve();
    queue.stop();
    queue.enqueue(Uint8Array.of(19).buffer, current);
    finishResume();
    await assert.doesNotReject(queue.chain);

    assert.deepEqual(decodeCalls, [19]);
    assert.deepEqual(events.at(-1), {
      active: true,
      metadata: current,
      contextState: "running",
      phase: "started",
    });
  });

  it("preserves aggregate activity and scheduling after a queued decode failure", async () => {
    const events = [];
    const activity = [];
    const starts = [];
    let decodeCalls = 0;
    const context = {
      state: "running",
      currentTime: 0,
      destination: {},
      async decodeAudioData() {
        decodeCalls += 1;
        if (decodeCalls === 2) {
          throw new Error("invalid wav");
        }
        return { duration: decodeCalls === 1 ? 1 : 0.1 };
      },
      createBufferSource() {
        const source = new EventTarget();
        source.connect = () => {};
        source.start = (startAt) => starts.push(startAt);
        return source;
      },
    };
    let playbackActive = false;
    const queue = createPlaybackQueue(context, (active, metadata, contextState, phase) => {
      events.push({ active, metadata, contextState, phase });
      if (playbackActive !== active) {
        playbackActive = active;
        activity.push(active ? "started" : "completed");
      }
    });
    const failed = { audioId: 21, generation: 4 };

    queue.enqueue(new ArrayBuffer(1), { audioId: 20, generation: 4 });
    queue.enqueue(new ArrayBuffer(1), failed);
    queue.enqueue(new ArrayBuffer(1), { audioId: 22, generation: 4 });
    await assert.doesNotReject(queue.chain);

    assert.deepEqual(events[1], {
      active: true,
      metadata: failed,
      contextState: "running",
      phase: "failed",
    });
    assert.deepEqual(activity, ["started"]);
    assert.deepEqual(starts, [0.02, 1.02]);
  });

  it("does not acknowledge playback before the scheduled audio time", async () => {
    const events = [];
    const scheduled = [];
    const source = new EventTarget();
    source.connect = () => {};
    source.start = () => {};
    const context = {
      state: "running",
      currentTime: 5,
      destination: {},
      async decodeAudioData() {
        return { duration: 0.1 };
      },
      createBufferSource: () => source,
    };
    const timers = {
      set(callback, delayMs) {
        const handle = scheduled.length;
        scheduled.push({ callback, delayMs, handle });
        return handle;
      },
      clear() {},
    };
    const metadata = { audioId: 23, generation: 4 };
    const queue = new AudioPlaybackQueue(
      context,
      (active, correlated, contextState, phase) =>
        events.push({ active, metadata: correlated, contextState, phase }),
      () => {},
      timers,
    );

    queue.enqueue(new ArrayBuffer(1), metadata);
    await assert.doesNotReject(queue.chain);

    assert.deepEqual(events, []);
    assert.ok(scheduled[0].delayMs >= 19.9);
    context.currentTime = 5.019;
    scheduled[0].callback();
    assert.deepEqual(events, []);

    context.currentTime = 5.02;
    scheduled[1].callback();
    assert.deepEqual(events, [
      { active: true, metadata, contextState: "running", phase: "started" },
    ]);
  });

  it("acknowledges a short playback before completion when its timer is delayed", async () => {
    const events = [];
    const scheduled = [];
    const source = new EventTarget();
    source.connect = () => {};
    source.start = () => {};
    const context = {
      state: "running",
      currentTime: 5,
      destination: {},
      async decodeAudioData() {
        return { duration: 0.01 };
      },
      createBufferSource: () => source,
    };
    const timers = {
      set(callback, delayMs) {
        const handle = scheduled.length;
        scheduled.push({ callback, delayMs, handle });
        return handle;
      },
      clear() {},
    };
    const metadata = { audioId: 24, generation: 4 };
    const queue = new AudioPlaybackQueue(
      context,
      (active, correlated, contextState, phase) =>
        events.push({ active, metadata: correlated, contextState, phase }),
      () => {},
      timers,
    );

    queue.enqueue(new ArrayBuffer(1), metadata);
    await assert.doesNotReject(queue.chain);
    assert.deepEqual(events, []);

    context.currentTime = 5.1;
    source.dispatchEvent(new Event("ended"));

    assert.deepEqual(events, [
      { active: true, metadata, contextState: "running", phase: "started" },
      { active: false, metadata, contextState: "running", phase: "completed" },
    ]);
  });

  it("lets current audio start while a stale decode remains unresolved", async () => {
    const starts = [];
    let finishStaleDecode;
    const context = {
      state: "running",
      currentTime: 0,
      destination: {},
      decodeAudioData(bytes) {
        const id = new Uint8Array(bytes)[0];
        if (id === 24) {
          return new Promise((resolve) => {
            finishStaleDecode = resolve;
          });
        }
        return Promise.resolve({ duration: 0.1 });
      },
      createBufferSource() {
        const source = new EventTarget();
        source.connect = () => {};
        source.start = () => starts.push(true);
        return source;
      },
    };
    const queue = createPlaybackQueue(context, () => {});

    queue.enqueue(Uint8Array.of(24).buffer, { audioId: 24, generation: 4 });
    await Promise.resolve();
    queue.stop();
    queue.enqueue(Uint8Array.of(25).buffer, { audioId: 25, generation: 5 });
    await new Promise((resolve) => setImmediate(resolve));
    const currentStartedWhileStalePending = starts.length === 1;

    finishStaleDecode({ duration: 0.1 });
    await assert.doesNotReject(queue.chain);

    assert.equal(currentStartedWhileStalePending, true);
  });
});

function harness() {
  const messages = [];
  const track = { enabled: false };
  const playback = {
    isPlaying: true,
    enqueues: [],
    stops: 0,
    enqueue(bytes, metadata) {
      this.enqueues.push({ bytes, metadata });
    },
    stop() {
      this.isPlaying = false;
      this.stops += 1;
    },
  };
  const controller = new MocoController({
    stream: { getAudioTracks: () => [track] },
    playback,
    send: (message) => messages.push(message),
    reconnect: async () => {},
  });
  return { controller, messages, playback, track };
}

describe("MocoController", () => {
  it("passes matching server audio metadata into playback", () => {
    const { controller, playback } = harness();
    const bytes = new ArrayBuffer(1);
    const metadata = { audioId: 17, generation: controller.audioGeneration };

    controller.acceptAudio(metadata);
    controller.consumeAudio(bytes);

    assert.deepEqual(playback.enqueues, [{ bytes, metadata }]);
  });

  it("discards audio whose generation does not match", () => {
    const { controller, playback } = harness();

    controller.acceptAudio({ audioId: 17, generation: controller.audioGeneration + 1 });
    controller.consumeAudio(new ArrayBuffer(1));

    assert.deepEqual(playback.enqueues, []);
  });

  it("keeps the microphone enabled until listening is explicitly stopped", async () => {
    const { controller, track } = harness();

    await controller.applyControl("listen_start");
    assert.equal(track.enabled, true);

    await controller.applyControl("listen_stop");
    assert.equal(track.enabled, false);
  });

  it("stops microphone input without invalidating current output", async () => {
    const { controller, playback, track } = harness();
    const generation = controller.audioGeneration;

    await controller.applyControl("listen_stop");

    assert.equal(track.enabled, false);
    assert.equal(controller.audioGeneration, generation);
    assert.equal(playback.isPlaying, true);
  });

  it("fails closed when the control socket disconnects", async () => {
    const { controller, playback, track } = harness();
    const generation = controller.audioGeneration;
    const mediaTrack = {
      stopped: false,
      stop() {
        this.stopped = true;
      },
    };
    const peer = {
      closed: false,
      close() {
        this.closed = true;
      },
    };
    const context = {
      state: "running",
      closed: false,
      async close() {
        this.closed = true;
        this.state = "closed";
      },
    };
    const controls = [{ disabled: false }, { disabled: false }, { disabled: false }];
    await controller.applyControl("listen_start");

    await closeDisconnectedMedia({
      context,
      controller,
      controls,
      peer,
      stream: { getTracks: () => [mediaTrack] },
    });

    assert.equal(track.enabled, false);
    assert.equal(controller.audioGeneration, generation + 1);
    assert.equal(playback.isPlaying, false);
    assert.equal(peer.closed, true);
    assert.equal(mediaTrack.stopped, true);
    assert.equal(context.closed, true);
    assert.deepEqual(
      controls.map((control) => control.disabled),
      [true, true, true],
    );
  });

  it("reconnects an idle-expired conversation before listening", async () => {
    const { controller } = harness();
    let reconnects = 0;
    controller.reconnect = async () => {
      reconnects += 1;
    };
    controller.idleExpired = true;

    await controller.applyControl("listen_start");

    assert.equal(reconnects, 1);
  });

  it("stops stale input while preserving output when a conversation expires", () => {
    const { controller, playback, track } = harness();
    track.enabled = true;

    controller.expire();

    assert.equal(track.enabled, false);
    assert.equal(playback.isPlaying, true);
    assert.equal(controller.idleExpired, true);
  });

  it("lets a later stop supersede an in-flight reconnect", async () => {
    const { controller, messages, track } = harness();
    let finishReconnect;
    controller.reconnect = () =>
      new Promise((resolve) => {
        finishReconnect = resolve;
      });
    controller.idleExpired = true;

    const starting = controller.applyControl("listen_start");
    await Promise.resolve();
    await controller.applyControl("listen_stop");
    finishReconnect();
    await starting;

    assert.equal(track.enabled, false);
    assert.deepEqual(messages, [{ type: "control", control: "listen_stop" }]);
  });
});

describe("BrowserHotkeyMapper", () => {
  it("defers keyboard input to the global listener when it is enabled", () => {
    const mapper = new BrowserHotkeyMapper({
      globalHotkeysEnabled: true,
      startKey: "v",
      stopKey: "escape",
    });

    assert.equal(mapper.keyDown("v"), null);
    assert.equal(mapper.keyUp("v"), null);
  });

  it("maps configured browser fallback keys without repeated controls", () => {
    const mapper = new BrowserHotkeyMapper({
      globalHotkeysEnabled: false,
      startKey: "v",
      stopKey: "escape",
    });

    assert.equal(mapper.keyDown("v"), "listen_start");
    assert.equal(mapper.keyDown("v"), null);
    assert.equal(mapper.keyUp("v"), null);
    assert.equal(mapper.keyDown("escape"), "listen_stop");
    assert.equal(mapper.keyUp("escape"), null);
  });

  it("preserves a held fallback key across lifecycle state updates", () => {
    const settings = {
      globalHotkeysEnabled: false,
      startKey: "v",
      stopKey: "escape",
    };
    const mapper = new BrowserHotkeyMapper(settings);

    assert.equal(mapper.keyDown("v"), "listen_start");
    mapper.configure(settings);

    assert.equal(mapper.keyUp("v"), null);
    assert.equal(mapper.keyDown("v"), "listen_start");
  });

  it("does not capture fallback hotkeys while a theme input is focused", () => {
    const mapper = new BrowserHotkeyMapper({
      globalHotkeysEnabled: false,
      startKey: "v",
      stopKey: "escape",
    });
    const input = { tagName: "INPUT", closest: () => null };

    assert.equal(shouldHandleHotkey({ target: input, key: "v" }, mapper), false);
    assert.equal(shouldHandleHotkey({ target: null, key: "v" }, mapper), true);
  });
});

describe("operator status", () => {
  it("keeps active processing independent from microphone stop", async () => {
    const { controller, track } = harness();
    const progress = new ProgressTracker({ now: () => 10_000 });
    progress.consume({
      kind: "turn",
      phase: "started",
      label: "応答処理",
      occurredAtMs: 1_000,
    });

    await controller.applyControl("listen_start");
    await controller.applyControl("listen_stop");

    assert.equal(track.enabled, false);
    assert.equal(progress.snapshot().active, true);
  });

  it("retains activity, reasoning summary, and dismissed errors in history", () => {
    const dom = new JSDOM(`
      <section id="error" hidden><span id="error-text"></span></section>
    `);
    const buffer = new ActivityBuffer();
    const progress = new ProgressTracker({ now: () => 5_000 });
    let rendered = [];
    const status = new OperatorStatus({
      activityBuffer: buffer,
      activityView: { render: (items) => (rendered = [...items]) },
      error: dom.window.document.querySelector("#error"),
      errorText: dom.window.document.querySelector("#error-text"),
      progress,
      progressView: { render: () => {} },
    });

    status.consume({
      type: "activity",
      kind: "turn",
      phase: "started",
      label: "応答処理",
      occurredAtMs: 1_000,
    });
    status.consume({
      type: "reasoning_summary",
      itemId: "reasoning-1",
      delta: "確認を続けています",
      occurredAtMs: 2_000,
    });
    status.consume({ type: "error", code: "codex_realtime_error" });
    status.expire();
    status.dismissError();

    assert.equal(rendered.length, 3);
    assert.deepEqual(
      rendered.map((item) => item.kind),
      ["turn", "reasoning", "error"],
    );
    assert.match(rendered[2].label, /codex_realtime_error/);
    assert.equal(status.error.hidden, true);
    assert.equal(progress.snapshot().active, false);
    assert.equal(progress.snapshot().label, "会話が終了しました");
  });

  it("renders the stable setup failure code with user-facing copy", () => {
    const dom = new JSDOM(`
      <section id="error" hidden><span id="error-text"></span></section>
    `);
    const status = new OperatorStatus({
      activityBuffer: new ActivityBuffer(),
      activityView: { render: () => {} },
      error: dom.window.document.querySelector("#error"),
      errorText: dom.window.document.querySelector("#error-text"),
      progress: new ProgressTracker(),
      progressView: { render: () => {} },
    });

    status.showError("enable_failed");

    assert.equal(
      status.errorText.textContent,
      "enable_failed — 音声セッションを開始できませんでした",
    );
  });
});

describe("operator console DOM", () => {
  it("removes the connection action after success and restores it after disconnect", () => {
    const row = { hidden: false };
    const button = { disabled: true, textContent: "接続" };

    setConnectionAction({ row, button }, "connected");
    assert.equal(row.hidden, true);

    setConnectionAction({ row, button }, "disconnected");
    assert.deepEqual(
      { row, button },
      {
        row: { hidden: false },
        button: { disabled: false, textContent: "再接続" },
      },
    );
  });

  it("renders grouped color previews for every theme preset", () => {
    const dom = new JSDOM('<fieldset id="presets"><legend>プリセット</legend></fieldset>');
    const container = dom.window.document.querySelector("#presets");

    renderPresetChoices(container, (tag) => dom.window.document.createElement(tag));

    assert.equal(container.querySelectorAll('input[name="theme-preset"]').length, 13);
    assert.deepEqual(
      [...container.querySelectorAll(".theme-preset-group-title")].map((node) => node.textContent),
      ["自動", "Light", "Dark", "アクセシビリティ"],
    );
    assert.equal(container.querySelectorAll(".theme-preset-swatch span").length, 39);
  });

  it("contains the compact operator-console landmarks", async () => {
    const html = await readFile(
      new URL("../../src/moco/web/static/index.html", import.meta.url),
      "utf8",
    );
    const dom = new JSDOM(html);
    const document = dom.window.document;
    for (const id of [
      "enable",
      "connection-row",
      "state",
      "connection",
      "mic-state",
      "voice",
      "listen-start",
      "listen-stop",
      "progress-label",
      "progress-elapsed",
      "progress-updated",
      "error",
      "error-text",
      "error-close",
      "transcript",
      "activity",
      "activity-latest",
      "theme-toggle",
      "theme-panel",
      "theme-close",
      "theme-presets",
      "theme-colors",
      "theme-validation",
      "theme-reset",
      "pairing-open",
      "pairing-panel",
      "pairing-close",
      "pairing-image",
    ]) {
      assert.equal(document.querySelectorAll(`#${id}`).length, 1, id);
    }
    assert.equal(document.querySelector("h1"), null);
    assert.equal(document.querySelector(".signal-rail"), null);
  });

  it("contains mobile pairing and semantic listening controls", async () => {
    const html = await readFile(
      new URL("../../src/moco/web/static/index.html", import.meta.url),
      "utf8",
    );
    const document = new JSDOM(html).window.document;

    assert.equal(
      document.querySelector("meta[name=viewport]").content.includes("viewport-fit=cover"),
      true,
    );
    assert.equal(
      document.querySelector("#listen-start").getAttribute("aria-label"),
      "音声入力を開始",
    );
    assert.equal(
      document.querySelector("#listen-stop").getAttribute("aria-label"),
      "音声入力を停止",
    );
    assert.equal(document.querySelector("#voice").getAttribute("aria-label"), "音声モデル");
    assert.equal(document.querySelector("#pairing-panel").getAttribute("role"), "dialog");
  });

  it("loads and revokes a pairing QR only after a successful private probe", async () => {
    const calls = [];
    const fetch = async (_url, options) => {
      calls.push(options);
      return options.method === "HEAD"
        ? { ok: true }
        : { ok: true, blob: async () => new Blob(["<svg/>"]) };
    };
    const image = {
      src: "",
      removeAttribute(name) {
        this[name] = "";
      },
    };
    const panel = new PairingPanel({
      capability: "private-capability",
      dom: {
        open: { hidden: true, focus() {} },
        panel: { hidden: true },
        image,
        close: { focus() {} },
      },
      fetch,
      location: { hostname: "127.0.0.1" },
      createObjectURL: () => "blob:pairing",
      revokeObjectURL: (value) => calls.push(value),
    });

    await panel.probe();
    assert.equal(panel.dom.open.hidden, false);
    await panel.open();
    assert.equal(image.src, "blob:pairing");
    panel.close();
    assert.equal(image.src, "");
    assert.equal(calls.at(-1), "blob:pairing");
    assert.equal(calls[0].headers["X-Moco-Capability"], "private-capability");
  });

  it("applies a hex color edit while the user is typing", () => {
    const dom = new JSDOM(`
      <button id="toggle"></button>
      <section id="panel" hidden></section>
      <button id="close"></button>
      <fieldset id="presets"><legend>プリセット</legend></fieldset>
      <div id="colors"></div>
      <p id="validation"></p>
      <button id="reset"></button>
    `);
    const originalDocument = globalThis.document;
    const originalInput = globalThis.HTMLInputElement;
    globalThis.document = dom.window.document;
    globalThis.HTMLInputElement = dom.window.HTMLInputElement;
    const palette = Object.fromEntries(EDITABLE_TOKENS.map((token) => [token, "#112233"]));
    const edits = [];
    const controller = {
      theme: { preset: "midnight", overrides: {} },
      apply: () => ({ ...palette, ...controller.theme.overrides }),
      contrastWarnings: () => [],
      resetOverride: () => {},
      resetOverrides: () => {},
      selectPreset: () => {},
      setOverride(token, value) {
        edits.push([token, value]);
        this.theme.overrides[token] = value;
      },
    };

    try {
      const panel = new ThemePanel({
        controller,
        dom: {
          themeToggle: dom.window.document.querySelector("#toggle"),
          themePanel: dom.window.document.querySelector("#panel"),
          themeClose: dom.window.document.querySelector("#close"),
          themePresets: dom.window.document.querySelector("#presets"),
          themeColors: dom.window.document.querySelector("#colors"),
          themeValidation: dom.window.document.querySelector("#validation"),
          themeReset: dom.window.document.querySelector("#reset"),
        },
      });
      panel.render();
      const input = dom.window.document.querySelector(
        'input[aria-label="情報アクセントの16進カラー"]',
      );
      input.value = "#ff00ff";
      input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));

      assert.deepEqual(edits, [["accent", "#ff00ff"]]);
    } finally {
      globalThis.document = originalDocument;
      globalThis.HTMLInputElement = originalInput;
    }
  });
});

describe("VoiceModelController", () => {
  function voiceOptions(...labels) {
    return labels.map((label, index) => ({
      id: `fixture-${index}`,
      label,
      default: index === labels.length - 1,
    }));
  }

  function voiceHarness() {
    const dom = new JSDOM("<select></select>");
    const messages = [];
    const select = dom.window.document.querySelector("select");
    const controller = new VoiceModelController({
      select,
      send: (message) => messages.push(message),
      createOption: (label, value) => new dom.window.Option(label, value),
    });
    return { controller, messages, select };
  }

  function renderedOptions(select) {
    return [...select.options].map(({ disabled, label, value }) => ({ disabled, label, value }));
  }

  it("is safe before the first runtime catalog arrives", () => {
    const { controller, select } = voiceHarness();

    controller.confirm(null);

    assert.equal(select.disabled, true);
    assert.equal(select.value, "");
    assert.equal(select.options[0].label, "音声モデルを読み込み中");
  });

  it("renders runtime labels and IDs in server order without a built-in voice", () => {
    const { controller, select } = voiceHarness();
    const options = voiceOptions("表示 B", "表示 A", "表示 C");

    controller.configure({
      options,
      selected: options[1].id,
      ready: true,
      readiness: "ready",
    });

    assert.equal(select.disabled, false);
    assert.deepEqual(renderedOptions(select), [
      { disabled: false, label: "表示 B", value: "fixture-0" },
      { disabled: false, label: "表示 A", value: "fixture-1" },
      { disabled: false, label: "表示 C", value: "fixture-2" },
    ]);
    assert.equal(select.value, options[1].id);
  });

  it("disables selection with an accessible status for loading, unready, and empty catalogs", () => {
    const { controller, select } = voiceHarness();
    const options = voiceOptions("表示 X");

    for (const state of [
      { options, ready: false, readiness: "loading", label: "音声モデルを読み込み中" },
      {
        options,
        ready: false,
        readiness: "voice_bank_invalid",
        label: "音声モデルを利用できません",
      },
      { options: [], ready: true, readiness: "ready", label: "利用可能な音声モデルがありません" },
    ]) {
      controller.configure({ ...state, selected: null });
      assert.equal(select.disabled, true);
      assert.deepEqual(renderedOptions(select), [
        { disabled: true, label: state.label, value: "" },
      ]);
    }
  });

  it("keeps an absent or unknown confirmed selection unselected", () => {
    const { controller, select } = voiceHarness();
    const options = voiceOptions("表示 1", "表示 2");

    controller.configure({ options, selected: null, ready: true, readiness: "ready" });
    assert.equal(select.value, "");
    assert.equal(select.options[0].label, "音声モデルを選択してください");
    assert.equal(select.options[0].disabled, true);

    controller.confirm("unknown-fixture");
    assert.equal(select.value, "");
    assert.deepEqual(
      [...select.options].slice(1).map((option) => option.value),
      options.map((option) => option.id),
    );
  });

  it("rolls back pending UI selection and sends only nonblank opaque IDs", () => {
    const { controller, messages, select } = voiceHarness();
    const options = voiceOptions("表示 α", "表示 β");
    controller.configure({
      options,
      selected: options[0].id,
      ready: true,
      readiness: "ready",
    });

    select.value = options[1].id;
    controller.select(options[1].id);
    assert.equal(select.value, options[0].id);
    assert.deepEqual(messages, [{ type: "select_voice", voice_id: options[1].id }]);

    controller.select("");
    controller.select("   ");
    controller.select(null);
    assert.deepEqual(messages, [{ type: "select_voice", voice_id: options[1].id }]);

    controller.confirm(options[1].id);
    assert.equal(select.value, options[1].id);
  });
});

describe("browser connection timeouts", () => {
  it("resets transport indicators after setup fails", () => {
    const connection = { textContent: "WS ONLINE", dataset: { status: "ok" } };
    const micState = { textContent: "MIC ON", dataset: { status: "ok" } };

    setTransportOffline({ connection, micState });

    assert.deepEqual(connection, { textContent: "WS OFFLINE", dataset: { status: "error" } });
    assert.deepEqual(micState, { textContent: "MIC OFF", dataset: { status: "muted" } });
  });

  it("does not replace an already displayed root cause with a disconnect error", () => {
    assert.equal(
      connectionCloseErrorCode({ code: "irodori_not_ready", displayed: true }, true),
      null,
    );
    assert.equal(
      connectionCloseErrorCode({ code: "webrtc_connection_failed", displayed: false }, true),
      "webrtc_connection_failed",
    );
    assert.equal(connectionCloseErrorCode(undefined, true), "websocket_disconnected");
    assert.equal(connectionCloseErrorCode(undefined, false), null);
  });

  it("closes the transport after a conversation setup failure", () => {
    const socket = {
      closes: 0,
      close() {
        this.closes += 1;
      },
    };
    const failures = [];
    const error = new Error("failed SDP");
    error.name = "webrtc_connection_failed";

    assert.equal(
      closeSocketForFailure(socket, error, (failure) => failures.push(failure)),
      true,
    );
    assert.equal(socket.closes, 1);
    assert.deepEqual(failures, [{ code: "webrtc_connection_failed", displayed: false }]);
    assert.equal(
      closeSocketForFailure(undefined, error, () => {}),
      false,
    );
  });

  it("waits for the SDP answer and rejects every stable terminal start error", async () => {
    const descriptions = [];
    const success = new ConversationHandshake(async (sdp) => descriptions.push(sdp));
    const waitingForSuccess = success.promise;

    assert.equal(await success.consume({ type: "sdp_answer", sdp: "answer-sdp" }), true);
    await waitingForSuccess;
    assert.deepEqual(descriptions, ["answer-sdp"]);

    for (const code of [
      "conversation_start_failed",
      "configured_voice_unavailable",
      "voice_catalog_empty",
      "voice_selection_required",
      "model_loading",
      "model_not_loaded",
      "voice_bank_invalid",
      "capability_mismatch",
      "irodori_unavailable",
      "runtime_generation_mismatch",
      "voice_not_found",
    ]) {
      const failure = new ConversationHandshake(async () => {});
      const waitingForFailure = assert.rejects(failure.promise, (error) => error.name === code);
      assert.equal(await failure.consume({ type: "error", code }), true);
      assert.equal(failure.settled, true);
      await waitingForFailure;
    }
  });

  it("reports a peer connection that enters failed state", () => {
    const peer = new EventTarget();
    peer.connectionState = "connecting";
    const errors = [];
    const stopWatching = watchPeerFailure(peer, (code) => errors.push(code));

    peer.connectionState = "failed";
    peer.dispatchEvent(new Event("connectionstatechange"));
    stopWatching();
    peer.dispatchEvent(new Event("connectionstatechange"));

    assert.deepEqual(errors, ["webrtc_connection_failed"]);
  });

  it("clears an opened socket promise and timer after later media setup fails", () => {
    const cleared = [];

    const reset = resetConnectionAttempt(42, (timer) => cleared.push(timer));

    assert.deepEqual(reset, { openPromise: null, progressTimer: undefined });
    assert.deepEqual(cleared, [42]);
  });

  it("retains the capability within the tab when the URL is reloaded", () => {
    assert.equal(typeof appModule.loadCapability, "function");
    const values = new Map();
    const storage = {
      getItem: (key) => values.get(key) ?? null,
      setItem: (key, value) => values.set(key, value),
    };
    const history = {
      replaceState: (_state, _unused, url) => {
        assert.equal(url, "/");
      },
    };

    assert.equal(
      appModule.loadCapability({
        history,
        location: { hash: "#test-capability", pathname: "/" },
        storage,
      }),
      "test-capability",
    );
    assert.equal(
      appModule.loadCapability({
        history,
        location: { hash: "", pathname: "/" },
        storage,
      }),
      "test-capability",
    );
  });

  it("rejects ICE gathering that never completes", async () => {
    const peer = new EventTarget();
    peer.iceGatheringState = "gathering";

    await assert.rejects(
      waitForIce(peer, { timeoutMs: 1 }),
      (error) => error.name === "ice_gathering_timeout",
    );
  });

  it("resolves ICE gathering when the peer completes", async () => {
    const peer = new EventTarget();
    peer.iceGatheringState = "gathering";
    const waiting = waitForIce(peer, { timeoutMs: 100 });

    peer.iceGatheringState = "complete";
    peer.dispatchEvent(new Event("icegatheringstatechange"));

    await waiting;
  });

  it("rejects a WebSocket that never opens", async () => {
    const socket = new EventTarget();

    await assert.rejects(
      waitForSocketOpen(socket, { timeoutMs: 1 }),
      (error) => error.name === "websocket_open_timeout",
    );
  });

  it("resolves when the WebSocket opens", async () => {
    const socket = new EventTarget();
    const waiting = waitForSocketOpen(socket, { timeoutMs: 100 });

    socket.dispatchEvent(new Event("open"));

    await waiting;
  });
});

describe("media cleanup", () => {
  it("starts AudioContext resume synchronously before microphone permission", async () => {
    const order = [];
    class FakeAudioContext {
      resume() {
        order.push("resume");
        return Promise.resolve();
      }
    }

    const activation = beginAudioActivation(FakeAudioContext);
    order.push("permission");
    await activation.ready;

    assert.deepEqual(order, ["resume", "permission"]);
  });

  it("classifies mobile audio and microphone failures without a success fallback", () => {
    assert.equal(
      connectionSetupErrorCode("audio", { name: "NotAllowedError" }),
      "audio_resume_failed",
    );
    assert.equal(
      connectionSetupErrorCode("microphone", { name: "NotAllowedError" }),
      "microphone_permission_denied",
    );
    assert.equal(
      connectionSetupErrorCode("microphone", { name: "NotFoundError" }),
      "microphone_unavailable",
    );
    assert.equal(
      connectionSetupErrorCode("microphone", { name: "AbortError" }),
      "microphone_failed",
    );
  });

  it("allows microphone permission denial before an audio context exists", async () => {
    await assert.doesNotReject(closeAudioContext(undefined));
  });

  it("closes an open audio context exactly once", async () => {
    const context = {
      state: "running",
      closes: 0,
      async close() {
        this.closes += 1;
        this.state = "closed";
      },
    };

    await closeAudioContext(context);
    await closeAudioContext(context);

    assert.equal(context.closes, 1);
  });
});
