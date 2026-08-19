const SYSTEM_DEFAULT_LABEL = "システム既定";
const RESERVED_DEVICE_IDS = new Set(["default", "communications"]);

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
    createOption = (label, value) => new Option(label, value),
  }) {
    this.inputSelect = inputSelect;
    this.outputSelect = outputSelect;
    this.context = context;
    this.mediaDevices = mediaDevices;
    this.createOption = createOption;
    this.closed = false;
    this.listening = false;
    this.handleDeviceChange = () => {
      void this.refresh();
    };
  }

  async start() {
    if (this.closed) {
      return;
    }
    await this.refresh();
    if (this.closed || this.listening) {
      return;
    }
    this.mediaDevices.addEventListener("devicechange", this.handleDeviceChange);
    this.listening = true;
  }

  async refresh() {
    let devices = [];
    try {
      devices = await this.mediaDevices.enumerateDevices();
    } catch {}

    const supportsOutputSelection = typeof this.context.setSinkId === "function";
    renderOptions(this.inputSelect, candidates(devices, "audioinput"), "マイク", this.createOption);
    renderOptions(
      this.outputSelect,
      supportsOutputSelection ? candidates(devices, "audiooutput") : [],
      "出力",
      this.createOption,
    );
    this.inputSelect.disabled = this.closed;
    this.outputSelect.disabled = this.closed || !supportsOutputSelection;
  }

  close() {
    if (this.closed) {
      return;
    }
    this.closed = true;
    if (this.listening) {
      this.mediaDevices.removeEventListener("devicechange", this.handleDeviceChange);
      this.listening = false;
    }
    this.inputSelect.disabled = true;
    this.outputSelect.disabled = true;
  }
}
