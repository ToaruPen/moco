# Irodori 動的 delivery caption クライアント設計

**日付:** 2026-08-18  
**状態:** 承認済み  
**対象:** moco の Irodori capability、speech plan、音声合成要求  
**非対象:** Windows 上の Irodori ランタイム、モデル、Tailscale Serve

## 背景

Windows 上で稼働する Irodori は、`GET /capabilities` で delivery caption 対応を次のように
広告している。

```json
{
  "conditioning": {
    "delivery_caption": {
      "supported": true,
      "max_chars": 300
    }
  }
}
```

現行 moco が固定する `irodori-tts-infra` クライアント契約は
`supported=false / max_chars=null` のみを受理する。このため、Tailscale 経路と Irodori
本体が正常でも、moco doctor は `capability_mismatch` で停止する。

2026-08-04 の動的 caption 移行設計では `caption_mode=auto` を再承認まで保留していた。
今回、次の範囲が明示的に承認された。

- Windows 上の Irodori 契約へ moco を合わせる。
- `delivery_caption` の構造検証と合成要求への送信まで実装する。
- caption の文字数上限は capability の `max_chars` に追従する。
- 上限は caption だけへ適用し、読み上げ本文には適用しない。
- 不正な speech plan は制御行だけを破棄し、本文を caption なしで継続する。
- Codex delta の先行読み上げは行わず、現在の確定回答経路を維持する。

## 目標

1. moco が delivery caption 対応 Irodori の capability を型付き境界で受理できる。
2. `caption_mode=auto` で、Codex の確定回答先頭にある speech plan を一度だけ解析できる。
3. 制御行を画面と読み上げ本文へ漏らさず、検証済み caption を全音声文節へ送れる。
4. 無効な plan、取消、次ターンへの状態漏れを安定した挙動で処理できる。
5. caption や本文そのものをログと telemetry に残さない。

## 非目標

- Codex の途中 delta を読み上げて初回音声を早めること。
- caption の自然言語内容を moco が意味解析すること。
- caption の preset、履歴、ユーザー編集 UI を追加すること。
- AGENTS.md へ speech plan 生成を強制する規則を追加すること。
- Windows 上の Irodori ソース、モデル、Scheduled Task、Serve 設定を変更すること。
- capability にない値へ自動 fallback すること。

## 採用方式

### 確定回答の Web 境界で speech plan を解析する

現在の moco は Codex の途中 delta ではなく、確定した `TurnResult.final_answer` を画面表示と
SpeechQueue へ渡す。分岐前の `_BrowserConnection.on_turn_finished` で一度だけ解析する。

```text
TurnResult.final_answer
  -> SpeechPlanParser
     -> body -----------------> browser transcript
     -> body + caption -------> SpeechQueue
                                 -> IrodoriSynthesizer
                                    -> POST /synthesize
```

SpeechQueue 内で解析すると、表示経路へ未処理の制御行が残る。Codex delta を解析すると、現在
採用していない途中出力、確定回答との差分、取消、重複除去まで同時に実装する必要がある。
確定回答境界は、表示と読み上げの本文を同一に保ちながら変更範囲を最小化できる。

### moco が使用する Irodori 契約を moco 側で所有する

公開済み dependency commit は Windows ランタイムの delivery caption 契約を表現できない。
未公開・未コミットの dependency 状態へ依存せず、moco が実際に使用する capability と
synthesis request の境界型を `moco.speech` 配下で所有する。

capability は moco の bounded HTTP transport で `GET /capabilities` を取得し、moco 所有型へ
厳密に検証する。health と synthesis response は既存クライアントを引き続き使用する。
synthesis request は既存 request 契約を拡張し、`delivery_caption` だけを追加する。Windows
Irodori が返す WAV response 形式は現行契約と同じため、response の置換は行わない。

## Speech plan 契約

`caption_mode=auto` の場合、確定回答の先頭の非空行を検査する。最初の非空文字が `{` で
なければ plan なしの通常本文として扱う。

有効な plan は一行 JSON とする。

```json
{"type":"moco.speech_plan","version":1,"delivery_caption":"落ち着いて、親しみを込めて話す。"}
```

標準表現を明示する場合は `null` を使う。

```json
{"type":"moco.speech_plan","version":1,"delivery_caption":null}
```

検証規則は次のとおりとする。

- top-level は JSON object とする。
- `type` は厳密に `moco.speech_plan` とする。
- `version` は strict integer `1` とする。`true` や `1.0` は受理しない。
- `delivery_caption` は `null` または文字列とする。
- 未知フィールドと重複フィールドを拒否する。
- 文字列は前後空白を除去した後に空であってはならない。
- code point 数は capability の `max_chars` 以下とする。
- Unicode control character、改行、`<`、`>` を拒否する。
- 読み上げ本文は空であってはならない。

moco は caption の意味、感情表現の適切さ、人物属性を自然言語解析しない。そのような生成方針
は、機能を使用する環境の AGENTS.md などで後から指定する。

## 無効 plan の扱い

先頭の非空文字が `{` で plan 候補になった後、JSON または型検証に失敗した場合は次の結果に
する。

- plan 候補の一行を画面と読み上げから除去する。
- 後続本文を `delivery_caption=None` で表示・読み上げする。
- browser へ `speech_caption_invalid` を一度通知する。
- caption 内容、制御行、本文をログへ出さない。

plan のない通常本文と、明示的な `delivery_caption=null` はエラーではない。

## Capability と設定

`IrodoriSettings.caption_mode` は `off | auto` を受理し、既定値は `off` のまま維持する。

- `off`: speech plan を解析せず、caption を送らない。
- `auto`: capability が広告した `max_chars` で plan を検証し、caption を送る。

`auto` なのに `delivery_caption.supported=false` または有効な `max_chars` がない場合は、会話開始
を `caption_unsupported` で停止する。別 style や caption なしへ黙って切り替えない。

browser state の conditioning は静的値をやめ、次を実データから返す。

- `captionMode`: 設定された `off` または `auto`
- `deliveryCaptionSupported`: capability の値
- `emojiSupported`: capability の値

## 音声キューと合成

SpeechQueue は解析済み caption だけを受け取る。caption は一つの assistant turn 内で固定し、
分割された全 `_SpeechItem` へコピーする。

IrodoriSynthesizer は合成直前に次を確認する。

- capability が読み込み済みである。
- caption を付ける場合、delivery caption が supported である。
- caption の code point 数が現在の `max_chars` 以下である。

検証後、`SynthesisRequest.delivery_caption` へ値を設定する。`text` の既存分割、voice ID、runtime
generation、12 steps、sway、cfg scale は変更しない。

取消、user transcript、queue close は既存の generation invalidation で caption 付き item も
破棄する。caption を queue 全体の永続設定として保持しないため、次ターンへ再利用されない。

## 観測性とプライバシー

追加 telemetry は metadata だけに限定する。

- `speech_plan_received`: version、caption_present、plan_chars
- `speech_plan_invalid`: stable error code、plan_chars
- `caption_mode_selected`: `off | auto`

次の値を telemetry、通常ログ、browser state へ含めない。

- delivery caption 本文
- 読み上げ本文
- plan 制御行
- voice label と aliases
- runtime generation token
- WAV bytes

## オーバーヘッド

Web 境界の解析は、先頭一行の改行検索、JSON decode、型検証、文字列分離だけである。現行経路
は確定回答を待ってから読み上げるため、plan 行を待つ追加 latency はない。capability は会話
開始時に取得してキャッシュし、各文節で再取得しない。

Irodori サービス層は caption の有無にかかわらず一つの `SamplingRequest` を作り、一回の
`runtime.synthesize` を呼ぶ。caption conditioning の内部計算は増え得るが、別 HTTP request や
二回目の service-level synthesis は追加しない。実配備後は同一本文・voice・sampling settings
で caption なし／ありを交互に実行し、response の `elapsed_seconds` を比較する。

## エラーコード

| code | 条件 | 挙動 |
|---|---|---|
| `caption_unsupported` | `auto` だが capability が非対応 | 会話開始を停止 |
| `speech_caption_invalid` | plan の JSON、型、文字、長さ、本文が不正 | plan 行を除去し本文を caption なしで継続 |
| `runtime_generation_mismatch` | capability 後に runtime generation が変化 | 既存どおり合成を停止 |

## テスト方針

すべての挙動変更は Red -> Green -> Refactor で追加する。

1. capability contract
   - `supported=true / max_chars=300` を受理する。
   - supported と max_chars の不整合を拒否する。
   - address override 経由の raw capability response を bounded に取得する。
2. configuration
   - `off` を既定値として維持する。
   - `auto` を受理し、未知値を拒否する。
3. speech plan
   - 有効、`null`、plan なしを区別する。
   - malformed、重複 key、unknown field、unsupported version、超過、禁止文字を拒否する。
   - 制御行を本文へ残さない。
4. web integration
   - 表示 transcript と読み上げ本文が同じ body になる。
   - 無効 plan で `speech_caption_invalid` を一度送る。
   - conditioning state が設定と capability を反映する。
5. queue and synthesis
   - 全文節へ同じ caption を送る。
   - caption なしでは request field を `null` のままにする。
   - invalidation 後の次ターンへ caption を再利用しない。
   - synthesis 前の capability 上限を超える caption を拒否する。
6. regression
   - doctor の caption なし probe を維持する。
   - 全 unit/integration/static check を実行する。
   - 実配備後に Windows Irodori へ caption 付き合成を一度実行し、完全な WAV を確認する。

## 配備と rollback

PR merge 後、main を更新して moco service を再配備する。ユーザー設定の
`irodori.caption_mode` を `auto` へ変更し、Tailscale の hostname と connect IP は現在の値を
維持する。

rollback は設定を `caption_mode=off` へ戻して service を再起動する。Windows Irodori と
Tailscale Serve は変更しないため、音声経路自体の rollback は不要である。
