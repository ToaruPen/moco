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
  shouldHandleHotkey,
  ThemePanel,
  VoiceModelController,
  waitForIce,
  waitForSocketOpen,
  watchPeerFailure,
} = appModule;

describe("AudioPlaybackQueue", () => {
  it("reports a decode error and continues with later audio", async () => {
    const errors = [];
    const states = [];
    let attempts = 0;
    const context = {
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
    const queue = new AudioPlaybackQueue(
      context,
      (active) => states.push(active),
      (code) => errors.push(code),
    );

    queue.enqueue(new ArrayBuffer(1));
    await assert.doesNotReject(queue.chain);
    queue.enqueue(new ArrayBuffer(1));
    await assert.doesNotReject(queue.chain);

    assert.equal(attempts, 2);
    assert.deepEqual(errors, ["audio_decode_failed"]);
    assert.deepEqual(states, [false, true]);
  });

  it("recovers when an audio source cannot start", async () => {
    const errors = [];
    const states = [];
    const source = new EventTarget();
    source.connect = () => {};
    source.start = () => {
      throw new Error("output unavailable");
    };
    const context = {
      currentTime: 0,
      destination: {},
      async decodeAudioData() {
        return { duration: 0.1 };
      },
      createBufferSource: () => source,
    };
    const queue = new AudioPlaybackQueue(
      context,
      (active) => states.push(active),
      (code) => errors.push(code),
    );

    queue.enqueue(new ArrayBuffer(1));
    await assert.doesNotReject(queue.chain);

    assert.equal(queue.isPlaying, false);
    assert.deepEqual(states, [false]);
    assert.deepEqual(errors, ["audio_start_failed"]);
  });
});

function harness() {
  const messages = [];
  const track = { enabled: false };
  const playback = {
    isPlaying: true,
    stops: 0,
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
  it("renders configured models and sends an immediate selection", () => {
    const messages = [];
    const select = {
      disabled: true,
      options: [],
      value: "",
      replaceChildren(...options) {
        this.options = options;
      },
    };
    const controller = new VoiceModelController({
      select,
      send: (message) => messages.push(message),
      createOption: (label, value) => ({ label, value }),
    });

    controller.configure({
      options: ["kasumi", "alternate"],
      selected: "kasumi",
    });
    select.value = "alternate";
    controller.select("alternate");

    assert.equal(select.disabled, false);
    assert.deepEqual(select.options, [
      { label: "NARRATOR / DEFAULT", value: "" },
      { label: "kasumi", value: "kasumi" },
      { label: "alternate", value: "alternate" },
    ]);
    assert.equal(select.value, "kasumi");
    assert.deepEqual(messages, [{ type: "select_voice", speaker: "alternate" }]);
    controller.confirm("alternate");
    assert.equal(select.value, "alternate");
  });
});

describe("browser connection timeouts", () => {
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

  it("waits for the SDP answer and rejects a terminal start error", async () => {
    const descriptions = [];
    const success = new ConversationHandshake(async (sdp) => descriptions.push(sdp));
    const waitingForSuccess = success.promise;

    assert.equal(await success.consume({ type: "sdp_answer", sdp: "answer-sdp" }), true);
    await waitingForSuccess;
    assert.deepEqual(descriptions, ["answer-sdp"]);

    const failure = new ConversationHandshake(async () => {});
    const waitingForFailure = assert.rejects(
      failure.promise,
      (error) => error.name === "conversation_start_failed",
    );
    assert.equal(
      await failure.consume({ type: "error", code: "conversation_start_failed" }),
      false,
    );
    await waitingForFailure;
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
