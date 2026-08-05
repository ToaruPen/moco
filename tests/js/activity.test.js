import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { JSDOM } from "jsdom";

import {
  ActivityBuffer,
  ActivityView,
  ProgressTracker,
  ProgressView,
} from "../../src/moco/web/static/activity.js";

describe("ActivityBuffer", () => {
  it("retains only the newest 200 events", () => {
    const buffer = new ActivityBuffer({ limit: 200 });
    for (let index = 0; index < 205; index += 1) {
      buffer.add({
        kind: "work",
        phase: "started",
        label: `event-${index}`,
        occurredAtMs: index,
      });
    }

    assert.equal(buffer.items.length, 200);
    assert.equal(buffer.items[0].label, "event-5");
  });

  it("joins bounded reasoning summaries by item", () => {
    const buffer = new ActivityBuffer({ summaryLimit: 10 });
    buffer.addSummary({ itemId: "r-1", delta: "設定を", occurredAtMs: 10 });
    buffer.addSummary({ itemId: "r-1", delta: "確認しています。", occurredAtMs: 11 });

    assert.equal(buffer.items.at(-1).label, "設定を確認しています");
    assert.equal(buffer.items.length, 1);
  });

  it("clears activity and summary state together", () => {
    const buffer = new ActivityBuffer();
    buffer.addSummary({ itemId: "r-1", delta: "確認中", occurredAtMs: 10 });

    buffer.clear();
    buffer.addSummary({ itemId: "r-1", delta: "再開", occurredAtMs: 11 });

    assert.equal(buffer.items.length, 1);
    assert.equal(buffer.items[0].label, "再開");
  });
});

describe("ProgressTracker", () => {
  it("reports elapsed and stale activity without inventing completion", () => {
    const tracker = new ProgressTracker({ now: () => 25_000 });
    tracker.consume({
      kind: "turn",
      phase: "started",
      label: "応答処理",
      occurredAtMs: 1_000,
    });
    tracker.consume({
      kind: "work",
      phase: "started",
      label: "Web 検索",
      occurredAtMs: 20_000,
    });

    assert.deepEqual(tracker.snapshot(), {
      active: true,
      label: "Web 検索を実行しています",
      elapsedMs: 24_000,
      staleMs: 5_000,
    });
  });

  it("changes to waiting only when the turn completes", () => {
    const tracker = new ProgressTracker({ now: () => 30_000 });
    tracker.consume({
      kind: "turn",
      phase: "started",
      label: "応答処理",
      occurredAtMs: 1_000,
    });
    tracker.consume({
      kind: "work",
      phase: "completed",
      label: "コマンド実行",
      occurredAtMs: 20_000,
    });
    assert.equal(tracker.snapshot().active, true);
    assert.equal(tracker.snapshot().label, "Codex が処理を続けています");

    tracker.consume({
      kind: "turn",
      phase: "completed",
      label: "応答処理",
      occurredAtMs: 25_000,
    });
    assert.equal(tracker.snapshot().active, false);
    assert.equal(tracker.snapshot().label, "発話を待っています");
  });

  it("keeps playback visible after turn and synthesis complete", () => {
    const tracker = new ProgressTracker({ now: () => 30_000 });
    tracker.consume({ kind: "turn", phase: "started", label: "応答処理", occurredAtMs: 1_000 });
    tracker.consume({
      kind: "playback",
      phase: "started",
      label: "音声再生",
      occurredAtMs: 20_000,
    });
    tracker.consume({ kind: "turn", phase: "completed", label: "応答処理", occurredAtMs: 21_000 });

    assert.equal(tracker.snapshot().active, true);
    assert.equal(tracker.snapshot().label, "音声を再生しています");

    tracker.consume({
      kind: "playback",
      phase: "completed",
      label: "音声再生",
      occurredAtMs: 25_000,
    });
    assert.equal(tracker.snapshot().active, false);
    assert.equal(tracker.snapshot().label, "発話を待っています");
  });

  it("distinguishes initial, ready, expired, and disconnected states", () => {
    const tracker = new ProgressTracker({ now: () => 30_000 });
    assert.equal(tracker.snapshot().label, "接続を待っています");

    tracker.ready();
    assert.equal(tracker.snapshot().label, "発話を待っています");
    tracker.expire();
    assert.equal(tracker.snapshot().label, "会話が終了しました");
    tracker.disconnect();
    assert.equal(tracker.snapshot().label, "接続が切断されました");
  });

  it("marks a disconnected session explicitly", () => {
    const tracker = new ProgressTracker({ now: () => 30_000 });
    tracker.disconnect();

    assert.deepEqual(tracker.snapshot(), {
      active: false,
      label: "接続が切断されました",
      elapsedMs: 0,
      staleMs: 0,
    });
  });
});

describe("activity DOM views", () => {
  it("renders text without interpreting activity labels as markup", () => {
    const dom = new JSDOM('<div id="activity"></div><button id="latest"></button>');
    const document = dom.window.document;
    const view = new ActivityView({
      container: document.querySelector("#activity"),
      latestButton: document.querySelector("#latest"),
      createElement: (tag) => document.createElement(tag),
      formatTime: () => "20:14:08",
    });

    view.render([
      {
        kind: "work",
        phase: "started",
        label: "<img src=x onerror=alert(1)>",
        occurredAtMs: 1,
      },
    ]);

    assert.equal(document.querySelectorAll("img").length, 0);
    assert.equal(
      document.querySelector(".activity-label").textContent,
      "<img src=x onerror=alert(1)>",
    );
  });

  it("formats progress as elapsed and last-update times", () => {
    const dom = new JSDOM(
      '<span id="label"></span><span id="elapsed"></span><span id="updated"></span>',
    );
    const document = dom.window.document;
    const view = new ProgressView({
      label: document.querySelector("#label"),
      elapsed: document.querySelector("#elapsed"),
      updated: document.querySelector("#updated"),
    });

    view.render({ active: true, label: "確認中", elapsedMs: 65_000, staleMs: 3_900 });

    assert.equal(document.querySelector("#label").textContent, "確認中");
    assert.equal(document.querySelector("#elapsed").textContent, "01:05");
    assert.equal(document.querySelector("#updated").textContent, "最終更新 3 秒前");
    assert.equal(document.querySelector("#elapsed").hidden, false);
    assert.equal(document.querySelector("#updated").hidden, false);

    view.render({ active: false, label: "発話を待っています", elapsedMs: 0, staleMs: 9_000 });
    assert.equal(document.querySelector("#elapsed").hidden, true);
    assert.equal(document.querySelector("#updated").hidden, true);
  });

  it("shows activity phases with compact stable kind labels", () => {
    const dom = new JSDOM('<div id="activity"></div><button id="latest"></button>');
    const document = dom.window.document;
    const view = new ActivityView({
      container: document.querySelector("#activity"),
      latestButton: document.querySelector("#latest"),
      createElement: (tag) => document.createElement(tag),
      formatTime: () => "20:14:08",
    });

    view.render([
      { kind: "microphone", phase: "started", label: "マイク入力", occurredAtMs: 1 },
      { kind: "work", phase: "completed", label: "Web 検索", occurredAtMs: 2 },
      { kind: "reasoning", phase: "updated", label: "確認中", occurredAtMs: 3 },
    ]);

    assert.deepEqual(
      [...document.querySelectorAll(".activity-kind")].map((node) => node.textContent),
      ["MIC · 開始", "WORK · 完了", "REASON · 更新"],
    );
  });

  it("updates one reasoning row without rebuilding stable activity rows", () => {
    const dom = new JSDOM('<div id="activity"></div><button id="latest"></button>');
    const document = dom.window.document;
    let created = 0;
    const view = new ActivityView({
      container: document.querySelector("#activity"),
      latestButton: document.querySelector("#latest"),
      createElement: (tag) => {
        created += 1;
        return document.createElement(tag);
      },
      formatTime: () => "20:14:08",
    });
    const stable = { kind: "work", phase: "started", label: "検索", occurredAtMs: 1 };
    const firstSummary = {
      kind: "reasoning",
      phase: "updated",
      label: "確認中",
      occurredAtMs: 2,
      itemId: "reasoning-1",
    };
    view.render([stable, firstSummary]);
    const stableRow = document.querySelector(".activity-row--work");
    const initialCreated = created;

    view.render([stable, { ...firstSummary, label: "確認を続けています", occurredAtMs: 3 }]);

    assert.equal(document.querySelector(".activity-row--work"), stableRow);
    assert.equal(
      document.querySelector(".activity-row--reasoning .activity-label").textContent,
      "確認を続けています",
    );
    assert.equal(created - initialCreated, initialCreated / 2);
  });
});
