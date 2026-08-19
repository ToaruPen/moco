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
  const promise = new Promise((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function createController({ devices = [], context = {} } = {}) {
  const dom = new JSDOM(`
    <select id="audio-input" disabled></select>
    <select id="audio-output" disabled></select>
  `);
  const document = dom.window.document;
  const mediaDevices = new FakeMediaDevices(devices);
  const inputSelect = document.querySelector("#audio-input");
  const outputSelect = document.querySelector("#audio-output");
  const controller = new AudioDeviceController({
    inputSelect,
    outputSelect,
    context,
    mediaDevices,
    createOption: (label, value) => new dom.window.Option(label, value),
  });

  return { controller, inputSelect, mediaDevices, outputSelect };
}

function options(select) {
  return [...select.options].map((option) => [option.value, option.textContent]);
}

describe("AudioDeviceController", () => {
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
