const WEBSOCKET_PROTOCOL = "moco";
const CAPABILITY_PREFIX = `${WEBSOCKET_PROTOCOL}.capability.`;

export class AudioPlaybackQueue {
  constructor(context, onState) {
    this.context = context;
    this.onState = onState;
    this.sources = new Set();
    this.epoch = 0;
    this.chain = Promise.resolve();
    this.nextStart = context.currentTime;
  }

  get isPlaying() {
    return this.sources.size > 0;
  }

  enqueue(bytes) {
    const epoch = this.epoch;
    this.chain = this.chain.then(async () => {
      const buffer = await this.context.decodeAudioData(bytes.slice(0));
      if (epoch !== this.epoch) {
        return;
      }
      const source = this.context.createBufferSource();
      source.buffer = buffer;
      source.connect(this.context.destination);
      const startAt = Math.max(this.context.currentTime + 0.02, this.nextStart);
      this.nextStart = startAt + buffer.duration;
      this.sources.add(source);
      this.onState(true);
      source.addEventListener(
        "ended",
        () => {
          this.sources.delete(source);
          if (!this.isPlaying) {
            this.nextStart = this.context.currentTime;
            this.onState(false);
          }
        },
        { once: true },
      );
      source.start(startAt);
    });
  }

  stop() {
    this.epoch += 1;
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
    this.onState(false);
  }
}

export class MocoController {
  constructor({ stream, playback, send, reconnect }) {
    this.stream = stream;
    this.playback = playback;
    this.send = send;
    this.reconnect = reconnect;
    this.audioGeneration = 0;
    this.idleExpired = false;
    this.pendingAudio = null;
  }

  async applyControl(control) {
    const track = this.stream.getAudioTracks()[0];
    if (!track) {
      return;
    }
    if (control === "ptt_down") {
      if (this.idleExpired) {
        await this.reconnect();
        this.idleExpired = false;
      }
      this.playback.stop();
      this.audioGeneration += 1;
      track.enabled = true;
    } else if (control === "ptt_up") {
      track.enabled = false;
    } else if (control === "cancel") {
      track.enabled = false;
      this.audioGeneration += 1;
      this.playback.stop();
    } else {
      return;
    }
    this.send({ type: "control", control });
  }

  acceptAudio(metadata) {
    this.pendingAudio = {
      accepted: metadata.generation === this.audioGeneration,
    };
  }

  consumeAudio(bytes) {
    const pending = this.pendingAudio;
    this.pendingAudio = null;
    if (pending?.accepted) {
      this.playback.enqueue(bytes);
    }
  }

  invalidateAudio(generation) {
    this.audioGeneration = generation;
    this.pendingAudio = null;
    this.playback.stop();
  }
}

class TranscriptView {
  constructor(container) {
    this.container = container;
    this.active = new Map();
  }

  append(role, delta, done) {
    this.container.querySelector(".transcript-empty")?.remove();
    let entry = this.active.get(role);
    if (!entry) {
      const wrapper = document.createElement("article");
      const label = document.createElement("span");
      const text = document.createElement("p");
      wrapper.className = "utterance";
      label.className = "utterance-role";
      text.className = "utterance-text";
      label.textContent = role === "user" ? "YOU" : "MOCO";
      wrapper.append(label, text);
      this.container.append(wrapper);
      entry = text;
      this.active.set(role, entry);
    }
    entry.append(document.createTextNode(delta));
    if (done) {
      this.active.delete(role);
    }
    this.container.scrollTop = this.container.scrollHeight;
  }

  clear() {
    this.active.clear();
    this.container.replaceChildren();
    const empty = document.createElement("p");
    empty.className = "transcript-empty";
    empty.textContent = "会話を消去しました。次の発話を待っています。";
    this.container.append(empty);
  }
}

function waitForIce(peer) {
  if (peer.iceGatheringState === "complete") {
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    const listener = () => {
      if (peer.iceGatheringState === "complete") {
        peer.removeEventListener("icegatheringstatechange", listener);
        resolve();
      }
    };
    peer.addEventListener("icegatheringstatechange", listener);
  });
}

function boot() {
  const dom = {
    enable: document.querySelector("#enable"),
    state: document.querySelector("#state"),
    connection: document.querySelector("#connection"),
    ptt: document.querySelector("#ptt"),
    pttKey: document.querySelector("#ptt-key"),
    cancel: document.querySelector("#cancel"),
    cancelKey: document.querySelector("#cancel-key"),
    error: document.querySelector("#error"),
    transcript: document.querySelector("#transcript"),
    clear: document.querySelector("#clear"),
  };
  const transcript = new TranscriptView(dom.transcript);
  const capability = window.location.hash.slice(1);
  if (capability) {
    window.history.replaceState(null, "", window.location.pathname);
  }

  let socket;
  let peer;
  let stream;
  let context;
  let controller;
  let openPromise;
  let pttKey = null;
  let cancelKey = null;

  const showError = (code) => {
    dom.error.hidden = false;
    dom.error.textContent = `ERROR / ${code}`;
  };

  const send = (message) => {
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(message));
    }
  };

  const connectSocket = () => {
    if (openPromise) {
      return openPromise;
    }
    openPromise = new Promise((resolve, reject) => {
      const url = new URL("/ws", window.location.href);
      url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(url, [WEBSOCKET_PROTOCOL, `${CAPABILITY_PREFIX}${capability}`]);
      socket.binaryType = "arraybuffer";
      socket.addEventListener(
        "open",
        () => {
          dom.connection.textContent = "SOCKET ONLINE";
          resolve();
        },
        { once: true },
      );
      socket.addEventListener("error", () => reject(new Error("websocket_failed")), {
        once: true,
      });
      socket.addEventListener("close", () => {
        dom.connection.textContent = "SOCKET OFFLINE";
        openPromise = null;
      });
      socket.addEventListener("message", async (event) => {
        if (typeof event.data !== "string") {
          controller?.consumeAudio(event.data);
          return;
        }
        const message = JSON.parse(event.data);
        if (message.type === "state") {
          dom.state.textContent = message.state.toUpperCase();
          controller.idleExpired = message.state === "idle_expired";
          pttKey = message.hotkeys.pushToTalk.toLowerCase();
          cancelKey = message.hotkeys.cancel.toLowerCase();
          dom.pttKey.textContent = pttKey.toUpperCase();
          dom.cancelKey.textContent = cancelKey.toUpperCase();
        } else if (message.type === "sdp_answer") {
          await peer.setRemoteDescription({ type: "answer", sdp: message.sdp });
        } else if (message.type === "control") {
          await controller.applyControl(message.control);
        } else if (message.type === "audio") {
          controller.acceptAudio(message);
        } else if (message.type === "audio_invalidate") {
          controller.invalidateAudio(message.generation);
        } else if (message.type === "transcript") {
          transcript.append(message.role, message.delta, message.done);
        } else if (message.type === "error") {
          showError(message.code);
        }
      });
    });
    return openPromise;
  };

  const connectConversation = async () => {
    await connectSocket();
    peer?.close();
    peer = new RTCPeerConnection();
    peer.addTrack(stream.getAudioTracks()[0], stream);
    peer.createDataChannel("oai-events");
    const offer = await peer.createOffer();
    await peer.setLocalDescription(offer);
    await waitForIce(peer);
    send({ type: "start", sdp: peer.localDescription.sdp });
  };

  dom.enable.addEventListener("click", async () => {
    dom.enable.disabled = true;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getAudioTracks()[0].enabled = false;
      context = new AudioContext();
      await context.resume();
      const playback = new AudioPlaybackQueue(context, (active) => {
        send({ type: "playback", active });
      });
      controller = new MocoController({
        stream,
        playback,
        send,
        reconnect: connectConversation,
      });
      await connectConversation();
      dom.ptt.disabled = false;
      dom.cancel.disabled = false;
      dom.enable.textContent = "音声卓は有効です";
    } catch (error) {
      showError(error.name || "enable_failed");
      dom.enable.disabled = false;
    }
  });

  const apply = async (control) => {
    if (!controller) {
      return;
    }
    dom.ptt.classList.toggle("is-active", control === "ptt_down");
    await controller.applyControl(control);
  };

  const pressed = new Set();
  window.addEventListener("keydown", (event) => {
    const key = event.key.toLowerCase();
    if (pressed.has(key)) {
      return;
    }
    if (key === pttKey || key === cancelKey) {
      event.preventDefault();
      pressed.add(key);
      void apply(key === pttKey ? "ptt_down" : "cancel");
    }
  });
  window.addEventListener("keyup", (event) => {
    const key = event.key.toLowerCase();
    pressed.delete(key);
    if (key === pttKey) {
      event.preventDefault();
      void apply("ptt_up");
    }
  });
  dom.ptt.addEventListener("pointerdown", () => void apply("ptt_down"));
  dom.ptt.addEventListener("pointerup", () => void apply("ptt_up"));
  dom.ptt.addEventListener("pointercancel", () => void apply("ptt_up"));
  dom.cancel.addEventListener("click", () => void apply("cancel"));
  dom.clear.addEventListener("click", () => transcript.clear());
}

if (typeof window !== "undefined" && typeof document !== "undefined") {
  boot();
}
