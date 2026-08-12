# moco オペレーターコンソール再設計

## 目的

moco のブラウザ画面を、紹介ページ風の画面から、会話と処理状況を監視するためのコンパクトなオペレーターコンソールへ再設計する。

解決する中心課題は、依頼を受け付けた後、処理が続いているのか、停止したのか、何を行っているのかが見えないことである。画面は内部思考を推測せず、Codex App Server と音声ランタイムから観測できる事実を継続的に表示する。

## 成功条件

- 初期表示から会話、現在の処理、直近イベントへすぐ到達できる。
- Codex の turn が継続中なら、経過時間と最終更新からの時間が常に分かる。
- マイク停止後も Codex の処理、Irodori の音声生成、再生、Realtime セッションが継続し得ることが画面から分かる。
- Realtime、Codex、Irodori、WebSocket の失敗を隠さず、現在のエラーと履歴の両方を確認できる。
- 配色はプリセットから選択でき、必要なら各表示色を個別に調整できる。
- カスタム配色でも、エラーや処理状態の意味とログの可読性が失われない。
- capability、SDP、認証情報、コマンド本文、ツール引数、音声データを画面ログへ出さない。

## 対象外

- 長期記憶と会話履歴の永続化
- Realtime セッションの明示的な終了と再生成
- モデルの raw reasoning や chain-of-thought の表示
- ログのファイル保存、エクスポート、外部送信
- F1/F2 の固定化。表示するキー名は現在の設定値を使う。

## 採用する画面構成

比較モックの推奨案 A を採用する。

```text
┌ moco ───────────────────────────────────────────────────────────────────┐
│ ● 処理中 00:18  MIC OFF  WS ONLINE  VOICE [霞⌄] [F1 開始] [F2 停止] [◐] │
├─────────────────────────────────────────────────────────────────────────┤
│ Codex が確認を続けています                         最終更新 3 秒前       │
├────────────────────────────────────┬────────────────────────────────────┤
│ 会話                               │ アクティビティ                     │
│ YOU   設定を確認してください。     │ 20:14:08 TURN  応答処理を開始      │
│ MOCO  処理中                       │ 20:14:12 WORK  Web 検索を開始      │
│                                    │ 20:14:23 WORK  コマンド実行を完了  │
└────────────────────────────────────┴────────────────────────────────────┘
```

### 上部バー

高さを約 44px に抑え、以下を左から表示する。

1. moco の小さな識別名
2. 現在の主要状態と経過時間
3. マイクの ON/OFF
4. WebSocket 接続状態
5. Irodori 音声モデル選択
6. 音声入力の開始と停止
7. テーマ設定

未接続時は接続ボタンを主要操作として表示する。接続後は状態表示へ置き換え、ボタンを状態ラベルとして使わない。開始・停止ボタンのラベルには設定済みキーを表示し、F1/F2 を HTML や JavaScript に固定しない。

### 進行状況バー

上部バー直下に、現在の最も重要な処理を一行で表示する。

- turn 稼働中: `Codex が処理を続けています`
- reasoning activity 更新中: `推論要約を更新しています`
- コマンド実行中: `コマンドを実行しています`
- Web 検索中: `Web を検索しています`
- MCP または動的ツール: `外部ツールを実行しています`
- 音声合成中: `音声を生成しています`
- 再生中: `音声を再生しています`
- 待機中: `発話を待っています`

turn、音声生成、再生のいずれかが稼働中のときだけ、開始からの経過時間と、最後にイベントを受信してからの時間をクライアント時計で更新する。待機中と未接続時は時計を表示しない。進捗率は表示しない。イベントがしばらく届かなくても turn が完了していなければ、`処理継続中 · 最終更新から 24 秒` と表示する。停止と断定するのは turn 完了、明示的エラー、または接続切断を受信した場合だけとする。

### 会話ペイン

会話本文を主要領域として表示する。

- `YOU` と `MOCO` に表記を統一する。
- 発話開始時刻をクライアント時計で付ける。
- 現在タブのメモリ内だけで保持し、再読み込みや切断後に復元しない。
- `この画面のみ・保存されません` をヘッダーに一度だけ表示する。
- 消去は表示内容だけを消し、Realtime セッションや Codex の状態を変更しない。

### アクティビティペイン

最大 200 件のリングバッファとしてクライアントメモリ内に保持する。書式は `時刻 / 種別 / 説明` とし、新しいイベントへ自動追従する。ユーザーが上へスクロールしている間は追従を止め、`最新へ` を表示する。

表示する種別は以下に限定する。

| 種別 | 表示例 |
| --- | --- |
| `CONNECTION` | `Realtime に接続`、`WebSocket が切断` |
| `MIC` | `音声入力を開始`、`音声入力を停止` |
| `TURN` | `応答処理を開始`、`応答処理を完了` |
| `REASONING` | `推論要約を更新`という固定ラベル |
| `WORK` | `コマンド実行`、`ファイル変更`、`Web 検索`、`外部ツール`、`サブエージェント`、`画像確認`、`コンテキスト整理` |
| `VOICE` | `音声生成を開始`、`音声生成を完了`、`再生を開始`、`再生を完了`、`音声モデルを変更` |
| `ERROR` | 安定したエラーコードと短い説明 |
| `SETTINGS` | テーマ設定の読込失敗など、表示設定に関する警告 |

コマンド文字列、作業パス、検索語、MCP 引数、ツール結果、reasoning の raw text と
reasoning summary の本文は表示しない。`item/reasoning/summaryTextDelta` と
`item/reasoning/textDelta` は本文を破棄し、schemaで証明されたitem lifecycleだけを固定ラベルへ変換する。

## Codex イベント契約

ローカルの Codex App Server が生成する実験的 JSON Schema を根拠とし、現在の Realtime transcript に加えて以下を取り込む。

- `turn/started`
- `turn/completed`
- `item/started`
- `item/completed`
- `item/reasoning/summaryTextDelta`

`item/started` と `item/completed` の `item.type` は、表示用の安全な分類へ変換する。

| App Server item type | 表示 |
| --- | --- |
| `reasoning` | `推論要約を更新` |
| `commandExecution` | `コマンド実行` |
| `fileChange` | `ファイル変更` |
| `mcpToolCall`, `dynamicToolCall` | `外部ツール` |
| `collabAgentToolCall`, `subAgentActivity` | `サブエージェント` |
| `webSearch` | `Web 検索` |
| `imageView` | `画像確認` |
| `imageGeneration` | `画像生成` |
| `contextCompaction` | `コンテキスト整理` |
| その他 | `Codex 処理` |

サーバーからブラウザへは次の strict なメッセージを送る。

```json
{
  "type": "activity",
  "kind": "work",
  "phase": "started",
  "label": "Web 検索",
  "occurredAtMs": 1785496800000
}
```

reasoning summary の本文は表示しない。item ID、delta、raw reasoning はBrowserへ転送せず、
`item/started` と `item/completed` から導いた固定category／phase／labelだけを表示する。
Realtime turnでreasoning item通知が発生しない場合は、turnと他itemの開始・完了だけを表示し、
summaryを推測または生成して補わない。

## 音声とセッションの状態分離

画面状態は一つの巨大な `state` 文字列へ集約せず、次の独立した観測値として扱う。

- WebSocket: offline / connecting / online
- Realtime: inactive / connecting / active / failed
- microphone: off / on
- Codex turn: idle / active
- synthesis: idle / active
- playback: idle / active

音声入力停止は microphone だけを `off` にする。Codex turn、synthesis、playback、Realtime は変更しない。WebSocket 切断時は既存の fail-closed 方針に従い、マイクとローカル再生を停止し、現在の失敗をエラー帯と履歴の両方に残す。

## エラー表示

現在の失敗は進行状況バーの直下に alert として表示する。閉じる操作は alert だけを閉じ、アクティビティの `ERROR` 行は残す。

既知エラーは `安定コード — 短い日本語説明` として表示する。未知コードはコード自体を残し、一般的な成功状態へフォールバックしない。切断時は `処理中` を残さず、接続切断と再接続操作を明示する。

## テーマシステム

ChatGPT.app の Appearance と同様に、プリセット選択を入口とし、その下に詳細配色を置く。

### プリセット

`System` は OS の light/dark に追従し、Light と Dark はそれぞれ独立したプリセット群として表示する。プリセット数は一画面で比較できる範囲に保ちつつ、ChatGPT.app の Appearance と同程度の選択幅を持たせる。

- 自動: `System`
- Light: `Porcelain`、`Paper`、`Mist`、`Sage`、`Rose`
- Dark: `Midnight`、`Graphite`、`Ocean`、`Forest`、`Aubergine`
- Accessibility: `High Contrast Light`、`High Contrast Dark`

上部バーには 32px 程度のテーマボタンだけを置く。押すと非モーダル設定パネルを開き、グループ見出しと、背景・面・アクセントを示すスウォッチ付き radio を表示する。詳細配色は初期状態で折り畳む。

### 編集可能な配色

次の 8 項目を編集可能にする。

- 背景
- パネル
- 浮上パネル
- 境界線
- 本文
- 補助文字
- アクセント
- 操作アクセント

各行は color input、16 進入力、項目単位のリセットを持つ。変更後は由来プリセットを保持した `Custom` として扱う。

エラー、警告、正常、情報、フォーカスリングの意味色はプリセットの polarity ごとに管理し、個別編集の対象にしない。背景とのコントラストを検査し、本文 4.5:1、補助文字 3:1、操作境界 3:1 を下回る場合は実測値と警告を表示する。色を黙って自動補正しない。

### 永続化

テーマだけを `localStorage` の `moco.theme.v1` へ保存する。スキーマは次の allowlist に限定する。

```json
{
  "v": 1,
  "preset": "midnight",
  "overrides": {
    "accent": "#9da8ff"
  }
}
```

capability、会話、音声モデル、ホットキー、Realtime 状態は保存しない。JSON、バージョン、プリセット名、override キー、`#rrggbb` 値をすべて検証し、一項目でも不正なら保存値全体を拒否する。既定テーマへ戻した事実を `SETTINGS / theme_config_invalid` として表示し、黙って隠さない。

設定パネル内の input、select、textarea、color picker にフォーカスがある間は、ブラウザフォールバックの音声ホットキーを処理しない。Escape が停止キーに設定されていても、設定パネル内ではパネルを閉じる操作を優先する。

## 視覚方針

- 巨大なヒーロー、静的シグナルレール、重複するプライバシー文言、装飾フッターを削除する。
- 本文 13px、ログ補助文字 11px を基準にする。
- 4 / 8 / 12 / 16px の間隔スケールを使う。
- 影と発光は常設せず、境界と面の差で階層を作る。
- 記憶に残す要素は「現在処理と経過時間を示す一行」だけに絞る。
- 820px 以下では上部バーを二行に折り、会話とアクティビティを縦積みにする。
- 520px 以下ではテーマ設定を全幅シートにする。

## アクセシビリティ

- `role=status` は現在の主要状態だけに使う。
- 現在エラーは `role=alert`、会話は `role=log` と `aria-live=polite`、アクティビティは `aria-live=off` とする。
- すべての操作に 2px の visible focus ring を付ける。
- 色だけに依存せず、状態ラベルとアイコンを併記する。
- `prefers-reduced-motion` を維持し、タイマー更新に点滅やパルスを使わない。
- テーマパネルを閉じたら呼出ボタンへフォーカスを戻す。

## テスト方針

### Python

- turn と item 通知を厳密にパースし、安全な activity event へ変換する。
- reasoning summary と raw reasoning の本文を転送しない。
- 他 thread/turn のイベントを無視する。
- 未知 item type を安全な一般ラベルへ変換する。
- ブラウザメッセージにコマンド、パス、引数、結果が含まれない。
- マイク停止が turn、synthesis、playback、Realtime を停止しない。

### JavaScript

- activity ring buffer が 200 件を超えない。
- turn 経過時間と最終更新時間を決定論的な時計で表示する。
- reasoning activity は固定ラベルだけを表示する。
- 切断時に現在処理を failed/offline とし、エラー履歴を残す。
- テーマ保存値を strict に検証する。
- 全プリセットと custom override を適用・リセットできる。
- コントラスト比を正しく計算する。
- 設定入力中は音声ホットキーを処理しない。
- 既存の continuous listening、マイクのみ停止、音声選択、capability 再読込、切断時 fail-closed のテストを維持する。

### 画面

- desktop、820px、520px、320px で会話とアクティビティが読める。
- light/dark/high-contrast と custom theme を実ブラウザで確認する。
- キーボードだけで接続、音声選択、開始、停止、テーマ変更、ログ移動、消去ができる。
- 長い会話、200 件のログ、現在エラーを同時表示しても操作が隠れない。

## 実装境界

既存の session、web controller、静的 frontend の責務分離を保つ。新しい永続化層やフロントエンドフレームワークは導入しない。

- `codex/session.py`: App Server 通知を型付きの安全なイベントへ変換
- `web/app.py`: イベントを strict なブラウザメッセージへ変換
- `web/static/app.js`: 表示状態、経過時計、リングバッファ、テーマ設定
- `web/static/index.html`: コンパクトな意味構造
- `web/static/styles.css`: トークン化したレイアウトとテーマ

既存 DOM ID と公開 JavaScript API は、必要なテストを先に追加した上で段階的に変更する。
