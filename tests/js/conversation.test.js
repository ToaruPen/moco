import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { JSDOM } from "jsdom";
import { TranscriptView } from "../../src/moco/web/static/app.js";

function fixture() {
  const dom = new JSDOM('<div id="log" tabindex="0"></div><button hidden>最新へ</button>');
  const container = dom.window.document.querySelector("#log");
  const latestButton = dom.window.document.querySelector("button");
  let height = 800;
  let clock = Date.parse("2026-09-05T03:04:05Z");
  Object.defineProperties(container, {
    scrollHeight: { get: () => height },
    clientHeight: { value: 200 },
  });
  const view = new TranscriptView(container, { latestButton, now: () => clock });
  return {
    container,
    latestButton,
    view,
    scroll(top) {
      container.scrollTop = top;
      container.dispatchEvent(new dom.window.Event("scroll"));
    },
    grow() {
      height += 200;
      clock += 60_000;
    },
  };
}

describe("TranscriptView reading experience", () => {
  it("preserves the reader position during new and streaming utterances", () => {
    const f = fixture();
    f.view.update("user", "依頼", true);
    f.scroll(100);
    f.grow();
    f.view.update("assistant", "作業中", false);
    f.view.update("assistant", "作業が完了しました", true);
    assert.equal(f.container.scrollTop, 100);
    assert.equal(f.latestButton.hidden, false);
  });

  it("resumes following on latest action and returns keyboard focus to the log", () => {
    const f = fixture();
    f.view.update("user", "依頼", true);
    f.scroll(100);
    f.latestButton.focus();
    f.latestButton.click();
    assert.equal(f.container.scrollTop, 800);
    assert.equal(f.latestButton.hidden, true);
    assert.equal(f.container.ownerDocument.activeElement, f.container);
    f.grow();
    f.view.update("assistant", "完了", true);
    assert.equal(f.container.scrollTop, 1000);
  });

  it("resumes following after manually returning within 48px of the bottom", () => {
    const f = fixture();
    f.scroll(100);
    assert.equal(f.latestButton.hidden, false);
    f.scroll(560);
    assert.equal(f.latestButton.hidden, true);
    f.grow();
    f.view.update("assistant", "続き", true);
    assert.equal(f.container.scrollTop, 1000);
  });

  it("keeps the first received timestamp and treats transcript content as plain text", () => {
    const f = fixture();
    f.view.update("assistant", "<img src=x>", false);
    const time = f.container.querySelector("time");
    assert.ok(time);
    assert.equal(time.dateTime, "2026-09-05T03:04:05.000Z");
    const label = time.textContent;
    f.grow();
    f.view.update("assistant", "<script>text</script>", true);
    assert.equal(f.container.querySelector("time"), time);
    assert.equal(time.textContent, label);
    assert.equal(f.container.querySelector("script, img"), null);
    f.view.update("user", "次の発話", true);
    assert.equal(f.container.querySelectorAll("time")[1].dateTime, "2026-09-05T03:05:05.000Z");
  });

  it("shows the next action for each empty conversation state", () => {
    const f = fixture();
    for (const [state, text] of [
      ["disconnected", "再接続"],
      ["connecting", "マイク"],
      ["ready", "入力開始"],
      ["listening", "そのまま話しかけて"],
      ["idle_expired", "入力開始"],
      ["voice_reconnect_required", "入力開始"],
      ["connection_lost", "入力開始"],
    ]) {
      f.view.setGuidance(state);
      assert.ok(f.container.querySelector(".transcript-empty").textContent.includes(text), state);
    }
  });

  it("restores current guidance on clear and starts a fresh utterance while following", () => {
    const f = fixture();
    f.view.setGuidance("listening");
    f.view.update("assistant", "途中", false);
    f.view.setGuidance("disconnected");
    assert.equal(f.container.querySelector(".transcript-empty"), null);
    f.scroll(100);
    f.view.clear();
    assert.match(f.container.textContent, /再接続/);
    assert.equal(f.latestButton.hidden, true);
    f.view.update("assistant", "新しい本文", true);
    assert.equal(f.container.querySelectorAll(".utterance").length, 1);
    assert.equal(f.container.scrollTop, 800);
  });
});
