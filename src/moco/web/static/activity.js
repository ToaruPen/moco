const DEFAULT_ACTIVITY_LIMIT = 200;
const DEFAULT_SUMMARY_LIMIT = 500;
const CLOCK_FORMATTER = new Intl.DateTimeFormat("ja-JP", {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});
const ACTIVITY_KIND_LABELS = Object.freeze({
  connection: "WS",
  error: "ERROR",
  microphone: "MIC",
  playback: "VOICE",
  reasoning: "REASON",
  settings: "SETTING",
  turn: "TURN",
  voice: "VOICE",
  work: "WORK",
});
const ACTIVITY_PHASE_LABELS = Object.freeze({
  completed: "完了",
  started: "開始",
  updated: "更新",
});

function formatClock(occurredAtMs) {
  return CLOCK_FORMATTER.format(new Date(occurredAtMs));
}

function formatDuration(milliseconds) {
  const totalSeconds = Math.floor(milliseconds / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

export class ActivityBuffer {
  constructor({ limit = DEFAULT_ACTIVITY_LIMIT, summaryLimit = DEFAULT_SUMMARY_LIMIT } = {}) {
    if (!Number.isInteger(limit) || limit <= 0) {
      throw new TypeError("activity limit must be a positive integer");
    }
    if (!Number.isInteger(summaryLimit) || summaryLimit <= 0) {
      throw new TypeError("summary limit must be a positive integer");
    }
    this.limit = limit;
    this.summaryLimit = summaryLimit;
    this.items = [];
    this.summaries = new Map();
  }

  add(event) {
    const item = Object.freeze({ ...event });
    this.items.push(item);
    this.#trim();
    return item;
  }

  addSummary(message) {
    const current = this.summaries.get(message.itemId) ?? "";
    const label = `${current}${message.delta}`.slice(0, this.summaryLimit);
    this.summaries.set(message.itemId, label);
    const existing = this.items.findIndex((item) => item.itemId === message.itemId);
    const item = Object.freeze({
      kind: "reasoning",
      phase: "updated",
      label,
      occurredAtMs: message.occurredAtMs,
      itemId: message.itemId,
    });
    if (existing === -1) {
      this.items.push(item);
    } else {
      this.items[existing] = item;
    }
    this.#trim();
    return item;
  }

  clear() {
    this.items.length = 0;
    this.summaries.clear();
  }

  #trim() {
    if (this.items.length > this.limit) {
      this.items.splice(0, this.items.length - this.limit);
    }
    const retained = new Set(this.items.map((item) => item.itemId).filter(Boolean));
    for (const itemId of this.summaries.keys()) {
      if (!retained.has(itemId)) {
        this.summaries.delete(itemId);
      }
    }
  }
}

export class ProgressTracker {
  constructor({ now = () => Date.now() } = {}) {
    this.now = now;
    this.activeTurnSources = new Set();
    this.unscopedActiveTurns = 0;
    this.turnActive = false;
    this.voiceActive = false;
    this.playbackActive = false;
    this.label = "接続を待っています";
    this.startedAtMs = null;
    this.updatedAtMs = null;
  }

  consume(event) {
    const occurredAtMs = event.occurredAtMs ?? this.now();
    if (event.kind === "turn") {
      if (event.source === "agent" || event.source === "voice") {
        if (event.phase === "started") {
          this.activeTurnSources.add(event.source);
        } else {
          this.activeTurnSources.delete(event.source);
        }
      } else if (event.phase === "started") {
        this.unscopedActiveTurns += 1;
      } else {
        this.unscopedActiveTurns = Math.max(0, this.unscopedActiveTurns - 1);
      }
      this.turnActive = this.activeTurnSources.size > 0 || this.unscopedActiveTurns > 0;
      if (this.turnActive) {
        this.startedAtMs ??= occurredAtMs;
        this.label = "Codex が処理を続けています";
      } else if (this.playbackActive) {
        this.label = "音声を再生しています";
      } else if (this.voiceActive) {
        this.label = "音声を生成しています";
      } else {
        this.#setWaiting();
      }
    } else if (event.kind === "voice") {
      this.voiceActive = event.phase === "started";
      if (this.voiceActive) {
        this.startedAtMs ??= occurredAtMs;
        this.label = "音声を生成しています";
      } else if (this.playbackActive) {
        this.label = "音声を再生しています";
      } else if (this.turnActive) {
        this.label = "Codex が処理を続けています";
      } else {
        this.#setWaiting();
      }
    } else if (event.kind === "playback") {
      this.playbackActive = event.phase === "started";
      if (this.playbackActive) {
        this.startedAtMs ??= occurredAtMs;
        this.label = "音声を再生しています";
      } else if (this.voiceActive) {
        this.label = "音声を生成しています";
      } else if (this.turnActive) {
        this.label = "Codex が処理を続けています";
      } else {
        this.#setWaiting();
      }
    } else if (this.turnActive) {
      this.label =
        event.phase === "completed"
          ? "Codex が処理を続けています"
          : event.kind === "reasoning"
            ? event.label
            : `${event.label}を実行しています`;
    }
    this.updatedAtMs = occurredAtMs;
  }

  snapshot() {
    const now = this.now();
    return {
      active: this.turnActive || this.voiceActive || this.playbackActive,
      label: this.label,
      elapsedMs: this.startedAtMs === null ? 0 : Math.max(0, now - this.startedAtMs),
      staleMs: this.updatedAtMs === null ? 0 : Math.max(0, now - this.updatedAtMs),
    };
  }

  disconnect(now = this.now()) {
    this.activeTurnSources.clear();
    this.unscopedActiveTurns = 0;
    this.turnActive = false;
    this.voiceActive = false;
    this.playbackActive = false;
    this.label = "接続が切断されました";
    this.startedAtMs = null;
    this.updatedAtMs = now;
  }

  expire(now = this.now()) {
    this.activeTurnSources.clear();
    this.unscopedActiveTurns = 0;
    this.turnActive = false;
    this.voiceActive = false;
    this.playbackActive = false;
    this.label = "会話が終了しました";
    this.startedAtMs = null;
    this.updatedAtMs = now;
  }

  ready(now = this.now()) {
    this.activeTurnSources.clear();
    this.unscopedActiveTurns = 0;
    this.turnActive = false;
    this.voiceActive = false;
    this.playbackActive = false;
    this.#setWaiting();
    this.updatedAtMs = now;
  }

  #setWaiting() {
    this.label = "発話を待っています";
    this.startedAtMs = null;
  }
}

export class ActivityView {
  constructor({
    container,
    latestButton,
    createElement = (tag) => document.createElement(tag),
    formatTime = formatClock,
  }) {
    this.container = container;
    this.latestButton = latestButton;
    this.createElement = createElement;
    this.formatTime = formatTime;
    this.autoFollow = true;
    this.items = [];
    this.rows = [];
    this.container.addEventListener("scroll", () => this.#handleScroll());
    this.latestButton.addEventListener("click", () => this.scrollToLatest());
  }

  render(items) {
    if (items.length === 0) {
      this.clear();
      return;
    }
    const reasoningUpdate = this.#reasoningUpdateIndex(items);
    if (reasoningUpdate !== -1) {
      const row = this.#createRow(items[reasoningUpdate]);
      this.rows[reasoningUpdate].replaceWith(row);
      this.rows[reasoningUpdate] = row;
    } else {
      this.rows = items.map((item) => this.#createRow(item));
      this.container.replaceChildren(...this.rows);
    }
    this.items = [...items];
    if (this.autoFollow) {
      this.scrollToLatest();
    }
  }

  clear() {
    const empty = this.createElement("p");
    empty.className = "activity-empty";
    empty.textContent = "接続後の処理状況がここに表示されます。";
    this.container.replaceChildren(empty);
    this.items = [];
    this.rows = [];
    this.autoFollow = true;
    this.latestButton.hidden = true;
  }

  scrollToLatest() {
    this.container.scrollTop = this.container.scrollHeight;
    this.autoFollow = true;
    this.latestButton.hidden = true;
  }

  #createRow(item) {
    const row = this.createElement("div");
    row.className = `activity-row activity-row--${item.kind}`;
    const cells = [
      ["activity-time", this.formatTime(item.occurredAtMs)],
      [
        "activity-kind",
        `${ACTIVITY_KIND_LABELS[item.kind] ?? "WORK"} · ${ACTIVITY_PHASE_LABELS[item.phase] ?? "更新"}`,
      ],
      ["activity-label", item.label],
    ];
    for (const [className, value] of cells) {
      const cell = this.createElement("span");
      cell.className = className;
      cell.textContent = value;
      row.append(cell);
    }
    return row;
  }

  #reasoningUpdateIndex(items) {
    if (items.length !== this.items.length) {
      return -1;
    }
    let changed = -1;
    for (let index = 0; index < items.length; index += 1) {
      if (items[index] === this.items[index]) {
        continue;
      }
      if (
        changed !== -1 ||
        items[index].kind !== "reasoning" ||
        items[index].itemId !== this.items[index]?.itemId
      ) {
        return -1;
      }
      changed = index;
    }
    return changed;
  }

  #handleScroll() {
    const remaining =
      this.container.scrollHeight - this.container.scrollTop - this.container.clientHeight;
    this.autoFollow = remaining <= 8;
    this.latestButton.hidden = this.autoFollow;
  }
}

export class ProgressView {
  constructor({ label, elapsed, updated }) {
    this.label = label;
    this.elapsed = elapsed;
    this.updated = updated;
  }

  render(snapshot) {
    this.label.textContent = snapshot.label;
    this.elapsed.textContent = formatDuration(snapshot.elapsedMs);
    this.updated.textContent = `最終更新 ${Math.floor(snapshot.staleMs / 1000)} 秒前`;
    this.elapsed.hidden = !snapshot.active;
    this.updated.hidden = !snapshot.active;
  }
}
