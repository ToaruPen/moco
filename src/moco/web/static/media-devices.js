const SYSTEM_DEFAULT_LABEL = "システム既定";
const RESERVED_DEVICE_IDS = new Set(["default", "communications"]);
const INPUT_STORAGE_KEY = "moco.audio.inputDeviceId";
const OUTPUT_STORAGE_KEY = "moco.audio.outputDeviceId";

function candidates(devices, kind) {
  const seen = new Set();
  return devices.filter((device) => {
    if (
      device.kind !== kind ||
      !device.deviceId ||
      RESERVED_DEVICE_IDS.has(device.deviceId) ||
      seen.has(device.deviceId)
    ) {
      return false;
    }
    seen.add(device.deviceId);
    return true;
  });
}

function renderOptions(select, devices, fallbackName, createOption) {
  select.replaceChildren(
    createOption(SYSTEM_DEFAULT_LABEL, ""),
    ...devices.map((device, index) =>
      createOption(device.label || `${fallbackName} ${index + 1}`, device.deviceId),
    ),
  );
}

export class AudioDeviceController {
  constructor({
    inputSelect,
    outputSelect,
    context,
    mediaDevices,
    storage,
    getCurrentStream = () => undefined,
    getAudioSender = () => undefined,
    replaceCurrentStream = () => {},
    onError = () => {},
    createOption = (label, value) => new Option(label, value),
  }) {
    this.inputSelect = inputSelect;
    this.outputSelect = outputSelect;
    this.context = context;
    this.mediaDevices = mediaDevices;
    this.storage = storage;
    this.getCurrentStream = getCurrentStream;
    this.getAudioSender = getAudioSender;
    this.replaceCurrentStream = replaceCurrentStream;
    this.onError = onError;
    this.createOption = createOption;
    this.inputId = "";
    this.outputId = "";
    this.devices = [];
    this.closed = false;
    this.listening = false;
    this.started = false;
    this.hasSuccessfulCatalog = false;
    this.restorationStarted = false;
    this.restorationPromise = null;
    this.inputPending = 0;
    this.outputPending = 0;
    this.inputQueue = Promise.resolve();
    this.outputQueue = Promise.resolve();
    this.refreshGeneration = 0;
    this.switchGeneration = 0;
    this.handleDeviceChange = () => {
      void this.refresh();
    };
  }

  async start() {
    if (this.closed) {
      return;
    }
    this.started = true;
    if (!this.listening) {
      this.mediaDevices.addEventListener("devicechange", this.handleDeviceChange);
      this.listening = true;
    }
    await this.refresh();
  }

  async refresh() {
    if (this.closed) {
      return;
    }
    const generation = ++this.refreshGeneration;
    let devices;
    try {
      devices = await this.mediaDevices.enumerateDevices();
    } catch {
      if (!this.hasSuccessfulCatalog) {
        this.#renderCatalog();
      }
      return false;
    }
    if (this.closed || generation !== this.refreshGeneration) {
      return false;
    }

    this.devices = devices;
    this.hasSuccessfulCatalog = true;
    this.#renderCatalog();
    if (this.started && !this.restorationStarted) {
      this.restorationStarted = true;
      this.restorationPromise = this.#restoreStoredSelections();
    }
    if (this.restorationPromise) {
      await this.restorationPromise;
    }
    if (this.closed || generation !== this.refreshGeneration) {
      return false;
    }

    const inputIds = new Set(
      candidates(this.devices, "audioinput").map((device) => device.deviceId),
    );
    const outputIds = new Set(
      candidates(this.devices, "audiooutput").map((device) => device.deviceId),
    );
    const fallbacks = [];
    if (this.inputId && !inputIds.has(this.inputId)) {
      fallbacks.push(this.selectInput(""));
    }
    if (
      this.outputId &&
      typeof this.context.setSinkId === "function" &&
      !outputIds.has(this.outputId)
    ) {
      fallbacks.push(this.selectOutput(""));
    }
    await Promise.all(fallbacks);
    return true;
  }

  #renderCatalog() {
    const supportsOutputSelection = typeof this.context.setSinkId === "function";
    renderOptions(
      this.inputSelect,
      candidates(this.devices, "audioinput"),
      "マイク",
      this.createOption,
    );
    renderOptions(
      this.outputSelect,
      supportsOutputSelection ? candidates(this.devices, "audiooutput") : [],
      "出力",
      this.createOption,
    );
    this.#renderSelectionAndAvailability();
  }

  async #restoreStoredSelections() {
    if (this.closed) {
      return;
    }
    const inputId = this.#readDeviceId(INPUT_STORAGE_KEY);
    const outputId = this.#readDeviceId(OUTPUT_STORAGE_KEY);
    const inputIds = new Set(
      candidates(this.devices, "audioinput").map((device) => device.deviceId),
    );
    const outputIds = new Set(
      candidates(this.devices, "audiooutput").map((device) => device.deviceId),
    );
    const restorations = [];

    if (inputId && inputIds.has(inputId)) {
      restorations.push(this.selectInput(inputId));
    } else if (inputId) {
      this.#storeDeviceId(INPUT_STORAGE_KEY, "");
    }

    if (outputId && typeof this.context.setSinkId === "function" && outputIds.has(outputId)) {
      restorations.push(this.selectOutput(outputId));
    } else if (outputId) {
      this.#storeDeviceId(OUTPUT_STORAGE_KEY, "");
    }

    await Promise.all(restorations);
  }

  async selectInput(deviceId) {
    if (this.closed) {
      return false;
    }
    this.inputPending += 1;
    this.#renderSelectionAndAvailability();
    const operation = this.inputQueue.then(() => this.#switchInput(deviceId));
    this.inputQueue = operation.catch(() => false);
    try {
      return await operation;
    } finally {
      this.inputPending -= 1;
      this.#renderSelectionAndAvailability();
    }
  }

  async #switchInput(deviceId) {
    if (this.closed || deviceId === this.inputId) {
      return false;
    }
    const generation = this.switchGeneration;
    let candidate;
    try {
      candidate = await this.mediaDevices.getUserMedia({
        audio: deviceId ? { deviceId: { exact: deviceId } } : true,
      });
      if (this.closed || generation !== this.switchGeneration) {
        this.#stopStream(candidate);
        return false;
      }

      const nextTrack = candidate?.getAudioTracks()[0];
      const current = this.getCurrentStream();
      const currentTrack = current?.getAudioTracks()[0];
      const sender = this.getAudioSender();
      if (!nextTrack || !currentTrack || typeof sender?.replaceTrack !== "function") {
        throw new Error("audio route unavailable");
      }

      nextTrack.enabled = currentTrack.enabled;
      await sender.replaceTrack(nextTrack);
      if (this.closed || generation !== this.switchGeneration) {
        this.#stopStream(candidate);
        return false;
      }

      this.replaceCurrentStream(candidate);
      this.#stopStream(current);
      this.inputId = deviceId;
      this.#storeDeviceId(INPUT_STORAGE_KEY, deviceId);
      return true;
    } catch {
      this.#stopStream(candidate);
      if (!this.closed && generation === this.switchGeneration) {
        this.#emitError("microphone_switch_failed");
      }
      return false;
    }
  }

  async selectOutput(deviceId) {
    if (this.closed || typeof this.context.setSinkId !== "function") {
      return false;
    }
    this.outputPending += 1;
    this.#renderSelectionAndAvailability();
    const operation = this.outputQueue.then(() => this.#switchOutput(deviceId));
    this.outputQueue = operation.catch(() => false);
    try {
      return await operation;
    } finally {
      this.outputPending -= 1;
      this.#renderSelectionAndAvailability();
    }
  }

  async #switchOutput(deviceId) {
    if (this.closed || deviceId === this.outputId) {
      return false;
    }
    const generation = this.switchGeneration;
    try {
      await this.context.setSinkId(deviceId);
      if (this.closed || generation !== this.switchGeneration) {
        return false;
      }
      this.outputId = deviceId;
      this.#storeDeviceId(OUTPUT_STORAGE_KEY, deviceId);
      return true;
    } catch {
      if (!this.closed && generation === this.switchGeneration) {
        this.#emitError("audio_output_switch_failed");
      }
      return false;
    }
  }

  close() {
    if (this.closed) {
      return;
    }
    this.closed = true;
    this.refreshGeneration += 1;
    this.switchGeneration += 1;
    if (this.listening) {
      this.mediaDevices.removeEventListener("devicechange", this.handleDeviceChange);
      this.listening = false;
    }
    this.inputSelect.disabled = true;
    this.outputSelect.disabled = true;
  }

  #renderSelectionAndAvailability() {
    const inputExists = [...this.inputSelect.options].some(
      (option) => option.value === this.inputId,
    );
    const outputExists = [...this.outputSelect.options].some(
      (option) => option.value === this.outputId,
    );
    this.inputSelect.value = inputExists ? this.inputId : "";
    this.outputSelect.value = outputExists ? this.outputId : "";
    this.inputSelect.disabled = this.closed || this.inputPending > 0;
    this.outputSelect.disabled =
      this.closed || this.outputPending > 0 || typeof this.context.setSinkId !== "function";
  }

  #storeDeviceId(key, deviceId) {
    try {
      if (deviceId) {
        this.storage?.setItem(key, deviceId);
      } else {
        this.storage?.removeItem(key);
      }
    } catch {}
  }

  #readDeviceId(key) {
    try {
      return this.storage?.getItem(key) || "";
    } catch {
      return "";
    }
  }

  #stopStream(stream) {
    for (const track of stream?.getTracks?.() ?? []) {
      track.stop();
    }
  }

  #emitError(code) {
    try {
      this.onError(code);
    } catch {}
  }
}
