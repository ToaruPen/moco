# ブラウザ音声デバイス切り替え実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** mocoのブラウザ画面から、Codex Realtime会話を維持したまま入力マイクとIrodori出力先を切り替えられるようにする。

**Architecture:** 新しい`AudioDeviceController`がブラウザのデバイス列挙、選択IDの保存、入力trackの原子的交換、`AudioContext` sink変更、`devicechange`を所有する。既存`app.js`は現在のpeer・stream・AudioContextとの接続だけを担当し、Realtime Thread、SpeechQueue、Irodori generationには変更を加えない。

**Tech Stack:** browser MediaDevices/WebRTC/Web Audio APIs, ES modules, Node test runner + JSDOM, Playwright, existing Python/uv quality gates

---

### Task 1: 入出力セレクトのUI contract

**Files:**
- Modify: `tests/js/app.test.js:1242`
- Modify: `tests/e2e/mobile-console.spec.js:3`
- Modify: `src/moco/web/static/index.html:14`
- Modify: `src/moco/web/static/styles.css:152`

- [ ] **Step 1: INPUT/OUTPUT landmarkの失敗テストを書く**

`tests/js/app.test.js`のoperator console DOM testへ`audio-input`と`audio-output`を追加し、初期状態が
disabled、accessible nameがそれぞれ`入力マイク`と`音声出力先`であることを検証する。

```js
for (const id of [
  "enable",
  "audio-input",
  "audio-output",
  "connection-row",
  "state",
  "connection",
  "mic-state",
  "voice",
  "listen-start",
  "listen-stop",
  "turn-cancel",
  "progress-label",
  "progress-elapsed",
  "progress-updated",
  "error",
  "error-text",
  "error-close",
  "transcript",
  "activity",
  "activity-latest",
  "theme-toggle",
  "theme-panel",
  "theme-close",
  "theme-presets",
  "theme-colors",
  "theme-validation",
  "theme-reset",
  "pairing-open",
  "pairing-panel",
  "pairing-close",
  "pairing-image",
]) {
  assert.equal(document.querySelectorAll(`#${id}`).length, 1, id);
}
assert.equal(document.querySelector("#audio-input").disabled, true);
assert.equal(document.querySelector("#audio-output").disabled, true);
assert.equal(document.querySelector("#audio-input").ariaLabel, "入力マイク");
assert.equal(document.querySelector("#audio-output").ariaLabel, "音声出力先");
```

`tests/e2e/mobile-console.spec.js`では幅320 pxでも二つのcomboboxが存在し、既存のhorizontal overflow
条件を満たすことを追加する。

```js
await expect(page.getByRole("combobox", { name: "入力マイク" })).toBeVisible();
await expect(page.getByRole("combobox", { name: "音声出力先" })).toBeVisible();
```

- [ ] **Step 2: REDを確認する**

Run: `just test-frontend 'operator console DOM'`

Expected: `#audio-input`または`#audio-output`が存在せずFAIL。

Run: `npm run test:e2e -- --grep 'mobile console'`

Expected: 入出力comboboxが見つからずFAIL。

- [ ] **Step 3: 最小のHTML/CSSを追加する**

`src/moco/web/static/index.html`のVOICE直前へ次を追加する。

```html
<label class="device-control" for="audio-input">
  <span>INPUT</span>
  <select id="audio-input" aria-label="入力マイク" disabled>
    <option value="">接続待ち</option>
  </select>
</label>
<label class="device-control" for="audio-output">
  <span>OUTPUT</span>
  <select id="audio-output" aria-label="音声出力先" disabled>
    <option value="">接続待ち</option>
  </select>
</label>
```

`styles.css`では`voice-control`と同じ文字・高さを共有し、desktopではdevice selectを180 px以下、
820 px以下では三つのselectを折り返し可能、520 px以下ではlabel文字を隠して各selectを一行幅へ収める。

```css
.voice-control,
.device-control {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--c-text-muted);
  font: 10px ui-monospace, "SFMono-Regular", Menlo, monospace;
  letter-spacing: 0.08em;
}

.voice-control select,
.device-control select {
  height: 30px;
  padding: 3px 26px 3px 8px;
}

.device-control select {
  width: min(18vw, 180px);
}

@media (max-width: 820px) {
  .operator-actions {
    flex-wrap: wrap;
  }

  .voice-control,
  .device-control {
    min-width: min(100%, 150px);
    flex: 1;
  }

  .voice-control select,
  .device-control select {
    width: 100%;
  }
}

@media (max-width: 520px) {
  .voice-control > span,
  .device-control > span {
    display: none;
  }
}
```

- [ ] **Step 4: GREENを確認する**

Run: `just test-frontend 'operator console DOM'`

Expected: PASS。

Run: `npm run test:e2e -- --grep 'mobile console'`

Expected: Chromium 320/430とWebKit 390の全projectでPASS、horizontal overflowなし。

- [ ] **Step 5: Task 1をcommitする**

```bash
git add tests/js/app.test.js tests/e2e/mobile-console.spec.js \
  src/moco/web/static/index.html src/moco/web/static/styles.css
git commit -m "feat: add browser audio device controls"
```

### Task 2: デバイス一覧とブラウザ能力差

**Files:**
- Create: `tests/js/media-devices.test.js`
- Create: `src/moco/web/static/media-devices.js`

- [ ] **Step 1: catalogと能力差の失敗テストを書く**

JSDOMの二つのselect、Map backed storage、EventTarget backed MediaDevicesを使い、次を一件ずつtestにする。

```js
it("renders one system default plus unique devices of each audio kind", async () => {
  const harness = deviceHarness({
    devices: [
      { kind: "audioinput", deviceId: "default", label: "Default microphone" },
      { kind: "audioinput", deviceId: "mic-1", label: "Studio mic" },
      { kind: "audioinput", deviceId: "mic-1", label: "Duplicate" },
      { kind: "audiooutput", deviceId: "speaker-1", label: "USB speakers" },
      { kind: "videoinput", deviceId: "camera-1", label: "Camera" },
    ],
  });

  await harness.controller.start();

  assert.deepEqual(harness.options("input"), [
    ["", "システム既定"],
    ["mic-1", "Studio mic"],
  ]);
  assert.deepEqual(harness.options("output"), [
    ["", "システム既定"],
    ["speaker-1", "USB speakers"],
  ]);
});
```

`context.setSinkId`がないharnessではOUTPUTが「システム既定」の一項だけでdisabledになり、INPUTは
有効なままであることを確認する。

- [ ] **Step 2: REDを確認する**

Run: `node --test tests/js/media-devices.test.js`

Expected: `media-devices.js`または`AudioDeviceController`が存在せずFAIL。

- [ ] **Step 3: catalogと安全なstorageの最小実装を書く**

`src/moco/web/static/media-devices.js`へ次のpublic contractを作る。

```js
const RESERVED_DEFAULT_IDS = new Set(["default", "communications"]);

export class AudioDeviceController {
  constructor({
    inputSelect,
    outputSelect,
    context,
    mediaDevices,
    getCurrentStream,
    getAudioSender,
    replaceCurrentStream,
    onError = () => {},
    createOption = (label, value) => new Option(label, value),
  }) {
    this.inputSelect = inputSelect;
    this.outputSelect = outputSelect;
    this.context = context;
    this.mediaDevices = mediaDevices;
    this.getCurrentStream = getCurrentStream;
    this.getAudioSender = getAudioSender;
    this.replaceCurrentStream = replaceCurrentStream;
    this.onError = onError;
    this.createOption = createOption;
    this.inputId = "";
    this.outputId = "";
    this.devices = [];
    this.closed = false;
    this.inputBusy = false;
    this.outputBusy = false;
    this.deviceChange = () => void this.refresh();
  }

  async start() {
    await this.refresh();
    this.mediaDevices.addEventListener?.("devicechange", this.deviceChange);
  }

  async refresh() {
    if (this.closed) return;
    try {
      this.devices = await this.mediaDevices.enumerateDevices();
    } catch {
      this.devices = [];
    }
    this.#render();
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    this.mediaDevices.removeEventListener?.("devicechange", this.deviceChange);
    this.inputSelect.disabled = true;
    this.outputSelect.disabled = true;
  }

}
```

private helperはkind別に空ID・reserved default ID・重複IDを除き、空labelには`マイク N`または
`出力 N`を使う。selectの先頭へ`システム既定`を作り、storage操作は全てtry/catch内で行う。

- [ ] **Step 4: GREENを確認する**

Run: `node --test tests/js/media-devices.test.js`

Expected: catalogとunsupported outputのtestがPASS。

- [ ] **Step 5: Task 2をcommitする**

```bash
git add tests/js/media-devices.test.js src/moco/web/static/media-devices.js
git commit -m "feat: manage browser audio device catalog"
```

### Task 3: 会話を維持する入力・出力切り替えと選択保存

**Files:**
- Modify: `tests/js/media-devices.test.js`
- Modify: `src/moco/web/static/media-devices.js`

- [ ] **Step 1: 入力の原子的交換を示す失敗テストを書く**

current trackをenabled true、new trackをenabled falseにしたfixtureで`selectInput("mic-2")`を呼び、
次を順序込みで確認する。

```js
assert.deepEqual(harness.getUserMediaCalls, [
  { audio: { deviceId: { exact: "mic-2" } } },
]);
assert.equal(harness.newTrack.enabled, true);
assert.deepEqual(harness.sender.replacements, [harness.newTrack]);
assert.equal(harness.currentStream(), harness.newStream);
assert.equal(harness.oldTrack.stopCalls, 1);
assert.equal(harness.input.value, "mic-2");
assert.equal(harness.storage.getItem("moco.audio.inputDeviceId"), "mic-2");
```

MIC OFF fixtureでもnew trackがfalseを維持するtestを分ける。システム既定への変更では
`getUserMedia({ audio: true })`になることを確認する。

- [ ] **Step 2: REDを確認する**

Run: `node --test --test-name-pattern='input' tests/js/media-devices.test.js`

Expected: `selectInput`未実装でFAIL。

- [ ] **Step 3: 入力交換の最小実装を書く**

constructorへ`storage`を追加し、module定数として次のkeyを定義してから入力交換を実装する。

```js
const INPUT_STORAGE_KEY = "moco.audio.inputDeviceId";
const OUTPUT_STORAGE_KEY = "moco.audio.outputDeviceId";
```

```js
async selectInput(deviceId) {
  if (this.closed || this.inputBusy || deviceId === this.inputId) return false;
  this.inputBusy = true;
  this.#render();
  let candidate;
  try {
    candidate = await this.mediaDevices.getUserMedia({
      audio: deviceId ? { deviceId: { exact: deviceId } } : true,
    });
    const nextTrack = candidate.getAudioTracks()[0];
    const current = this.getCurrentStream();
    const currentTrack = current?.getAudioTracks()[0];
    const sender = this.getAudioSender();
    if (!nextTrack || !currentTrack || !sender) throw new Error("audio sender unavailable");
    nextTrack.enabled = currentTrack.enabled;
    await sender.replaceTrack(nextTrack);
    this.replaceCurrentStream(candidate);
    for (const track of current.getTracks()) track.stop();
    this.inputId = deviceId;
    this.#store(INPUT_STORAGE_KEY, deviceId);
    return true;
  } catch {
    for (const track of candidate?.getTracks() ?? []) track.stop();
    this.onError("microphone_switch_failed");
    return false;
  } finally {
    this.inputBusy = false;
    this.#render();
  }
}
```

- [ ] **Step 4: 入力失敗時の維持を示す失敗テストを書く**

`getUserMedia`失敗と`replaceTrack`失敗を別testにする。後者ではcandidate trackだけが停止され、old track、
current stream、選択値、storageが変化せず、error codeが一度だけ通知されることを確認する。

Run: `node --test --test-name-pattern='failure' tests/js/media-devices.test.js`

Expected: rollback条件の不足でFAIL。

- [ ] **Step 5: 失敗testをGREENにする**

candidate streamを交換成功前にownerへ渡さず、catchではcandidateだけを停止する。例外本文やdevice labelを
`onError`へ渡さない。

Run: `node --test --test-name-pattern='failure' tests/js/media-devices.test.js`

Expected: PASS。

- [ ] **Step 6: 出力変更の失敗テストを書く**

`selectOutput("speaker-2")`が`context.setSinkId("speaker-2")`を一回呼び、成功時だけ表示とstorageを
更新することを確認する。reject時は現在値を維持し`audio_output_switch_failed`を一度だけ通知する。

Run: `node --test --test-name-pattern='output' tests/js/media-devices.test.js`

Expected: `selectOutput`未実装でFAIL。

- [ ] **Step 7: 出力変更の最小実装を書く**

```js
async selectOutput(deviceId) {
  if (
    this.closed ||
    this.outputBusy ||
    !this.#supportsOutputSelection() ||
    deviceId === this.outputId
  ) return false;
  this.outputBusy = true;
  this.#render();
  try {
    await this.context.setSinkId(deviceId);
    this.outputId = deviceId;
    this.#store(OUTPUT_STORAGE_KEY, deviceId);
    return true;
  } catch {
    this.onError("audio_output_switch_failed");
    return false;
  } finally {
    this.outputBusy = false;
    this.#render();
  }
}
```

- [ ] **Step 8: devicechange復帰とcloseの失敗テストを書く**

選択中IDを次の`enumerateDevices()`結果から除き`devicechange`をdispatchする。INPUTは
`getUserMedia({ audio: true })`とreplaceTrack、OUTPUTは`setSinkId("")`で既定へ戻り、両storage値が
removeされることを確認する。`close()`後のdispatchでは再列挙されないことも別testにする。

Run: `node --test --test-name-pattern='devicechange|close' tests/js/media-devices.test.js`

Expected: 自動復帰未実装でFAIL。

- [ ] **Step 9: 保存済み選択の復元とstorage障害の失敗テストを書く**

`moco.audio.inputDeviceId`と`moco.audio.outputDeviceId`の利用可能な保存値をstart時に実経路へ適用する
こと、未検出値をremoveすること、storageのget/set/removeがthrowしてもstartと明示選択が成功することを
別testで確認する。

Run: `node --test --test-name-pattern='stored|storage' tests/js/media-devices.test.js`

Expected: start時の復元とsafe storage helperが未実装でFAIL。

- [ ] **Step 10: refresh時の既定復帰と保存復元を実装する**

`refresh()`は新一覧をrenderした後、非空の現在IDが対応kindに存在しなければ`selectInput("")`または
`selectOutput("")`をawaitする。kind別操作中は同じkindを再入せず、次のdevicechangeで再評価する。

`start()`は初回refresh後にsafe storage helperで選択IDを読み、現在一覧にある非空IDだけを
`selectInput`／`selectOutput`へ渡す。未検出値はremoveし、storage例外は握りつぶして接続中の状態を
source of truthにする。

Run: `node --test tests/js/media-devices.test.js`

Expected: 全test PASS。

- [ ] **Step 11: Task 3をcommitする**

```bash
git add tests/js/media-devices.test.js src/moco/web/static/media-devices.js
git commit -m "feat: switch live browser audio devices"
```

### Task 4: operator bootと既存cleanupへの統合

**Files:**
- Modify: `tests/js/app.test.js`
- Modify: `src/moco/web/static/app.js`

- [ ] **Step 1: error copyとcleanupの失敗テストを書く**

OperatorStatus testで二つのstable codeの日本語copyを確認する。

```js
status.showError("microphone_switch_failed");
assert.equal(
  status.errorText.textContent,
  "microphone_switch_failed — 入力マイクを切り替えられませんでした",
);
status.showError("audio_output_switch_failed");
assert.equal(
  status.errorText.textContent,
  "audio_output_switch_failed — 音声出力先を切り替えられませんでした",
);
```

`closeDisconnectedMedia` testでは`deviceController.close()`がtrack stopとAudioContext closeより前に一度だけ
呼ばれ、INPUT/OUTPUTを含むcontrolsがdisabledになることを確認する。

Run: `just test-frontend 'stable audio device|media cleanup'`

Expected: copyまたはdevice controller cleanup不足でFAIL。

- [ ] **Step 2: app.jsのerrorとcleanupをGREENにする**

`ERROR_COPY`へ二つのcodeを加え、`closeDisconnectedMedia`の引数へ`deviceController`を追加し、冒頭で
`deviceController?.close()`する。既存track/context cleanup順序は維持する。

Run: `just test-frontend 'stable audio device|media cleanup'`

Expected: PASS。

- [ ] **Step 3: boot harnessへデバイス統合の失敗テストを書く**

`bootConversationHarness`のFakePeerは`addTrack()`からaudio senderを返し、`getSenders()`と
`replaceTrack()`を記録する。Fake MediaDevicesは`enumerateDevices`、EventTarget listener、複数streamを
提供する。接続後にINPUTを変更し、次を確認する。

```js
connection.dom.window.document.querySelector("#audio-input").value = "mic-2";
connection.dom.window.document.querySelector("#audio-input").dispatchEvent(new Event("change"));
await connection.waitFor(() => connection.peers[0].sender.replacements.length === 1);
assert.equal(connection.sent.filter(({ type }) => type === "start").length, 1);
assert.equal(connection.peers.length, 1);
```

OUTPUT変更ではFakeAudioContextの`sinkIds`だけが増え、`start`、peer、Irodori websocket audio metadataに
追加がないことを確認する。Realtime再接続後のpeer `addTrack`が現在の交換済みtrackを受け取るtestも追加する。

Run: `just test-frontend 'audio device switching'`

Expected: DOM changeがcontrollerへ接続されておらずFAIL。

- [ ] **Step 4: bootへAudioDeviceControllerを統合する**

`app.js`先頭でcontrollerをimportし、DOM mapへ`audioInput`と`audioOutput`を追加する。boot closureへ
`let deviceController;`を追加する。会話接続成功後に次を作ってstartする。

```js
deviceController = new AudioDeviceController({
  inputSelect: dom.audioInput,
  outputSelect: dom.audioOutput,
  context,
  mediaDevices: navigator.mediaDevices,
  storage: window.localStorage,
  getCurrentStream: () => stream,
  getAudioSender: () => peer?.getSenders().find((sender) => sender.track?.kind === "audio"),
  replaceCurrentStream: (nextStream) => {
    stream = nextStream;
    controller.stream = nextStream;
  },
  onError: (code) => operatorStatus.showError(code),
});
await deviceController.start();
```

change listenerは現在のcontrollerへvalueを渡す。

```js
dom.audioInput.addEventListener("change", () => {
  void deviceController?.selectInput(dom.audioInput.value);
});
dom.audioOutput.addEventListener("change", () => {
  void deviceController?.selectOutput(dom.audioOutput.value);
});
```

socket closeとsetup catchではcontrollerをcloseしてundefinedにし、controls配列へ両selectを追加する。

- [ ] **Step 5: 統合testをGREENにする**

Run: `just test-frontend 'audio device switching'`

Expected: 入力・出力変更とpeer再接続testがPASSし、`start`件数が増えない。

- [ ] **Step 6: frontend全体の回帰を確認する**

Run: `npm run test:frontend`

Expected: 全test PASS、unhandled rejectionやwarningなし。

Run: `npm run check:frontend`

Expected: Biome PASS。

- [ ] **Step 7: Task 4をcommitする**

```bash
git add tests/js/app.test.js src/moco/web/static/app.js
git commit -m "feat: integrate live audio device switching"
```

### Task 5: 品質ゲートと実機確認

**Files:**
- Modify only if a test exposes an in-scope defect.

- [ ] **Step 1: formatterを適用し、変更識別子の所有範囲を確認する**

Run: `just format`

Run: `rg -n 'AudioDeviceController|audio-input|audio-output|microphone_switch_failed|audio_output_switch_failed' src tests docs`

Expected: 新規識別子は設計済みのmedia controller、boot、UI、tests、docsにだけ存在する。

- [ ] **Step 2: 全ローカルゲートを実行する**

Run: `just check`

Expected: format、lint、mypy、dead-code、dependency、ast-grep、coverage、Playwright、secret scan、buildが全てPASS。

- [ ] **Step 3: 実サービスをbranch codeから一時起動してブラウザ確認する**

既存mainサービスは停止・上書きせず、別portのforeground processまたはテスト用serverで確認する。

確認項目:

- 権限取得後、INPUT/OUTPUTに実デバイス名とシステム既定が表示される。
- MIC OFFで入力変更後、MIC ONにして選択マイクが使われる。
- MIC ON中の入力変更で会話が終了せず、次の発話を同じThreadが処理する。
- Irodori再生先を変更しても再接続、二重synthesis、二重再生がない。
- 利用可能なら使用中デバイスを切断し、システム既定へ戻る。
- reload後、利用可能な保存済みIDが復元される。
- AudioContext sample rate 48 kHz、decoded sample rate 48 kHz、playbackRate 1、detune 0が維持される。

- [ ] **Step 4: 実機で確認できない条件を明示する**

複数の実入力または出力がない、OSが切断操作を許さない、ブラウザが`setSinkId`非対応の場合は、対応する
自動testの結果と実機で確認できなかった理由を最終報告へ記録する。音声・transcript・device labelを保存しない。

- [ ] **Step 5: 検証後の最終commitを作る**

formatによる変更または検証で見つかったin-scope修正がある場合だけcommitする。

```bash
git add src/moco/web/static/app.js src/moco/web/static/media-devices.js \
  src/moco/web/static/index.html src/moco/web/static/styles.css \
  tests/js/app.test.js tests/js/media-devices.test.js tests/e2e/mobile-console.spec.js
git commit -m "fix: close audio device switching gaps"
```

変更がなければ空commitは作らない。
