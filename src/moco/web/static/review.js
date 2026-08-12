const REVIEW_PROTOCOL = "moco-review";
const REVIEW_SOCKET_PATH = "/review/ws";

const DECISIONS = new Set(["accept", "decline", "cancel"]);
const REVIEW_CATEGORIES = new Set(["command_approval", "file_change_approval"]);
const STATUS_REVIEW_NEEDED = "ローカル承認が必要です";
const STATUS_DECISION_SENT = "ローカルレビューを送信しました";
const STATUS_COMPLETED = "ローカルレビューが完了しました";
const STATUS_CANCELLED = "ローカルレビューが取り消されました";

export class ReviewController {
  constructor({ document, root, status, send = () => {}, onInvalid = () => {} }) {
    this.document = document;
    this.root = root;
    this.status = status;
    this.send = send;
    this.onInvalid = onInvalid;
    this.reviews = new Map();
    this.outcomes = new Map();
    this.invalid = false;
  }

  receive(message) {
    if (this.invalid || !message || typeof message !== "object") {
      this.invalidate();
      return;
    }
    if (message.type === "ready") {
      if (Object.keys(message).length !== 1) {
        this.invalidate();
        return;
      }
      this.status.textContent = "ローカル承認を待っています";
      return;
    }
    if (message.type === "review") {
      this.#show(message);
      return;
    }
    if (message.type === "resolved" || message.type === "withdrawn") {
      if (
        Object.keys(message).some((key) => !new Set(["type", "reviewHandle"]).has(key)) ||
        typeof message.reviewHandle !== "string" ||
        !message.reviewHandle
      ) {
        this.invalidate();
        return;
      }
      if (!this.reviews.has(message.reviewHandle) && !this.outcomes.has(message.reviewHandle)) {
        this.invalidate();
        return;
      }
      const outcome = this.outcomes.get(message.reviewHandle);
      this.outcomes.delete(message.reviewHandle);
      this.#remove(message.reviewHandle);
      this.#setTerminalStatus(
        message.type === "withdrawn" || outcome === "cancel" ? STATUS_CANCELLED : STATUS_COMPLETED,
      );
      return;
    }
    this.invalidate();
  }

  clear() {
    for (const handle of this.reviews.keys()) {
      this.#remove(handle);
    }
    this.reviews.clear();
    this.outcomes.clear();
    if (!this.invalid) {
      this.status.textContent = "Reviewer 接続が終了しました";
    }
  }

  invalidate() {
    if (this.invalid) {
      return;
    }
    this.invalid = true;
    this.clear();
    this.status.textContent = "Reviewer メッセージを利用できません";
    this.onInvalid();
  }

  #show(message) {
    if (
      typeof message.reviewHandle !== "string" ||
      !message.reviewHandle ||
      typeof message.category !== "string" ||
      !REVIEW_CATEGORIES.has(message.category) ||
      !Array.isArray(message.decisions) ||
      message.decisions.length === 0 ||
      new Set(message.decisions).size !== message.decisions.length ||
      message.decisions.some((decision) => typeof decision !== "string" || !DECISIONS.has(decision))
    ) {
      this.invalidate();
      return;
    }
    const allowedKeys =
      message.category === "command_approval"
        ? new Set([
            "type",
            "reviewHandle",
            "category",
            "decisions",
            "commandText",
            "command",
            "cwd",
            "reason",
          ])
        : new Set(["type", "reviewHandle", "category", "decisions", "changes", "reason"]);
    if (Object.keys(message).some((key) => !allowedKeys.has(key))) {
      this.invalidate();
      return;
    }
    if (typeof message.reason !== "undefined" && typeof message.reason !== "string") {
      this.invalidate();
      return;
    }
    if (message.category === "command_approval") {
      const hasCommandText = Object.hasOwn(message, "commandText");
      const hasCommand = Object.hasOwn(message, "command");
      if (
        hasCommandText === hasCommand ||
        (hasCommandText && typeof message.commandText !== "string") ||
        (hasCommand &&
          (!Array.isArray(message.command) ||
            message.command.length === 0 ||
            message.command.some((argument) => typeof argument !== "string"))) ||
        typeof message.cwd !== "string" ||
        "changes" in message
      ) {
        this.invalidate();
        return;
      }
    } else if (
      !Array.isArray(message.changes) ||
      message.changes.length === 0 ||
      message.changes.some((change) => !isChange(change)) ||
      "command" in message ||
      "commandText" in message ||
      "cwd" in message
    ) {
      this.invalidate();
      return;
    }
    if (this.reviews.has(message.reviewHandle) || this.outcomes.has(message.reviewHandle)) {
      this.invalidate();
      return;
    }
    const card = this.document.createElement("article");
    card.className = "review-card";
    const title = this.document.createElement("h2");
    title.textContent = message.category;
    card.append(title);
    this.#appendDetails(card, message);
    const actions = this.document.createElement("div");
    actions.className = "review-actions";
    for (const decision of message.decisions) {
      const button = this.document.createElement("button");
      button.type = "button";
      button.textContent = decision;
      button.addEventListener("click", () => this.#decide(message.reviewHandle, decision));
      actions.append(button);
    }
    card.append(actions);
    this.root.append(card);
    this.reviews.set(message.reviewHandle, card);
    this.status.textContent = STATUS_REVIEW_NEEDED;
  }

  #appendDetails(card, message) {
    if (typeof message.commandText === "string") {
      const commandText = this.document.createElement("p");
      commandText.className = "review-command-text";
      commandText.textContent = `command: ${JSON.stringify(message.commandText)}`;
      card.append(commandText);
    }
    if (Array.isArray(message.command)) {
      const command = this.document.createElement("ol");
      command.className = "review-arguments";
      for (const [index, argument] of message.command.entries()) {
        const item = this.document.createElement("li");
        item.className = "review-argument";
        item.textContent = `argv[${index}]: ${JSON.stringify(argument)}`;
        command.append(item);
      }
      card.append(command);
    }
    if (typeof message.cwd === "string") {
      const cwd = this.document.createElement("p");
      cwd.className = "review-cwd";
      cwd.textContent = `cwd: ${JSON.stringify(message.cwd)}`;
      card.append(cwd);
    }
    if (Array.isArray(message.changes)) {
      const changes = this.document.createElement("ul");
      for (const change of message.changes) {
        const item = this.document.createElement("li");
        const kind = this.document.createElement("span");
        kind.textContent = `${change.kind}: `;
        const source = this.document.createElement("div");
        source.className = "review-change-source";
        source.textContent = `source: ${JSON.stringify(change.path)}`;
        item.append(kind, source);
        if (typeof change.destination === "string") {
          const destination = this.document.createElement("div");
          destination.className = "review-change-destination";
          destination.textContent = `destination: ${JSON.stringify(change.destination)}`;
          item.append(destination);
        }
        changes.append(item);
      }
      card.append(changes);
    }
    if (typeof message.reason === "string") {
      const reason = this.document.createElement("p");
      reason.textContent = message.reason;
      card.append(reason);
    }
  }

  #decide(handle, decision) {
    if (!this.reviews.has(handle)) {
      return;
    }
    this.#remove(handle);
    this.outcomes.set(handle, decision);
    this.#setTerminalStatus(STATUS_DECISION_SENT);
    const message = { reviewHandle: handle, decision };
    this.send(message);
  }

  #setTerminalStatus(status) {
    this.status.textContent = this.reviews.size > 0 ? STATUS_REVIEW_NEEDED : status;
  }

  #remove(handle) {
    if (typeof handle !== "string") {
      return;
    }
    const card = this.reviews.get(handle);
    if (!card) {
      return;
    }
    card.remove();
    this.reviews.delete(handle);
  }
}

function isChange(change) {
  if (
    !change ||
    typeof change !== "object" ||
    (change.kind !== "add" && change.kind !== "delete" && change.kind !== "update") ||
    typeof change.path !== "string"
  ) {
    return false;
  }
  const allowedKeys =
    change.kind === "update" ? new Set(["kind", "path", "destination"]) : new Set(["kind", "path"]);
  return (
    !Object.keys(change).some((key) => !allowedKeys.has(key)) &&
    (!("destination" in change) || typeof change.destination === "string")
  );
}

export function startReviewPage({ window = globalThis.window, nonce } = {}) {
  const status = window.document.querySelector("#review-status");
  const root = window.document.querySelector("#reviews");
  let socket;
  const controller = new ReviewController({
    document: window.document,
    root,
    status,
    send: (message) => socket?.send(JSON.stringify(message)),
    onInvalid: () => socket?.close(1008),
  });
  if (!nonce) {
    controller.invalidate();
    return controller;
  }
  const url = new URL(REVIEW_SOCKET_PATH, window.location.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  socket = new window.WebSocket(url, [REVIEW_PROTOCOL]);
  socket.addEventListener("open", () => {
    socket.send(JSON.stringify({ nonce }));
    nonce = "";
  });
  socket.addEventListener("message", (event) => {
    try {
      controller.receive(JSON.parse(event.data));
    } catch {
      controller.invalidate();
    }
  });
  socket.addEventListener("close", () => controller.clear());
  return controller;
}
