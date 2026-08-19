import { ActivityBuffer, ActivityView, ProgressTracker, ProgressView } from "./activity.js";
import {
  EDITABLE_TOKENS,
  isEditableTarget,
  PRESET_OPTIONS,
  PRESETS,
  ThemeController,
  watchSystemTheme,
} from "./theme.js";

const WEBSOCKET_PROTOCOL = "moco";
const CAPABILITY_PREFIX = `${WEBSOCKET_PROTOCOL}.capability.`;
const CAPABILITY_STORAGE_KEY = "moco.capability";
const ICE_GATHERING_TIMEOUT_MS = 10_000;
const WEBSOCKET_OPEN_TIMEOUT_MS = 10_000;

const ERROR_COPY = Object.freeze({
  invalid_message: "受信した操作を解釈できませんでした",
  already_started: "Realtime 会話はすでに開始しています",
  irodori_unavailable: "Irodori に接続できません",
  capability_mismatch: "Irodori の機能契約に互換性がありません",
  caption_unsupported: "Irodori が話し方指定に対応していません",
  speech_caption_invalid: "話し方指定を検証できなかったため標準表現で読み上げます",
  configured_voice_unavailable: "設定した音声モデルを利用できません",
  voice_catalog_empty: "利用可能な音声モデルがありません",
  voice_selection_required: "音声モデルを選択してください",
  model_loading: "音声モデルを読み込み中です",
  model_not_loaded: "音声モデルが読み込まれていません",
  voice_bank_invalid: "音声モデル一覧を利用できません",
  runtime_generation_mismatch: "音声ランタイムが更新されたため再接続が必要です",
  voice_not_found: "選択した音声モデルが見つかりません",
  conversation_start_failed: "Realtime 会話を開始できませんでした",
  conversation_not_started: "Realtime 会話が開始していません",
  interaction_busy: "別の処理を受け付けています",
  turn_not_active: "取り消せる処理はありません",
  voice_not_available: "選択した音声モデルを利用できません",
  codex_realtime_error: "Codex Realtime でエラーが発生しました",
  invalid_realtime_event: "Codex から不正なイベントを受信しました",
  single_operator_only: "別のオペレーター画面が接続しています",
  websocket_disconnected: "オペレーター接続が切断されました",
  ice_gathering_timeout: "音声接続の準備が完了しませんでした",
  websocket_open_timeout: "オペレーター接続が時間内に開きませんでした",
  websocket_failed: "オペレーター接続に失敗しました",
  websocket_closed_before_open: "オペレーター接続が開始前に閉じました",
  webrtc_connection_failed: "Realtime 音声接続が失敗しました",
  theme_config_invalid: "保存済み配色を読み込めないため既定値へ戻しました",
  audio_decode_failed: "受信した音声を再生できませんでした",
  audio_start_failed: "音声出力を開始できませんでした",
  audio_resume_failed: "音声出力を有効化できませんでした",
  microphone_permission_denied: "マイクの使用が許可されませんでした",
  microphone_unavailable: "利用可能なマイクが見つかりませんでした",
  microphone_failed: "マイクを開始できませんでした",
  synthesis_failed: "音声生成中に予期しないエラーが発生しました",
  pairing_failed: "スマートフォン接続用QRを取得できませんでした",
  enable_failed: "音声セッションを開始できませんでした",
});

const CONVERSATION_START_ERRORS = new Set([
  "already_started",
  "capability_mismatch",
  "caption_unsupported",
  "configured_voice_unavailable",
  "conversation_start_failed",
  "irodori_unavailable",
  "model_loading",
  "model_not_loaded",
  "runtime_generation_mismatch",
  "voice_bank_invalid",
  "voice_catalog_empty",
  "voice_not_found",
  "voice_selection_required",
]);

const THEME_LABELS = Object.freeze({
  background: "背景",
  surface: "パネル",
  surfaceRaised: "強調パネル",
  border: "境界線",
  text: "本文",
  textMuted: "補助文字",
  accent: "情報アクセント",
  actionAccent: "操作アクセント",
});

const THEME_GROUP_LABELS = Object.freeze({
  automatic: "自動",
  light: "Light",
  dark: "Dark",
  accessibility: "アクセシビリティ",
});

export class AudioPlaybackQueue {
  constructor(
    context,
    onState,
    onError = () => {},
    timers = {
      set: (callback, delayMs) => globalThis.setTimeout(callback, delayMs),
      clear: (handle) => globalThis.clearTimeout(handle),
    },
  ) {
    this.context = context;
    this.onState = onState;
    this.onError = onError;
    this.timers = timers;
    this.sources = new Set();
    this.pendingStartAcknowledgements = new Map();
    this.epoch = 0;
    this.chain = Promise.resolve();
    this.nextStart = context.currentTime;
  }

  get isPlaying() {
    return this.sources.size > 0;
  }

  enqueue(bytes, metadata) {
    const epoch = this.epoch;
    this.chain = this.chain
      .then(async () => {
        if (epoch !== this.epoch) {
          return;
        }
        if (this.context.state === "suspended") {
          try {
            await this.context.resume();
          } catch {
            throw namedError("audio_resume_failed");
          }
          if (epoch !== this.epoch) {
            return;
          }
          if (this.context.state !== "running") {
            throw namedError("audio_resume_failed");
          }
        }
        const buffer = await this.context.decodeAudioData(bytes.slice(0));
        if (epoch !== this.epoch) {
          return;
        }
        const source = this.context.createBufferSource();
        source.buffer = buffer;
        source.connect(this.context.destination);
        const startAt = Math.max(this.context.currentTime + 0.02, this.nextStart);
        source.addEventListener(
          "ended",
          () => {
            if (epoch !== this.epoch) {
              return;
            }
            this.#confirmStartAcknowledgement(source, metadata, epoch);
            this.sources.delete(source);
            if (!this.isPlaying) {
              this.nextStart = this.context.currentTime;
            }
            this.onState(this.isPlaying, metadata, this.context.state, "completed");
          },
          { once: true },
        );
        try {
          source.start(startAt);
        } catch {
          throw namedError("audio_start_failed");
        }
        this.nextStart = startAt + buffer.duration;
        this.sources.add(source);
        this.#acknowledgeAtStart(source, metadata, epoch, startAt);
      })
      .catch((error) => {
        if (epoch !== this.epoch) {
          return;
        }
        if (!this.isPlaying) {
          this.nextStart = this.context.currentTime;
        }
        this.onState(this.isPlaying, metadata, this.context.state, "failed");
        this.onError(
          error.name === "audio_resume_failed" || error.name === "audio_start_failed"
            ? error.name
            : "audio_decode_failed",
        );
      });
  }

  stop() {
    this.epoch += 1;
    this.chain = Promise.resolve();
    for (const source of this.pendingStartAcknowledgements.keys()) {
      this.#cancelStartAcknowledgement(source);
    }
    for (const source of this.sources) {
      try {
        source.stop();
      } catch (error) {
        if (error.name !== "InvalidStateError") {
          throw error;
        }
      }
    }
    this.sources.clear();
    this.nextStart = this.context.currentTime;
    this.onState(false, undefined, undefined, "stopped");
  }

  #acknowledgeAtStart(source, metadata, epoch, startAt) {
    const token = { handle: undefined };
    const checkStart = () => {
      if (this.pendingStartAcknowledgements.get(source) !== token) {
        return;
      }
      const remainingMs = Math.max(0, (startAt - this.context.currentTime) * 1000);
      if (this.context.state !== "running" || remainingMs > 0) {
        const retryMs = this.context.state === "running" ? Math.max(1, remainingMs) : 50;
        token.handle = this.timers.set(checkStart, retryMs);
        return;
      }
      this.#confirmStartAcknowledgement(source, metadata, epoch);
    };
    this.pendingStartAcknowledgements.set(source, token);
    const delayMs = Math.max(0, (startAt - this.context.currentTime) * 1000);
    token.handle = this.timers.set(checkStart, delayMs);
  }

  #confirmStartAcknowledgement(source, metadata, epoch) {
    if (!this.pendingStartAcknowledgements.has(source)) {
      return;
    }
    this.#cancelStartAcknowledgement(source);
    if (epoch === this.epoch && this.sources.has(source)) {
      this.onState(true, metadata, this.context.state, "started");
    }
  }

  #cancelStartAcknowledgement(source) {
    const token = this.pendingStartAcknowledgements.get(source);
    if (!token) {
      return;
    }
    this.pendingStartAcknowledgements.delete(source);
    if (token.handle !== undefined) {
      this.timers.clear(token.handle);
    }
  }
}

export class BrowserHotkeyMapper {
  constructor({ globalHotkeysEnabled, startKey, stopKey }) {
    this.configure({ globalHotkeysEnabled, startKey, stopKey });
  }

  configure({ globalHotkeysEnabled, startKey, stopKey }) {
    const canonicalStart = startKey.toLowerCase();
    const canonicalStop = stopKey.toLowerCase();
    const changed =
      this.globalHotkeysEnabled !== globalHotkeysEnabled ||
      this.startKey !== canonicalStart ||
      this.stopKey !== canonicalStop;
    this.globalHotkeysEnabled = globalHotkeysEnabled;
    this.startKey = canonicalStart;
    this.stopKey = canonicalStop;
    if (changed) {
      this.pressed = new Set();
    }
  }

  handles(key) {
    const canonical = key.toLowerCase();
    return (
      !this.globalHotkeysEnabled && (canonical === this.startKey || canonical === this.stopKey)
    );
  }

  keyDown(key) {
    const canonical = key.toLowerCase();
    if (!this.handles(canonical) || this.pressed.has(canonical)) {
      return null;
    }
    this.pressed.add(canonical);
    return canonical === this.startKey ? "listen_start" : "listen_stop";
  }

  keyUp(key) {
    const canonical = key.toLowerCase();
    if (!this.handles(canonical) || !this.pressed.delete(canonical)) {
      return null;
    }
    return null;
  }
}

export class TurnCancelController {
  constructor({ button, send }) {
    this.button = button;
    this.send = send;
    this.button.addEventListener("click", () => this.cancel());
  }

  configure(canCancel) {
    this.button.disabled = canCancel !== true;
  }

  cancel() {
    if (this.button.disabled) {
      return false;
    }
    this.button.disabled = true;
    this.send({ type: "control", control: "turn_cancel" });
    return true;
  }
}

export function shouldHandleHotkey(event, mapper) {
  return !isEditableTarget(event.target) && mapper.handles(event.key);
}

export class OperatorStatus {
  constructor({ activityBuffer, activityView, error, errorText, progress, progressView }) {
    this.activityBuffer = activityBuffer;
    this.activityView = activityView;
    this.error = error;
    this.errorText = errorText;
    this.progress = progress;
    this.progressView = progressView;
  }

  consume(message) {
    if (message.type === "activity") {
      this.#append(message);
    } else if (message.type === "reasoning_summary") {
      const item = this.activityBuffer.addSummary(message);
      this.progress.consume(item);
      this.#render();
    } else if (message.type === "error") {
      this.showError(message.code);
    }
  }

  addLocal({ kind, phase, label, occurredAtMs = Date.now() }) {
    this.#append({ kind, phase, label, occurredAtMs });
  }

  showError(code) {
    const label = `${code} — ${ERROR_COPY[code] ?? "不明なエラー"}`;
    this.errorText.textContent = label;
    this.error.hidden = false;
    this.addLocal({ kind: "error", phase: "completed", label });
  }

  dismissError() {
    this.error.hidden = true;
  }

  renderProgress() {
    this.progressView.render(this.progress.snapshot());
  }

  disconnect() {
    this.progress.disconnect();
    this.renderProgress();
  }

  expire() {
    this.progress.expire();
    this.renderProgress();
  }

  #append(event) {
    const item = this.activityBuffer.add(event);
    this.progress.consume(item);
    this.#render();
  }

  #render() {
    this.activityView.render(this.activityBuffer.items);
    this.renderProgress();
  }
}

export class VoiceModelController {
  constructor({ select, send, createOption = (label, value) => new Option(label, value) }) {
    this.element = select;
    this.send = send;
    this.createOption = createOption;
    this.options = [];
    this.ready = false;
    this.readiness = "loading";
    this.selected = "";
  }

  configure({ options, selected, ready, readiness }) {
    this.options = options;
    this.ready = ready;
    this.readiness = readiness;
    this.confirm(selected);
  }

  confirm(selected) {
    this.selected = typeof selected === "string" ? selected : "";
    this.render();
  }

  select(value) {
    this.element.value = this.selected;
    if (typeof value === "string" && value.trim()) {
      this.send({ type: "select_voice", voice_id: value });
    }
  }

  render() {
    if (!this.ready || this.options.length === 0) {
      let label = "音声モデルを利用できません";
      if (this.ready) {
        label = "利用可能な音声モデルがありません";
      } else if (this.readiness === "loading" || this.readiness === "model_loading") {
        label = "音声モデルを読み込み中";
      }
      const status = this.createOption(label, "");
      status.disabled = true;
      this.element.replaceChildren(status);
      this.element.value = "";
      this.element.disabled = true;
      return;
    }

    const selectedIsAvailable = this.options.some(({ id }) => id === this.selected);
    const choices = this.options.map(({ id, label }) => this.createOption(label, id));
    if (!selectedIsAvailable) {
      const prompt = this.createOption("音声モデルを選択してください", "");
      prompt.disabled = true;
      choices.unshift(prompt);
    }
    this.element.replaceChildren(...choices);
    this.element.value = selectedIsAvailable ? this.selected : "";
    this.element.disabled = false;
  }
}

export class MocoController {
  #reconnectPromise = null;

  constructor({ stream, playback, send, reconnect }) {
    this.stream = stream;
    this.playback = playback;
    this.send = send;
    this.reconnect = reconnect;
    this.audioGeneration = 0;
    this.idleExpired = false;
    this.reconnectRequired = false;
    this.pendingAudio = null;
    this.controlEpoch = 0;
    this.listeningRequested = false;
  }

  async applyControl(control) {
    const epoch = ++this.controlEpoch;
    const track = this.stream.getAudioTracks()[0];
    if (!track) {
      return false;
    }
    if (control === "listen_start") {
      if (this.idleExpired || this.reconnectRequired) {
        let reconnectPromise = this.#reconnectPromise;
        if (!reconnectPromise) {
          reconnectPromise = this.reconnect();
          this.#reconnectPromise = reconnectPromise;
        }
        try {
          await reconnectPromise;
        } finally {
          if (this.#reconnectPromise === reconnectPromise) {
            this.#reconnectPromise = null;
          }
        }
        // The replacement succeeded even if a newer stop superseded this start. Keep the
        // microphone stopped, but do not ask the next start to replace the healthy Voice.
        this.idleExpired = false;
        this.reconnectRequired = false;
        if (epoch !== this.controlEpoch) {
          return false;
        }
      }
      track.enabled = true;
      this.listeningRequested = true;
    } else if (control === "listen_stop") {
      track.enabled = false;
      this.listeningRequested = false;
    } else {
      return false;
    }
    this.send({ type: "control", control });
    return true;
  }

  acceptAudio(metadata) {
    this.pendingAudio = {
      accepted: metadata.generation === this.audioGeneration,
      metadata,
    };
  }

  consumeAudio(bytes) {
    const pending = this.pendingAudio;
    this.pendingAudio = null;
    if (pending?.accepted) {
      this.playback.enqueue(bytes, pending.metadata);
    }
  }

  invalidateAudio(generation) {
    this.audioGeneration = generation;
    this.pendingAudio = null;
    this.playback.stop();
  }

  disconnect() {
    this.controlEpoch += 1;
    this.listeningRequested = false;
    const track = this.stream.getAudioTracks()[0];
    if (track) {
      track.enabled = false;
    }
    this.audioGeneration += 1;
    this.pendingAudio = null;
    this.playback.stop();
  }

  expire() {
    this.controlEpoch += 1;
    this.listeningRequested = false;
    const track = this.stream.getAudioTracks()[0];
    if (track) {
      track.enabled = false;
    }
    this.idleExpired = true;
    this.reconnectRequired = true;
  }

  requireReconnect() {
    this.listeningRequested = false;
    const track = this.stream.getAudioTracks()[0];
    if (track) {
      track.enabled = false;
    }
    this.reconnectRequired = true;
  }
}

export function reconcileListeningState({ controller, state, listenStart, micState }) {
  if (!controller) {
    return;
  }
  const track = controller.stream.getAudioTracks()[0];
  if (state === "listening" && controller.listeningRequested) {
    if (track) {
      track.enabled = true;
    }
    listenStart.classList.add("is-active");
    listenStart.setAttribute("aria-pressed", "true");
    micState.textContent = "MIC ON";
    micState.dataset.status = "ok";
    return;
  }
  if (track) {
    track.enabled = false;
  }
  listenStart.classList.remove("is-active");
  listenStart.setAttribute("aria-pressed", "false");
  micState.textContent = "MIC OFF";
  micState.dataset.status = "muted";
}

export class TranscriptView {
  constructor(container) {
    this.container = container;
    this.active = new Map();
  }

  update(role, text, done) {
    const document = this.container.ownerDocument;
    this.container.querySelector(".transcript-empty")?.remove();
    let entry = this.active.get(role);
    if (!entry) {
      const wrapper = document.createElement("article");
      const label = document.createElement("span");
      const content = document.createElement("p");
      wrapper.className = "utterance";
      label.className = "utterance-role";
      content.className = "utterance-text";
      label.textContent = role === "user" ? "YOU" : "MOCO";
      wrapper.append(label, content);
      this.container.append(wrapper);
      entry = content;
      this.active.set(role, entry);
    }
    entry.textContent = text;
    if (done) {
      this.active.delete(role);
    }
    this.container.scrollTop = this.container.scrollHeight;
  }

  clear() {
    const document = this.container.ownerDocument;
    this.active.clear();
    this.container.replaceChildren();
    const empty = document.createElement("p");
    empty.className = "transcript-empty";
    empty.textContent = "会話を消去しました。次の発話を待っています。";
    this.container.append(empty);
  }
}

function namedError(code, { displayed = false } = {}) {
  const error = new Error(code);
  error.name = code;
  error.displayed = displayed;
  return error;
}

export class ConversationHandshake {
  constructor(applyAnswer) {
    this.applyAnswer = applyAnswer;
    this.settled = false;
    this.promise = new Promise((resolve, reject) => {
      this.resolve = resolve;
      this.reject = reject;
    });
  }

  async consume(message) {
    if (this.settled) {
      return false;
    }
    if (message.type === "sdp_answer") {
      try {
        await this.applyAnswer(message.sdp);
      } catch {
        this.#fail("webrtc_connection_failed");
        return true;
      }
      this.settled = true;
      this.resolve();
      return true;
    }
    if (message.type === "error" && CONVERSATION_START_ERRORS.has(message.code)) {
      this.#fail(message.code);
      return true;
    }
    return false;
  }

  cancel(code, { displayed = false } = {}) {
    this.#fail(code, displayed);
  }

  #fail(code, displayed = false) {
    if (this.settled) {
      return;
    }
    this.settled = true;
    this.reject(namedError(code, { displayed }));
  }
}

export function waitForIce(peer, { timeoutMs = ICE_GATHERING_TIMEOUT_MS } = {}) {
  if (peer.iceGatheringState === "complete") {
    return Promise.resolve();
  }
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      clearTimeout(timer);
      peer.removeEventListener("icegatheringstatechange", listener);
    };
    const listener = () => {
      if (peer.iceGatheringState === "complete") {
        cleanup();
        resolve();
      }
    };
    const timer = setTimeout(() => {
      cleanup();
      reject(namedError("ice_gathering_timeout"));
    }, timeoutMs);
    peer.addEventListener("icegatheringstatechange", listener);
  });
}

export function waitForSocketOpen(socket, { timeoutMs = WEBSOCKET_OPEN_TIMEOUT_MS } = {}) {
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      clearTimeout(timer);
      socket.removeEventListener("open", opened);
      socket.removeEventListener("error", failed);
      socket.removeEventListener("close", closed);
    };
    const opened = () => {
      cleanup();
      resolve();
    };
    const fail = (code) => {
      cleanup();
      reject(namedError(code));
    };
    const failed = () => fail("websocket_failed");
    const closed = () => fail("websocket_closed_before_open");
    const timer = setTimeout(() => fail("websocket_open_timeout"), timeoutMs);
    socket.addEventListener("open", opened, { once: true });
    socket.addEventListener("error", failed, { once: true });
    socket.addEventListener("close", closed, { once: true });
  });
}

export function watchPeerFailure(peer, onFailure) {
  const listener = () => {
    if (peer.connectionState === "failed") {
      onFailure("webrtc_connection_failed");
    }
  };
  peer.addEventListener("connectionstatechange", listener);
  return () => peer.removeEventListener("connectionstatechange", listener);
}

export function closeCurrentPeer(failedPeer, currentPeer, { stopWatching, requireReconnect }) {
  if (failedPeer !== currentPeer) {
    return false;
  }
  stopWatching?.();
  failedPeer.close();
  requireReconnect();
  return true;
}

export function closeFailedHandshakePeer(
  failedPeer,
  currentPeer,
  { replacement, startSent, stopWatching, requireReconnect, send, claimVoiceLoss },
) {
  const closed = closeCurrentPeer(failedPeer, currentPeer, {
    stopWatching,
    requireReconnect,
  });
  if (closed) {
    if (claimVoiceLoss) {
      claimVoiceLoss();
    } else if (replacement && startSent) {
      send({ type: "voice_lost" });
    }
  }
  return closed;
}

export function closeSocketForFailure(
  socket,
  error,
  onFailure,
  { preserveTransport = false } = {},
) {
  if (!socket || preserveTransport) {
    return false;
  }
  const code = error?.name && error.name !== "Error" ? error.name : "conversation_start_failed";
  onFailure({ code, displayed: error?.displayed === true });
  socket.close();
  return true;
}

export function connectionCloseErrorCode(failure, wasOnline) {
  if (failure) {
    return failure.displayed ? null : failure.code;
  }
  return wasOnline ? "websocket_disconnected" : null;
}

export function beginAudioActivation(AudioContextConstructor = globalThis.AudioContext) {
  const context = new AudioContextConstructor({ sampleRate: 48_000 });
  return { context, ready: context.resume() };
}

export function connectionSetupErrorCode(stage, error) {
  if (stage === "audio") {
    return "audio_resume_failed";
  }
  if (stage === "microphone" && error?.name === "NotAllowedError") {
    return "microphone_permission_denied";
  }
  if (stage === "microphone" && error?.name === "NotFoundError") {
    return "microphone_unavailable";
  }
  if (stage === "microphone") {
    return "microphone_failed";
  }
  return error?.name || "enable_failed";
}

export async function closeAudioContext(context) {
  if (context && context.state !== "closed") {
    await context.close();
  }
}

export async function closeDisconnectedMedia({ context, controller, controls, peer, stream }) {
  for (const control of controls) {
    control.disabled = true;
  }
  controller?.disconnect();
  peer?.close();
  for (const track of stream?.getTracks() ?? []) {
    track.stop();
  }
  await closeAudioContext(context);
}

export function resetConnectionAttempt(progressTimer, clearTimer = clearInterval) {
  if (progressTimer !== undefined) {
    clearTimer(progressTimer);
  }
  return { openPromise: null, progressTimer: undefined };
}

export function setConnectionAction({ row, button }, state) {
  const connected = state === "connected";
  row.hidden = connected;
  if (!connected) {
    button.disabled = false;
    button.textContent = state === "disconnected" ? "再接続" : "接続";
  }
}

export function setTransportOffline({ connection, micState }) {
  connection.textContent = "WS OFFLINE";
  connection.dataset.status = "error";
  micState.textContent = "MIC OFF";
  micState.dataset.status = "muted";
}

function isLoopbackHostname(hostname) {
  const normalized = hostname.toLowerCase();
  if (normalized === "localhost" || normalized === "::1" || normalized === "[::1]") {
    return true;
  }
  const octets = normalized.split(".");
  return (
    octets.length === 4 &&
    octets.every((octet) => /^(0|[1-9]\d{0,2})$/.test(octet) && Number(octet) <= 255) &&
    Number(octets[0]) === 127
  );
}

export function loadCapability({ location, history, storage }) {
  const capabilityFromUrl = location.hash.slice(1);
  if (capabilityFromUrl) {
    storage.setItem(CAPABILITY_STORAGE_KEY, capabilityFromUrl);
    history.replaceState(null, "", location.pathname);
    return capabilityFromUrl;
  }
  return storage.getItem(CAPABILITY_STORAGE_KEY) ?? "";
}

export class PairingPanel {
  constructor({
    capability,
    dom,
    fetch,
    location,
    createObjectURL,
    revokeObjectURL,
    onError = () => {},
  }) {
    this.capability = capability;
    this.dom = dom;
    this.fetch = fetch;
    this.location = location;
    this.createObjectURL = createObjectURL;
    this.revokeObjectURL = revokeObjectURL;
    this.onError = onError;
    this.objectURL = undefined;
  }

  get options() {
    return {
      cache: "no-store",
      headers: { "X-Moco-Capability": this.capability },
    };
  }

  async probe() {
    if (!isLoopbackHostname(this.location.hostname)) {
      return;
    }
    try {
      const response = await this.fetch("/pairing.svg", {
        ...this.options,
        method: "HEAD",
      });
      this.dom.open.hidden = !response.ok;
    } catch {
      this.dom.open.hidden = true;
      this.onError("pairing_failed");
    }
  }

  async open() {
    try {
      const response = await this.fetch("/pairing.svg", this.options);
      if (!response.ok) {
        throw new Error("pairing_failed");
      }
      this.#discardObjectURL();
      this.objectURL = this.createObjectURL(await response.blob());
      this.dom.image.src = this.objectURL;
      this.dom.panel.hidden = false;
      this.dom.close.focus();
    } catch {
      this.onError("pairing_failed");
    }
  }

  close() {
    this.dom.panel.hidden = true;
    this.dom.image.removeAttribute("src");
    this.#discardObjectURL();
    this.dom.open.focus();
  }

  #discardObjectURL() {
    if (this.objectURL !== undefined) {
      this.revokeObjectURL(this.objectURL);
    }
    this.objectURL = undefined;
  }
}

export function renderPresetChoices(
  container,
  createElement = (tag) => document.createElement(tag),
) {
  let currentGroup;
  let group;
  for (const option of PRESET_OPTIONS) {
    if (option.group !== currentGroup) {
      currentGroup = option.group;
      group = createElement("div");
      group.className = "theme-preset-group";
      const title = createElement("span");
      title.className = "theme-preset-group-title";
      title.textContent = THEME_GROUP_LABELS[option.group];
      group.append(title);
      container.append(group);
    }
    const label = createElement("label");
    label.className = "theme-preset-choice";
    const input = createElement("input");
    input.type = "radio";
    input.name = "theme-preset";
    input.value = option.id;
    const swatch = createElement("span");
    swatch.className = "theme-preset-swatch";
    swatch.setAttribute("aria-hidden", "true");
    for (const color of option.preview) {
      const sample = createElement("span");
      sample.style.backgroundColor = color;
      swatch.append(sample);
    }
    const name = createElement("span");
    name.textContent = option.label;
    label.append(input, swatch, name);
    group.append(label);
  }
}

export class ThemePanel {
  constructor({ controller, dom }) {
    this.controller = controller;
    this.dom = dom;
    this.inputs = new Map();
    renderPresetChoices(this.dom.themePresets);
    this.#createColorRows();
    this.#bind();
  }

  render() {
    const palette = this.controller.apply();
    const selected = this.dom.themePresets.querySelector(
      `input[value="${this.controller.theme.preset}"]`,
    );
    if (selected) {
      selected.checked = true;
    }
    for (const [token, controls] of this.inputs) {
      controls.color.value = palette[token];
      controls.hex.value = palette[token];
      controls.reset.disabled = !(token in this.controller.theme.overrides);
    }
    const warnings = this.controller.contrastWarnings();
    this.dom.themeValidation.dataset.status = warnings.length === 0 ? "ok" : "warning";
    this.dom.themeValidation.textContent =
      warnings.length === 0
        ? "コントラスト基準を満たしています"
        : warnings
            .map(
              ({ foreground, background, minimum, ratio }) =>
                `${THEME_LABELS[foreground]} / ${THEME_LABELS[background]}: ${ratio.toFixed(2)}（基準 ${minimum}）`,
            )
            .join(" · ");
  }

  open() {
    this.dom.themePanel.hidden = false;
    this.dom.themeToggle.setAttribute("aria-expanded", "true");
    this.dom.themeClose.focus();
  }

  close() {
    this.dom.themePanel.hidden = true;
    this.dom.themeToggle.setAttribute("aria-expanded", "false");
    this.dom.themeToggle.focus();
  }

  #createColorRows() {
    for (const token of EDITABLE_TOKENS) {
      const row = document.createElement("div");
      const label = document.createElement("label");
      const color = document.createElement("input");
      const hex = document.createElement("input");
      const reset = document.createElement("button");
      const inputId = `theme-${token}`;
      row.className = "theme-color-row";
      label.htmlFor = inputId;
      label.textContent = THEME_LABELS[token];
      color.id = inputId;
      color.type = "color";
      color.setAttribute("aria-label", `${THEME_LABELS[token]}のカラーピッカー`);
      hex.type = "text";
      hex.inputMode = "text";
      hex.maxLength = 7;
      hex.setAttribute("aria-label", `${THEME_LABELS[token]}の16進カラー`);
      reset.type = "button";
      reset.textContent = "戻す";
      reset.addEventListener("click", () => {
        this.controller.resetOverride(token);
        this.render();
      });
      const update = (value) => {
        try {
          this.controller.setOverride(token, value);
          this.render();
        } catch {
          this.dom.themeValidation.dataset.status = "warning";
          this.dom.themeValidation.textContent = "色は #rrggbb 形式で入力してください";
        }
      };
      color.addEventListener("input", () => update(color.value));
      hex.addEventListener("input", () => update(hex.value));
      row.append(label, color, hex, reset);
      this.dom.themeColors.append(row);
      this.inputs.set(token, { color, hex, reset });
    }
  }

  #bind() {
    this.dom.themeToggle.addEventListener("click", () => this.open());
    this.dom.themeClose.addEventListener("click", () => this.close());
    this.dom.themePanel.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        this.close();
      }
    });
    this.dom.themePresets.addEventListener("change", (event) => {
      if (event.target instanceof HTMLInputElement && PRESETS.includes(event.target.value)) {
        this.controller.selectPreset(event.target.value);
        this.render();
      }
    });
    this.dom.themeReset.addEventListener("click", () => {
      this.controller.resetOverrides();
      this.render();
    });
  }
}

function boot() {
  const dom = {
    enable: document.querySelector("#enable"),
    connectionRow: document.querySelector("#connection-row"),
    state: document.querySelector("#state"),
    connection: document.querySelector("#connection"),
    micState: document.querySelector("#mic-state"),
    listenStart: document.querySelector("#listen-start"),
    startKey: document.querySelector("#start-key"),
    listenStop: document.querySelector("#listen-stop"),
    turnCancel: document.querySelector("#turn-cancel"),
    stopKey: document.querySelector("#stop-key"),
    voice: document.querySelector("#voice"),
    error: document.querySelector("#error"),
    errorText: document.querySelector("#error-text"),
    errorClose: document.querySelector("#error-close"),
    transcript: document.querySelector("#transcript"),
    clear: document.querySelector("#clear"),
    activity: document.querySelector("#activity"),
    activityLatest: document.querySelector("#activity-latest"),
    progressLabel: document.querySelector("#progress-label"),
    progressElapsed: document.querySelector("#progress-elapsed"),
    progressUpdated: document.querySelector("#progress-updated"),
    themeToggle: document.querySelector("#theme-toggle"),
    themePanel: document.querySelector("#theme-panel"),
    themeClose: document.querySelector("#theme-close"),
    themePresets: document.querySelector("#theme-presets"),
    themeColors: document.querySelector("#theme-colors"),
    themeValidation: document.querySelector("#theme-validation"),
    themeReset: document.querySelector("#theme-reset"),
    pairingOpen: document.querySelector("#pairing-open"),
    pairingPanel: document.querySelector("#pairing-panel"),
    pairingClose: document.querySelector("#pairing-close"),
    pairingImage: document.querySelector("#pairing-image"),
  };
  const transcript = new TranscriptView(dom.transcript);
  const activityBuffer = new ActivityBuffer();
  const progress = new ProgressTracker();
  const operatorStatus = new OperatorStatus({
    activityBuffer,
    activityView: new ActivityView({
      container: dom.activity,
      latestButton: dom.activityLatest,
    }),
    error: dom.error,
    errorText: dom.errorText,
    progress,
    progressView: new ProgressView({
      label: dom.progressLabel,
      elapsed: dom.progressElapsed,
      updated: dom.progressUpdated,
    }),
  });
  const themeController = new ThemeController({
    root: document.documentElement,
    storage: window.localStorage,
    onWarning: (code) => operatorStatus.showError(code),
  });
  themeController.load();
  const themePanel = new ThemePanel({ controller: themeController, dom });
  themePanel.render();
  watchSystemTheme(themeController, window.matchMedia("(prefers-color-scheme: dark)"), () =>
    themePanel.render(),
  );
  operatorStatus.renderProgress();
  const capability = loadCapability({
    location: window.location,
    history: window.history,
    storage: window.sessionStorage,
  });
  const pairingPanel = new PairingPanel({
    capability,
    dom: {
      open: dom.pairingOpen,
      panel: dom.pairingPanel,
      close: dom.pairingClose,
      image: dom.pairingImage,
    },
    fetch: window.fetch.bind(window),
    location: window.location,
    createObjectURL: (blob) => window.URL.createObjectURL(blob),
    revokeObjectURL: (url) => window.URL.revokeObjectURL(url),
    onError: (code) => operatorStatus.showError(code),
  });
  void pairingPanel.probe();

  let socket;
  let peer;
  let stream;
  let context;
  let controller;
  let openPromise;
  let progressTimer;
  let conversationHandshake;
  let discardFailedHandshakeTerminal = false;
  let stopPeerWatch;
  let socketCloseError;
  const hotkeyMapper = new BrowserHotkeyMapper({
    globalHotkeysEnabled: true,
    startKey: "",
    stopKey: "",
  });

  const send = (message) => {
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(message));
    }
  };
  const voiceModels = new VoiceModelController({
    select: dom.voice,
    send,
  });
  const turnCancel = new TurnCancelController({ button: dom.turnCancel, send });

  const connectSocket = () => {
    if (openPromise) {
      return openPromise;
    }
    const url = new URL("/ws", window.location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    const nextSocket = new WebSocket(url, [
      WEBSOCKET_PROTOCOL,
      `${CAPABILITY_PREFIX}${capability}`,
    ]);
    socket = nextSocket;
    nextSocket.binaryType = "arraybuffer";
    let wasOnline = false;
    nextSocket.addEventListener("close", () => {
      if (socket !== nextSocket) {
        return;
      }
      const disconnectError = socketCloseError;
      const disconnectCode = disconnectError?.code;
      socketCloseError = undefined;
      setTransportOffline(dom);
      clearInterval(progressTimer);
      progressTimer = undefined;
      openPromise = null;
      socket = undefined;
      stopPeerWatch?.();
      stopPeerWatch = undefined;
      conversationHandshake?.cancel(disconnectCode ?? "websocket_disconnected", {
        displayed: true,
      });
      conversationHandshake = undefined;
      discardFailedHandshakeTerminal = false;
      const disconnectedMedia = {
        context,
        controller,
        controls: [dom.listenStart, dom.listenStop, dom.turnCancel, dom.voice],
        peer,
        stream,
      };
      context = undefined;
      controller = undefined;
      peer = undefined;
      stream = undefined;
      void closeDisconnectedMedia(disconnectedMedia);
      setConnectionAction({ row: dom.connectionRow, button: dom.enable }, "disconnected");
      operatorStatus.disconnect();
      const closeErrorCode = connectionCloseErrorCode(disconnectError, wasOnline);
      if (closeErrorCode) {
        operatorStatus.showError(closeErrorCode);
      }
    });
    nextSocket.addEventListener("message", async (event) => {
      if (typeof event.data !== "string") {
        controller?.consumeAudio(event.data);
        return;
      }
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        operatorStatus.showError("invalid_message");
        return;
      }
      if (
        discardFailedHandshakeTerminal &&
        (message.type === "sdp_answer" ||
          (message.type === "error" && CONVERSATION_START_ERRORS.has(message.code)))
      ) {
        discardFailedHandshakeTerminal = false;
        return;
      }
      if (conversationHandshake && (await conversationHandshake.consume(message))) {
        return;
      }
      if (message.type === "state") {
        dom.state.textContent = message.state.toUpperCase();
        dom.state.dataset.status = message.state === "ready" ? "ok" : "info";
        if (controller) {
          controller.idleExpired = message.state === "idle_expired";
          if (message.state === "voice_reconnect_required" || message.state === "connection_lost") {
            controller.requireReconnect();
          }
        }
        if (message.state === "voice_reconnect_required") {
          progress.consume({ kind: "turn", source: "voice", phase: "completed" });
          operatorStatus.renderProgress();
        } else if (message.state === "connection_lost") {
          operatorStatus.disconnect();
        }
        reconcileListeningState({
          controller,
          state: message.state,
          listenStart: dom.listenStart,
          micState: dom.micState,
        });
        if (message.state === "idle_expired") {
          controller?.expire();
          dom.micState.textContent = "MIC OFF";
          dom.micState.dataset.status = "muted";
          dom.listenStart.classList.remove("is-active");
          dom.listenStart.setAttribute("aria-pressed", "false");
          operatorStatus.expire();
        } else if (message.state === "ready" && !progress.snapshot().active) {
          progress.ready();
          operatorStatus.renderProgress();
        }
        const startKey = message.hotkeys.startListening.toLowerCase();
        const stopKey = message.hotkeys.stopListening.toLowerCase();
        hotkeyMapper.configure({
          globalHotkeysEnabled: message.hotkeys.enabled,
          startKey,
          stopKey,
        });
        dom.startKey.textContent = startKey.toUpperCase();
        dom.stopKey.textContent = stopKey.toUpperCase();
        voiceModels.configure(message.voice);
        turnCancel.configure(message.canCancel);
      } else if (message.type === "control") {
        await apply(message.control);
      } else if (message.type === "audio") {
        controller?.acceptAudio(message);
      } else if (message.type === "audio_invalidate") {
        controller?.invalidateAudio(message.generation);
      } else if (message.type === "transcript") {
        transcript.update(message.role, message.text, message.done);
      } else if (message.type === "voice") {
        voiceModels.confirm(message.selected);
        operatorStatus.addLocal({
          kind: "settings",
          phase: "completed",
          label: `音声モデル: ${message.selected || "既定"}`,
        });
      } else if (
        message.type === "activity" ||
        message.type === "reasoning_summary" ||
        message.type === "error"
      ) {
        operatorStatus.consume(message);
      }
    });
    openPromise = waitForSocketOpen(nextSocket)
      .then(() => {
        wasOnline = true;
        dom.connection.textContent = "WS ONLINE";
        dom.connection.dataset.status = "ok";
        operatorStatus.addLocal({
          kind: "connection",
          phase: "completed",
          label: "オペレーター接続",
        });
        clearInterval(progressTimer);
        progressTimer = window.setInterval(() => operatorStatus.renderProgress(), 1_000);
      })
      .catch((error) => {
        openPromise = null;
        if (socket === nextSocket) {
          socket = undefined;
        }
        nextSocket.close();
        throw error;
      });
    return openPromise;
  };

  const connectConversation = async () => {
    const replacement = controller?.reconnectRequired === true;
    let startSent = false;
    let established = false;
    let voiceLossClaimed = false;
    const claimVoiceLoss = () => {
      if (voiceLossClaimed || (!established && !(replacement && startSent))) {
        return;
      }
      voiceLossClaimed = true;
      send({ type: "voice_lost" });
    };
    await connectSocket();
    stopPeerWatch?.();
    peer?.close();
    const nextPeer = new RTCPeerConnection();
    peer = nextPeer;
    let handshake;
    stopPeerWatch = watchPeerFailure(nextPeer, (code) => {
      if (
        !closeCurrentPeer(nextPeer, peer, {
          stopWatching: stopPeerWatch,
          requireReconnect: () => controller?.requireReconnect(),
        })
      ) {
        return;
      }
      stopPeerWatch = undefined;
      peer = undefined;
      claimVoiceLoss();
      if (handshake && conversationHandshake === handshake) {
        conversationHandshake = undefined;
        discardFailedHandshakeTerminal = true;
        handshake.cancel(code, { displayed: true });
      }
      operatorStatus.showError(code);
    });
    try {
      nextPeer.addTrack(stream.getAudioTracks()[0], stream);
      nextPeer.createDataChannel("oai-events");
      const offer = await nextPeer.createOffer();
      await nextPeer.setLocalDescription(offer);
      await waitForIce(nextPeer);
      handshake = new ConversationHandshake((sdp) =>
        nextPeer.setRemoteDescription({ type: "answer", sdp }),
      );
      conversationHandshake = handshake;
      send({ type: "start", sdp: nextPeer.localDescription.sdp });
      startSent = true;
      await handshake.promise;
      established = true;
    } catch (error) {
      if (
        closeFailedHandshakePeer(nextPeer, peer, {
          replacement,
          startSent,
          stopWatching: stopPeerWatch,
          requireReconnect: () => controller?.requireReconnect(),
          send,
          claimVoiceLoss,
        })
      ) {
        stopPeerWatch = undefined;
        peer = undefined;
      }
      throw error;
    } finally {
      if (handshake && conversationHandshake === handshake) {
        conversationHandshake = undefined;
      }
    }
  };

  dom.enable.addEventListener("click", async () => {
    dom.enable.disabled = true;
    let stage = "audio";
    try {
      const activation = beginAudioActivation();
      context = activation.context;
      await activation.ready;
      stage = "microphone";
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getAudioTracks()[0].enabled = false;
      stage = "conversation";
      let playbackActive = false;
      const playback = new AudioPlaybackQueue(
        context,
        (active, metadata, contextState, phase) => {
          if (metadata) {
            send({
              type: "playback",
              phase,
              audio_id: metadata.audioId,
              generation: metadata.generation,
              context_state: contextState,
            });
          }
          if (playbackActive !== active) {
            playbackActive = active;
            operatorStatus.addLocal({
              kind: "playback",
              phase: active ? "started" : "completed",
              label: "音声再生",
            });
          }
        },
        (code) => operatorStatus.showError(code),
      );
      controller = new MocoController({
        stream,
        playback,
        send,
        reconnect: connectConversation,
      });
      await connectConversation();
      dom.listenStart.disabled = false;
      dom.listenStop.disabled = false;
      setConnectionAction({ row: dom.connectionRow, button: dom.enable }, "connected");
    } catch (error) {
      stopPeerWatch?.();
      stopPeerWatch = undefined;
      peer?.close();
      ({ openPromise, progressTimer } = resetConnectionAttempt(progressTimer));
      const failedSocket = socket;
      socket = undefined;
      failedSocket?.close();
      setTransportOffline(dom);
      for (const track of stream?.getTracks() ?? []) {
        track.stop();
      }
      await closeAudioContext(context);
      peer = undefined;
      socket = undefined;
      stream = undefined;
      context = undefined;
      controller = undefined;
      if (!error.displayed) {
        operatorStatus.showError(connectionSetupErrorCode(stage, error));
      }
      setConnectionAction({ row: dom.connectionRow, button: dom.enable }, "disconnected");
    }
  });

  const apply = async (control) => {
    if (!controller) {
      return;
    }
    const listening = control === "listen_start";
    let applied;
    try {
      applied = await controller.applyControl(control);
    } catch (error) {
      const closing = closeSocketForFailure(
        socket,
        error,
        (failure) => {
          socketCloseError = failure;
        },
        { preserveTransport: controller.reconnectRequired },
      );
      if (!closing && !error.displayed) {
        operatorStatus.showError(error.name || "conversation_start_failed");
      }
      return;
    }
    if (!applied) {
      return;
    }
    dom.listenStart.classList.toggle("is-active", listening);
    dom.listenStart.setAttribute("aria-pressed", String(listening));
    dom.micState.textContent = listening ? "MIC ON" : "MIC OFF";
    dom.micState.dataset.status = listening ? "ok" : "muted";
    operatorStatus.addLocal({
      kind: "microphone",
      phase: listening ? "started" : "completed",
      label: listening ? "マイク入力" : "マイク入力を停止",
    });
  };

  window.addEventListener("keydown", (event) => {
    if (shouldHandleHotkey(event, hotkeyMapper)) {
      event.preventDefault();
      const control = hotkeyMapper.keyDown(event.key);
      if (control !== null) {
        void apply(control);
      }
    }
  });
  window.addEventListener("keyup", (event) => {
    if (shouldHandleHotkey(event, hotkeyMapper)) {
      event.preventDefault();
      const control = hotkeyMapper.keyUp(event.key);
      if (control !== null) {
        void apply(control);
      }
    }
  });
  dom.listenStart.addEventListener("click", () => void apply("listen_start"));
  dom.listenStop.addEventListener("click", () => void apply("listen_stop"));
  dom.voice.addEventListener("change", () => {
    voiceModels.select(dom.voice.value);
  });
  dom.clear.addEventListener("click", () => transcript.clear());
  dom.errorClose.addEventListener("click", () => operatorStatus.dismissError());
  dom.pairingOpen.addEventListener("click", () => void pairingPanel.open());
  dom.pairingClose.addEventListener("click", () => pairingPanel.close());
  dom.pairingPanel.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      pairingPanel.close();
    }
  });
}

if (typeof window !== "undefined" && typeof document !== "undefined") {
  boot();
}
