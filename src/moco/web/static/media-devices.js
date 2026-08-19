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
    this.retainedInput = null;
    this.retainedOutput = null;
    this.inputPending = 0;
    this.outputPending = 0;
    this.inputQueue = Promise.resolve();
    this.outputQueue = Promise.resolve();
    this.inputFallback = null;
    this.outputFallback = null;
    this.inputAvailability = { deviceId: "", epoch: 0, missing: false };
    this.outputAvailability = { deviceId: "", epoch: 0, missing: false };
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
    const inputAvailability = this.#updateAvailability("inputAvailability", this.inputId, inputIds);
    const outputAvailability = this.#updateAvailability(
      "outputAvailability",
      this.outputId,
      outputIds,
    );
    const fallbacks = [];
    if (this.inputId && !inputIds.has(this.inputId)) {
      fallbacks.push(this.#selectInputFallback(this.inputId, inputAvailability.epoch));
    }
    if (
      this.outputId &&
      typeof this.context.setSinkId === "function" &&
      !outputIds.has(this.outputId)
    ) {
      fallbacks.push(this.#selectOutputFallback(this.outputId, outputAvailability.epoch));
    }
    await Promise.all(fallbacks);
    return true;
  }

  #renderCatalog() {
    if (this.closed) {
      return;
    }
    const supportsOutputSelection = typeof this.context.setSinkId === "function";
    const inputDevices = this.#devicesWithRetainedSelection(
      this.inputSelect,
      candidates(this.devices, "audioinput"),
      this.inputId,
      "retainedInput",
    );
    const outputDevices = this.#devicesWithRetainedSelection(
      this.outputSelect,
      supportsOutputSelection ? candidates(this.devices, "audiooutput") : [],
      this.outputId,
      "retainedOutput",
    );
    renderOptions(this.inputSelect, inputDevices, "マイク", this.createOption);
    renderOptions(this.outputSelect, outputDevices, "出力", this.createOption);
    this.#renderSelectionAndAvailability();
  }

  #devicesWithRetainedSelection(select, devices, selectedId, retainedProperty) {
    if (!selectedId || devices.some((device) => device.deviceId === selectedId)) {
      this[retainedProperty] = null;
      return devices;
    }

    if (this[retainedProperty]?.deviceId !== selectedId) {
      const selectedOption = [...select.options].find((option) => option.value === selectedId);
      this[retainedProperty] = selectedOption
        ? { deviceId: selectedId, label: selectedOption.textContent }
        : null;
    }
    return this[retainedProperty] ? [...devices, this[retainedProperty]] : devices;
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
    const pendingSelection = this.#capturePendingSelection(
      this.inputSelect,
      deviceId,
      "選択中のマイク",
    );
    return await this.#enqueueInput(deviceId, null, pendingSelection);
  }

  #selectInputFallback(expectedId, missingEpoch) {
    if (
      this.inputFallback?.expectedId === expectedId &&
      this.inputFallback.missingEpoch === missingEpoch
    ) {
      return this.inputFallback.promise;
    }
    const fallback = { expectedId };
    const promise = this.#enqueueInput("", fallback);
    const pending = { expectedId, missingEpoch, promise };
    this.inputFallback = pending;
    const clear = () => {
      if (this.inputFallback === pending) {
        this.inputFallback = null;
      }
    };
    promise.then(clear, clear);
    return promise;
  }

  async #enqueueInput(deviceId, fallback = null, pendingSelection = null) {
    if (this.closed) {
      return false;
    }
    this.inputPending += 1;
    this.#renderSelectionAndAvailability();
    const operation = this.inputQueue.then(() =>
      this.#switchInput(deviceId, fallback, pendingSelection),
    );
    this.inputQueue = operation.catch(() => false);
    try {
      return await operation;
    } finally {
      this.inputPending -= 1;
      this.#renderCatalog();
    }
  }

  async #switchInput(deviceId, fallback, pendingSelection) {
    fallback = this.#prepareFallback(fallback, "audioinput", this.inputId);
    if (
      this.closed ||
      (deviceId === this.inputId && this.#currentInputTrackIsLive()) ||
      fallback === false
    ) {
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
      if (!this.#fallbackIsCurrent(fallback, "audioinput", this.inputId)) {
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
      if (!this.#fallbackIsCurrent(fallback, "audioinput", this.inputId)) {
        this.#stopStream(candidate);
        return false;
      }
      await sender.replaceTrack(nextTrack);
      if (this.closed || generation !== this.switchGeneration) {
        this.#stopStream(candidate);
        return false;
      }
      const authoritativeCurrent = this.getCurrentStream();
      const authoritativeTrack = authoritativeCurrent?.getAudioTracks()[0];
      const authoritativeSender = this.getAudioSender();
      if (
        authoritativeCurrent !== current ||
        authoritativeTrack !== currentTrack ||
        authoritativeSender !== sender
      ) {
        const rollbackTrack =
          authoritativeSender === sender && authoritativeTrack ? authoritativeTrack : currentTrack;
        const rolledBack = await this.#rollbackInputFallback(sender, rollbackTrack);
        if (this.closed || generation !== this.switchGeneration) {
          this.#stopStream(candidate);
          return false;
        }
        const latestSender = this.getAudioSender();
        if (rolledBack) {
          this.#stopStream(candidate);
          if (!fallback && (authoritativeSender !== sender || latestSender !== sender)) {
            this.#emitError("microphone_switch_failed");
          }
          return false;
        }
        const latestCurrent = this.getCurrentStream();
        const latestTrack = latestCurrent?.getAudioTracks()[0];
        if (latestSender !== sender || ("track" in sender && sender.track !== nextTrack)) {
          this.#stopStream(candidate);
          if (!fallback && latestSender !== sender) {
            this.#emitError("microphone_switch_failed");
          }
          return false;
        }

        nextTrack.enabled =
          latestTrack?.enabled ?? authoritativeTrack?.enabled ?? currentTrack.enabled;
        this.replaceCurrentStream(candidate);
        for (const superseded of new Set([current, authoritativeCurrent, latestCurrent])) {
          if (superseded && superseded !== candidate) {
            this.#stopStream(superseded);
          }
        }
        this.inputId = deviceId;
        if (pendingSelection?.deviceId === deviceId) {
          this.retainedInput = pendingSelection;
        }
        this.#storeDeviceId(INPUT_STORAGE_KEY, deviceId);
        this.#emitError("microphone_switch_failed");
        return true;
      }
      if (!this.#fallbackIsCurrent(fallback, "audioinput", this.inputId)) {
        const rolledBack = await this.#rollbackInputFallback(sender, currentTrack);
        if (this.closed || generation !== this.switchGeneration) {
          this.#stopStream(candidate);
          return false;
        }
        if (!rolledBack) {
          this.replaceCurrentStream(candidate);
          this.#stopStream(current);
          this.inputId = "";
          this.#storeDeviceId(INPUT_STORAGE_KEY, "");
          return true;
        }
        this.#stopStream(candidate);
        return false;
      }

      nextTrack.enabled = authoritativeTrack.enabled;
      this.replaceCurrentStream(candidate);
      this.#stopStream(current);
      this.inputId = deviceId;
      if (pendingSelection?.deviceId === deviceId) {
        this.retainedInput = pendingSelection;
      }
      this.#storeDeviceId(INPUT_STORAGE_KEY, deviceId);
      return true;
    } catch {
      this.#stopStream(candidate);
      if (
        !this.closed &&
        generation === this.switchGeneration &&
        this.#fallbackIsCurrent(fallback, "audioinput", this.inputId)
      ) {
        this.#emitError("microphone_switch_failed");
      }
      return false;
    }
  }

  async selectOutput(deviceId) {
    const pendingSelection = this.#capturePendingSelection(
      this.outputSelect,
      deviceId,
      "選択中の出力",
    );
    return await this.#enqueueOutput(deviceId, null, pendingSelection);
  }

  #selectOutputFallback(expectedId, missingEpoch) {
    if (
      this.outputFallback?.expectedId === expectedId &&
      this.outputFallback.missingEpoch === missingEpoch
    ) {
      return this.outputFallback.promise;
    }
    const fallback = { expectedId };
    const promise = this.#enqueueOutput("", fallback);
    const pending = { expectedId, missingEpoch, promise };
    this.outputFallback = pending;
    const clear = () => {
      if (this.outputFallback === pending) {
        this.outputFallback = null;
      }
    };
    promise.then(clear, clear);
    return promise;
  }

  async #enqueueOutput(deviceId, fallback = null, pendingSelection = null) {
    if (this.closed || typeof this.context.setSinkId !== "function") {
      return false;
    }
    this.outputPending += 1;
    this.#renderSelectionAndAvailability();
    const operation = this.outputQueue.then(() =>
      this.#switchOutput(deviceId, fallback, pendingSelection),
    );
    this.outputQueue = operation.catch(() => false);
    try {
      return await operation;
    } finally {
      this.outputPending -= 1;
      this.#renderCatalog();
    }
  }

  async #switchOutput(deviceId, fallback, pendingSelection) {
    fallback = this.#prepareFallback(fallback, "audiooutput", this.outputId);
    if (this.closed || deviceId === this.outputId || fallback === false) {
      return false;
    }
    const generation = this.switchGeneration;
    const previousId = this.outputId;
    try {
      if (!this.#fallbackIsCurrent(fallback, "audiooutput", this.outputId)) {
        return false;
      }
      await this.context.setSinkId(deviceId);
      if (this.closed || generation !== this.switchGeneration) {
        return false;
      }
      if (!this.#fallbackIsCurrent(fallback, "audiooutput", this.outputId)) {
        const rolledBack = await this.#rollbackOutputFallback(previousId);
        if (this.closed || generation !== this.switchGeneration) {
          return false;
        }
        if (!rolledBack) {
          this.outputId = "";
          this.#storeDeviceId(OUTPUT_STORAGE_KEY, "");
          return true;
        }
        return false;
      }
      this.outputId = deviceId;
      if (pendingSelection?.deviceId === deviceId) {
        this.retainedOutput = pendingSelection;
      }
      this.#storeDeviceId(OUTPUT_STORAGE_KEY, deviceId);
      return true;
    } catch {
      if (
        !this.closed &&
        generation === this.switchGeneration &&
        this.#fallbackIsCurrent(fallback, "audiooutput", this.outputId)
      ) {
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

  #capturePendingSelection(select, deviceId, fallbackLabel) {
    if (!deviceId) {
      return null;
    }
    const option = [...select.options].find((candidate) => candidate.value === deviceId);
    return { deviceId, label: option?.textContent || fallbackLabel };
  }

  #updateAvailability(property, deviceId, availableIds) {
    const previous = this[property];
    const missing = Boolean(deviceId) && !availableIds.has(deviceId);
    if (previous.deviceId !== deviceId || previous.missing !== missing) {
      this[property] = { deviceId, epoch: previous.epoch + 1, missing };
    }
    return this[property];
  }

  #fallbackIsCurrent(fallback, kind, currentId) {
    if (!fallback) {
      return true;
    }
    return (
      !this.closed &&
      fallback.expectedId === currentId &&
      (!candidates(this.devices, kind).some((device) => device.deviceId === fallback.expectedId) ||
        (kind === "audioinput" && !this.#currentInputTrackIsLive()))
    );
  }

  #prepareFallback(fallback, kind, currentId) {
    if (!fallback) {
      return null;
    }
    if (
      this.closed ||
      !currentId ||
      (candidates(this.devices, kind).some((device) => device.deviceId === currentId) &&
        (kind !== "audioinput" || this.#currentInputTrackIsLive()))
    ) {
      return false;
    }
    fallback.expectedId = currentId;
    return fallback;
  }

  #currentInputTrackIsLive() {
    return this.getCurrentStream()?.getAudioTracks()[0]?.readyState !== "ended";
  }

  async #rollbackInputFallback(sender, currentTrack) {
    try {
      await sender.replaceTrack(currentTrack);
      return true;
    } catch {
      return false;
    }
  }

  async #rollbackOutputFallback(previousId) {
    try {
      await this.context.setSinkId(previousId);
      return true;
    } catch {
      return false;
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
