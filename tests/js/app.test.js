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
  closeCurrentPeer,
  connectionCloseErrorCode,
  connectionSetupErrorCode,
  closeDisconnectedMedia,
  closeFailedHandshakePeer,
  closeSocketForFailure,
  ConversationHandshake,
  MocoController,
  OperatorStatus,
  PairingPanel,
  renderPresetChoices,
  resetConnectionAttempt,
  reconcileListeningState,
  setConnectionAction,
  setTransportOffline,
  shouldHandleHotkey,
  ThemePanel,
  TurnCancelController,
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
    const cleanupOrder = [];
    const originalDisconnect = controller.disconnect.bind(controller);
    controller.disconnect = () => {
      cleanupOrder.push("controller");
      originalDisconnect();
    };
    const deviceController = {
      closes: 0,
      close() {
        this.closes += 1;
        cleanupOrder.push("devices");
      },
    };
    const mediaTrack = {
      stopped: false,
      stop() {
        this.stopped = true;
        cleanupOrder.push("track");
      },
    };
    const peer = {
      closed: false,
      close() {
        this.closed = true;
        cleanupOrder.push("peer");
      },
    };
    const context = {
      state: "running",
      closed: false,
      async close() {
        this.closed = true;
        this.state = "closed";
        cleanupOrder.push("context");
      },
    };
    const controls = Array.from({ length: 5 }, (_value, index) => {
      let disabled = false;
      return {
        get disabled() {
          return disabled;
        },
        set disabled(value) {
          disabled = value;
          if (value) {
            cleanupOrder.push(`control-${index}`);
          }
        },
      };
    });
    await controller.applyControl("listen_start");

    await closeDisconnectedMedia({
      context,
      controller,
      controls,
      deviceController,
      peer,
      stream: { getTracks: () => [mediaTrack] },
    });

    assert.equal(track.enabled, false);
    assert.equal(controller.audioGeneration, generation + 1);
    assert.equal(playback.isPlaying, false);
    assert.equal(peer.closed, true);
    assert.equal(mediaTrack.stopped, true);
    assert.equal(context.closed, true);
    assert.equal(deviceController.closes, 1);
    assert.deepEqual(
      controls.map((control) => control.disabled),
      [true, true, true, true, true],
    );
    assert.deepEqual(cleanupOrder, [
      "devices",
      "control-0",
      "control-1",
      "control-2",
      "control-3",
      "control-4",
      "controller",
      "peer",
      "track",
      "context",
    ]);
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

  it("manually reconnects Voice or a lost lease exactly once on the next listen", async () => {
    const { controller } = harness();
    let reconnects = 0;
    controller.reconnect = async () => {
      reconnects += 1;
    };
    controller.requireReconnect();

    await controller.applyControl("listen_start");
    await controller.applyControl("listen_stop");

    assert.equal(reconnects, 1);
  });

  it("shares one in-flight reconnect across concurrent listen starts", async () => {
    const { controller, messages } = harness();
    let finishReconnect;
    let reconnects = 0;
    controller.reconnect = () => {
      reconnects += 1;
      return new Promise((resolve) => {
        finishReconnect = resolve;
      });
    };
    controller.requireReconnect();

    const first = controller.applyControl("listen_start");
    const second = controller.applyControl("listen_start");
    await Promise.resolve();

    assert.equal(reconnects, 1);
    finishReconnect();
    assert.deepEqual(await Promise.all([first, second]), [false, true]);
    assert.deepEqual(messages, [{ type: "control", control: "listen_start" }]);
  });

  it("enables the current microphone when it changes during Voice reconnect", async () => {
    const { controller, messages, track: oldTrack } = harness();
    const currentTrack = { enabled: false };
    let finishReconnect;
    controller.reconnect = () =>
      new Promise((resolve) => {
        finishReconnect = resolve;
      });
    controller.requireReconnect();

    const starting = controller.applyControl("listen_start");
    await Promise.resolve();
    controller.stream = { getAudioTracks: () => [currentTrack] };
    finishReconnect();

    assert.equal(await starting, true);
    assert.equal(oldTrack.enabled, false);
    assert.equal(currentTrack.enabled, true);
    assert.deepEqual(messages, [{ type: "control", control: "listen_start" }]);
  });

  it("does not send listening success when the current track disappears during reconnect", async () => {
    const { controller, messages, track: oldTrack } = harness();
    let finishReconnect;
    controller.reconnect = () =>
      new Promise((resolve) => {
        finishReconnect = resolve;
      });
    controller.requireReconnect();

    const starting = controller.applyControl("listen_start");
    await Promise.resolve();
    controller.stream = { getAudioTracks: () => [] };
    finishReconnect();

    assert.equal(await starting, false);
    assert.equal(oldTrack.enabled, false);
    assert.deepEqual(messages, []);
  });

  it("allows an explicit retry after a shared reconnect failure", async () => {
    const { controller, messages } = harness();
    const reconnectError = new Error("synthetic reconnect failure");
    let reconnects = 0;
    controller.reconnect = async () => {
      reconnects += 1;
      if (reconnects === 1) {
        throw reconnectError;
      }
    };
    controller.requireReconnect();

    await assert.rejects(controller.applyControl("listen_start"), reconnectError);
    assert.equal(controller.reconnectRequired, true);
    assert.equal(await controller.applyControl("listen_start"), true);

    assert.equal(reconnects, 2);
    assert.deepEqual(messages, [{ type: "control", control: "listen_start" }]);
  });

  it("reconciles a non-listening transcribing state to microphone off", async () => {
    const { controller, track } = harness();
    const listenStart = {
      classes: new Set(["is-active"]),
      attributes: new Map([["aria-pressed", "true"]]),
      classList: {
        remove(name) {
          listenStart.classes.delete(name);
        },
      },
      setAttribute(name, value) {
        this.attributes.set(name, value);
      },
    };
    const micState = { textContent: "MIC ON", dataset: { status: "ok" } };
    await controller.applyControl("listen_start");

    reconcileListeningState({ controller, state: "transcribing", listenStart, micState });

    assert.equal(track.enabled, false);
    assert.equal(listenStart.classes.has("is-active"), false);
    assert.equal(listenStart.attributes.get("aria-pressed"), "false");
    assert.deepEqual(micState, { textContent: "MIC OFF", dataset: { status: "muted" } });
  });

  it("restores microphone state when listening follows a delayed stop state", async () => {
    const { controller, track } = harness();
    const listenStart = {
      classes: new Set(),
      attributes: new Map([["aria-pressed", "false"]]),
      classList: {
        add(name) {
          listenStart.classes.add(name);
        },
        remove(name) {
          listenStart.classes.delete(name);
        },
      },
      setAttribute(name, value) {
        this.attributes.set(name, value);
      },
    };
    const micState = { textContent: "MIC OFF", dataset: { status: "muted" } };

    await controller.applyControl("listen_stop");
    await controller.applyControl("listen_start");
    reconcileListeningState({ controller, state: "ready", listenStart, micState });
    reconcileListeningState({ controller, state: "listening", listenStart, micState });

    assert.equal(track.enabled, true);
    assert.equal(listenStart.classes.has("is-active"), true);
    assert.equal(listenStart.attributes.get("aria-pressed"), "true");
    assert.deepEqual(micState, { textContent: "MIC ON", dataset: { status: "ok" } });
  });

  it("does not let a stale listening state undo the latest local stop", async () => {
    const { controller, track } = harness();
    const listenStart = {
      classes: new Set(),
      attributes: new Map(),
      classList: {
        add(name) {
          listenStart.classes.add(name);
        },
        remove(name) {
          listenStart.classes.delete(name);
        },
      },
      setAttribute(name, value) {
        this.attributes.set(name, value);
      },
    };
    const micState = { textContent: "MIC OFF", dataset: { status: "muted" } };

    await controller.applyControl("listen_start");
    await controller.applyControl("listen_stop");
    reconcileListeningState({ controller, state: "listening", listenStart, micState });

    assert.equal(track.enabled, false);
    assert.equal(listenStart.classes.has("is-active"), false);
    assert.equal(listenStart.attributes.get("aria-pressed"), "false");
    assert.deepEqual(micState, { textContent: "MIC OFF", dataset: { status: "muted" } });
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
    let reconnects = 0;
    controller.reconnect = () =>
      new Promise((resolve) => {
        reconnects += 1;
        finishReconnect = resolve;
      });
    controller.requireReconnect();

    const starting = controller.applyControl("listen_start");
    await Promise.resolve();
    await controller.applyControl("listen_stop");
    finishReconnect();
    await starting;

    assert.equal(track.enabled, false);
    assert.equal(controller.reconnectRequired, false);
    assert.deepEqual(messages, [{ type: "control", control: "listen_stop" }]);

    assert.equal(await controller.applyControl("listen_start"), true);
    assert.equal(reconnects, 1);
    assert.deepEqual(messages, [
      { type: "control", control: "listen_stop" },
      { type: "control", control: "listen_start" },
    ]);
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

  it("never maps a global or browser fallback key to turn cancellation", () => {
    const mapper = new BrowserHotkeyMapper({
      globalHotkeysEnabled: false,
      startKey: "v",
      stopKey: "escape",
    });

    for (const key of ["c", "delete", "backspace", "enter", " "]) {
      assert.equal(mapper.keyDown(key), null);
      assert.equal(mapper.keyUp(key), null);
    }
  });
});

describe("turn cancel", () => {
  it("sends one explicit control only while the server says cancellation is allowed", () => {
    const dom = new JSDOM('<button id="turn-cancel" type="button" disabled>取消</button>');
    const button = dom.window.document.querySelector("#turn-cancel");
    const messages = [];
    const controller = new TurnCancelController({
      button,
      send: (message) => messages.push(message),
    });

    button.click();
    controller.configure(true);
    assert.equal(button.disabled, false);
    button.click();
    button.click();

    assert.deepEqual(messages, [{ type: "control", control: "turn_cancel" }]);
    assert.equal(button.disabled, true);
    controller.configure(false);
    assert.equal(button.disabled, true);
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

  it("renders interaction backpressure without falling back to an unknown error", () => {
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

    status.showError("interaction_busy");

    assert.equal(status.errorText.textContent, "interaction_busy — 別の処理を受け付けています");
  });

  it("renders stable delivery caption errors with user-facing copy", () => {
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

    status.showError("caption_unsupported");
    assert.equal(
      status.errorText.textContent,
      "caption_unsupported — Irodori が話し方指定に対応していません",
    );

    status.showError("speech_caption_invalid");
    assert.equal(
      status.errorText.textContent,
      "speech_caption_invalid — 話し方指定を検証できなかったため標準表現で読み上げます",
    );
  });

  it("renders stable audio device errors with user-facing copy", () => {
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

    status.showError("microphone_switch_failed");
    assert.equal(
      status.errorText.textContent,
      "microphone_switch_failed — 入力マイクを切り替えられませんでした",
    );

    status.showError("audio_output_switch_failed");
    assert.equal(
      status.errorText.textContent,
      "audio_output_switch_failed — 音声出力先を切り替えられませんでした",
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
      "audio-input",
      "audio-output",
      "voice",
      "listen-start",
      "listen-stop",
      "turn-cancel",
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
    assert.equal(
      document.querySelector("#turn-cancel").getAttribute("aria-label"),
      "実行中の処理を取り消す",
    );
    assert.equal(document.querySelector("#voice").getAttribute("aria-label"), "音声モデル");
    for (const [id, label] of [
      ["audio-input", "入力マイク"],
      ["audio-output", "音声出力先"],
    ]) {
      const select = document.querySelector(`#${id}`);
      assert.ok(select, id);
      assert.equal(select.getAttribute("aria-label"), label);
      assert.equal(select.disabled, true);
    }
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

  it("treats arbitrary numeric IPv4 loopback as local for pairing probe", async () => {
    const calls = [];
    const panel = new PairingPanel({
      capability: "private-capability",
      dom: { open: { hidden: true } },
      fetch: async (_url, options) => {
        calls.push(options);
        return { ok: true };
      },
      location: { hostname: "127.0.0.42" },
      createObjectURL: () => "blob:pairing",
      revokeObjectURL: () => {},
    });

    await panel.probe();

    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, "HEAD");
    assert.equal(panel.dom.open.hidden, false);
  });

  it("does not probe non-loopback or hostname-trick pairing locations", async () => {
    for (const hostname of [
      "192.0.2.1",
      "127.0.0.42.evil",
      "evil127.0.0.42",
      "[::ffff:7f00:1]",
      "::ffff:7f00:1",
      "[::1%lo0]",
      "::1%lo0",
    ]) {
      const calls = [];
      const panel = new PairingPanel({
        capability: "private-capability",
        dom: { open: { hidden: true } },
        fetch: async (_url, options) => {
          calls.push(options);
          return { ok: true };
        },
        location: { hostname },
        createObjectURL: () => "blob:pairing",
        revokeObjectURL: () => {},
      });

      await panel.probe();

      assert.equal(calls.length, 0);
      assert.equal(panel.dom.open.hidden, true);
    }
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

let bootHarnessSequence = 0;

async function bootConversationHarness({
  answerStarts = [],
  pendingEnumerations = [],
  pendingOffers = [],
} = {}) {
  const html = await readFile(
    new URL("../../src/moco/web/static/index.html", import.meta.url),
    "utf8",
  );
  const dom = new JSDOM(html, { url: "http://127.0.0.1:8765/?capability=test-capability" });
  dom.window.matchMedia = () => ({ matches: false, addEventListener() {} });
  dom.window.fetch = async () => ({ ok: false });

  const sent = [];
  const sockets = [];
  const peers = [];
  const audioContexts = [];
  const streams = [];
  const tracks = [];
  let startIndex = 0;

  class FakeSocket extends dom.window.EventTarget {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSED = 3;

    constructor() {
      super();
      this.readyState = FakeSocket.CONNECTING;
      sockets.push(this);
      queueMicrotask(() => {
        this.readyState = FakeSocket.OPEN;
        this.dispatchEvent(new dom.window.Event("open"));
      });
    }

    send(payload) {
      const message = JSON.parse(payload);
      sent.push(message);
      if (message.type !== "start") {
        return;
      }
      const currentStart = startIndex;
      startIndex += 1;
      if (answerStarts.includes(currentStart)) {
        queueMicrotask(() => this.receive({ type: "sdp_answer", sdp: `answer-${currentStart}` }));
      }
    }

    receive(message) {
      this.dispatchEvent(new dom.window.MessageEvent("message", { data: JSON.stringify(message) }));
    }

    close() {
      if (this.readyState === FakeSocket.CLOSED) {
        return;
      }
      this.readyState = FakeSocket.CLOSED;
      this.dispatchEvent(new dom.window.Event("close"));
    }
  }

  class FakePeer extends dom.window.EventTarget {
    constructor() {
      super();
      this.index = peers.length;
      this.connectionState = "connecting";
      this.iceGatheringState = "complete";
      this.localDescription = undefined;
      this.closeCalls = 0;
      this.remoteDescriptions = [];
      this.addedTracks = [];
      this.sender = undefined;
      peers.push(this);
    }

    addTrack(track, stream) {
      this.addedTracks.push({ track, stream });
      this.sender = {
        track,
        replacements: [],
        async replaceTrack(nextTrack) {
          this.replacements.push(nextTrack);
          this.track = nextTrack;
        },
      };
      return this.sender;
    }

    getSenders() {
      return this.sender ? [this.sender] : [];
    }

    createDataChannel() {}

    createOffer() {
      if (!pendingOffers.includes(this.index)) {
        return Promise.resolve({ type: "offer", sdp: `offer-${this.index}` });
      }
      return new Promise((_resolve, reject) => {
        this.rejectOffer = reject;
      });
    }

    async setLocalDescription(description) {
      this.localDescription = description;
    }

    async setRemoteDescription(description) {
      this.remoteDescriptions.push(description);
    }

    close() {
      this.closeCalls += 1;
      this.rejectOffer?.(new Error("peer closed before offer"));
      this.rejectOffer = undefined;
    }

    fail() {
      this.connectionState = "failed";
      this.dispatchEvent(new dom.window.Event("connectionstatechange"));
    }
  }

  class FakeAudioContext {
    constructor() {
      this.state = "running";
      this.currentTime = 0;
      this.destination = {};
      this.sinkIds = [];
      audioContexts.push(this);
    }

    async resume() {}

    async setSinkId(deviceId) {
      this.sinkIds.push(deviceId);
    }

    async close() {
      this.state = "closed";
    }
  }

  class FakeMediaDevices extends dom.window.EventTarget {
    constructor() {
      super();
      this.devices = [
        { kind: "audioinput", deviceId: "mic-1", label: "Built-in microphone" },
        { kind: "audioinput", deviceId: "mic-2", label: "USB microphone" },
        { kind: "audiooutput", deviceId: "speaker-1", label: "USB speakers" },
      ];
      this.requests = [];
      this.enumerations = 0;
      this.enumerationResolvers = new Map();
    }

    async enumerateDevices() {
      const index = this.enumerations;
      this.enumerations += 1;
      if (pendingEnumerations.includes(index)) {
        return await new Promise((resolve) => {
          this.enumerationResolvers.set(index, resolve);
        });
      }
      return this.devices;
    }

    resolveEnumeration(index) {
      const resolve = this.enumerationResolvers.get(index);
      assert.ok(resolve, `enumeration ${index} must be pending`);
      this.enumerationResolvers.delete(index);
      resolve(this.devices);
    }

    async getUserMedia(constraints) {
      this.requests.push(constraints);
      const exact = constraints.audio?.deviceId?.exact;
      const deviceId = exact || "mic-1";
      const track = {
        kind: "audio",
        deviceId,
        enabled: false,
        stopCalls: 0,
        stop() {
          this.stopCalls += 1;
        },
      };
      const stream = {
        deviceId,
        getAudioTracks: () => [track],
        getTracks: () => [track],
      };
      tracks.push(track);
      streams.push(stream);
      return stream;
    }
  }

  const mediaDevices = new FakeMediaDevices();
  Object.defineProperty(dom.window.navigator, "mediaDevices", {
    configurable: true,
    value: mediaDevices,
  });

  const replacements = {
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    HTMLInputElement: dom.window.HTMLInputElement,
    Option: dom.window.Option,
    WebSocket: FakeSocket,
    RTCPeerConnection: FakePeer,
    AudioContext: FakeAudioContext,
  };
  const descriptors = new Map(
    Object.keys(replacements).map((name) => [
      name,
      Object.getOwnPropertyDescriptor(globalThis, name),
    ]),
  );
  for (const [name, value] of Object.entries(replacements)) {
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  }

  try {
    bootHarnessSequence += 1;
    await import(
      `../../src/moco/web/static/app.js?boot-conversation-harness=${bootHarnessSequence}`
    );
  } catch (error) {
    for (const [name, descriptor] of descriptors) {
      if (descriptor) {
        Object.defineProperty(globalThis, name, descriptor);
      } else {
        delete globalThis[name];
      }
    }
    dom.window.close();
    throw error;
  }

  const waitFor = async (predicate) => {
    for (let attempt = 0; attempt < 100; attempt += 1) {
      if (predicate()) {
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
    assert.fail(
      `timed out waiting for boot conversation state: peers=${peers.length} sent=${JSON.stringify(sent)} error=${dom.window.document.querySelector("#error-text").textContent}`,
    );
  };
  const requireReplacement = () => {
    sockets[0].receive({
      type: "state",
      state: "voice_reconnect_required",
      canCancel: false,
      hotkeys: { enabled: true, startListening: "f1", stopListening: "f2" },
      voice: { selected: null, options: [], ready: false, readiness: "loading" },
    });
    dom.window.document.querySelector("#listen-start").click();
  };
  const close = async () => {
    if (startIndex > 0 && sockets[0]?.readyState === FakeSocket.OPEN) {
      sockets[0].receive({ type: "sdp_answer", sdp: "cleanup-answer" });
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
    for (const socket of sockets) {
      socket.close();
    }
    await new Promise((resolve) => setTimeout(resolve, 0));
    for (const [name, descriptor] of descriptors) {
      if (descriptor) {
        Object.defineProperty(globalThis, name, descriptor);
      } else {
        delete globalThis[name];
      }
    }
    dom.window.close();
  };

  dom.window.document.querySelector("#enable").click();
  return {
    audioContexts,
    close,
    disconnect: (index = 0) => sockets[index].close(),
    dom,
    mediaDevices,
    peers,
    receive: (message) => sockets[0].receive(message),
    requireReplacement,
    retryListening: () => dom.window.document.querySelector("#listen-start").click(),
    sent,
    sockets,
    streams,
    tracks,
    waitFor,
  };
}

describe("audio device switching", () => {
  it("replaces the current microphone track without reconnecting the conversation", async () => {
    const connection = await bootConversationHarness({ answerStarts: [0] });
    try {
      await connection.waitFor(
        () =>
          connection.peers[0]?.remoteDescriptions.length === 1 &&
          connection.dom.window.document.querySelector("#audio-input").disabled === false,
      );
      const initialTrack = connection.tracks[0];
      initialTrack.enabled = true;
      const starts = connection.sent.filter(({ type }) => type === "start").length;
      const input = connection.dom.window.document.querySelector("#audio-input");

      input.value = "mic-2";
      input.dispatchEvent(new connection.dom.window.Event("change"));

      await connection.waitFor(() => connection.peers[0].sender.replacements.length === 1);
      const switchedTrack = connection.peers[0].sender.track;
      assert.equal(switchedTrack.deviceId, "mic-2");
      assert.equal(switchedTrack.enabled, true);
      assert.equal(initialTrack.stopCalls, 1);
      assert.equal(connection.sent.filter(({ type }) => type === "start").length, starts);
      assert.equal(connection.peers.length, 1);
      assert.equal(connection.sockets.length, 1);
    } finally {
      await connection.close();
    }
  });

  it("changes only the Irodori playback sink", async () => {
    const connection = await bootConversationHarness({ answerStarts: [0] });
    try {
      await connection.waitFor(
        () =>
          connection.peers[0]?.remoteDescriptions.length === 1 &&
          connection.dom.window.document.querySelector("#audio-output").disabled === false,
      );
      const starts = connection.sent.filter(({ type }) => type === "start").length;
      const mediaMessages = connection.sent.filter(
        ({ type }) => type === "audio" || type === "playback",
      ).length;
      const output = connection.dom.window.document.querySelector("#audio-output");

      output.value = "speaker-1";
      output.dispatchEvent(new connection.dom.window.Event("change"));

      await connection.waitFor(() => connection.audioContexts[0].sinkIds.length === 1);
      assert.deepEqual(connection.audioContexts[0].sinkIds, ["speaker-1"]);
      assert.equal(connection.sent.filter(({ type }) => type === "start").length, starts);
      assert.equal(connection.peers.length, 1);
      assert.equal(connection.sockets.length, 1);
      assert.equal(
        connection.sent.filter(({ type }) => type === "audio" || type === "playback").length,
        mediaMessages,
      );
    } finally {
      await connection.close();
    }
  });

  it("adds the switched microphone track to a later Voice replacement peer", async () => {
    const connection = await bootConversationHarness({ answerStarts: [0, 1] });
    try {
      await connection.waitFor(
        () =>
          connection.peers[0]?.remoteDescriptions.length === 1 &&
          connection.dom.window.document.querySelector("#audio-input").disabled === false,
      );
      const input = connection.dom.window.document.querySelector("#audio-input");
      input.value = "mic-2";
      input.dispatchEvent(new connection.dom.window.Event("change"));
      await connection.waitFor(() => connection.peers[0].sender.replacements.length === 1);
      const switchedTrack = connection.peers[0].sender.track;
      const initialTrack = connection.tracks[0];

      connection.requireReplacement();

      await connection.waitFor(() => connection.peers[1]?.remoteDescriptions.length === 1);
      assert.equal(connection.peers[1].addedTracks[0].track, switchedTrack);
      assert.equal(connection.peers[1].addedTracks[0].stream, connection.streams[1]);
      assert.equal(initialTrack.stopCalls, 1);
      assert.notEqual(connection.peers[1].addedTracks[0].track, initialTrack);
    } finally {
      await connection.close();
    }
  });

  it("enables a microphone switched while Voice reconnect is pending", async () => {
    const connection = await bootConversationHarness({ answerStarts: [0] });
    try {
      await connection.waitFor(
        () =>
          connection.peers[0]?.remoteDescriptions.length === 1 &&
          connection.dom.window.document.querySelector("#audio-input").disabled === false,
      );
      const oldTrack = connection.tracks[0];

      connection.requireReplacement();
      await connection.waitFor(
        () =>
          connection.peers.length === 2 &&
          connection.sent.filter(({ type }) => type === "start").length === 2,
      );

      const input = connection.dom.window.document.querySelector("#audio-input");
      input.value = "mic-2";
      input.dispatchEvent(new connection.dom.window.Event("change"));
      await connection.waitFor(() => connection.peers[1].sender.replacements.length === 1);
      const currentTrack = connection.peers[1].sender.track;

      connection.receive({ type: "sdp_answer", sdp: "replacement-answer" });
      await connection.waitFor(
        () => connection.sent.filter(({ type }) => type === "control").length === 1,
      );

      assert.equal(currentTrack.deviceId, "mic-2");
      assert.equal(currentTrack.enabled, true);
      assert.equal(oldTrack.enabled, false);
      assert.equal(oldTrack.stopCalls, 1);
      assert.deepEqual(
        connection.sent.filter(({ type }) => type === "control"),
        [{ type: "control", control: "listen_start" }],
      );
      assert.equal(connection.sent.filter(({ type }) => type === "start").length, 2);
      assert.equal(connection.peers.length, 2);
    } finally {
      await connection.close();
    }
  });

  it("creates a fresh device controller after a full socket reconnect", async () => {
    const connection = await bootConversationHarness({ answerStarts: [0, 1] });
    try {
      await connection.waitFor(
        () =>
          connection.peers[0]?.remoteDescriptions.length === 1 &&
          connection.dom.window.document.querySelector("#audio-input").disabled === false,
      );
      connection.disconnect();
      await connection.waitFor(
        () => connection.dom.window.document.querySelector("#audio-input").disabled === true,
      );

      connection.dom.window.document.querySelector("#enable").click();
      await connection.waitFor(
        () =>
          connection.peers[1]?.remoteDescriptions.length === 1 &&
          connection.dom.window.document.querySelector("#audio-input").disabled === false,
      );
      const input = connection.dom.window.document.querySelector("#audio-input");
      input.value = "mic-2";
      input.dispatchEvent(new connection.dom.window.Event("change"));

      await connection.waitFor(() => connection.peers[1].sender.replacements.length === 1);
      assert.equal(connection.peers[0].sender.replacements.length, 0);
      assert.equal(connection.peers[1].sender.track.deviceId, "mic-2");
      assert.equal(connection.audioContexts[0].state, "closed");
      assert.equal(connection.audioContexts.length, 2);
    } finally {
      await connection.close();
    }
  });

  it("does not let delayed device startup restore connected UI after disconnect", async () => {
    const connection = await bootConversationHarness({
      answerStarts: [0],
      pendingEnumerations: [0],
    });
    try {
      await connection.waitFor(
        () =>
          connection.peers[0]?.remoteDescriptions.length === 1 &&
          connection.mediaDevices.enumerationResolvers.has(0),
      );

      connection.disconnect();
      connection.mediaDevices.resolveEnumeration(0);
      await new Promise((resolve) => setTimeout(resolve, 0));

      assert.equal(connection.dom.window.document.querySelector("#audio-input").disabled, true);
      assert.equal(connection.dom.window.document.querySelector("#audio-output").disabled, true);
      assert.equal(connection.dom.window.document.querySelector("#listen-start").disabled, true);
      assert.equal(connection.dom.window.document.querySelector("#connection-row").hidden, false);
    } finally {
      await connection.close();
    }
  });
});

describe("browser connection timeouts", () => {
  it("settles only the lost Voice turn before a manual re-offer", async () => {
    const connection = await bootConversationHarness({ answerStarts: [0] });
    const state = {
      type: "state",
      state: "voice_reconnect_required",
      canCancel: false,
      hotkeys: { enabled: true, startListening: "f1", stopListening: "f2" },
      voice: { selected: null, options: [], ready: false, readiness: "loading" },
    };
    try {
      await connection.waitFor(() => connection.peers[0]?.remoteDescriptions.length === 1);
      connection.receive({
        type: "activity",
        kind: "turn",
        source: "voice",
        phase: "started",
        label: "応答処理",
        occurredAtMs: Date.now(),
      });
      await connection.waitFor(() =>
        connection.dom.window.document
          .querySelector("#progress-label")
          .textContent.includes("Codex"),
      );

      connection.receive(state);
      await connection.waitFor(
        () =>
          connection.dom.window.document.querySelector("#progress-label").textContent ===
          "発話を待っています",
      );

      connection.receive({
        type: "activity",
        kind: "turn",
        source: "voice",
        phase: "started",
        label: "応答処理",
        occurredAtMs: Date.now(),
      });
      connection.receive({
        type: "activity",
        kind: "turn",
        source: "voice",
        phase: "completed",
        label: "応答処理",
        occurredAtMs: Date.now(),
      });
      await connection.waitFor(
        () =>
          connection.dom.window.document.querySelector("#progress-label").textContent ===
          "発話を待っています",
      );
    } finally {
      await connection.close();
    }
  });

  it("suppresses Voice loss from the actual watcher before initial or replacement Start", async () => {
    const initial = await bootConversationHarness({ pendingOffers: [0] });
    try {
      await initial.waitFor(() => initial.peers.length === 1);
      initial.peers[0].fail();
      initial.peers[0].fail();
      await new Promise((resolve) => setTimeout(resolve, 0));
      assert.deepEqual(
        initial.sent.filter(({ type }) => type === "voice_lost"),
        [],
      );
      assert.equal(initial.peers[0].closeCalls, 1);
    } finally {
      await initial.close();
    }

    const replacement = await bootConversationHarness({
      answerStarts: [0],
      pendingOffers: [1],
    });
    try {
      await replacement.waitFor(() => replacement.peers[0]?.remoteDescriptions.length === 1);
      replacement.requireReplacement();
      await replacement.waitFor(() => replacement.peers.length === 2);
      replacement.peers[1].fail();
      replacement.peers[1].fail();
      await new Promise((resolve) => setTimeout(resolve, 0));
      assert.deepEqual(
        replacement.sent.filter(({ type }) => type === "voice_lost"),
        [],
      );
      assert.equal(replacement.peers[1].closeCalls, 1);
    } finally {
      await replacement.close();
    }
  });

  it("sends Voice loss once from the actual watcher for published Voice phases", async () => {
    const established = await bootConversationHarness({ answerStarts: [0] });
    try {
      await established.waitFor(() => established.peers[0]?.remoteDescriptions.length === 1);
      established.peers[0].fail();
      established.peers[0].fail();
      await new Promise((resolve) => setTimeout(resolve, 0));
      assert.deepEqual(
        established.sent.filter(({ type }) => type === "voice_lost"),
        [{ type: "voice_lost" }],
      );
      assert.equal(established.peers[0].closeCalls, 1);
    } finally {
      await established.close();
    }

    const replacement = await bootConversationHarness({ answerStarts: [0] });
    try {
      await replacement.waitFor(() => replacement.peers[0]?.remoteDescriptions.length === 1);
      replacement.requireReplacement();
      await replacement.waitFor(
        () => replacement.sent.filter(({ type }) => type === "start").length === 2,
      );
      replacement.peers[1].fail();
      replacement.peers[1].fail();
      await new Promise((resolve) => setTimeout(resolve, 0));
      assert.deepEqual(
        replacement.sent.filter(({ type }) => type === "voice_lost"),
        [{ type: "voice_lost" }],
      );
      assert.equal(replacement.peers[1].closeCalls, 1);
    } finally {
      await replacement.close();
    }
  });

  it("rejects the failed peer handshake and ignores its delayed answer during manual retry", async () => {
    const connection = await bootConversationHarness({ answerStarts: [0] });
    try {
      await connection.waitFor(() => connection.peers[0]?.remoteDescriptions.length === 1);
      connection.requireReplacement();
      await connection.waitFor(
        () => connection.sent.filter(({ type }) => type === "start").length === 2,
      );

      connection.peers[1].fail();
      await connection.waitFor(
        () => connection.sent.filter(({ type }) => type === "voice_lost").length === 1,
      );
      await new Promise((resolve) => setTimeout(resolve, 0));
      connection.retryListening();
      await connection.waitFor(
        () => connection.sent.filter(({ type }) => type === "start").length === 3,
      );

      connection.receive({ type: "sdp_answer", sdp: "delayed-old-answer" });
      await new Promise((resolve) => setTimeout(resolve, 0));
      assert.deepEqual(connection.peers[2].remoteDescriptions, []);

      connection.receive({ type: "sdp_answer", sdp: "current-answer" });
      await connection.waitFor(() => connection.peers[2].remoteDescriptions.length === 1);
      await connection.waitFor(
        () => connection.sent.filter(({ type }) => type === "control").length === 1,
      );
      assert.deepEqual(connection.peers[2].remoteDescriptions, [
        { type: "answer", sdp: "current-answer" },
      ]);
      assert.deepEqual(
        connection.sent.filter(({ type }) => type === "voice_lost"),
        [{ type: "voice_lost" }],
      );
      assert.deepEqual(
        connection.sent.filter(({ type }) => type === "control"),
        [{ type: "control", control: "listen_start" }],
      );
    } finally {
      await connection.close();
    }
  });

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

  it("keeps the socket open after a replacement failure", () => {
    const socket = {
      closes: 0,
      close() {
        this.closes += 1;
      },
    };
    const failures = [];
    const error = new Error("failed replacement SDP");

    assert.equal(
      closeSocketForFailure(socket, error, (failure) => failures.push(failure), {
        preserveTransport: true,
      }),
      false,
    );
    assert.equal(socket.closes, 0);
    assert.deepEqual(failures, []);
  });

  it("closes only the current failed peer before manual reconnect", () => {
    const stalePeer = {
      closes: 0,
      close() {
        this.closes += 1;
      },
    };
    const currentPeer = {
      closes: 0,
      close() {
        this.closes += 1;
      },
    };
    let stops = 0;
    let reconnects = 0;
    const cleanup = {
      stopWatching: () => {
        stops += 1;
      },
      requireReconnect: () => {
        reconnects += 1;
      },
    };

    assert.equal(closeCurrentPeer(stalePeer, currentPeer, cleanup), false);
    assert.equal(closeCurrentPeer(currentPeer, currentPeer, cleanup), true);
    assert.deepEqual(
      { staleCloses: stalePeer.closes, currentCloses: currentPeer.closes, stops, reconnects },
      { staleCloses: 0, currentCloses: 1, stops: 1, reconnects: 1 },
    );
  });

  it("notifies Voice loss only for a current replacement after Start was sent", () => {
    const run = ({ replacement, startSent, stale = false }) => {
      const sent = [];
      let reconnects = 0;
      const currentPeer = {
        closes: 0,
        close() {
          this.closes += 1;
        },
      };
      const failedPeer = stale
        ? {
            closes: 0,
            close() {
              this.closes += 1;
            },
          }
        : currentPeer;
      const closed = closeFailedHandshakePeer(failedPeer, currentPeer, {
        replacement,
        startSent,
        stopWatching: () => {},
        requireReconnect: () => {
          reconnects += 1;
        },
        send: (message) => sent.push(message),
      });
      return { closed, currentCloses: currentPeer.closes, reconnects, sent };
    };

    assert.deepEqual(run({ replacement: true, startSent: true }), {
      closed: true,
      currentCloses: 1,
      reconnects: 1,
      sent: [{ type: "voice_lost" }],
    });
    for (const input of [
      { replacement: false, startSent: true },
      { replacement: true, startSent: false },
      { replacement: true, startSent: true, stale: true },
    ]) {
      const result = run(input);
      assert.deepEqual(result.sent, []);
      assert.equal(result.closed, input.stale !== true);
    }
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
      "caption_unsupported",
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
      constructor(options) {
        order.push(["construct", options]);
      }

      resume() {
        order.push("resume");
        return Promise.resolve();
      }
    }

    const activation = beginAudioActivation(FakeAudioContext);
    order.push("permission");
    await activation.ready;

    assert.deepEqual(order, [["construct", { sampleRate: 48_000 }], "resume", "permission"]);
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
