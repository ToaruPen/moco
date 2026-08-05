# moco Irodori-TTS v4 capability-driven 移行と動的キャプション評価設計

> **状態:** 2026-08-04、v4 の初期移行設計と隔離 WebRTC probe の実施方針は承認済み。
> Codex Realtime v3 では text output modality を利用できないことが確認されたため、初期移行は
> caption なしと inline emoji を基準線とする。動的キャプションの production 追加は、probe の
> gate と別の設計承認を通るまで未承認とする。

## 目的

moco の会話と作業は引き続き Codex Realtime v3 が所有し、最終的に聞こえる日本語音声だけを
Irodori-TTS v4-Small で生成する。話者選択は Irodori が返す実行時 catalog を正とし、moco
やテストへ話者名、件数、順序を固定しない。

v4-Small の自由記述 caption は有用だが、会話の滑らかさや Codex v3 の作業能力を犠牲にして
まで初期移行へ含めない。まず caption なしと既存の inline emoji で v4 の runtime、voice
catalog、Speaker Inversion 品質を検証する。動的 caption は、同じ v3/WebRTC 境界で本文との
相関と遅延を実証できた場合にだけ、別の承認を経て追加する。

## 確認済みの事実

- Irodori-TTS v4-Small は text、reference speech、caption を一つのモデルで扱う。
- caption は声の特徴、感情、話し方、発話表現を記述する自由文であり、`calm`、
  `cheerful`、`clear` は v4 の公式 enum ではない。これらは現行 infra が便宜的に定義した
  preset である。
- reference speech または Speaker Inversion embedding と矛盾する話者属性を caption に
  含めると、音質が不安定になったり一方の条件が優先されたりする可能性がある。
- Speaker Inversion embedding は学習時と同じ base model と組み合わせる必要がある。
- 現行 moco は assistant transcript を文単位に分割すると、caption を待たず直ちに Irodori
  合成へ投入する。
- 現行 moco は Realtime の `outputModality` に `audio` を指定しているが、GPT 音声を再生
  せず、その transcript を Irodori へ渡している。
- Codex App Server の生成スキーマは `text` output modality を列挙するが、2026-08-04 の
  実接続では Realtime v3 が `text realtime output modality requires realtime v2` として
  拒否した。現行 moco を v3 のまま text output へ切り替える設計は成立しない。
- App Server の websocket transport を使った隔離 probe は、ChatGPT 認証経路で Realtime
  websocket への接続が `401 Unauthorized` となった。製品と同じ WebRTC transport を通さない
  latency 検証は代替にならない。
- 公式 Realtime API の out-of-band `response.create` や function calling は、現在の App
  Server RPC には型付き操作として公開されていない。
- ブラウザは `oai-events` data channel を作成しているが、現行コードは保持、購読、送信を
  行っていない。
- 現行 Irodori client payload は v4 対応中の infra 契約でも caption なしとして受理できる。
  合成 timeout は意図的に設けられていない。
- moco の静的 UI 候補は 13 名だが、v4 学習成果物の model ID は 12 件である。アイ、ミウ、
  narrator を含む対応関係は配備前に Irodori 所有データとして明示する必要がある。

### 2026-08-04 隔離 probe の記録

ChatGPT.app 同梱 `codex-cli 0.146.0-alpha.9.2` を一時起動し、ephemeral、read-only、
approvalなしの thread に固定した非機密入力を送った。transcript、caption、audioは保存して
いない。

| version | transport | output | 結果 |
|---|---|---|---|
| v3 | App Server websocket | text | `text realtime output modality requires realtime v2` |
| v3 | App Server websocket | audio | Realtime websocket接続が`401 Unauthorized` |

この結果は WebRTC の成否を示さない。ただし、v3 text output と websocket-only latency probeを
前提にした設計は否定する。

### 2026-08-05 closed preset selector follow-up

同じv3/audio応答の先頭から閉じたpresetだけを取得する案も隔離probeで比較した。非発話の
emoji selectorと短い発話可能なselector文を各10試行したが、どちらもassistant transcriptで
selectorを0件しか得られなかった。prompt追加によるfirst segment latencyには一貫した中央値悪化が
なかったものの、選択成功率0%なのでproduction契約として成立しない。自由captionを閉じたenumへ
狭めるだけでは、App Server v3に型付き制御境界がない問題は解消しない。

別途、既存v4-Small評価の`neutral`/`calm` 770ペアを再集計した結果、Irodori合成時間のペア差は
中央値-5.5ms、p95 +75.6msだった。したがって現在の主な阻害要因は固定captionを与えたIrodori
推論時間ではなく、Realtime応答から選択結果を確実に取得できないことである。動的presetを必須に
する場合は、別の型付きclassifier呼び出しまたは会話protocol変更を別設計として評価する。

## 対象範囲

- Irodori が runtime generation、voice catalog、caption/emoji 能力、readiness を返す。初期
  runtime は `delivery_caption.supported=false` を広告する。
- moco が voice catalog を実行時に取得し、ブラウザの選択肢を動的に構成する。
- capability、話者、generation の不一致を安定したエラーコードで可視化する。
- v4 を隔離検証し、明示的な判断後にだけ標準経路へ昇格する。
- caption なしと inline emoji で v4 の初期移行を成立させる。
- 動的 caption 候補を production から隔離した WebRTC probe で比較する。

## 対象外

- checkpoint path、snapshot revision、tokenizer、hash、embedding path の moco または
  browser への公開
- GPT が Irodori の `cfg_scale_caption` や sampling parameter を決めること
- `calm`、`cheerful`、`clear` を moco の固定 enum または固定 UI として採用すること
- caption の保存、会話 transcript の永続化、音声の telemetry 出力
- Realtime v2 への移行と、それに伴う Codex v3 handoff 能力の変更
- 検証未完了の動的 caption を v4 初期移行の必須条件にすること
- OpenAI 互換 Irodori server への全面移行
- 学習成果物の自動配備、voice bank の自動置換、v3 への無通知 fallback
- 話者名、話者数、表示順をテストの期待値として固定すること

## 検討した方式

### A. caption なしと capability-driven voice catalog で移行する

caption なしで v4-Small と v4 用 embedding へ切り替え、話者候補だけは Irodori の
capability response から取得する。caption 同期の新しい失敗点がなく、v4 runtime、readiness、
voice catalog、話者同一性の検証に集中できる。本文中の対応済み emoji は従来どおり Irodori
へ渡す。

この方式を v4 初期移行として採用する。自由記述 caption は利用しないが、静的話者設定と
voice bank の乖離は解消できる。動的 caption が後続検証を通らなくても、この構成だけで安全な
v4 移行と rollback が成立する。

### B. v3 audio 応答の transcript 先頭に発話計画を付ける

現行と同じ v3/audio 応答に、機械可読な発話計画を最初に発声させ、その transcript から制御行
を除いて Irodori へ渡す。同じモデル応答なので本文との意味的相関は高い。GPT 音声自体は
moco が再生しないため、制御行が GPT 音声に含まれることは直接の漏出にはならない。

ただし、audio transcript が JSON の記号、改行、`null` を忠実に保持する保証はない。制御行の
発声時間も初回本文を遅らせる。本方式は WebRTC probe の第一候補とするが、採用はしない。

### C. out-of-band 応答または function call で caption を作る

公式 Realtime API は、会話へ追加しない text-only 応答を並行生成できる。しかし現行 App
Server RPC はその操作を公開しておらず、browser data channel から直接送ると App Server
所有境界を迂回する。本文と caption が別応答になるため、順序、相関、費用、遅延も増える。

function call は型付き引数を得られるが、tool result を返して本文生成を再開する往復が必要に
なる。音声対話の各 turn に必須とする方式には採用しない。

### D. Realtime v2 の text outputへ切り替える

v2 なら App Server の text output 条件を満たす可能性がある。しかし moco は Codex の会話と
作業 handoff を含む v3 を採用しており、caption のために session protocol を下げると製品の
中心能力を変える。v2 の機能同等性を別設計で証明しない限り採用しない。

### E. OpenAI 互換 Irodori server へ移行する

HTTP 形式を OpenAI Audio API に寄せても、GPT の発話意図と caption をいつ確定するかという
同期問題は解決しない。現行製品に必要な voice catalog、generation pin、readiness の契約も
別途必要になるため、本移行では採用しない。

## 責務境界

```text
Codex Realtime v3
  └─ assistant transcriptとinline emoji
              │
              ▼
moco
  ├─ 本文を表示・文分割
  ├─ voice catalogから選ばれたopaque voice idを保持
  └─ generation条件付きでIrodoriへ合成要求
              │
              ▼
Irodori HTTP
  ├─ runtime generationとreadinessを所有
  ├─ voice id・aliasからv4 embeddingを解決
  ├─ caption/emoji能力と制約を所有
  ├─ cfg_scale_captionを含むmodel parameterを所有
  └─ checkpoint・tokenizer・voice-bank assetを非公開に保つ
```

moco は Irodori のモデル成果物を知らない。Irodori は会話文脈を知らず、初期移行では本文と
voice id だけを受け取る。delivery caption の field 自体は WebRTC probe と再承認が済むまで
production HTTP 契約へ追加しない。

## Realtime 発話計画の候補契約

以下は WebRTC probe の比較対象であり、production 契約ではない。v3 audio transcript で形式
遵守と遅延 gateを通過し、再承認された場合にだけ moco の型付き境界へ昇格する。

assistant 応答は UTF-8 の一行 JSON から始める。JSON の直後に改行を一つ置き、以降を
読み上げ本文とする。

```json
{"type":"moco.speech_plan","version":1,"delivery_caption":"落ち着いて、親しみを込め、自然な速さで話す。"}
```

neutral が適切な場合は `delivery_caption` を `null` とする。

```json
{"type":"moco.speech_plan","version":1,"delivery_caption":null}
```

契約は次を満たさなければならない。

- 制御行全体は 256 Unicode code point 以下とする。
- `delivery_caption` は `null` または、前後空白を除いて 1 から 80 code point とする。
- caption は改行、Unicode control、`<`、`>` を含めない。
- caption は感情、テンポ、強さ、距離感、発話姿勢だけを記述する。
- 話者名、人物名、性別、年齢、方言、基礎声質を指定しない。
- 本文は空にせず、JSON や Markdown として包まない。
- inline emoji は本文にだけ置き、caption には置かない。

probe は改行が届くまで本文候補を保持する。制御行を検証した後は、同じ delta に含まれた本文
から直ちに文分割時刻を測る。assistant turn が終了するまで caption は変更しない。production
moco に同じ状態機械を入れるのは再承認後とする。

制御行がない場合は、最初の非空文字が `{` でなければ本文として扱い、caption なしで継続
する。`moco.speech_plan` として始まった行が malformed、長すぎる、unsupported version の
場合は制御行を読み上げず、本文だけを caption なしで継続し、`speech_caption_invalid` を
通知する。劣化は可視であり、別 style や別話者へは fallback しない。

## Irodori capability 契約

`GET /capabilities` は browser 向けの安全な情報だけを返す。配列の内容、件数、順序は runtime
data であり、moco のコードまたはテスト契約ではない。

```json
{
  "contract_version": 1,
  "generation": "v4-small-2026-08-04-a",
  "ready": true,
  "readiness": "ready",
  "voices": [
    {
      "id": "voice_opaque_01",
      "label": "表示名",
      "aliases": ["旧表示名"],
      "default": false
    }
  ],
  "conditioning": {
    "delivery_caption": {
      "supported": false,
      "max_chars": null
    },
    "emoji": {
      "supported": true
    }
  }
}
```

- `generation` は active runtime と voice bank の組を表す opaque token とする。
- `readiness` は `ready`、`model_loading`、`model_not_loaded`、`voice_bank_invalid` のいずれか
  とする。
- `id` は portable な公開識別子であり、file path、model ID、embedding hash を含めない。
- `label` と `aliases` は表示・旧設定解決用であり、embedding の数と一対一である必要はない。
- alias が複数 voice に解決される catalog は Irodori 起動時に拒否する。
- narrator も一つの catalog entry として表現し、moco に特別な `null` 意味論を持たせない。
- browser には `id`、`label`、`default`、caption/emoji support、readiness だけを渡す。
  aliases と generation は moco server 内に保持する。

`ready=false` の間も安全な readiness と catalog metadata は返せる。moco は voice selector を
表示できるが、会話開始と合成を無効にし、理由を安定した表示文へ変換する。

## Irodori 合成契約

v1-aware client は取得した `generation` を各要求へ反映する。初期契約には自由記述 caption の
field を設けない。

```json
{
  "text": "承知しました。まず現在の状態を確認します。",
  "voice_id": "voice_opaque_01",
  "if_generation": "v4-small-2026-08-04-a",
  "num_steps": 40,
  "duration_scale": 1.0,
  "cfg_scale_text": 3.0,
  "cfg_scale_speaker": 5.0
}
```

`cfg_scale_caption` は active model の安全な既定値として Irodori 設定が所有し、moco、GPT、
browser の入力にはしない。動的 caption が後日承認された場合は、その時点の Irodori
`AGENTS.md` と設計を更新し、caption の構文、上限、error code、Speaker Inversion embedding
との同時 conditioning を新しい contract version として定義する。

移行期間中は、`voice_id`、`if_generation` を送らない旧 client payload を caption なしとして
受理する。この互換窓は Irodori contract commit を作成し、moco の dependency pin を更新し、
rollback 手順を確認するまでに限定する。`caption` を含む unknown field は引き続き拒否する。

HTTP error と安定コードは次のとおりとする。

| HTTP | code | 意味 |
|---:|---|---|
| 409 | `runtime_generation_mismatch` | capability取得後にactive generationが変わった |
| 404 | `voice_not_found` | voice idまたは一意なaliasを解決できない |
| 503 | `model_not_loaded` | runtimeが合成可能なreadinessに達していない |
| 503 | `voice_bank_invalid` | catalogとembeddingの整合性がない |

`caption_invalid` と `caption_unsupported` は初期 contract の error code ではない。動的 caption
が再承認された場合に、新しい contract version とともに定義する。

moco は error message を browser へ転送せず、code だけを表示・telemetry 化する。generation、
voice の不一致で v3、別 speaker、固定 style へ自動 fallback しない。将来 caption が承認された
場合も同じ fail-closed 原則を適用する。

## moco の設定、UI、状態

静的な `irodori.speakers` は capability-driven catalog へ置き換える。移行中は設定を読み込める
期間を設けても、UI候補の正としない。

- `irodori.speaker` は portable label ではなく catalog の `voice_id` を保存する。
- 起動時に設定済み ID が catalog にない場合は `configured_voice_unavailable` とし、default
  voice へ黙って切り替えない。
- `irodori.caption_mode` は初期移行で `off` だけを受理する。動的 caption の再承認時に
  `auto` を追加し、preset 名は持たない。
- browser の voice selector は capability response ごとに再構成する。
- 動的 caption が再承認された場合、UI は `表現: 自動`、`表現: 標準`、`caption劣化` の状態
  だけを表示し、自由文 caption は通常画面へ表示しない。

動的 caption が承認された場合にだけ、assistant turn 内部状態へ `awaiting_plan`、
`streaming_body`、`done` を追加する。新しい user utterance、conversation close、idle expiry
は未完了 plan、caption、本文 buffer、SpeechQueue generation を同時に無効化する。旧 turn
の caption が次の応答へ再利用されてはならない。

## 観測性とプライバシー

capability-driven移行で追加するtelemetryと、動的caption再承認時に追加するtelemetryはmetadata
に限定する。

- `speech_plan_received`: version、caption_present、plan_chars、duration_ms
- `speech_plan_invalid`: stable code、buffer_chars、duration_ms
- `caption_mode_selected`: `off` または `auto`
- `irodori_capabilities_received`: contract_version、ready、readiness、voice_count
- `irodori_generation_mismatch`: stable code
- `speech_first_body_delta`: user_done からの duration_ms
- `speech_first_audio`: user_done からの duration_ms

caption本文、発話本文、voice label、aliases、audio、prompt、generation token は telemetry へ
含めない。voice_count は許可するが、catalog の具体的な名前は記録しない。

## 段階移行と rollback

1. Irodori に capability、voice catalog、generation 条件を TDD で追加する。caption は
   unsupported として広告し、標準 voice bank と service 設定は変更しない。
2. v4 隔離 service で、実行時 catalog に含まれる全 voice を caption なしで合成する。voice
   名、件数、順序は検査コードへ固定せず、取得した catalog を反復する。
3. WAV、話者同一性、RTF、初回音声、最大VRAM、readiness、終了後GPU解放を確認
   する。アイ、ミウ、narrator を含む旧 UI 名の alias 解決表は、配備データの review item として
   人手承認する。
4. Irodori contract commit を作成した後にだけ、moco dependency をその commit へ pin する。
5. moco を `caption_mode=off` で v4 に接続し、voice catalog と caption なしの合成を確認する。
6. v4 初期移行とは別に、browser WebRTC の非配備 probe で v3/audio 発話計画と data-channel
   out-of-band caption を比較する。App Server websocket transport の結果は latency 証跡に
   使わない。
7. 動的 caption の gate を通した場合も自動昇格せず、方式、API、遅延結果を再提示して承認を
   得る。承認後にだけ `caption_mode=auto` を実装する。

初期移行は `caption_mode=off` なので、caption固有のrollbackを必要としない。v4自体を戻す
場合は承認済みv3 service設定とvoice bankへ明示的に切り替え、mocoは新しいcapabilityを再取得
する。generation不一致中は音声を出さず、切替完了を待つ。

## テスト方針

### 単体・契約テスト

- capability fixtureはテストごとに任意の0件、1件、複数件catalogを生成する。具体的な話者名、
  12件、13件、配列順をassertしない。
- alias一意性、default最大1件、opaque id一意性、空label拒否を構造として検査する。
- mocoは取得した任意のvoice idをそのまま合成要求へ渡し、消失時にfail closedすることを
  検査する。
- Irodoriはvoice idをserver-side embeddingへ解決し、pathをresponseへ出さないこと、および
  初期contractがcaption fieldを拒否することを検査する。
- probe専用speech plan parserは1文字ごとのdelta分割、同一delta内のplan＋本文、`null`
  caption、80文字境界、malformed JSON、truncation、unsupported version、control文字で検査する。
- probeでは制御行が本文previewへ一文字も漏れず、新しいuser utteranceで未完了planとcaptionを
  破棄することを検査する。production SpeechQueueへの伝搬testは再承認後の実装計画へ含める。

### 隔離ライブ検証

固定した非機密の日本語入力を使い、製品と同じ v3/WebRTC transport 上で次を比較する。

- 現行 audio/transcript と inline emojiだけ
- v3/audio transcript の先頭に発話計画を含める方式
- browser data channelからout-of-band text captionを並行要求する方式

transcriptとcaptionは保存せず、run-local memoryだけで形式と相関を判定し、出力証跡には集計値
とtimestampだけを残す。v2 text modalityとApp Server websocket transportは、現行製品境界と
異なるため比較対象にしない。

初期gateは次のとおりとする。

- Irodoriへ渡る本文または画面への`moco.speech_plan`制御行漏出が0件
- 100 turn以上でplan形式成功率99%以上
- user input完了から最初の読み上げ本文確定までのp95悪化が300ms以内
- captionが本文の感情・発話姿勢と矛盾する重大例が人手確認で0件
- interruption後に旧turnのcaptionまたは本文が合成される件数が0件
- out-of-band方式では、captionが最初の本文segmentより先に揃う割合99%以上

gateを満たさない場合は production実装へ進まず、`caption_mode=off` とinline emojiだけをv4
移行の候補として維持する。

### end-to-end

- `GET /capabilities` で取得した全voiceを反復し、captionなしを各1件以上合成する。初期
  runtimeはdelivery caption非対応を広告し、caption付き合成は実行しない。
- catalogが空なら明示的に失敗し、固定の代替話者を挿入しない。
- 各responseで完全なRIFF/WAVE、非空audio、有限duration、readiness維持を検査する。
- cold startでは`model_loading`から`ready`への遷移を観測し、ready前の合成が
  `model_not_loaded`になることを検査する。
- 通常gateは各repositoryの指示に従い、mocoでは`just check`を実行する。

## リスク

### 発話計画の先頭待ち

別モデル往復はないが、caption一行の生成時間だけ初回本文が遅れる。80文字上限でもgateを
超える場合は、上限を短くして品質を再評価するか、auto captionを採用しない。測定なしに
「十分速い」と判定しない。

### v3とtext modalityの非互換

App Server schemaのenumだけを見るとv3でもtextを使えるように見えるが、実接続はv2を要求
した。captionのためにv2へ下げるとCodex v3の会話・作業handoffへ影響するため、本設計では
audio v3を維持する。将来App Serverがv3 text outputを提供した場合は、binary versionをpinし、
生成schemaとlive probeの両方を再実行する。

### captionと話者identityの衝突

prompt制約だけで意味的違反を完全には防げない。captionは短くdeliveryへ限定し、Irodoriの
構文検証と人手サンプル評価を組み合わせる。自由文をbrowser入力として公開しない。

### voice catalogの変化

追加、削除、alias変更は通常のruntime data変更である。テストを固定しない代わりに、ID一意性、
alias非曖昧性、configured voice存在、全entry合成という不変条件をgateにする。旧13表示名から
現行catalogへの対応表は配備データとして明示し、テストコードへ写さない。

### cold start

v4のmodel loadが長い場合、TCP接続やHTTP 200だけをreadyとみなすと会話開始後に無音になる。
capabilityのreadinessとgenerationを会話開始前に確認し、readyになるまで音声操作を無効にする。
合成自体には従来どおりdeadlineを設けない。

## 完了条件

- Irodori capabilityと合成contractがcommitされ、mocoがそのcommitへpinされる。
- runtimeから取得した全voiceがcaptionなしで検証される。
- 旧UI名とvoice catalogの対応表が配備データとして人手承認される。
- v4のWAV、話者同一性、遅延、VRAM、health/readinessを確認する。
- rollback手順を実行前レビューし、明示的な配備判断を得る。
- production service、voice bank、標準generationは承認前に変更されない。

動的captionの追加完了条件は別に扱う。

- v3/WebRTC隔離ライブ検証gateをすべて通過する。
- 本文とのcaption相関、追加遅延、課金・usage、制御行漏出リスクを方式ごとに比較する。
- 採用方式と更新後のAPIを再提示し、実装前に明示的な承認を得る。
