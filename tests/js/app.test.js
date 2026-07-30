import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { MocoController } from "../../src/moco/web/static/app.js";

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
  it("enables the microphone only while F1 is held", async () => {
    const { controller, track } = harness();

    await controller.applyControl("ptt_down");
    assert.equal(track.enabled, true);

    await controller.applyControl("ptt_up");
    assert.equal(track.enabled, false);
  });

  it("F2 disables capture and invalidates old audio", async () => {
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
