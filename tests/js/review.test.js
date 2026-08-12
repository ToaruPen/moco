import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { describe, it } from "node:test";

import { JSDOM } from "jsdom";

import { ReviewController, startReviewPage } from "../../src/moco/web/static/review.js";

describe("review bootstrap", () => {
  it("removes the fragment before loading the module and passes the nonce explicitly", async () => {
    const source = await readFile(
      new URL("../../src/moco/web/static/review.html", import.meta.url),
      "utf8",
    );

    assert.equal(source.includes('history.replaceState(null, "", window.location.pathname)'), true);
    assert.equal(source.includes("startReviewPage({ window, nonce })"), true);
    assert.equal(
      source.indexOf("history.replaceState") < source.indexOf('import("/static/review.js")'),
      true,
    );
  });

  it("does not persist reviewer bootstrap material in browser storage", async () => {
    const source = await readFile(
      new URL("../../src/moco/web/static/review.js", import.meta.url),
      "utf8",
    );

    assert.equal(source.includes("localStorage"), false);
    assert.equal(source.includes("sessionStorage"), false);
    assert.equal(source.includes("document.cookie"), false);
  });
});

describe("review UI", () => {
  it("closes a protocol-invalid server message with 1008 and keeps the invalid status", () => {
    const dom = new JSDOM(`<section id="reviews"></section><p id="review-status"></p>`, {
      url: "http://127.0.0.1:8080/review#review-nonce",
    });
    const sockets = [];
    class ReviewSocket extends dom.window.EventTarget {
      constructor() {
        super();
        sockets.push(this);
      }

      send() {}

      close(code) {
        this.closeCode = code;
        this.dispatchEvent(new dom.window.CloseEvent("close", { code }));
      }
    }
    dom.window.WebSocket = ReviewSocket;
    startReviewPage({ window: dom.window, nonce: "review-nonce" });

    sockets[0].dispatchEvent(
      new dom.window.MessageEvent("message", {
        data: JSON.stringify({ type: "ready", unexpected: "detail" }),
      }),
    );

    assert.equal(sockets[0].closeCode, 1008);
    assert.equal(
      dom.window.document.querySelector("#review-status").textContent,
      "Reviewer メッセージを利用できません",
    );
  });

  it("does not keep the approval-needed status after sending a decision", () => {
    const dom = new JSDOM(`<section id="reviews"></section><p id="status"></p>`);
    const document = dom.window.document;
    const sent = [];
    const controller = new ReviewController({
      document,
      root: document.querySelector("#reviews"),
      status: document.querySelector("#status"),
      send: (message) => sent.push(message),
    });

    controller.receive({
      type: "review",
      reviewHandle: "cancel-review",
      category: "command_approval",
      command: ["tool"],
      cwd: "/workspace",
      decisions: ["accept", "cancel"],
    });
    document.querySelector("button:last-of-type").click();

    assert.deepEqual(sent, [{ reviewHandle: "cancel-review", decision: "cancel" }]);
    assert.equal(statusText(document), "ローカルレビューを送信しました");
    controller.receive({ type: "resolved", reviewHandle: "cancel-review" });
    assert.equal(statusText(document), "ローカルレビューが取り消されました");
  });

  it("reports completion after the last review is resolved", () => {
    const dom = new JSDOM(`<section id="reviews"></section><p id="status"></p>`);
    const document = dom.window.document;
    const controller = new ReviewController({
      document,
      root: document.querySelector("#reviews"),
      status: document.querySelector("#status"),
    });

    controller.receive({
      type: "review",
      reviewHandle: "resolved-review",
      category: "command_approval",
      command: ["tool"],
      cwd: "/workspace",
      decisions: ["accept"],
    });
    controller.receive({ type: "resolved", reviewHandle: "resolved-review" });

    assert.equal(statusText(document), "ローカルレビューが完了しました");
  });

  it("keeps waiting for remaining reviews and reports withdrawal of the last one", () => {
    const dom = new JSDOM(`<section id="reviews"></section><p id="status"></p>`);
    const document = dom.window.document;
    const controller = new ReviewController({
      document,
      root: document.querySelector("#reviews"),
      status: document.querySelector("#status"),
    });

    for (const reviewHandle of ["first-review", "remaining-review"]) {
      controller.receive({
        type: "review",
        reviewHandle,
        category: "command_approval",
        command: ["tool"],
        cwd: "/workspace",
        decisions: ["accept"],
      });
    }
    controller.receive({ type: "resolved", reviewHandle: "first-review" });
    assert.equal(statusText(document), "ローカル承認が必要です");
    assert.equal(document.querySelectorAll("article").length, 1);

    controller.receive({ type: "withdrawn", reviewHandle: "remaining-review" });
    assert.equal(statusText(document), "ローカルレビューが取り消されました");
    assert.equal(document.querySelectorAll("article").length, 0);
  });

  it("preserves command and path boundaries as JSON-quoted text", () => {
    const dom = new JSDOM(`<section id="reviews"></section><p id="status"></p>`);
    const document = dom.window.document;
    const controller = new ReviewController({
      document,
      root: document.querySelector("#reviews"),
      status: document.querySelector("#status"),
    });

    controller.receive({
      type: "review",
      reviewHandle: "raw-command-review",
      category: "command_approval",
      commandText: "tool --flag='argument with spaces' && next\nline",
      cwd: "/workspace",
      decisions: ["accept", "decline"],
    });
    controller.receive({
      type: "review",
      reviewHandle: "command-review",
      category: "command_approval",
      command: ["tool", "argument with spaces", "", "(empty string)"],
      cwd: " /workspace\nsubdir ",
      decisions: ["accept", "decline"],
    });
    controller.receive({
      type: "review",
      reviewHandle: "change-review",
      category: "file_change_approval",
      changes: [
        {
          kind: "update",
          path: " benign  source\n.txt ",
          destination: " .git/hooks/pre-commit\n ",
        },
      ],
      decisions: ["accept", "decline"],
    });

    assert.deepEqual(
      [...document.querySelectorAll(".review-argument")].map((element) => element.textContent),
      [
        'argv[0]: "tool"',
        'argv[1]: "argument with spaces"',
        'argv[2]: ""',
        'argv[3]: "(empty string)"',
      ],
    );
    assert.equal(
      document.querySelector(".review-command-text").textContent,
      "command: \"tool --flag='argument with spaces' && next\\nline\"",
    );
    assert.deepEqual(
      [...document.querySelectorAll(".review-cwd")].map((element) => element.textContent),
      ['cwd: "/workspace"', 'cwd: " /workspace\\nsubdir "'],
    );
    assert.deepEqual(
      [...document.querySelectorAll(".review-change-source")].map((element) => element.textContent),
      ['source: " benign  source\\n.txt "'],
    );
    assert.equal(document.querySelector(".review-change-source").tagName, "DIV");
    assert.deepEqual(
      [...document.querySelectorAll(".review-change-destination")].map(
        (element) => element.textContent,
      ),
      ['destination: " .git/hooks/pre-commit\\n "'],
    );
    assert.equal(document.querySelector(".review-change-destination").tagName, "DIV");
  });

  for (const commandMembers of [
    {},
    { commandText: "tool", command: ["tool"] },
    { commandText: 7 },
    { command: [] },
  ]) {
    it(`fails closed on an invalid command shape ${JSON.stringify(commandMembers)}`, () => {
      const dom = new JSDOM(`<section id="reviews"></section><p id="status"></p>`);
      const document = dom.window.document;
      const controller = new ReviewController({
        document,
        root: document.querySelector("#reviews"),
        status: document.querySelector("#status"),
      });

      controller.receive({
        type: "review",
        reviewHandle: "invalid-command-review",
        category: "command_approval",
        cwd: "/workspace",
        decisions: ["accept", "decline"],
        ...commandMembers,
      });

      assert.equal(controller.invalid, true);
      assert.equal(document.querySelector("#reviews").textContent, "");
    });
  }

  function statusText(document) {
    return document.querySelector("#status").textContent;
  }

  it("renders details as text and never accepts from focus or keyboard input", () => {
    const dom = new JSDOM(`
      <main>
        <p id="status"></p>
        <section id="reviews"></section>
      </main>
    `);
    const document = dom.window.document;
    const sent = [];
    const controller = new ReviewController({
      document,
      root: document.querySelector("#reviews"),
      status: document.querySelector("#status"),
      send: (message) => sent.push(message),
    });

    controller.receive({
      type: "review",
      reviewHandle: "review-1",
      category: "command_approval",
      command: ["echo", "<script>alert(1)</script>"],
      cwd: "/private/workspace",
      reason: "review reason",
      decisions: ["accept", "decline", "cancel"],
    });

    assert.equal(document.querySelectorAll("script").length, 0);
    assert.match(document.querySelector("#reviews").textContent, /<script>alert\(1\)<\/script>/);
    assert.equal(document.activeElement, document.body);

    document.body.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Enter" }));
    document.body.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: " " }));
    assert.deepEqual(sent, []);
  });

  it("removes the review detail before sending the one strict decision", () => {
    const dom = new JSDOM(`<section id="reviews"></section><p id="status"></p>`);
    const document = dom.window.document;
    const sent = [];
    const controller = new ReviewController({
      document,
      root: document.querySelector("#reviews"),
      status: document.querySelector("#status"),
      send: (message) => sent.push(message),
    });

    controller.receive({
      type: "review",
      reviewHandle: "review-2",
      category: "file_change_approval",
      changes: [{ kind: "update", path: "/private/secret.txt" }],
      decisions: ["accept", "decline"],
    });
    document.querySelector("button").click();

    assert.deepEqual(sent, [{ reviewHandle: "review-2", decision: "accept" }]);
    assert.equal(document.querySelector("#reviews").textContent, "");
  });

  it("fails closed when a decided review handle is replayed", () => {
    const dom = new JSDOM(`<section id="reviews"></section><p id="status"></p>`);
    const document = dom.window.document;
    const controller = new ReviewController({
      document,
      root: document.querySelector("#reviews"),
      status: document.querySelector("#status"),
    });
    const review = {
      type: "review",
      reviewHandle: "replayed-review",
      category: "command_approval",
      command: ["tool"],
      cwd: "/workspace",
      decisions: ["accept"],
    };

    controller.receive(review);
    document.querySelector("button").click();
    controller.receive(review);

    assert.equal(controller.invalid, true);
    assert.equal(document.querySelector("#reviews").textContent, "");
  });

  it("fails closed on an unknown server field", () => {
    const dom = new JSDOM(`<section id="reviews"></section><p id="status"></p>`);
    const document = dom.window.document;
    const controller = new ReviewController({
      document,
      root: document.querySelector("#reviews"),
      status: document.querySelector("#status"),
    });

    controller.receive({ type: "ready", unexpected: "detail" });

    assert.equal(controller.invalid, true);
    assert.equal(document.querySelector("#reviews").textContent, "");
  });

  for (const type of ["resolved", "withdrawn"]) {
    it(`fails closed when ${type} names an unknown review handle`, () => {
      const dom = new JSDOM(`<section id="reviews"></section><p id="status"></p>`);
      const document = dom.window.document;
      const controller = new ReviewController({
        document,
        root: document.querySelector("#reviews"),
        status: document.querySelector("#status"),
      });

      controller.receive({ type, reviewHandle: "unknown-review" });

      assert.equal(controller.invalid, true);
    });
  }

  it("fails closed on an unknown nested file change field", () => {
    const dom = new JSDOM(`<section id="reviews"></section><p id="status"></p>`);
    const document = dom.window.document;
    const controller = new ReviewController({
      document,
      root: document.querySelector("#reviews"),
      status: document.querySelector("#status"),
    });

    controller.receive({
      type: "review",
      reviewHandle: "change-review",
      category: "file_change_approval",
      changes: [{ kind: "update", path: "safe.txt", unexpected: "detail" }],
      decisions: ["accept", "decline"],
    });

    assert.equal(controller.invalid, true);
  });

  it("fails closed when an added file claims a destination", () => {
    const dom = new JSDOM(`<section id="reviews"></section><p id="status"></p>`);
    const document = dom.window.document;
    const controller = new ReviewController({
      document,
      root: document.querySelector("#reviews"),
      status: document.querySelector("#status"),
    });

    controller.receive({
      type: "review",
      reviewHandle: "change-review",
      category: "file_change_approval",
      changes: [{ kind: "add", path: "new.txt", destination: "moved.txt" }],
      decisions: ["accept", "decline"],
    });

    assert.equal(controller.invalid, true);
  });
});
