import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { JSDOM } from "jsdom";

import { AudioDeviceController } from "../../src/moco/web/static/media-devices.js";

class FakeMediaDevices extends EventTarget {
  constructor(devices = []) {
    super();
    this.devices = devices;
    this.enumerateCalls = 0;
    this.deviceChangeAdds = 0;
    this.deviceChangeRemoves = 0;
    this.enumerationError = null;
    this.enumerationResults = [];
    this.getUserMediaCalls = [];
    this.getUserMediaResults = [];
  }

  async enumerateDevices() {
    this.enumerateCalls += 1;
    if (this.enumerationError) {
      throw this.enumerationError;
    }
    if (this.enumerationResults.length > 0) {
      return this.enumerationResults.shift();
    }
    return this.devices;
  }

  async getUserMedia(constraints) {
    this.getUserMediaCalls.push(constraints);
    const result = this.getUserMediaResults.shift();
    if (result instanceof Error) {
      throw result;
    }
    return await result;
  }

  addEventListener(type, listener, options) {
    if (type === "devicechange") {
      this.deviceChangeAdds += 1;
    }
    super.addEventListener(type, listener, options);
  }

  removeEventListener(type, listener, options) {
    if (type === "devicechange") {
      this.deviceChangeRemoves += 1;
    }
    super.removeEventListener(type, listener, options);
  }
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

function createTrack({ enabled = true, events, name = "track", readyState = "live" } = {}) {
  return {
    enabled,
    readyState,
    stopCalls: 0,
    stop() {
      this.stopCalls += 1;
      this.readyState = "ended";
      events?.push(`stop:${name}`);
    },
  };
}

function createStream(audioTracks = [], tracks = audioTracks) {
  return {
    getAudioTracks: () => audioTracks,
    getTracks: () => tracks,
  };
}

class FakeStorage {
  constructor(entries = {}) {
    this.items = new Map(Object.entries(entries));
    this.getCalls = [];
    this.setCalls = [];
    this.removeCalls = [];
  }

  getItem(key) {
    this.getCalls.push(key);
    return this.items.get(key) ?? null;
  }

  setItem(key, value) {
    this.setCalls.push([key, value]);
    this.items.set(key, value);
  }

  removeItem(key) {
    this.removeCalls.push(key);
    this.items.delete(key);
  }
}

function createController({
  context = {},
  currentStream,
  devices = [],
  dom = new JSDOM(`
    <select id="audio-input" disabled></select>
    <select id="audio-output" disabled></select>
  `),
  getAudioSender,
  getCurrentStream,
  mediaDevices = new FakeMediaDevices(devices),
  onError,
  onReplaceCurrentStream,
  sender,
  storage = new FakeStorage(),
} = {}) {
  const document = dom.window.document;
  const inputSelect = document.querySelector("#audio-input");
  const outputSelect = document.querySelector("#audio-output");
  let ownedStream = currentStream;
  const streamReplacements = [];
  const controller = new AudioDeviceController({
    inputSelect,
    outputSelect,
    context,
    mediaDevices,
    storage,
    getCurrentStream: getCurrentStream ?? (() => ownedStream),
    getAudioSender: getAudioSender ?? (() => sender),
    replaceCurrentStream: (nextStream) => {
      streamReplacements.push(nextStream);
      ownedStream = nextStream;
      onReplaceCurrentStream?.(nextStream);
    },
    onError,
    createOption: (label, value) => new dom.window.Option(label, value),
  });

  return {
    controller,
    currentStream: () => ownedStream,
    inputSelect,
    mediaDevices,
    outputSelect,
    storage,
    streamReplacements,
  };
}

function options(select) {
  return [...select.options].map((option) => [option.value, option.textContent]);
}

function setCurrentInput(setup, deviceId) {
  setup.controller.inputId = deviceId;
  setup.inputSelect.value = deviceId;
  setup.storage.setItem("moco.audio.inputDeviceId", deviceId);
}

function setCurrentOutput(setup, deviceId) {
  setup.controller.outputId = deviceId;
  setup.outputSelect.value = deviceId;
  setup.storage.setItem("moco.audio.outputDeviceId", deviceId);
}

describe("AudioDeviceController", () => {
  it("atomically switches to an explicit microphone while preserving MIC ON", async () => {
    const events = [];
    const oldTrack = createTrack({ enabled: true, events, name: "old" });
    const nextTrack = createTrack({ enabled: false, events, name: "next" });
    const oldStream = createStream([oldTrack]);
    const nextStream = createStream([nextTrack]);
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
        events.push("replaceTrack");
      },
    };
    const setup = createController({
      currentStream: oldStream,
      devices: [
        { kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" },
        { kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" },
      ],
      onReplaceCurrentStream: () => events.push("replaceCurrentStream"),
      sender,
    });
    setup.mediaDevices.getUserMediaResults.push(nextStream);
    await setup.controller.start();

    const switched = await setup.controller.selectInput("mic-2");

    assert.equal(switched, true);
    assert.deepEqual(setup.mediaDevices.getUserMediaCalls, [
      { audio: { deviceId: { exact: "mic-2" } } },
    ]);
    assert.equal(nextTrack.enabled, true);
    assert.deepEqual(sender.replacements, [nextTrack]);
    assert.deepEqual(setup.streamReplacements, [nextStream]);
    assert.equal(setup.currentStream(), nextStream);
    assert.equal(oldTrack.stopCalls, 1);
    assert.deepEqual(events, ["replaceTrack", "replaceCurrentStream", "stop:old"]);
    assert.equal(setup.inputSelect.value, "mic-2");
    assert.equal(setup.controller.inputId, "mic-2");
    assert.equal(setup.storage.getItem("moco.audio.inputDeviceId"), "mic-2");
  });

  it("preserves MIC OFF when switching microphones", async () => {
    const oldTrack = createTrack({ enabled: false });
    const nextTrack = createTrack({ enabled: true });
    const oldStream = createStream([oldTrack]);
    const nextStream = createStream([nextTrack]);
    const sender = { async replaceTrack() {} };
    const setup = createController({
      currentStream: oldStream,
      devices: [{ kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" }],
      sender,
    });
    setup.mediaDevices.getUserMediaResults.push(nextStream);
    await setup.controller.start();

    await setup.controller.selectInput("mic-2");

    assert.equal(nextTrack.enabled, false);
  });

  it("switches input to the system default and removes its stored selection", async () => {
    const oldTrack = createTrack();
    const defaultTrack = createTrack();
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
      },
    };
    const storage = new FakeStorage();
    const setup = createController({
      currentStream: createStream([oldTrack]),
      devices: [{ kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" }],
      sender,
      storage,
    });
    setup.controller.inputId = "mic-2";
    setup.mediaDevices.getUserMediaResults.push(createStream([defaultTrack]));
    await setup.controller.start();
    storage.setItem("moco.audio.inputDeviceId", "mic-2");

    const switched = await setup.controller.selectInput("");

    assert.equal(switched, true);
    assert.deepEqual(setup.mediaDevices.getUserMediaCalls, [{ audio: true }]);
    assert.deepEqual(sender.replacements, [defaultTrack]);
    assert.equal(setup.controller.inputId, "");
    assert.equal(setup.inputSelect.value, "");
    assert.equal(storage.getItem("moco.audio.inputDeviceId"), null);
  });

  it("serializes rapid microphone switches and keeps input disabled until both finish", async () => {
    const first = deferred();
    const second = deferred();
    const oldTrack = createTrack();
    const secondTrack = createTrack();
    const thirdTrack = createTrack();
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
      },
    };
    const setup = createController({
      currentStream: createStream([oldTrack]),
      devices: [
        { kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" },
        { kind: "audioinput", deviceId: "mic-3", label: "Microphone 3" },
      ],
      sender,
    });
    setup.mediaDevices.getUserMediaResults.push(first.promise, second.promise);
    await setup.controller.start();

    const switchingToSecond = setup.controller.selectInput("mic-2");
    const switchingToThird = setup.controller.selectInput("mic-3");

    assert.equal(setup.inputSelect.disabled, true);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(setup.mediaDevices.getUserMediaCalls.length, 1);
    first.resolve(createStream([secondTrack]));
    assert.equal(await switchingToSecond, true);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(setup.inputSelect.disabled, true);
    assert.equal(setup.mediaDevices.getUserMediaCalls.length, 2);
    second.resolve(createStream([thirdTrack]));
    assert.equal(await switchingToThird, true);

    assert.equal(setup.inputSelect.disabled, false);
    assert.deepEqual(sender.replacements, [secondTrack, thirdTrack]);
    assert.equal(setup.currentStream().getAudioTracks()[0], thirdTrack);
    assert.equal(setup.controller.inputId, "mic-3");
    assert.equal(setup.inputSelect.value, "mic-3");
    assert.equal(setup.storage.getItem("moco.audio.inputDeviceId"), "mic-3");
  });

  it("keeps the current input route when microphone acquisition fails", async () => {
    const errors = [];
    const oldTrack = createTrack();
    const oldStream = createStream([oldTrack]);
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
      },
    };
    const setup = createController({
      currentStream: oldStream,
      devices: [
        { kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" },
        { kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" },
      ],
      onError: (code) => errors.push(code),
      sender,
    });
    setup.mediaDevices.getUserMediaResults.push(new Error("private device detail"));
    await setup.controller.start();
    setCurrentInput(setup, "mic-1");

    const switched = await setup.controller.selectInput("mic-2");

    assert.equal(switched, false);
    assert.equal(setup.currentStream(), oldStream);
    assert.equal(oldTrack.stopCalls, 0);
    assert.deepEqual(sender.replacements, []);
    assert.deepEqual(setup.streamReplacements, []);
    assert.equal(setup.controller.inputId, "mic-1");
    assert.equal(setup.inputSelect.value, "mic-1");
    assert.equal(setup.storage.getItem("moco.audio.inputDeviceId"), "mic-1");
    assert.deepEqual(errors, ["microphone_switch_failed"]);
  });

  it("stops only the candidate and rolls back display and storage when replaceTrack fails", async () => {
    const errors = [];
    const oldTrack = createTrack();
    const nextTrack = createTrack();
    const oldStream = createStream([oldTrack]);
    const nextStream = createStream([nextTrack]);
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
        throw new Error("private peer detail");
      },
    };
    const setup = createController({
      currentStream: oldStream,
      devices: [
        { kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" },
        { kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" },
      ],
      onError: (code) => errors.push(code),
      sender,
    });
    setup.mediaDevices.getUserMediaResults.push(nextStream);
    await setup.controller.start();
    setCurrentInput(setup, "mic-1");

    const switched = await setup.controller.selectInput("mic-2");

    assert.equal(switched, false);
    assert.deepEqual(sender.replacements, [nextTrack]);
    assert.equal(nextTrack.stopCalls, 1);
    assert.equal(oldTrack.stopCalls, 0);
    assert.equal(setup.currentStream(), oldStream);
    assert.deepEqual(setup.streamReplacements, []);
    assert.equal(setup.controller.inputId, "mic-1");
    assert.equal(setup.inputSelect.value, "mic-1");
    assert.equal(setup.storage.getItem("moco.audio.inputDeviceId"), "mic-1");
    assert.deepEqual(errors, ["microphone_switch_failed"]);
  });

  it("safely rejects input switches with a missing candidate track, current track, or sender", async (t) => {
    const cases = [
      {
        name: "candidate audio track",
        candidate: createStream([], [createTrack()]),
        current: createStream([createTrack()]),
        sender: { async replaceTrack() {} },
      },
      {
        name: "current audio track",
        candidate: createStream([createTrack()]),
        current: createStream([], [createTrack()]),
        sender: { async replaceTrack() {} },
      },
      {
        name: "audio sender",
        candidate: createStream([createTrack()]),
        current: createStream([createTrack()]),
        sender: undefined,
      },
    ];

    for (const testCase of cases) {
      await t.test(testCase.name, async () => {
        const errors = [];
        const setup = createController({
          currentStream: testCase.current,
          devices: [{ kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" }],
          onError: (code) => errors.push(code),
          sender: testCase.sender,
        });
        setup.mediaDevices.getUserMediaResults.push(testCase.candidate);
        await setup.controller.start();

        const switched = await setup.controller.selectInput("mic-2");

        assert.equal(switched, false);
        assert.equal(setup.currentStream(), testCase.current);
        assert.deepEqual(setup.streamReplacements, []);
        assert.ok(testCase.candidate.getTracks().every((track) => track.stopCalls === 1));
        assert.deepEqual(errors, ["microphone_switch_failed"]);
      });
    }
  });

  it("cancels a pending input switch without an error when closed", async () => {
    const pending = deferred();
    const errors = [];
    const oldTrack = createTrack();
    const nextTrack = createTrack();
    const oldStream = createStream([oldTrack]);
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
      },
    };
    const setup = createController({
      currentStream: oldStream,
      devices: [
        { kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" },
        { kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" },
      ],
      onError: (code) => errors.push(code),
      sender,
    });
    setup.mediaDevices.getUserMediaResults.push(pending.promise);
    await setup.controller.start();
    setCurrentInput(setup, "mic-1");

    const switching = setup.controller.selectInput("mic-2");
    await new Promise((resolve) => setImmediate(resolve));
    setup.controller.close();
    pending.resolve(createStream([nextTrack]));

    assert.equal(await switching, false);
    assert.equal(nextTrack.stopCalls, 1);
    assert.equal(oldTrack.stopCalls, 0);
    assert.deepEqual(sender.replacements, []);
    assert.equal(setup.currentStream(), oldStream);
    assert.deepEqual(setup.streamReplacements, []);
    assert.equal(setup.controller.inputId, "mic-1");
    assert.equal(setup.storage.getItem("moco.audio.inputDeviceId"), "mic-1");
    assert.deepEqual(errors, []);
    assert.equal(setup.inputSelect.disabled, true);
    assert.equal(setup.outputSelect.disabled, true);
  });

  it("switches output only after setSinkId succeeds and disables only output while pending", async () => {
    const pending = deferred();
    const context = {
      calls: [],
      async setSinkId(deviceId) {
        this.calls.push(deviceId);
        await pending.promise;
      },
    };
    const setup = createController({
      context,
      devices: [{ kind: "audiooutput", deviceId: "speaker-2", label: "Speaker 2" }],
    });
    await setup.controller.start();

    const switching = setup.controller.selectOutput("speaker-2");
    assert.equal(setup.inputSelect.disabled, false);
    assert.equal(setup.outputSelect.disabled, true);
    assert.equal(setup.controller.outputId, "");
    assert.equal(setup.storage.getItem("moco.audio.outputDeviceId"), null);
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(context.calls, ["speaker-2"]);
    pending.resolve();

    assert.equal(await switching, true);
    assert.equal(setup.controller.outputId, "speaker-2");
    assert.equal(setup.outputSelect.value, "speaker-2");
    assert.equal(setup.storage.getItem("moco.audio.outputDeviceId"), "speaker-2");
    assert.equal(setup.outputSelect.disabled, false);
  });

  it("switches output to the system default and removes its stored selection", async () => {
    const context = {
      calls: [],
      async setSinkId(deviceId) {
        this.calls.push(deviceId);
      },
    };
    const setup = createController({
      context,
      devices: [{ kind: "audiooutput", deviceId: "speaker-2", label: "Speaker 2" }],
    });
    await setup.controller.start();
    setCurrentOutput(setup, "speaker-2");

    const switched = await setup.controller.selectOutput("");

    assert.equal(switched, true);
    assert.deepEqual(context.calls, [""]);
    assert.equal(setup.controller.outputId, "");
    assert.equal(setup.outputSelect.value, "");
    assert.equal(setup.storage.getItem("moco.audio.outputDeviceId"), null);
  });

  it("keeps the current output route when setSinkId fails", async () => {
    const errors = [];
    const context = {
      calls: [],
      async setSinkId(deviceId) {
        this.calls.push(deviceId);
        throw new Error("private sink detail");
      },
    };
    const setup = createController({
      context,
      devices: [
        { kind: "audiooutput", deviceId: "speaker-1", label: "Speaker 1" },
        { kind: "audiooutput", deviceId: "speaker-2", label: "Speaker 2" },
      ],
      onError: (code) => errors.push(code),
    });
    await setup.controller.start();
    setCurrentOutput(setup, "speaker-1");

    const switched = await setup.controller.selectOutput("speaker-2");

    assert.equal(switched, false);
    assert.deepEqual(context.calls, ["speaker-2"]);
    assert.equal(setup.controller.outputId, "speaker-1");
    assert.equal(setup.outputSelect.value, "speaker-1");
    assert.equal(setup.storage.getItem("moco.audio.outputDeviceId"), "speaker-1");
    assert.deepEqual(errors, ["audio_output_switch_failed"]);
  });

  it("returns false without an error when output selection is unsupported", async () => {
    const errors = [];
    const setup = createController({ onError: (code) => errors.push(code) });
    await setup.controller.start();

    const switched = await setup.controller.selectOutput("speaker-2");

    assert.equal(switched, false);
    assert.equal(setup.controller.outputId, "");
    assert.equal(setup.outputSelect.disabled, true);
    assert.deepEqual(errors, []);
  });

  it("serializes rapid output switches without blocking input", async () => {
    const first = deferred();
    const second = deferred();
    const context = {
      calls: [],
      results: [first.promise, second.promise],
      async setSinkId(deviceId) {
        this.calls.push(deviceId);
        await this.results.shift();
      },
    };
    const setup = createController({
      context,
      devices: [
        { kind: "audiooutput", deviceId: "speaker-2", label: "Speaker 2" },
        { kind: "audiooutput", deviceId: "speaker-3", label: "Speaker 3" },
      ],
    });
    await setup.controller.start();

    const switchingToSecond = setup.controller.selectOutput("speaker-2");
    const switchingToThird = setup.controller.selectOutput("speaker-3");
    assert.equal(setup.inputSelect.disabled, false);
    assert.equal(setup.outputSelect.disabled, true);
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(context.calls, ["speaker-2"]);
    first.resolve();
    assert.equal(await switchingToSecond, true);
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(context.calls, ["speaker-2", "speaker-3"]);
    assert.equal(setup.outputSelect.disabled, true);
    second.resolve();

    assert.equal(await switchingToThird, true);
    assert.equal(setup.outputSelect.disabled, false);
    assert.equal(setup.controller.outputId, "speaker-3");
    assert.equal(setup.outputSelect.value, "speaker-3");
    assert.equal(setup.storage.getItem("moco.audio.outputDeviceId"), "speaker-3");
  });

  it("restores available stored input and output IDs through the live routes", async () => {
    const oldTrack = createTrack({ enabled: false });
    const nextTrack = createTrack({ enabled: true });
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
      },
    };
    const context = {
      calls: [],
      async setSinkId(deviceId) {
        this.calls.push(deviceId);
      },
    };
    const storage = new FakeStorage({
      "moco.audio.inputDeviceId": "mic-2",
      "moco.audio.outputDeviceId": "speaker-2",
    });
    const setup = createController({
      context,
      currentStream: createStream([oldTrack]),
      devices: [
        { kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" },
        { kind: "audiooutput", deviceId: "speaker-2", label: "Speaker 2" },
      ],
      sender,
      storage,
    });
    const nextStream = createStream([nextTrack]);
    setup.mediaDevices.getUserMediaResults.push(nextStream);

    await setup.controller.start();

    assert.deepEqual(setup.mediaDevices.getUserMediaCalls, [
      { audio: { deviceId: { exact: "mic-2" } } },
    ]);
    assert.equal(nextTrack.enabled, false);
    assert.deepEqual(sender.replacements, [nextTrack]);
    assert.equal(setup.currentStream(), nextStream);
    assert.deepEqual(context.calls, ["speaker-2"]);
    assert.equal(setup.controller.inputId, "mic-2");
    assert.equal(setup.controller.outputId, "speaker-2");
    assert.equal(setup.inputSelect.value, "mic-2");
    assert.equal(setup.outputSelect.value, "speaker-2");
  });

  it("removes missing stored IDs and ignores saved output when selection is unsupported", async () => {
    const storage = new FakeStorage({
      "moco.audio.inputDeviceId": "missing-mic",
      "moco.audio.outputDeviceId": "speaker-2",
    });
    const setup = createController({
      devices: [
        { kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" },
        { kind: "audiooutput", deviceId: "speaker-2", label: "Speaker 2" },
      ],
      storage,
    });

    await setup.controller.start();

    assert.deepEqual(setup.mediaDevices.getUserMediaCalls, []);
    assert.equal(storage.getItem("moco.audio.inputDeviceId"), null);
    assert.equal(storage.getItem("moco.audio.outputDeviceId"), null);
    assert.equal(setup.controller.inputId, "");
    assert.equal(setup.controller.outputId, "");
    assert.equal(setup.inputSelect.value, "");
    assert.equal(setup.outputSelect.value, "");
    assert.equal(setup.outputSelect.disabled, true);
  });

  it("keeps successful live routes when every storage operation throws", async () => {
    const storage = {
      getItem() {
        throw new Error("storage read denied");
      },
      setItem() {
        throw new Error("storage write denied");
      },
      removeItem() {
        throw new Error("storage remove denied");
      },
    };
    const firstTrack = createTrack();
    const secondTrack = createTrack();
    const thirdTrack = createTrack();
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
      },
    };
    const context = {
      calls: [],
      async setSinkId(deviceId) {
        this.calls.push(deviceId);
      },
    };
    const setup = createController({
      context,
      currentStream: createStream([firstTrack]),
      devices: [
        { kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" },
        { kind: "audiooutput", deviceId: "speaker-2", label: "Speaker 2" },
      ],
      sender,
      storage,
    });
    const secondStream = createStream([secondTrack]);
    const thirdStream = createStream([thirdTrack]);
    setup.mediaDevices.getUserMediaResults.push(secondStream, thirdStream);

    await assert.doesNotReject(setup.controller.start());
    assert.equal(await setup.controller.selectInput("mic-2"), true);
    assert.equal(await setup.controller.selectOutput("speaker-2"), true);
    assert.equal(await setup.controller.selectInput(""), true);
    assert.equal(await setup.controller.selectOutput(""), true);

    assert.equal(setup.currentStream(), thirdStream);
    assert.deepEqual(sender.replacements, [secondTrack, thirdTrack]);
    assert.deepEqual(context.calls, ["speaker-2", ""]);
    assert.equal(setup.controller.inputId, "");
    assert.equal(setup.controller.outputId, "");
    assert.equal(setup.inputSelect.value, "");
    assert.equal(setup.outputSelect.value, "");
  });

  it("falls back both disconnected selected devices through their normal live routes", async () => {
    const oldTrack = createTrack();
    const selectedTrack = createTrack();
    const defaultTrack = createTrack();
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
      },
    };
    const context = {
      calls: [],
      async setSinkId(deviceId) {
        this.calls.push(deviceId);
      },
    };
    const setup = createController({
      context,
      currentStream: createStream([oldTrack]),
      devices: [
        { kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" },
        { kind: "audiooutput", deviceId: "speaker-2", label: "Speaker 2" },
      ],
      sender,
    });
    setup.mediaDevices.getUserMediaResults.push(
      createStream([selectedTrack]),
      createStream([defaultTrack]),
    );
    await setup.controller.start();
    await setup.controller.selectInput("mic-2");
    await setup.controller.selectOutput("speaker-2");
    setup.mediaDevices.getUserMediaCalls.length = 0;
    sender.replacements.length = 0;
    context.calls.length = 0;
    setup.mediaDevices.devices = [];

    await setup.controller.refresh();

    assert.deepEqual(setup.mediaDevices.getUserMediaCalls, [{ audio: true }]);
    assert.deepEqual(sender.replacements, [defaultTrack]);
    assert.deepEqual(context.calls, [""]);
    assert.equal(setup.controller.inputId, "");
    assert.equal(setup.controller.outputId, "");
    assert.equal(setup.inputSelect.value, "");
    assert.equal(setup.outputSelect.value, "");
    assert.deepEqual(options(setup.inputSelect), [["", "システム既定"]]);
    assert.deepEqual(options(setup.outputSelect), [["", "システム既定"]]);
    assert.equal(setup.storage.getItem("moco.audio.inputDeviceId"), null);
    assert.equal(setup.storage.getItem("moco.audio.outputDeviceId"), null);
  });

  it("does not reroute selected devices that remain in the refreshed catalog", async () => {
    const track = createTrack();
    const sender = {
      replacements: [],
      async replaceTrack(nextTrack) {
        this.replacements.push(nextTrack);
      },
    };
    const context = {
      calls: [],
      async setSinkId(deviceId) {
        this.calls.push(deviceId);
      },
    };
    const devices = [
      { kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" },
      { kind: "audiooutput", deviceId: "speaker-2", label: "Speaker 2" },
    ];
    const setup = createController({
      context,
      currentStream: createStream([track]),
      devices,
      sender,
    });
    await setup.controller.start();
    setup.controller.inputId = "mic-2";
    setup.controller.outputId = "speaker-2";
    setup.inputSelect.value = "mic-2";
    setup.outputSelect.value = "speaker-2";

    await setup.controller.refresh();

    assert.deepEqual(setup.mediaDevices.getUserMediaCalls, []);
    assert.deepEqual(sender.replacements, []);
    assert.deepEqual(context.calls, []);
    assert.equal(setup.controller.inputId, "mic-2");
    assert.equal(setup.controller.outputId, "speaker-2");
  });

  it("keeps the last successful catalog and route when enumeration later fails", async () => {
    const oldTrack = createTrack();
    const nextTrack = createTrack();
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
      },
    };
    const context = {
      calls: [],
      async setSinkId(deviceId) {
        this.calls.push(deviceId);
      },
    };
    const setup = createController({
      context,
      currentStream: createStream([oldTrack]),
      devices: [
        { kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" },
        { kind: "audiooutput", deviceId: "speaker-2", label: "Speaker 2" },
      ],
      sender,
    });
    setup.mediaDevices.getUserMediaResults.push(createStream([nextTrack]));
    await setup.controller.start();
    await setup.controller.selectInput("mic-2");
    await setup.controller.selectOutput("speaker-2");
    setup.mediaDevices.getUserMediaCalls.length = 0;
    sender.replacements.length = 0;
    context.calls.length = 0;
    setup.mediaDevices.enumerationError = new Error("transient enumeration failure");

    assert.equal(await setup.controller.refresh(), false);

    assert.deepEqual(options(setup.inputSelect), [
      ["", "システム既定"],
      ["mic-2", "Microphone 2"],
    ]);
    assert.deepEqual(options(setup.outputSelect), [
      ["", "システム既定"],
      ["speaker-2", "Speaker 2"],
    ]);
    assert.deepEqual(setup.mediaDevices.getUserMediaCalls, []);
    assert.deepEqual(sender.replacements, []);
    assert.deepEqual(context.calls, []);
    assert.equal(setup.controller.inputId, "mic-2");
    assert.equal(setup.controller.outputId, "speaker-2");
  });

  it("never falls back from a stale enumeration that omitted the selected device", async () => {
    const stale = deferred();
    const latest = deferred();
    const track = createTrack();
    const sender = {
      replacements: [],
      async replaceTrack(nextTrack) {
        this.replacements.push(nextTrack);
      },
    };
    const setup = createController({
      currentStream: createStream([track]),
      devices: [{ kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" }],
      sender,
    });
    await setup.controller.start();
    setup.controller.inputId = "mic-2";
    setup.inputSelect.value = "mic-2";
    setup.mediaDevices.enumerationResults.push(stale.promise, latest.promise);

    const staleRefresh = setup.controller.refresh();
    const latestRefresh = setup.controller.refresh();
    latest.resolve([{ kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" }]);
    assert.equal(await latestRefresh, true);
    stale.resolve([]);
    assert.equal(await staleRefresh, false);

    assert.deepEqual(setup.mediaDevices.getUserMediaCalls, []);
    assert.deepEqual(sender.replacements, []);
    assert.equal(setup.controller.inputId, "mic-2");
    assert.equal(setup.inputSelect.value, "mic-2");
  });

  it("rechecks the latest catalog after a stored input restoration finishes", async () => {
    const initialCatalog = deferred();
    const latestCatalog = deferred();
    const savedAcquisition = deferred();
    const oldTrack = createTrack();
    const savedTrack = createTrack();
    const defaultTrack = createTrack();
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
      },
    };
    const storage = new FakeStorage({ "moco.audio.inputDeviceId": "mic-saved" });
    const setup = createController({
      currentStream: createStream([oldTrack]),
      sender,
      storage,
    });
    setup.mediaDevices.enumerationResults.push(initialCatalog.promise, latestCatalog.promise);
    setup.mediaDevices.getUserMediaResults.push(
      savedAcquisition.promise,
      createStream([defaultTrack]),
    );

    const starting = setup.controller.start();
    initialCatalog.resolve([
      { kind: "audioinput", deviceId: "mic-saved", label: "Saved microphone" },
    ]);
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(setup.mediaDevices.getUserMediaCalls, [
      { audio: { deviceId: { exact: "mic-saved" } } },
    ]);

    const latestRefresh = setup.controller.refresh();
    latestCatalog.resolve([]);
    await new Promise((resolve) => setImmediate(resolve));
    savedAcquisition.resolve(createStream([savedTrack]));
    await Promise.all([starting, latestRefresh]);

    assert.deepEqual(setup.mediaDevices.getUserMediaCalls, [
      { audio: { deviceId: { exact: "mic-saved" } } },
      { audio: true },
    ]);
    assert.deepEqual(sender.replacements, [savedTrack, defaultTrack]);
    assert.equal(setup.controller.inputId, "");
    assert.equal(setup.inputSelect.value, "");
    assert.equal(storage.getItem("moco.audio.inputDeviceId"), null);
  });

  it("skips a queued input fallback after an active direct switch reaches the latest catalog", async () => {
    const directAcquisition = deferred();
    const oldTrack = createTrack();
    const nextTrack = createTrack();
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
      },
    };
    const storage = new FakeStorage();
    const setup = createController({
      currentStream: createStream([oldTrack]),
      devices: [
        { kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" },
        { kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" },
      ],
      sender,
      storage,
    });
    setup.mediaDevices.getUserMediaResults.push(directAcquisition.promise);
    await setup.controller.start();
    setCurrentInput(setup, "mic-1");

    const switching = setup.controller.selectInput("mic-2");
    await new Promise((resolve) => setImmediate(resolve));
    setup.mediaDevices.devices = [{ kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" }];
    const refreshing = setup.controller.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    directAcquisition.resolve(createStream([nextTrack]));

    assert.equal(await switching, true);
    assert.equal(await refreshing, true);
    assert.deepEqual(setup.mediaDevices.getUserMediaCalls, [
      { audio: { deviceId: { exact: "mic-2" } } },
    ]);
    assert.deepEqual(sender.replacements, [nextTrack]);
    assert.equal(setup.currentStream().getAudioTracks()[0], nextTrack);
    assert.equal(setup.controller.inputId, "mic-2");
    assert.equal(setup.inputSelect.value, "mic-2");
    assert.equal(storage.getItem("moco.audio.inputDeviceId"), "mic-2");
  });

  it("cancels an acquired input fallback when a newer catalog restores the selected device", async () => {
    const fallbackAcquisition = deferred();
    const errors = [];
    const oldTrack = createTrack();
    const candidateTrack = createTrack();
    const oldStream = createStream([oldTrack]);
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
      },
    };
    const storage = new FakeStorage();
    const setup = createController({
      currentStream: oldStream,
      devices: [{ kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" }],
      onError: (code) => errors.push(code),
      sender,
      storage,
    });
    setup.mediaDevices.getUserMediaResults.push(fallbackAcquisition.promise);
    await setup.controller.start();
    setCurrentInput(setup, "mic-1");
    setup.mediaDevices.devices = [];

    const missingRefresh = setup.controller.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    setup.mediaDevices.devices = [{ kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" }];
    assert.equal(await setup.controller.refresh(), true);
    fallbackAcquisition.resolve(createStream([candidateTrack]));
    assert.equal(await missingRefresh, true);

    assert.equal(candidateTrack.stopCalls, 1);
    assert.deepEqual(sender.replacements, []);
    assert.equal(setup.currentStream(), oldStream);
    assert.deepEqual(setup.streamReplacements, []);
    assert.equal(setup.controller.inputId, "mic-1");
    assert.equal(setup.inputSelect.value, "mic-1");
    assert.equal(storage.getItem("moco.audio.inputDeviceId"), "mic-1");
    assert.deepEqual(errors, []);
  });

  it("commits an acquired default fallback when the restored selected track is ended", async () => {
    const fallbackAcquisition = deferred();
    const endedTrack = createTrack();
    const candidateTrack = createTrack();
    const endedStream = createStream([endedTrack]);
    const candidateStream = createStream([candidateTrack]);
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
      },
    };
    const storage = new FakeStorage();
    const setup = createController({
      currentStream: endedStream,
      devices: [{ kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" }],
      sender,
      storage,
    });
    setup.mediaDevices.getUserMediaResults.push(fallbackAcquisition.promise);
    await setup.controller.start();
    setCurrentInput(setup, "mic-1");
    endedTrack.readyState = "ended";
    setup.mediaDevices.devices = [];

    const missingRefresh = setup.controller.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    setup.mediaDevices.devices = [{ kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" }];
    assert.equal(await setup.controller.refresh(), true);
    fallbackAcquisition.resolve(candidateStream);
    assert.equal(await missingRefresh, true);

    assert.deepEqual(sender.replacements, [candidateTrack]);
    assert.equal(setup.currentStream(), candidateStream);
    assert.equal(candidateTrack.stopCalls, 0);
    assert.equal(endedTrack.stopCalls, 1);
    assert.equal(setup.controller.inputId, "");
    assert.equal(setup.inputSelect.value, "");
    assert.equal(storage.getItem("moco.audio.inputDeviceId"), null);
  });

  it("rolls back a replaced input fallback that becomes stale before commit", async () => {
    const replacement = deferred();
    const errors = [];
    const oldTrack = createTrack();
    const candidateTrack = createTrack();
    const oldStream = createStream([oldTrack]);
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
        if (this.replacements.length === 1) {
          await replacement.promise;
        }
      },
    };
    const storage = new FakeStorage();
    const setup = createController({
      currentStream: oldStream,
      devices: [{ kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" }],
      onError: (code) => errors.push(code),
      sender,
      storage,
    });
    setup.mediaDevices.getUserMediaResults.push(createStream([candidateTrack]));
    await setup.controller.start();
    setCurrentInput(setup, "mic-1");
    setup.mediaDevices.devices = [];

    const missingRefresh = setup.controller.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(sender.replacements, [candidateTrack]);
    setup.mediaDevices.devices = [{ kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" }];
    assert.equal(await setup.controller.refresh(), true);
    replacement.resolve();
    assert.equal(await missingRefresh, true);

    assert.deepEqual(sender.replacements, [candidateTrack, oldTrack]);
    assert.equal(candidateTrack.stopCalls, 1);
    assert.equal(oldTrack.stopCalls, 0);
    assert.equal(setup.currentStream(), oldStream);
    assert.deepEqual(setup.streamReplacements, []);
    assert.equal(setup.controller.inputId, "mic-1");
    assert.equal(setup.inputSelect.value, "mic-1");
    assert.equal(storage.getItem("moco.audio.inputDeviceId"), "mic-1");
    assert.deepEqual(errors, []);
  });

  it("does not roll back a replaced default fallback to a restored ended track", async () => {
    const replacement = deferred();
    const endedTrack = createTrack();
    const candidateTrack = createTrack();
    const endedStream = createStream([endedTrack]);
    const candidateStream = createStream([candidateTrack]);
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
        await replacement.promise;
      },
    };
    const storage = new FakeStorage();
    const setup = createController({
      currentStream: endedStream,
      devices: [{ kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" }],
      sender,
      storage,
    });
    setup.mediaDevices.getUserMediaResults.push(candidateStream);
    await setup.controller.start();
    setCurrentInput(setup, "mic-1");
    endedTrack.readyState = "ended";
    setup.mediaDevices.devices = [];

    const missingRefresh = setup.controller.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(sender.replacements, [candidateTrack]);
    setup.mediaDevices.devices = [{ kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" }];
    assert.equal(await setup.controller.refresh(), true);
    replacement.resolve();
    assert.equal(await missingRefresh, true);

    assert.deepEqual(sender.replacements, [candidateTrack]);
    assert.equal(setup.currentStream(), candidateStream);
    assert.equal(candidateTrack.stopCalls, 0);
    assert.equal(endedTrack.stopCalls, 1);
    assert.equal(setup.controller.inputId, "");
    assert.equal(setup.inputSelect.value, "");
    assert.equal(storage.getItem("moco.audio.inputDeviceId"), null);
  });

  it("reacquires an explicitly reselected ended input but no-ops once it is live", async () => {
    const endedTrack = createTrack();
    const candidateTrack = createTrack();
    const candidateStream = createStream([candidateTrack]);
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
      },
    };
    const storage = new FakeStorage();
    const setup = createController({
      currentStream: createStream([endedTrack]),
      devices: [{ kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" }],
      sender,
      storage,
    });
    setup.mediaDevices.getUserMediaResults.push(candidateStream);
    await setup.controller.start();
    setCurrentInput(setup, "mic-1");
    endedTrack.readyState = "ended";

    assert.equal(await setup.controller.selectInput("mic-1"), true);
    assert.equal(await setup.controller.selectInput("mic-1"), false);

    assert.deepEqual(setup.mediaDevices.getUserMediaCalls, [
      { audio: { deviceId: { exact: "mic-1" } } },
    ]);
    assert.deepEqual(sender.replacements, [candidateTrack]);
    assert.equal(setup.currentStream(), candidateStream);
    assert.equal(candidateTrack.stopCalls, 0);
    assert.equal(endedTrack.stopCalls, 1);
    assert.equal(setup.controller.inputId, "mic-1");
    assert.equal(setup.inputSelect.value, "mic-1");
    assert.equal(storage.getItem("moco.audio.inputDeviceId"), "mic-1");
  });

  it("commits the actual default input route when stale fallback rollback fails", async () => {
    const replacement = deferred();
    const errors = [];
    const oldTrack = createTrack();
    const candidateTrack = createTrack();
    const candidateStream = createStream([candidateTrack]);
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
        if (this.replacements.length === 1) {
          await replacement.promise;
          return;
        }
        throw new Error("rollback refused");
      },
    };
    const storage = new FakeStorage();
    const setup = createController({
      currentStream: createStream([oldTrack]),
      devices: [{ kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" }],
      onError: (code) => errors.push(code),
      sender,
      storage,
    });
    setup.mediaDevices.getUserMediaResults.push(candidateStream);
    await setup.controller.start();
    setCurrentInput(setup, "mic-1");
    setup.mediaDevices.devices = [];

    const missingRefresh = setup.controller.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    setup.mediaDevices.devices = [{ kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" }];
    assert.equal(await setup.controller.refresh(), true);
    replacement.resolve();
    assert.equal(await missingRefresh, true);

    assert.deepEqual(sender.replacements, [candidateTrack, oldTrack]);
    assert.equal(setup.currentStream(), candidateStream);
    assert.equal(candidateTrack.stopCalls, 0);
    assert.equal(oldTrack.stopCalls, 1);
    assert.equal(setup.controller.inputId, "");
    assert.equal(setup.inputSelect.value, "");
    assert.equal(storage.getItem("moco.audio.inputDeviceId"), null);
    assert.deepEqual(errors, []);
  });

  it("skips a queued output fallback after an active direct switch reaches the latest catalog", async () => {
    const directSwitch = deferred();
    const context = {
      calls: [],
      async setSinkId(deviceId) {
        this.calls.push(deviceId);
        if (this.calls.length === 1) {
          await directSwitch.promise;
        }
      },
    };
    const storage = new FakeStorage();
    const setup = createController({
      context,
      devices: [
        { kind: "audiooutput", deviceId: "speaker-1", label: "Speaker 1" },
        { kind: "audiooutput", deviceId: "speaker-2", label: "Speaker 2" },
      ],
      storage,
    });
    await setup.controller.start();
    setCurrentOutput(setup, "speaker-1");

    const switching = setup.controller.selectOutput("speaker-2");
    await new Promise((resolve) => setImmediate(resolve));
    setup.mediaDevices.devices = [
      { kind: "audiooutput", deviceId: "speaker-2", label: "Speaker 2" },
    ];
    const refreshing = setup.controller.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    directSwitch.resolve();

    assert.equal(await switching, true);
    assert.equal(await refreshing, true);
    assert.deepEqual(context.calls, ["speaker-2"]);
    assert.equal(setup.controller.outputId, "speaker-2");
    assert.equal(setup.outputSelect.value, "speaker-2");
    assert.equal(storage.getItem("moco.audio.outputDeviceId"), "speaker-2");
  });

  it("rolls back an output fallback that becomes stale before commit", async () => {
    const fallbackSwitch = deferred();
    const errors = [];
    const context = {
      calls: [],
      async setSinkId(deviceId) {
        this.calls.push(deviceId);
        if (this.calls.length === 1) {
          await fallbackSwitch.promise;
        }
      },
    };
    const storage = new FakeStorage();
    const setup = createController({
      context,
      devices: [{ kind: "audiooutput", deviceId: "speaker-1", label: "Speaker 1" }],
      onError: (code) => errors.push(code),
      storage,
    });
    await setup.controller.start();
    setCurrentOutput(setup, "speaker-1");
    setup.mediaDevices.devices = [];

    const missingRefresh = setup.controller.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(context.calls, [""]);
    setup.mediaDevices.devices = [
      { kind: "audiooutput", deviceId: "speaker-1", label: "Speaker 1" },
    ];
    assert.equal(await setup.controller.refresh(), true);
    fallbackSwitch.resolve();
    assert.equal(await missingRefresh, true);

    assert.deepEqual(context.calls, ["", "speaker-1"]);
    assert.equal(setup.controller.outputId, "speaker-1");
    assert.equal(setup.outputSelect.value, "speaker-1");
    assert.equal(storage.getItem("moco.audio.outputDeviceId"), "speaker-1");
    assert.deepEqual(errors, []);
  });

  it("commits the actual default output route when stale fallback rollback fails", async () => {
    const fallbackSwitch = deferred();
    const errors = [];
    const context = {
      calls: [],
      async setSinkId(deviceId) {
        this.calls.push(deviceId);
        if (this.calls.length === 1) {
          await fallbackSwitch.promise;
          return;
        }
        throw new Error("rollback refused");
      },
    };
    const storage = new FakeStorage();
    const setup = createController({
      context,
      devices: [{ kind: "audiooutput", deviceId: "speaker-1", label: "Speaker 1" }],
      onError: (code) => errors.push(code),
      storage,
    });
    await setup.controller.start();
    setCurrentOutput(setup, "speaker-1");
    setup.mediaDevices.devices = [];

    const missingRefresh = setup.controller.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    setup.mediaDevices.devices = [
      { kind: "audiooutput", deviceId: "speaker-1", label: "Speaker 1" },
    ];
    assert.equal(await setup.controller.refresh(), true);
    fallbackSwitch.resolve();
    assert.equal(await missingRefresh, true);

    assert.deepEqual(context.calls, ["", "speaker-1"]);
    assert.equal(setup.controller.outputId, "");
    assert.equal(setup.outputSelect.value, "");
    assert.equal(storage.getItem("moco.audio.outputDeviceId"), null);
    assert.deepEqual(errors, []);
  });

  it("retains a missing selected input option when its automatic fallback fails", async () => {
    const errors = [];
    const oldTrack = createTrack();
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
      },
    };
    const storage = new FakeStorage();
    const setup = createController({
      currentStream: createStream([oldTrack]),
      devices: [{ kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" }],
      onError: (code) => errors.push(code),
      sender,
      storage,
    });
    setup.mediaDevices.getUserMediaResults.push(new Error("default input unavailable"));
    await setup.controller.start();
    setCurrentInput(setup, "mic-1");
    setup.mediaDevices.devices = [];

    assert.equal(await setup.controller.refresh(), true);

    assert.deepEqual(options(setup.inputSelect), [
      ["", "システム既定"],
      ["mic-1", "Microphone 1"],
    ]);
    assert.equal(setup.inputSelect.value, "mic-1");
    assert.equal(setup.controller.inputId, "mic-1");
    assert.equal(storage.getItem("moco.audio.inputDeviceId"), "mic-1");
    assert.deepEqual(sender.replacements, []);
    assert.equal(oldTrack.stopCalls, 0);
    assert.deepEqual(errors, ["microphone_switch_failed"]);
  });

  it("retains a missing selected output option when its automatic fallback fails", async () => {
    const errors = [];
    const context = {
      calls: [],
      async setSinkId(deviceId) {
        this.calls.push(deviceId);
        throw new Error("default output unavailable");
      },
    };
    const storage = new FakeStorage();
    const setup = createController({
      context,
      devices: [{ kind: "audiooutput", deviceId: "speaker-1", label: "Speaker 1" }],
      onError: (code) => errors.push(code),
      storage,
    });
    await setup.controller.start();
    setCurrentOutput(setup, "speaker-1");
    setup.mediaDevices.devices = [];

    assert.equal(await setup.controller.refresh(), true);

    assert.deepEqual(options(setup.outputSelect), [
      ["", "システム既定"],
      ["speaker-1", "Speaker 1"],
    ]);
    assert.equal(setup.outputSelect.value, "speaker-1");
    assert.equal(setup.controller.outputId, "speaker-1");
    assert.equal(storage.getItem("moco.audio.outputDeviceId"), "speaker-1");
    assert.deepEqual(context.calls, [""]);
    assert.deepEqual(errors, ["audio_output_switch_failed"]);
  });

  it("does not commit an input fallback after close invalidates a pending failed rollback", async () => {
    const replacement = deferred();
    const rollback = deferred();
    const errors = [];
    const oldTrack = createTrack();
    const candidateTrack = createTrack();
    const oldStream = createStream([oldTrack]);
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
        await (this.replacements.length === 1 ? replacement.promise : rollback.promise);
      },
    };
    const storage = new FakeStorage();
    const setup = createController({
      currentStream: oldStream,
      devices: [{ kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" }],
      onError: (code) => errors.push(code),
      sender,
      storage,
    });
    setup.mediaDevices.getUserMediaResults.push(createStream([candidateTrack]));
    await setup.controller.start();
    setCurrentInput(setup, "mic-1");
    setup.mediaDevices.devices = [];

    const missingRefresh = setup.controller.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    setup.mediaDevices.devices = [{ kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" }];
    assert.equal(await setup.controller.refresh(), true);
    replacement.resolve();
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(sender.replacements, [candidateTrack, oldTrack]);
    setup.controller.close();
    rollback.reject(new Error("rollback failed after close"));
    assert.equal(await missingRefresh, true);

    assert.equal(candidateTrack.stopCalls, 1);
    assert.equal(oldTrack.stopCalls, 0);
    assert.equal(setup.currentStream(), oldStream);
    assert.deepEqual(setup.streamReplacements, []);
    assert.equal(setup.controller.inputId, "mic-1");
    assert.equal(storage.getItem("moco.audio.inputDeviceId"), "mic-1");
    assert.deepEqual(errors, []);
    assert.equal(setup.inputSelect.disabled, true);
    assert.equal(setup.outputSelect.disabled, true);
  });

  it("does not commit an output fallback after close invalidates a pending failed rollback", async () => {
    const fallbackSwitch = deferred();
    const rollback = deferred();
    const errors = [];
    const context = {
      calls: [],
      async setSinkId(deviceId) {
        this.calls.push(deviceId);
        await (this.calls.length === 1 ? fallbackSwitch.promise : rollback.promise);
      },
    };
    const storage = new FakeStorage();
    const setup = createController({
      context,
      devices: [{ kind: "audiooutput", deviceId: "speaker-1", label: "Speaker 1" }],
      onError: (code) => errors.push(code),
      storage,
    });
    await setup.controller.start();
    setCurrentOutput(setup, "speaker-1");
    setup.mediaDevices.devices = [];

    const missingRefresh = setup.controller.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    setup.mediaDevices.devices = [
      { kind: "audiooutput", deviceId: "speaker-1", label: "Speaker 1" },
    ];
    assert.equal(await setup.controller.refresh(), true);
    fallbackSwitch.resolve();
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(context.calls, ["", "speaker-1"]);
    setup.controller.close();
    rollback.reject(new Error("rollback failed after close"));
    assert.equal(await missingRefresh, true);

    assert.equal(setup.controller.outputId, "speaker-1");
    assert.equal(storage.getItem("moco.audio.outputDeviceId"), "speaker-1");
    assert.deepEqual(errors, []);
    assert.equal(setup.inputSelect.disabled, true);
    assert.equal(setup.outputSelect.disabled, true);
  });

  it("finishes a valid input fallback after a later enumeration fails", async () => {
    const acquisition = deferred();
    const oldTrack = createTrack();
    const defaultTrack = createTrack();
    const defaultStream = createStream([defaultTrack]);
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
      },
    };
    const storage = new FakeStorage();
    const setup = createController({
      currentStream: createStream([oldTrack]),
      devices: [{ kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" }],
      sender,
      storage,
    });
    setup.mediaDevices.getUserMediaResults.push(acquisition.promise);
    await setup.controller.start();
    setCurrentInput(setup, "mic-1");
    setup.mediaDevices.devices = [];

    const missingRefresh = setup.controller.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    setup.mediaDevices.enumerationError = new Error("transient enumeration failure");
    assert.equal(await setup.controller.refresh(), false);
    acquisition.resolve(defaultStream);
    assert.equal(await missingRefresh, true);

    assert.deepEqual(sender.replacements, [defaultTrack]);
    assert.equal(setup.currentStream(), defaultStream);
    assert.equal(setup.controller.inputId, "");
    assert.equal(setup.inputSelect.value, "");
    assert.equal(storage.getItem("moco.audio.inputDeviceId"), null);
  });

  it("finishes a valid output fallback after a later enumeration fails", async () => {
    const fallbackSwitch = deferred();
    const context = {
      calls: [],
      async setSinkId(deviceId) {
        this.calls.push(deviceId);
        await fallbackSwitch.promise;
      },
    };
    const storage = new FakeStorage();
    const setup = createController({
      context,
      devices: [{ kind: "audiooutput", deviceId: "speaker-1", label: "Speaker 1" }],
      storage,
    });
    await setup.controller.start();
    setCurrentOutput(setup, "speaker-1");
    setup.mediaDevices.devices = [];

    const missingRefresh = setup.controller.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    setup.mediaDevices.enumerationError = new Error("transient enumeration failure");
    assert.equal(await setup.controller.refresh(), false);
    fallbackSwitch.resolve();
    assert.equal(await missingRefresh, true);

    assert.deepEqual(context.calls, [""]);
    assert.equal(setup.controller.outputId, "");
    assert.equal(setup.outputSelect.value, "");
    assert.equal(storage.getItem("moco.audio.outputDeviceId"), null);
  });

  it("retains a pending direct input target after its automatic fallback fails", async () => {
    const directAcquisition = deferred();
    const errors = [];
    const oldTrack = createTrack();
    const nextTrack = createTrack();
    const nextStream = createStream([nextTrack]);
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
      },
    };
    const storage = new FakeStorage();
    const setup = createController({
      currentStream: createStream([oldTrack]),
      devices: [
        { kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" },
        { kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" },
      ],
      onError: (code) => errors.push(code),
      sender,
      storage,
    });
    setup.mediaDevices.getUserMediaResults.push(
      directAcquisition.promise,
      new Error("default input unavailable"),
    );
    await setup.controller.start();
    setCurrentInput(setup, "mic-1");

    const switching = setup.controller.selectInput("mic-2");
    await new Promise((resolve) => setImmediate(resolve));
    setup.mediaDevices.devices = [];
    const refreshing = setup.controller.refresh();
    directAcquisition.resolve(nextStream);
    assert.equal(await switching, true);
    assert.equal(await refreshing, true);

    assert.deepEqual(options(setup.inputSelect), [
      ["", "システム既定"],
      ["mic-2", "Microphone 2"],
    ]);
    assert.equal(setup.inputSelect.value, "mic-2");
    assert.equal(setup.currentStream(), nextStream);
    assert.equal(setup.controller.inputId, "mic-2");
    assert.equal(storage.getItem("moco.audio.inputDeviceId"), "mic-2");
    assert.deepEqual(errors, ["microphone_switch_failed"]);
  });

  it("retains a pending direct output target after its automatic fallback fails", async () => {
    const directSwitch = deferred();
    const errors = [];
    const context = {
      calls: [],
      async setSinkId(deviceId) {
        this.calls.push(deviceId);
        if (this.calls.length === 1) {
          await directSwitch.promise;
          return;
        }
        throw new Error("default output unavailable");
      },
    };
    const storage = new FakeStorage();
    const setup = createController({
      context,
      devices: [
        { kind: "audiooutput", deviceId: "speaker-1", label: "Speaker 1" },
        { kind: "audiooutput", deviceId: "speaker-2", label: "Speaker 2" },
      ],
      onError: (code) => errors.push(code),
      storage,
    });
    await setup.controller.start();
    setCurrentOutput(setup, "speaker-1");

    const switching = setup.controller.selectOutput("speaker-2");
    await new Promise((resolve) => setImmediate(resolve));
    setup.mediaDevices.devices = [];
    const refreshing = setup.controller.refresh();
    directSwitch.resolve();
    assert.equal(await switching, true);
    assert.equal(await refreshing, true);

    assert.deepEqual(options(setup.outputSelect), [
      ["", "システム既定"],
      ["speaker-2", "Speaker 2"],
    ]);
    assert.equal(setup.outputSelect.value, "speaker-2");
    assert.equal(setup.controller.outputId, "speaker-2");
    assert.equal(storage.getItem("moco.audio.outputDeviceId"), "speaker-2");
    assert.deepEqual(errors, ["audio_output_switch_failed"]);
  });

  it("removes a retained direct input target after its automatic fallback succeeds", async () => {
    const directAcquisition = deferred();
    const fallbackAcquisition = deferred();
    const oldTrack = createTrack();
    const nextTrack = createTrack();
    const defaultTrack = createTrack();
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
      },
    };
    const setup = createController({
      currentStream: createStream([oldTrack]),
      devices: [
        { kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" },
        { kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" },
      ],
      sender,
    });
    setup.mediaDevices.getUserMediaResults.push(
      directAcquisition.promise,
      fallbackAcquisition.promise,
    );
    await setup.controller.start();
    setup.controller.inputId = "mic-1";
    setup.inputSelect.value = "mic-1";

    const switching = setup.controller.selectInput("mic-2");
    await new Promise((resolve) => setImmediate(resolve));
    setup.mediaDevices.devices = [];
    const refreshing = setup.controller.refresh();
    directAcquisition.resolve(createStream([nextTrack]));
    assert.equal(await switching, true);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(setup.inputSelect.value, "mic-2");
    assert.ok(options(setup.inputSelect).some(([value]) => value === "mic-2"));
    fallbackAcquisition.resolve(createStream([defaultTrack]));
    assert.equal(await refreshing, true);

    assert.deepEqual(options(setup.inputSelect), [["", "システム既定"]]);
    assert.equal(setup.inputSelect.value, "");
    assert.equal(setup.controller.inputId, "");
  });

  it("removes a retained direct output target after its automatic fallback succeeds", async () => {
    const directSwitch = deferred();
    const fallbackSwitch = deferred();
    const context = {
      calls: [],
      async setSinkId(deviceId) {
        this.calls.push(deviceId);
        await (this.calls.length === 1 ? directSwitch.promise : fallbackSwitch.promise);
      },
    };
    const setup = createController({
      context,
      devices: [
        { kind: "audiooutput", deviceId: "speaker-1", label: "Speaker 1" },
        { kind: "audiooutput", deviceId: "speaker-2", label: "Speaker 2" },
      ],
    });
    await setup.controller.start();
    setup.controller.outputId = "speaker-1";
    setup.outputSelect.value = "speaker-1";

    const switching = setup.controller.selectOutput("speaker-2");
    await new Promise((resolve) => setImmediate(resolve));
    setup.mediaDevices.devices = [];
    const refreshing = setup.controller.refresh();
    directSwitch.resolve();
    assert.equal(await switching, true);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(setup.outputSelect.value, "speaker-2");
    assert.ok(options(setup.outputSelect).some(([value]) => value === "speaker-2"));
    fallbackSwitch.resolve();
    assert.equal(await refreshing, true);

    assert.deepEqual(options(setup.outputSelect), [["", "システム既定"]]);
    assert.equal(setup.outputSelect.value, "");
    assert.equal(setup.controller.outputId, "");
  });

  it("uses bounded generic labels for confirmed direct targets absent at enqueue", async () => {
    const oldTrack = createTrack();
    const nextTrack = createTrack();
    const sender = {
      async replaceTrack() {},
    };
    const context = {
      async setSinkId() {},
    };
    const setup = createController({
      context,
      currentStream: createStream([oldTrack]),
      sender,
    });
    setup.mediaDevices.getUserMediaResults.push(createStream([nextTrack]));
    await setup.controller.start();

    assert.equal(await setup.controller.selectInput("mic-unlisted"), true);
    assert.equal(await setup.controller.selectOutput("speaker-unlisted"), true);

    assert.deepEqual(options(setup.inputSelect), [
      ["", "システム既定"],
      ["mic-unlisted", "選択中のマイク"],
    ]);
    assert.deepEqual(options(setup.outputSelect), [
      ["", "システム既定"],
      ["speaker-unlisted", "選択中の出力"],
    ]);
    assert.equal(setup.inputSelect.value, "mic-unlisted");
    assert.equal(setup.outputSelect.value, "speaker-unlisted");
  });

  it("uses the latest MIC ON state when replaceTrack resolves", async () => {
    const replacement = deferred();
    const oldTrack = createTrack({ enabled: false });
    const nextTrack = createTrack({ enabled: false });
    const sender = {
      async replaceTrack() {
        await replacement.promise;
      },
    };
    const setup = createController({
      currentStream: createStream([oldTrack]),
      devices: [{ kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" }],
      sender,
    });
    setup.mediaDevices.getUserMediaResults.push(createStream([nextTrack]));
    await setup.controller.start();

    const switching = setup.controller.selectInput("mic-2");
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(nextTrack.enabled, false);
    oldTrack.enabled = true;
    replacement.resolve();

    assert.equal(await switching, true);
    assert.equal(nextTrack.enabled, true);
  });

  it("uses the latest MIC OFF state when replaceTrack resolves", async () => {
    const replacement = deferred();
    const oldTrack = createTrack({ enabled: true });
    const nextTrack = createTrack({ enabled: true });
    const sender = {
      async replaceTrack() {
        await replacement.promise;
      },
    };
    const setup = createController({
      currentStream: createStream([oldTrack]),
      devices: [{ kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" }],
      sender,
    });
    setup.mediaDevices.getUserMediaResults.push(createStream([nextTrack]));
    await setup.controller.start();

    const switching = setup.controller.selectInput("mic-2");
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(nextTrack.enabled, true);
    oldTrack.enabled = false;
    replacement.resolve();

    assert.equal(await switching, true);
    assert.equal(nextTrack.enabled, false);
  });

  it("cancels an input commit when current stream identity changes during replaceTrack", async () => {
    const replacement = deferred();
    const errors = [];
    const oldTrack = createTrack();
    const nextTrack = createTrack();
    const replacementTrack = createTrack();
    const oldStream = createStream([oldTrack]);
    const nextStream = createStream([nextTrack]);
    const replacementStream = createStream([replacementTrack]);
    let authoritativeStream = oldStream;
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
        if (this.replacements.length === 1) {
          await replacement.promise;
        }
      },
    };
    const setup = createController({
      currentStream: oldStream,
      devices: [{ kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" }],
      getCurrentStream: () => authoritativeStream,
      onError: (code) => errors.push(code),
      sender,
    });
    setup.mediaDevices.getUserMediaResults.push(nextStream);
    await setup.controller.start();

    const switching = setup.controller.selectInput("mic-2");
    await new Promise((resolve) => setImmediate(resolve));
    authoritativeStream = replacementStream;
    replacement.resolve();

    assert.equal(await switching, false);
    assert.deepEqual(sender.replacements, [nextTrack, replacementTrack]);
    assert.equal(nextTrack.stopCalls, 1);
    assert.equal(oldTrack.stopCalls, 0);
    assert.equal(replacementTrack.stopCalls, 0);
    assert.deepEqual(setup.streamReplacements, []);
    assert.equal(authoritativeStream, replacementStream);
    assert.equal(setup.controller.inputId, "");
    assert.equal(setup.storage.getItem("moco.audio.inputDeviceId"), null);
    assert.deepEqual(errors, []);
  });

  it("preserves the candidate route when authoritative rollback fails on the active sender", async () => {
    const initialReplacement = deferred();
    const rollback = deferred();
    const errors = [];
    const oldTrack = createTrack({ enabled: true });
    const authoritativeTrack = createTrack({ enabled: false });
    const candidateTrack = createTrack({ enabled: true });
    const oldStream = createStream([oldTrack]);
    const authoritativeStream = createStream([authoritativeTrack]);
    const candidateStream = createStream([candidateTrack]);
    let ownedStream = oldStream;
    const sender = {
      replacements: [],
      track: oldTrack,
      async replaceTrack(track) {
        this.replacements.push(track);
        if (this.replacements.length === 1) {
          await initialReplacement.promise;
          this.track = track;
          return;
        }
        await rollback.promise;
        throw new Error("authoritative rollback failed");
      },
    };
    const storage = new FakeStorage();
    const setup = createController({
      currentStream: oldStream,
      devices: [{ kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" }],
      getCurrentStream: () => ownedStream,
      onError: (code) => errors.push(code),
      onReplaceCurrentStream: (stream) => {
        ownedStream = stream;
      },
      sender,
      storage,
    });
    setup.mediaDevices.getUserMediaResults.push(candidateStream);
    await setup.controller.start();

    const switching = setup.controller.selectInput("mic-2");
    await new Promise((resolve) => setImmediate(resolve));
    ownedStream = authoritativeStream;
    initialReplacement.resolve();
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(sender.replacements, [candidateTrack, authoritativeTrack]);
    rollback.reject(new Error("rollback rejected"));

    assert.equal(await switching, true);
    assert.equal(sender.track, candidateTrack);
    assert.equal(ownedStream, candidateStream);
    assert.deepEqual(setup.streamReplacements, [candidateStream]);
    assert.equal(candidateTrack.enabled, false);
    assert.equal(candidateTrack.stopCalls, 0);
    assert.equal(oldTrack.stopCalls, 1);
    assert.equal(authoritativeTrack.stopCalls, 1);
    assert.equal(setup.controller.inputId, "mic-2");
    assert.equal(setup.inputSelect.value, "mic-2");
    assert.equal(storage.getItem("moco.audio.inputDeviceId"), "mic-2");
    assert.deepEqual(errors, ["microphone_switch_failed"]);
  });

  it("does not preserve a candidate after the active sender changes during rollback", async () => {
    const initialReplacement = deferred();
    const rollback = deferred();
    const errors = [];
    const oldTrack = createTrack();
    const authoritativeTrack = createTrack();
    const newestTrack = createTrack();
    const candidateTrack = createTrack();
    const oldStream = createStream([oldTrack]);
    const authoritativeStream = createStream([authoritativeTrack]);
    const newestStream = createStream([newestTrack]);
    const candidateStream = createStream([candidateTrack]);
    let ownedStream = oldStream;
    const oldSender = {
      replacements: [],
      track: oldTrack,
      async replaceTrack(track) {
        this.replacements.push(track);
        if (this.replacements.length === 1) {
          await initialReplacement.promise;
          this.track = track;
          return;
        }
        await rollback.promise;
        throw new Error("rollback failed");
      },
    };
    const newSender = { track: newestTrack, async replaceTrack() {} };
    let activeSender = oldSender;
    const storage = new FakeStorage();
    const setup = createController({
      currentStream: oldStream,
      devices: [{ kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" }],
      getAudioSender: () => activeSender,
      getCurrentStream: () => ownedStream,
      onError: (code) => errors.push(code),
      sender: oldSender,
      storage,
    });
    setup.mediaDevices.getUserMediaResults.push(candidateStream);
    await setup.controller.start();

    const switching = setup.controller.selectInput("mic-2");
    await new Promise((resolve) => setImmediate(resolve));
    ownedStream = authoritativeStream;
    initialReplacement.resolve();
    await new Promise((resolve) => setImmediate(resolve));
    activeSender = newSender;
    ownedStream = newestStream;
    rollback.reject(new Error("rollback rejected"));

    assert.equal(await switching, false);
    assert.equal(oldSender.track, candidateTrack);
    assert.equal(activeSender, newSender);
    assert.equal(ownedStream, newestStream);
    assert.equal(candidateTrack.stopCalls, 1);
    assert.equal(oldTrack.stopCalls, 0);
    assert.equal(authoritativeTrack.stopCalls, 0);
    assert.equal(newestTrack.stopCalls, 0);
    assert.deepEqual(setup.streamReplacements, []);
    assert.equal(setup.controller.inputId, "");
    assert.equal(storage.getItem("moco.audio.inputDeviceId"), null);
    assert.deepEqual(errors, ["microphone_switch_failed"]);
  });

  it("does not report or preserve a candidate when close invalidates a failed identity rollback", async () => {
    const initialReplacement = deferred();
    const rollback = deferred();
    const errors = [];
    const oldTrack = createTrack();
    const authoritativeTrack = createTrack();
    const candidateTrack = createTrack();
    const oldStream = createStream([oldTrack]);
    const authoritativeStream = createStream([authoritativeTrack]);
    const candidateStream = createStream([candidateTrack]);
    let ownedStream = oldStream;
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
        if (this.replacements.length === 1) {
          await initialReplacement.promise;
          return;
        }
        await rollback.promise;
        throw new Error("rollback failed");
      },
    };
    const storage = new FakeStorage();
    const setup = createController({
      currentStream: oldStream,
      devices: [{ kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" }],
      getCurrentStream: () => ownedStream,
      onError: (code) => errors.push(code),
      sender,
      storage,
    });
    setup.mediaDevices.getUserMediaResults.push(candidateStream);
    await setup.controller.start();

    const switching = setup.controller.selectInput("mic-2");
    await new Promise((resolve) => setImmediate(resolve));
    ownedStream = authoritativeStream;
    initialReplacement.resolve();
    await new Promise((resolve) => setImmediate(resolve));
    setup.controller.close();
    rollback.reject(new Error("rollback rejected"));

    assert.equal(await switching, false);
    assert.equal(candidateTrack.stopCalls, 1);
    assert.equal(oldTrack.stopCalls, 0);
    assert.equal(authoritativeTrack.stopCalls, 0);
    assert.deepEqual(setup.streamReplacements, []);
    assert.equal(setup.controller.inputId, "");
    assert.equal(storage.getItem("moco.audio.inputDeviceId"), null);
    assert.deepEqual(errors, []);
  });

  it("cancels an input commit when audio sender identity changes during replaceTrack", async () => {
    const replacement = deferred();
    const errors = [];
    const oldTrack = createTrack();
    const nextTrack = createTrack();
    const oldStream = createStream([oldTrack]);
    const nextStream = createStream([nextTrack]);
    const oldSender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
        if (this.replacements.length === 1) {
          await replacement.promise;
        }
      },
    };
    const newSender = { async replaceTrack() {} };
    let authoritativeSender = oldSender;
    const setup = createController({
      currentStream: oldStream,
      devices: [{ kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" }],
      getAudioSender: () => authoritativeSender,
      onError: (code) => errors.push(code),
      sender: oldSender,
    });
    setup.mediaDevices.getUserMediaResults.push(nextStream);
    await setup.controller.start();

    const switching = setup.controller.selectInput("mic-2");
    await new Promise((resolve) => setImmediate(resolve));
    authoritativeSender = newSender;
    replacement.resolve();

    assert.equal(await switching, false);
    assert.deepEqual(oldSender.replacements, [nextTrack, oldTrack]);
    assert.equal(nextTrack.stopCalls, 1);
    assert.equal(oldTrack.stopCalls, 0);
    assert.deepEqual(setup.streamReplacements, []);
    assert.equal(setup.controller.inputId, "");
    assert.equal(setup.storage.getItem("moco.audio.inputDeviceId"), null);
    assert.deepEqual(errors, ["microphone_switch_failed"]);
  });

  it("does not let a closed controller render after its delayed enumeration rejects", async () => {
    const dom = new JSDOM(`
      <select id="audio-input" disabled></select>
      <select id="audio-output" disabled></select>
    `);
    const pending = deferred();
    const oldMediaDevices = new FakeMediaDevices();
    oldMediaDevices.enumerationResults.push(pending.promise);
    const oldSetup = createController({
      context: { async setSinkId() {} },
      dom,
      mediaDevices: oldMediaDevices,
    });

    const oldStarting = oldSetup.controller.start();
    oldSetup.controller.close();
    const newSetup = createController({
      context: { async setSinkId() {} },
      devices: [
        { kind: "audioinput", deviceId: "mic-new", label: "New microphone" },
        { kind: "audiooutput", deviceId: "speaker-new", label: "New speaker" },
      ],
      dom,
    });
    await newSetup.controller.start();
    pending.reject(new Error("old enumeration failed"));
    await oldStarting;

    assert.deepEqual(options(newSetup.inputSelect), [
      ["", "システム既定"],
      ["mic-new", "New microphone"],
    ]);
    assert.deepEqual(options(newSetup.outputSelect), [
      ["", "システム既定"],
      ["speaker-new", "New speaker"],
    ]);
    assert.equal(newSetup.inputSelect.disabled, false);
    assert.equal(newSetup.outputSelect.disabled, false);
  });

  it("does not let a closed controller render from delayed input switch cleanup", async () => {
    const dom = new JSDOM(`
      <select id="audio-input" disabled></select>
      <select id="audio-output" disabled></select>
    `);
    const acquisition = deferred();
    const oldTrack = createTrack();
    const candidateTrack = createTrack();
    const oldSetup = createController({
      context: { async setSinkId() {} },
      currentStream: createStream([oldTrack]),
      devices: [{ kind: "audioinput", deviceId: "mic-old", label: "Old microphone" }],
      dom,
      sender: { async replaceTrack() {} },
    });
    oldSetup.mediaDevices.getUserMediaResults.push(acquisition.promise);
    await oldSetup.controller.start();
    const switching = oldSetup.controller.selectInput("mic-old");
    await new Promise((resolve) => setImmediate(resolve));
    oldSetup.controller.close();
    const newSetup = createController({
      context: { async setSinkId() {} },
      devices: [{ kind: "audioinput", deviceId: "mic-new", label: "New microphone" }],
      dom,
    });
    await newSetup.controller.start();
    acquisition.resolve(createStream([candidateTrack]));
    assert.equal(await switching, false);

    assert.deepEqual(options(newSetup.inputSelect), [
      ["", "システム既定"],
      ["mic-new", "New microphone"],
    ]);
    assert.equal(newSetup.inputSelect.disabled, false);
    assert.equal(candidateTrack.stopCalls, 1);
  });

  it("does not let a closed controller render from delayed output switch cleanup", async () => {
    const dom = new JSDOM(`
      <select id="audio-input" disabled></select>
      <select id="audio-output" disabled></select>
    `);
    const sinkSwitch = deferred();
    const oldSetup = createController({
      context: {
        async setSinkId() {
          await sinkSwitch.promise;
        },
      },
      devices: [{ kind: "audiooutput", deviceId: "speaker-old", label: "Old speaker" }],
      dom,
    });
    await oldSetup.controller.start();
    const switching = oldSetup.controller.selectOutput("speaker-old");
    await new Promise((resolve) => setImmediate(resolve));
    oldSetup.controller.close();
    const newSetup = createController({
      context: { async setSinkId() {} },
      devices: [{ kind: "audiooutput", deviceId: "speaker-new", label: "New speaker" }],
      dom,
    });
    await newSetup.controller.start();
    sinkSwitch.resolve();
    assert.equal(await switching, false);

    assert.deepEqual(options(newSetup.outputSelect), [
      ["", "システム既定"],
      ["speaker-new", "New speaker"],
    ]);
    assert.equal(newSetup.outputSelect.disabled, false);
  });

  it("deduplicates an input fallback across repeated identical missing catalogs", async () => {
    const replacement = deferred();
    const oldTrack = createTrack();
    const firstDefaultTrack = createTrack();
    const secondDefaultTrack = createTrack();
    const firstDefaultStream = createStream([firstDefaultTrack]);
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
        if (this.replacements.length === 1) {
          await replacement.promise;
        }
      },
    };
    const setup = createController({
      currentStream: createStream([oldTrack]),
      devices: [{ kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" }],
      sender,
    });
    setup.mediaDevices.getUserMediaResults.push(
      firstDefaultStream,
      createStream([secondDefaultTrack]),
    );
    await setup.controller.start();
    setup.controller.inputId = "mic-1";
    setup.inputSelect.value = "mic-1";
    setup.mediaDevices.devices = [];

    const firstRefresh = setup.controller.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    const secondRefresh = setup.controller.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    replacement.resolve();
    await Promise.all([firstRefresh, secondRefresh]);

    assert.deepEqual(setup.mediaDevices.getUserMediaCalls, [{ audio: true }]);
    assert.deepEqual(sender.replacements, [firstDefaultTrack]);
    assert.equal(setup.currentStream(), firstDefaultStream);
    assert.equal(firstDefaultTrack.stopCalls, 0);
    assert.equal(secondDefaultTrack.stopCalls, 0);
    assert.equal(setup.controller.inputId, "");
  });

  it("deduplicates an output fallback across repeated identical missing catalogs", async () => {
    const sinkSwitch = deferred();
    const context = {
      calls: [],
      async setSinkId(deviceId) {
        this.calls.push(deviceId);
        if (this.calls.length === 1) {
          await sinkSwitch.promise;
        }
      },
    };
    const setup = createController({
      context,
      devices: [{ kind: "audiooutput", deviceId: "speaker-1", label: "Speaker 1" }],
    });
    await setup.controller.start();
    setup.controller.outputId = "speaker-1";
    setup.outputSelect.value = "speaker-1";
    setup.mediaDevices.devices = [];

    const firstRefresh = setup.controller.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    const secondRefresh = setup.controller.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    sinkSwitch.resolve();
    await Promise.all([firstRefresh, secondRefresh]);

    assert.deepEqual(context.calls, [""]);
    assert.equal(setup.controller.outputId, "");
  });

  it("queues a new input fallback after a restored device disappears during rollback", async () => {
    const firstReplacement = deferred();
    const rollback = deferred();
    const oldTrack = createTrack();
    const firstDefaultTrack = createTrack();
    const secondDefaultTrack = createTrack();
    const oldStream = createStream([oldTrack]);
    const firstDefaultStream = createStream([firstDefaultTrack]);
    const secondDefaultStream = createStream([secondDefaultTrack]);
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
        if (this.replacements.length === 1) {
          await firstReplacement.promise;
        } else if (this.replacements.length === 2) {
          await rollback.promise;
        }
      },
    };
    const storage = new FakeStorage();
    const setup = createController({
      currentStream: oldStream,
      devices: [{ kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" }],
      sender,
      storage,
    });
    setup.mediaDevices.getUserMediaResults.push(firstDefaultStream, secondDefaultStream);
    await setup.controller.start();
    setCurrentInput(setup, "mic-1");

    setup.mediaDevices.devices = [];
    const firstMissing = setup.controller.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    setup.mediaDevices.devices = [{ kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" }];
    await setup.controller.refresh();
    firstReplacement.resolve();
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(sender.replacements, [firstDefaultTrack, oldTrack]);

    setup.mediaDevices.devices = [];
    const secondMissing = setup.controller.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    rollback.resolve();
    await Promise.all([firstMissing, secondMissing]);

    assert.deepEqual(setup.mediaDevices.getUserMediaCalls, [{ audio: true }, { audio: true }]);
    assert.deepEqual(sender.replacements, [firstDefaultTrack, oldTrack, secondDefaultTrack]);
    assert.equal(setup.currentStream(), secondDefaultStream);
    assert.equal(firstDefaultTrack.stopCalls, 1);
    assert.equal(secondDefaultTrack.stopCalls, 0);
    assert.equal(oldTrack.stopCalls, 1);
    assert.equal(setup.controller.inputId, "");
    assert.equal(setup.inputSelect.value, "");
    assert.equal(storage.getItem("moco.audio.inputDeviceId"), null);
  });

  it("queues a new output fallback after a restored device disappears during rollback", async () => {
    const firstSwitch = deferred();
    const rollback = deferred();
    const context = {
      calls: [],
      sinkId: "speaker-1",
      async setSinkId(deviceId) {
        this.calls.push(deviceId);
        if (this.calls.length === 1) {
          await firstSwitch.promise;
        } else if (this.calls.length === 2) {
          await rollback.promise;
        }
        this.sinkId = deviceId;
      },
    };
    const storage = new FakeStorage();
    const setup = createController({
      context,
      devices: [{ kind: "audiooutput", deviceId: "speaker-1", label: "Speaker 1" }],
      storage,
    });
    await setup.controller.start();
    setCurrentOutput(setup, "speaker-1");

    setup.mediaDevices.devices = [];
    const firstMissing = setup.controller.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    setup.mediaDevices.devices = [
      { kind: "audiooutput", deviceId: "speaker-1", label: "Speaker 1" },
    ];
    await setup.controller.refresh();
    firstSwitch.resolve();
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(context.calls, ["", "speaker-1"]);

    setup.mediaDevices.devices = [];
    const secondMissing = setup.controller.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    rollback.resolve();
    await Promise.all([firstMissing, secondMissing]);

    assert.deepEqual(context.calls, ["", "speaker-1", ""]);
    assert.equal(context.sinkId, "");
    assert.equal(setup.controller.outputId, "");
    assert.equal(setup.outputSelect.value, "");
    assert.equal(storage.getItem("moco.audio.outputDeviceId"), null);
  });

  it("queues a disconnected-input fallback behind an active microphone switch", async () => {
    const activeAcquisition = deferred();
    const oldTrack = createTrack();
    const selectedTrack = createTrack();
    const defaultTrack = createTrack();
    const sender = {
      replacements: [],
      async replaceTrack(track) {
        this.replacements.push(track);
      },
    };
    const setup = createController({
      currentStream: createStream([oldTrack]),
      devices: [
        { kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" },
        { kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" },
      ],
      sender,
    });
    setup.mediaDevices.getUserMediaResults.push(
      activeAcquisition.promise,
      createStream([defaultTrack]),
    );
    await setup.controller.start();
    setup.controller.inputId = "mic-1";
    setup.inputSelect.value = "mic-1";
    const switching = setup.controller.selectInput("mic-2");
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(setup.mediaDevices.getUserMediaCalls.length, 1);
    setup.mediaDevices.devices = [];

    const refreshing = setup.controller.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(setup.mediaDevices.getUserMediaCalls.length, 1);
    activeAcquisition.resolve(createStream([selectedTrack]));
    assert.equal(await switching, true);
    assert.equal(await refreshing, true);

    assert.deepEqual(setup.mediaDevices.getUserMediaCalls, [
      { audio: { deviceId: { exact: "mic-2" } } },
      { audio: true },
    ]);
    assert.deepEqual(sender.replacements, [selectedTrack, defaultTrack]);
    assert.equal(setup.controller.inputId, "");
    assert.equal(setup.inputSelect.value, "");
    assert.equal(setup.storage.getItem("moco.audio.inputDeviceId"), null);
  });

  it("renders unique real audio devices after the synthetic system defaults", async () => {
    const { controller, inputSelect, outputSelect } = createController({
      context: { setSinkId() {} },
      devices: [
        { kind: "audioinput", deviceId: "default", label: "Default input" },
        { kind: "audioinput", deviceId: "mic-1", label: "内蔵マイク" },
        { kind: "audioinput", deviceId: "", label: "Blank input" },
        { kind: "audioinput", deviceId: "mic-1", label: "Duplicate input" },
        { kind: "audioinput", deviceId: "mic-2", label: "USB マイク" },
        { kind: "audiooutput", deviceId: "communications", label: "Calls" },
        { kind: "audiooutput", deviceId: "speaker-1", label: "内蔵スピーカー" },
        { kind: "audiooutput", deviceId: "speaker-1", label: "Duplicate output" },
        { kind: "audiooutput", deviceId: "speaker-2", label: "ヘッドフォン" },
        { kind: "videoinput", deviceId: "camera-1", label: "Camera" },
        { kind: "other", deviceId: "other-1", label: "Other" },
      ],
    });

    await controller.start();

    assert.deepEqual(options(inputSelect), [
      ["", "システム既定"],
      ["mic-1", "内蔵マイク"],
      ["mic-2", "USB マイク"],
    ]);
    assert.deepEqual(options(outputSelect), [
      ["", "システム既定"],
      ["speaker-1", "内蔵スピーカー"],
      ["speaker-2", "ヘッドフォン"],
    ]);
  });

  it("derives fallback labels for unlabeled devices without storing names", async () => {
    const devices = [
      { kind: "audioinput", deviceId: "mic-1", label: "" },
      { kind: "audioinput", deviceId: "mic-2", label: "" },
      { kind: "audiooutput", deviceId: "speaker-1", label: "" },
      { kind: "audiooutput", deviceId: "speaker-2", label: "" },
    ];
    const { controller, inputSelect, outputSelect } = createController({
      context: { setSinkId() {} },
      devices,
    });

    await controller.start();

    assert.deepEqual(options(inputSelect), [
      ["", "システム既定"],
      ["mic-1", "マイク 1"],
      ["mic-2", "マイク 2"],
    ]);
    assert.deepEqual(options(outputSelect), [
      ["", "システム既定"],
      ["speaker-1", "出力 1"],
      ["speaker-2", "出力 2"],
    ]);
    assert.deepEqual(
      devices.map((device) => device.label),
      ["", "", "", ""],
    );
  });

  it("gates output candidates and control availability on setSinkId support", async () => {
    const devices = [
      { kind: "audioinput", deviceId: "mic-1", label: "Microphone" },
      { kind: "audiooutput", deviceId: "speaker-1", label: "Speaker" },
    ];
    const unsupported = createController({ devices });
    const supported = createController({ context: { setSinkId() {} }, devices });

    await unsupported.controller.start();
    await supported.controller.start();

    assert.equal(unsupported.inputSelect.disabled, false);
    assert.equal(unsupported.outputSelect.disabled, true);
    assert.deepEqual(options(unsupported.outputSelect), [["", "システム既定"]]);
    assert.equal(supported.inputSelect.disabled, false);
    assert.equal(supported.outputSelect.disabled, false);
    assert.deepEqual(options(supported.outputSelect), [
      ["", "システム既定"],
      ["speaker-1", "Speaker"],
    ]);
  });

  it("falls back to system defaults when enumeration fails", async () => {
    const setup = createController({ context: { setSinkId() {} } });
    setup.mediaDevices.enumerationError = new Error("permission state changed");

    await assert.doesNotReject(setup.controller.start());

    assert.deepEqual(options(setup.inputSelect), [["", "システム既定"]]);
    assert.deepEqual(options(setup.outputSelect), [["", "システム既定"]]);
    assert.equal(setup.inputSelect.disabled, false);
    assert.equal(setup.outputSelect.disabled, false);
  });

  it("keeps a newer devicechange result when the initial enumeration resolves last", async () => {
    const initial = deferred();
    const changed = deferred();
    const setup = createController({ context: { setSinkId() {} } });
    setup.mediaDevices.enumerationResults.push(initial.promise, changed.promise);

    const starting = setup.controller.start();
    setup.mediaDevices.dispatchEvent(new Event("devicechange"));
    changed.resolve([{ kind: "audioinput", deviceId: "new", label: "New microphone" }]);
    await new Promise((resolve) => setImmediate(resolve));
    initial.resolve([{ kind: "audioinput", deviceId: "old", label: "Old microphone" }]);
    await starting;

    assert.deepEqual(options(setup.inputSelect), [
      ["", "システム既定"],
      ["new", "New microphone"],
    ]);
    assert.equal(setup.mediaDevices.enumerateCalls, 2);
    assert.equal(setup.mediaDevices.deviceChangeAdds, 1);
  });

  it("applies the newest requested enumeration when the initial request resolves first", async () => {
    const initial = deferred();
    const changed = deferred();
    const setup = createController({ context: { setSinkId() {} } });
    setup.mediaDevices.enumerationResults.push(initial.promise, changed.promise);

    const starting = setup.controller.start();
    setup.mediaDevices.dispatchEvent(new Event("devicechange"));
    initial.resolve([{ kind: "audioinput", deviceId: "old", label: "Old microphone" }]);
    await starting;
    changed.resolve([{ kind: "audioinput", deviceId: "new", label: "New microphone" }]);
    await new Promise((resolve) => setImmediate(resolve));

    assert.deepEqual(options(setup.inputSelect), [
      ["", "システム既定"],
      ["new", "New microphone"],
    ]);
    assert.equal(setup.mediaDevices.enumerateCalls, 2);
    assert.equal(setup.mediaDevices.deviceChangeAdds, 1);
  });

  it("ignores an in-flight enumeration after close", async () => {
    const pending = deferred();
    const setup = createController({ context: { setSinkId() {} } });
    setup.mediaDevices.enumerationResults.push(pending.promise);

    const starting = setup.controller.start();
    setup.controller.close();
    setup.mediaDevices.dispatchEvent(new Event("devicechange"));
    pending.resolve([{ kind: "audioinput", deviceId: "late", label: "Late microphone" }]);
    await starting;
    await new Promise((resolve) => setImmediate(resolve));

    assert.deepEqual(options(setup.inputSelect), []);
    assert.equal(setup.inputSelect.disabled, true);
    assert.equal(setup.outputSelect.disabled, true);
    assert.equal(setup.mediaDevices.enumerateCalls, 1);
    assert.equal(setup.mediaDevices.deviceChangeRemoves, 1);
  });

  it("refreshes on devicechange and detaches the listener when closed", async () => {
    const setup = createController({
      context: { setSinkId() {} },
      devices: [{ kind: "audioinput", deviceId: "mic-1", label: "Microphone 1" }],
    });

    await setup.controller.start();
    assert.equal(setup.mediaDevices.deviceChangeAdds, 1);
    assert.equal(setup.mediaDevices.enumerateCalls, 1);

    setup.mediaDevices.devices = [{ kind: "audioinput", deviceId: "mic-2", label: "Microphone 2" }];
    setup.mediaDevices.dispatchEvent(new Event("devicechange"));
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(setup.mediaDevices.enumerateCalls, 2);
    assert.deepEqual(options(setup.inputSelect), [
      ["", "システム既定"],
      ["mic-2", "Microphone 2"],
    ]);

    setup.controller.close();
    setup.controller.close();
    assert.equal(setup.mediaDevices.deviceChangeRemoves, 1);
    assert.equal(setup.inputSelect.disabled, true);
    assert.equal(setup.outputSelect.disabled, true);

    setup.mediaDevices.dispatchEvent(new Event("devicechange"));
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(setup.mediaDevices.enumerateCalls, 2);
  });
});
