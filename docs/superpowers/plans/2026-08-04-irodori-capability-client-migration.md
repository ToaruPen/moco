# moco Irodori capability client 移行実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development and superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** moco の静的話者候補を廃止し、Irodori の runtime-derived catalog、generation、readiness を会話開始前に検証して、caption なしの v4-Small 音声を fail-closed で利用できるようにする。

**Architecture:** typed Irodori client が `GET /capabilities` を取得して generation と aliases を server memory にだけ保持する。browser には `{id,label,default}` と安全な readiness だけを渡し、選択した ID と generation を各 synthesis request に付与する。caption mode は初期移行では `off` だけとし、Codex Realtime v3、assistant transcript 分割、inline emoji、timeout なしの synthesis は維持する。

**Tech Stack:** Python 3.13、Pydantic 2、httpx、FastAPI WebSocket、vanilla JavaScript、pytest、Node test runner、Playwright、`uv`、`just`

---

## 前提、非目標、編集境界

- 設計の source of truth は `docs/superpowers/specs/2026-08-04-irodori-v4-dynamic-caption-migration-design.md`。
- Irodori の先行計画は `${IRODORI_CONTRACT_REPO}/docs/superpowers/plans/2026-08-04-capability-driven-voice-catalog.md`。`IRODORI_CONTRACT_REPO` は各作業者が自分の clone 先へ設定する。
- この計画は Irodori capability contract が accepted commit に存在するまで開始しない。未コミットの Irodori source を moco dependency として参照しない。
- `irodori.speakers` は削除する。`irodori.speaker` は portable name ではなく preferred voice ID または旧 alias として読み、catalog 取得後は canonical ID に解決する。
- production 話者名、12/13件、表示順を moco の code、config example、test expectation に固定しない。test catalog は helper が requested count から生成する。
- narrator は Irodori catalog の通常 entry であり、browser の空 value や `null` に特別な意味を持たせない。
- `calm`、`cheerful`、`clear` UI、自由記述 caption、`cfg_scale_caption` の browser/config 入力を追加しない。初期 `caption_mode` は `off` のみ。
- Realtime v3/audio、Codex の会話・作業、inline emoji、SpeechQueue の interruption、synthesis timeout なしは変更しない。
- Irodori checkpoint、tokenizer、hash、embedding path、generation token、aliases を browser へ出さない。
- 既存 dirty worktree を保持する。commit、deploy、service restart、voice bank replacement は明示指示なしに行わない。

## browser WebSocket 契約

`state` message は voice catalog を object 配列で返す。最初の state は非 blocking に `loading` を返し、capability 取得後に更新 state を送る。

```json
{
  "type": "state",
  "state": "ready",
  "voice": {
    "selected": "opaque-voice-id",
    "options": [
      {"id": "opaque-voice-id", "label": "表示名", "default": true}
    ],
    "ready": true,
    "readiness": "ready"
  },
  "conditioning": {
    "captionMode": "off",
    "deliveryCaptionSupported": false,
    "emojiSupported": true
  }
}
```

browser からの選択は canonical ID を必須にする。

```json
{"type":"select_voice","voice_id":"opaque-voice-id"}
```

browser readiness は Irodori の4状態に加え、moco-local の `loading`、`unavailable`、`capability_mismatch` を許す。aliases と generation は一切送らない。

## voice 選択規則

1. `irodori.speaker` が canonical ID ならそれを選ぶ。
2. canonical ID でなく、ちょうど一つの alias に一致すればその ID を選ぶ。
3. 設定値があるのに解決できなければ `configured_voice_unavailable`。default へ fallback しない。
4. 設定値がなく、catalog に default が一つあればそれを選ぶ。
5. 設定値も default もなければ UI 選択まで会話開始を `voice_selection_required` で拒否する。
6. catalog が空なら `voice_catalog_empty`。固定 narrator を挿入しない。
7. refresh 後に選択 ID が消えた場合は選択を無効化し、別話者へ切り替えない。

## ファイル構成

- Modify `pyproject.toml`, `uv.lock`: accepted Irodori contract commit へ pin。
- Modify `src/moco/config.py`: static speakers を削除し、`caption_mode="off"` を strict 化。
- Modify `src/moco/speech/irodori.py`: capability fetch/cache、canonical voice selection、generation-conditional synthesis。
- Modify `src/moco/web/messages.py`: browser voice ID contract。
- Modify `src/moco/web/app.py`: catalog refresh/poll、readiness gate、安全な browser projection、stable errors/telemetry。
- Modify `src/moco/web/static/app.js`: runtime options の描画、hardcoded narrator 削除、readiness disable。
- Modify `src/moco/web/static/index.html`: loading/empty state の accessible copy only if needed by the controller contract。
- Modify `src/moco/doctor.py`: capability/generation/default-aware probe。
- Modify `tests/test_config.py`, `tests/test_irodori.py`, `tests/test_web_messages.py`, `tests/test_web.py`, `tests/test_doctor.py`, `tests/test_integration.py`, `tests/js/app.test.js`。
- Modify `config/moco.example.yaml`, `README.md`: dynamic catalog と migration error を説明。

### Task 1: accepted Irodori contract pin を前提条件として固定する

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`

- [ ] **Step 1: Irodori contract が HEAD に commit 済みか検査する**

Run:

```bash
: "${IRODORI_CONTRACT_REPO:?set IRODORI_CONTRACT_REPO to the irodori-tts-infra clone path}"
git -C "$IRODORI_CONTRACT_REPO" diff --quiet -- \
  src/irodori_tts_infra/contracts/capabilities.py \
  src/irodori_tts_infra/contracts/synthesis.py \
  src/irodori_tts_infra/client/async_.py \
  src/irodori_tts_infra/client/sync.py
IRODORI_CONTRACT_COMMIT=$(git -C "$IRODORI_CONTRACT_REPO" rev-parse HEAD)
git -C "$IRODORI_CONTRACT_REPO" show \
  "$IRODORI_CONTRACT_COMMIT:src/irodori_tts_infra/contracts/capabilities.py" >/dev/null
```

Expected: 対象 contract/client files に HEAD 外の差分がなく、capability file が commit object に存在する。失敗した場合はここで停止し、ユーザーへ Irodori commit の明示指示を求める。

- [ ] **Step 2: pin を accepted commit へ更新する**

Run:

```bash
: "${IRODORI_CONTRACT_REPO:?set IRODORI_CONTRACT_REPO to the irodori-tts-infra clone path}"
IRODORI_CONTRACT_COMMIT=$(git -C "$IRODORI_CONTRACT_REPO" rev-parse HEAD)
uv add "irodori-tts-infra @ git+https://github.com/ToaruPen/irodori-tts-infra.git@${IRODORI_CONTRACT_COMMIT}"
```

Expected: `pyproject.toml` と `uv.lock` だけが新 commit を参照する。

- [ ] **Step 3: import surface を確認する**

Run:

```bash
uv run python -c 'from irodori_tts_infra.contracts import CapabilitiesResponse, SynthesisRequest; print(CapabilitiesResponse.__name__, SynthesisRequest.__name__)'
git diff --check pyproject.toml uv.lock
```

Expected: import 成功。commit、push はしない。

### Task 2: static speaker config を dynamic catalog preference へ縮小する

**Files:**
- Modify: `tests/test_config.py`
- Modify: `src/moco/config.py`
- Modify: `config/moco.example.yaml`

- [ ] **Step 1: config migration tests を RED で追加する**

次を検査する。

- `speaker` は strip した preferred ID/alias または `None`。
- `caption_mode` は `off` だけを受理する。
- 旧 `speakers` key は unknown key として明示的に拒否する。
- `available_speakers` property は存在しない。

- [ ] **Step 2: RED を確認する**

Run: `uv run pytest tests/test_config.py -k 'irodori and (speaker or caption or static)' -v`

Expected: static fields が残り、caption mode がなく FAIL。

- [ ] **Step 3: `IrodoriSettings` を最小変更する**

```python
class IrodoriSettings(StrictSettings):
    base_url: HttpUrl = HttpUrl("http://127.0.0.1:8923")
    connect_ip: IPvAnyAddress | None = None
    speaker: str | None = None
    caption_mode: Literal["off"] = "off"
    # Existing synthesis and transport settings remain unchanged.
```

`speakers` validator と `available_speakers` を削除する。config example の speaker comment は「catalog の preferred voice ID。null は catalog default」に変更し、候補配列を削除する。

- [ ] **Step 4: config tests を GREEN にする**

Run: `uv run pytest tests/test_config.py -q`

Expected: 全件 PASS。

### Task 3: typed capability cache と generation-conditional synthesis

**Files:**
- Modify: `tests/test_irodori.py`
- Modify: `src/moco/speech/irodori.py`

- [ ] **Step 1: runtime-generated catalog fixture を導入して RED にする**

test helper は requested count から `CapabilitiesResponse` を生成する。

```python
def make_capabilities(count: int, *, ready: bool = True) -> CapabilitiesResponse:
    voices = tuple(
        VoiceCapability(
            id=f"fixture-id-{index}",
            label=f"Fixture label {index}",
            aliases=(f"fixture-alias-{index}",),
            default=index == 0,
        )
        for index in range(count)
    )
    return CapabilitiesResponse(
        generation="fixture-generation",
        ready=ready,
        readiness="ready" if ready else "model_loading",
        voices=voices,
    )
```

次を検査する。

- `.capabilities()` が client response を strict validation して cache する。
- selected canonical ID と cached generation が synthesis request に入る。
- alias selection は canonical ID に正規化される。
- capability 取得前の select/synthesize、unknown ID、empty catalog は stable `IrodoriError`。
- client の `runtime_generation_mismatch`、`voice_not_found`、readiness codes をそのまま stable code として保持する。
- synthesis client は引き続き `timeout=None`、capability client は bounded timeout。
- request に `caption`、`style` preset、`cfg_scale_caption` を moco から設定しない。

- [ ] **Step 2: RED を確認する**

Run: `uv run pytest tests/test_irodori.py -q`

Expected: protocol/client/cache が capability を持たず FAIL。

- [ ] **Step 3: cache と選択を最小実装する**

`IrodoriClient` protocol へ `capabilities()` を追加する。`IrodoriSynthesizer` は `_capabilities` と `_voice_id` を保持し、次の public methods を提供する。

```python
async def capabilities(self) -> CapabilitiesResponse: ...
def select_voice(self, voice_id: str) -> None: ...
```

`synthesize()` は cache が ready かつ selected ID が現在の catalog に存在することを再検査し、次を送る。

```python
SynthesisRequest(
    text=text,
    voice_id=self._voice_id,
    if_generation=self._capabilities.generation,
    num_steps=config.num_steps,
    duration_scale=config.duration_scale,
    cfg_scale_text=config.cfg_scale_text,
    cfg_scale_speaker=config.cfg_scale_speaker,
)
```

generation、aliases、label は log/telemetry に出さない。WAV size/header checks と address override は維持する。

- [ ] **Step 4: synthesizer tests と structural search を GREEN にする**

Run:

```bash
uv run pytest tests/test_irodori.py -q
rg -n "CapabilitiesResponse|select_voice|if_generation|voice_id" src/moco/speech/irodori.py tests/test_irodori.py
```

Expected: 全件 PASS。new fields は Irodori boundary と owner tests に限定される。

### Task 4: browser message と dynamic selector を object catalog にする

**Files:**
- Modify: `tests/test_web_messages.py`
- Modify: `tests/js/app.test.js`
- Modify: `src/moco/web/messages.py`
- Modify: `src/moco/web/static/app.js`
- Modify: `src/moco/web/static/index.html` only if an accessible loading label cannot be expressed with the existing select.

- [ ] **Step 1: strict browser contract tests を RED にする**

Python は `{"type":"select_voice","voice_id":<nonblank>}` だけを受理し、`speaker` と `null` を拒否する。JavaScript は helper-generated options を描画し、次を検査する。

- label と value を分離する。
- server が返した順だけを表示し、hardcoded narrator/default option を挿入しない。
- empty/loading/unready では select を disable する。
- selection message は opaque ID をそのまま送る。
- selected ID が options にない場合は勝手に先頭へ fallback しない。

- [ ] **Step 2: RED を確認する**

Run:

```bash
uv run pytest tests/test_web_messages.py -q
npm test -- --test-name-pattern='VoiceModelController'
```

Expected: legacy `speaker`/string list/hardcoded narrator により FAIL。

- [ ] **Step 3: controller を runtime object projection に変更する**

```javascript
configure({ options, selected, ready }) {
  const choices = options.map(({ id, label }) => this.createOption(label, id));
  this.element.replaceChildren(...choices);
  this.selected = selected ?? "";
  this.element.value = this.selected;
  this.element.disabled = !ready || choices.length === 0;
}

select(value) {
  this.element.value = this.selected;
  this.send({ type: "select_voice", voice_id: value });
}
```

選択不能な placeholder が必要なら `disabled` option とし、voice ID として送信しない。

- [ ] **Step 4: browser unit tests を GREEN にする**

Run: `uv run pytest tests/test_web_messages.py -q && npm run test:frontend`

Expected: 全件 PASS、`NARRATOR / DEFAULT` literal が production JS から消える。

### Task 5: WebSocket lifecycle に capability refresh と fail-closed start を入れる

**Files:**
- Modify: `tests/test_web.py`
- Modify: `src/moco/web/app.py`

- [ ] **Step 1: lifecycle tests を generated catalog で RED にする**

`FakeSynthesizer` は test ごとに helper-generated capability を返す。assert は fixture object から期待 projection を組み立てる。次を検査する。

- connect 直後は voice readiness `loading`、取得後は safe catalog update。
- `model_loading` 中はbounded intervalでcapabilityを再取得し、ready/terminal readiness/socket closeで
  pollを止める。poll回数やcatalog内容をlogしない。
- browser projection に aliases/generation/path がない。
- config ID、unique alias、catalog default の選択規則。
- configured missing、empty catalog、no default、model_loading、model_not_loaded、voice_bank_invalid、contract version mismatch の stable errors。
- start 時に capability を再取得してから Realtime session を作る。
- refresh で選択 ID または generation が変わったら fail closed。
- mid-conversation generation mismatch は SpeechQueue error として browser へ出し、別 voice/v3へ fallback しない。
- idle expiry 後の next start は新 synthesizer と新 capabilities を使う。
- catalog names/count/order を test source に固定しない。

- [ ] **Step 2: RED を確認する**

Run: `uv run pytest tests/test_web.py -k 'voice or irodori or capability or generation' -v`

Expected: static settings projection と health-only start により FAIL。

- [ ] **Step 3: browser connection state を最小拡張する**

`_BrowserConnection` は `_voice_options`、`_voice_readiness`、`_selected_voice_id` を server memory に持つ。`run()` は最初の state を即時送信した後、bounded client で capability を取得し、更新 state を送る。`model_loading` の間は1秒intervalのlifespan-owned taskで再取得し、`ready`、`model_not_loaded`、`voice_bank_invalid`、socket closeのいずれかで停止する。close時はpoll taskをcancel/awaitする。network/contract failureでもsocket自体は維持し、voice操作だけをdisableする。

`_start()` は新 synthesizer を作り、capability を再取得し、選択規則と readiness を検査してから `session_factory()` と `session.start()` を呼ぶ。Irodori failure 時は Codex Realtime session を作らない。`health()` は診断互換のため残してもよいが、会話開始 gate の source of truth は capabilities とする。

safe telemetry は次だけを記録する。

```text
irodori_capabilities_received: contract_version, ready, readiness, voice_count
irodori_generation_mismatch: stable code
```

generation token、voice ID/label/aliases、caption、transcript を記録しない。

- [ ] **Step 4: WebSocket tests を GREEN にする**

Run: `uv run pytest tests/test_web.py -q`

Expected: 全件 PASS。既存 transcript、activity、interruption、idle cleanup の回帰も通る。

### Task 6: doctor と end-to-end boundary を catalog-aware にする

**Files:**
- Modify: `tests/test_doctor.py`
- Modify: `tests/test_integration.py`
- Modify: `src/moco/doctor.py`

- [ ] **Step 1: doctor/integration tests を RED にする**

doctor は `irodori_capabilities` check を追加し、設定 ID/alias/default 規則で一つの voice を選んで synthesis probe する。catalog が空、unready、configured missing、generation mismatch を別の stable detail にする。名前、count、generation は rendered output に含めない。

integration は fake Irodori capability → browser state → UI selection → conditional synthesis request → WAV deliveryを通し、fixture catalog から期待値を導出する。

- [ ] **Step 2: RED を確認する**

Run: `uv run pytest tests/test_doctor.py tests/test_integration.py -q`

Expected: doctor fake/client と integration protocol が health/speaker-only で FAIL。

- [ ] **Step 3: doctor と fakes を capability-aware にする**

doctor の error detail は `catalog_empty`、`configured_voice_unavailable`、`model_loading` など bounded code のみとする。synthesis probe は selected canonical ID と response generation を使う。fallback speaker は定義しない。

- [ ] **Step 4: GREEN を確認する**

Run: `uv run pytest tests/test_doctor.py tests/test_integration.py -q`

Expected: 全件 PASS、doctor output に catalog content がない。

### Task 7: 文書、全 gate、非配備 handoff

**Files:**
- Modify: `README.md`
- Modify: `config/moco.example.yaml`

- [ ] **Step 1: operator migration を文書化する**

README へ次を記載する。

- selector は Irodori runtime catalog 由来で、moco config に候補を列挙しない。
- `speaker` は preferred canonical ID。旧 alias は一意な場合だけ migration resolution する。
- `speakers` key を残すと strict config validation が失敗するため削除する。
- caption mode は `off`、表情は inline emoji のみ。
- readiness/generation/voice mismatch で音声は止まり、別 voice や v3 へ自動 fallback しない。
- synthesis timeout は引き続き設けない。

- [ ] **Step 2: production-name pin を機械検索する**

Run:

```bash
rg -n "NARRATOR / DEFAULT|available_speakers|speakers:" src tests config README.md
rg -n "アイ|ミウ|12[^0-9]*(voice|speaker|話者)|13[^0-9]*(voice|speaker|話者)" src tests config README.md
```

Expected: legacy static selector identifiers/literals は削除済み。実話者名と12/13件の期待値は存在しない。数字が無関係な設定値として出る場合は意味を確認する。

- [ ] **Step 3: repository gate を実行する**

Run:

```bash
just check
git diff --check
git status --short
```

Expected: 全 gate PASS。開始前の dirty changes と本計画対象だけが残り、commit は作られていない。

## 非配備の接続確認と rollback review

repository 実装後も標準 service へ接続しない。Irodori の隔離 v4 service が別承認で準備された後に限り、次を実施する。

1. moco の一時設定から隔離 URL と preferred voice ID を指定する。
2. `just doctor` で capabilities、readiness、selected voice、WAV を確認する。
3. browser が runtime catalog だけを表示し、全 entry を選択できることを runtime-derived loop で検査する。
4. representative transcript と inline emoji を caption なしで合成し、first audio latency と中断を確認する。
5. generation を意図的にずらした隔離 test で無音・stable error・no fallback を確認する。
6. 標準 v3/v4 切替や voice bank replacement は自動実行せず、測定結果と alias review を提示して別の配備承認を得る。

rollback は moco code の旧 static speaker list へ戻すことではない。承認済み Irodori runtime+voice-bank generation を復元し、moco が新 capability を取得できるまで voice を無効化する。

## 完了条件

- static `speakers` config と hardcoded narrator option がない。
- browser options は runtime response から構築され、production names/count/order を固定する test がない。
- configured ID/alias/default の選択規則が fail-closed で型付けされている。
- synthesis は canonical voice ID と generation を毎回送る。
- caption mode は `off`、自由記述 caption と非公式 preset UI は未実装。
- capability mismatch、missing voice、generation mismatch、cold-start readiness が stable code で見える。
- `just check` が通る。
- deploy、restart、voice-bank replacement、commit は行われていない。
