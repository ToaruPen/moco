import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  BrowserHotkeyMapper,
  closeAudioContext,
  MocoController,
  waitForIce,
  waitForSocketOpen,
} from "../../src/moco/web/static/app.js";

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
  it("enables the microphone only while push-to-talk is held", async () => {
    const { controller, track } = harness();

    await controller.applyControl("ptt_down");
    assert.equal(track.enabled, true);

    await controller.applyControl("ptt_up");
    assert.equal(track.enabled, false);
  });

  it("cancel disables capture and invalidates old audio", async () => {
    const { controller, playback, track } = harness();
    const generation = controller.audioGeneration;

    await controller.applyControl("cancel");

    assert.equal(track.enabled, false);
    assert.equal(controller.audioGeneration, generation + 1);
    assert.equal(playback.isPlaying, false);
  });

  it("reconnects an idle-expired conversation before recording", async () => {
    const { controller } = harness();
    let reconnects = 0;
    controller.reconnect = async () => {
      reconnects += 1;
    };
    controller.idleExpired = true;

    await controller.applyControl("ptt_down");

    assert.equal(reconnects, 1);
  });
});

describe("BrowserHotkeyMapper", () => {
  it("defers keyboard input to the global listener when it is enabled", () => {
    const mapper = new BrowserHotkeyMapper({
      globalHotkeysEnabled: true,
      pttKey: "v",
      cancelKey: "escape",
    });

    assert.equal(mapper.keyDown("v"), null);
    assert.equal(mapper.keyUp("v"), null);
  });

  it("maps configured browser fallback keys without repeated controls", () => {
    const mapper = new BrowserHotkeyMapper({
      globalHotkeysEnabled: false,
      pttKey: "v",
      cancelKey: "escape",
    });

    assert.equal(mapper.keyDown("v"), "ptt_down");
    assert.equal(mapper.keyDown("v"), null);
    assert.equal(mapper.keyUp("v"), "ptt_up");
    assert.equal(mapper.keyDown("escape"), "cancel");
    assert.equal(mapper.keyUp("escape"), null);
  });

  it("preserves a held fallback key across lifecycle state updates", () => {
    const settings = {
      globalHotkeysEnabled: false,
      pttKey: "v",
      cancelKey: "escape",
    };
    const mapper = new BrowserHotkeyMapper(settings);

    assert.equal(mapper.keyDown("v"), "ptt_down");
    mapper.configure(settings);

    assert.equal(mapper.keyUp("v"), "ptt_up");
  });
});

describe("browser connection timeouts", () => {
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
