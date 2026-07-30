# moco

moco は、Mac に話しかけて Codex に作業を頼み、その返答を Irodori の声で聞くための
ローカル音声エージェントです。既定では F1 を押している間だけマイクを有効にし、
離すと発話を確定します。応答や読み上げを止める操作は F2 です。

ただし、F1/F2 が機能そのものなのではありません。内部の契約は
`PTT_DOWN` / `PTT_UP` / `CANCEL` であり、キーは YAML で変更できます。F1/F2 は
初期設定と機能テストで使う既定値です。

> [!WARNING]
> moco は初期リリースです。Codex App Server の experimental Realtime API を使うため、
> ChatGPT/Codex の更新に合わせて追従が必要になる場合があります。

## 何が常駐するのか

常駐する本体は、`launchd` から起動できる Python ランタイムです。会話の状態、
Codex との接続、Irodori への音声合成要求、グローバルキー、設定、テレメトリは
このランタイムが管理します。

ブラウザは常駐本体ではありません。Chrome のページは、マイク権限、WebRTC の音声入力、
生成済み WAV の再生だけを担うメディア操作面です。使うときにページを開き、一度
「音声卓を有効にする」を押します。ページを閉じても launchd サービスは残りますが、
マイクと再生先がなくなるため音声会話はできません。

この分割は完成形ではなく、まず動作実績のある WebRTC 経路を製品として使える形にした
ものです。将来、メニューバーアプリやネイティブ音声クライアントへ移る場合も、
会話と音声合成のランタイムはそのまま利用できます。

## 必要なもの

- macOS と Chrome
- Python 3.13、[uv](https://docs.astral.sh/uv/)、Node.js、`just`
- ChatGPT.app に同梱された Codex と、利用可能な ChatGPT アカウント
- Irodori-TTS API。通常は Windows GPU ホストで起動し、Tailscale 経由で接続します
- 初回利用時の Chrome マイク許可
- グローバルキーを使う場合は、moco を起動するターミナルまたは実行ファイルへの
  macOS Input Monitoring 許可

StackChan と長期記憶は初期リリースの対象外です。長期記憶の取得、保持、減衰、統合、
削除は [Issue #1](https://github.com/ToaruPen/moco/issues/1) で追跡しています。

## 最短の起動手順

```bash
git clone https://github.com/ToaruPen/moco.git
cd moco
just sync
uv run moco config init
uv run moco config validate
uv run moco doctor
uv run moco run
```

設定は `~/Library/Application Support/moco/moco.yaml` に作成されます。別のターミナルで
次を実行すると、実行中プロセスだけが知る capability を使って操作ページが開きます。
capability はターミナルへ表示されません。

```bash
uv run moco open
```

ページで「音声卓を有効にする」を押し、Chrome にマイクを許可してください。既定操作は
次のとおりです。

1. F1 を押したまま話す
2. 話し終えたら F1 を離す
3. Codex の返答を Irodori の声で聞く
4. 途中で止める場合は F2 を押す

F1 を再び押すと、再生中の古い音声を止めてから次の発話を受け付けます。設定した
アイドル時間を過ぎると会話だけが閉じられ、デーモンと操作ページは残ります。次の
push-to-talk で新しい会話が作られ、以前の文字起こしは自動投入されません。

## Irodori の接続先

公開設定例は安全のため localhost を指しています。Windows ホストを使う場合は
`moco.yaml` の `irodori.base_url` を、そのホストの Tailscale HTTP URL に変更します。

```yaml
irodori:
  base_url: http://TAILSCALE_IP:8923
  speaker: null
```

`speaker` にはサーバーが認識する portable speaker 名だけを指定してください。
Windows ローカルの embedding パスは送信しません。URL にユーザー名やパスワードを
埋め込む設定は拒否されます。

疎通確認だけでなく合成まで試す場合は、音声を保存せず WAV バイト数だけを確認できます。

```bash
uv run moco doctor --synthesize "接続確認です。"
```

## キーを変更する

グローバル監視とブラウザのフォールバック操作は、同じ設定を使います。

```yaml
hotkeys:
  enabled: true
  push_to_talk: f1
  cancel: f2
```

二つのキーを同じ値にはできません。キーリピートによる重複 down は無視され、対応する
down がない up も無視されます。Input Monitoring を許可できない場合でも、操作ページの
ボタンは利用できます。

## launchd で本体を常駐させる

マイクと Input Monitoring の許可を確認するため、最初の起動は前景の `moco run` を
推奨します。その後はユーザー LaunchAgent として登録できます。

```bash
uv run moco service install
uv run moco service start
uv run moco service status
```

利用時は `uv run moco open` で操作ページを開きます。停止と削除は別操作です。

```bash
uv run moco service stop
uv run moco service uninstall
```

uninstall はラベルと実行ファイルが moco のものと一致する plist だけを削除します。
改変済みまたは別アプリの plist は拒否します。

## `doctor` の見方

`doctor` は次の境界を安定したコードで報告します。

| コード | 確認対象 |
| --- | --- |
| `python` / `config` | Python と厳格 YAML |
| `codex_binary` | ChatGPT.app 同梱 Codex の実行可否 |
| `codex_account` | 認証済みかどうか。メールアドレス等は表示しません |
| `codex_features` / `codex_voices` | experimental API と Realtime voice |
| `irodori_health` / `irodori_synthesis` | モデルの読込状態と任意の合成 |
| `hotkeys` | グローバルキー監視。失敗時は Input Monitoring を確認します |

`conversation_start_failed` は Codex または Irodori に接続できない状態、
`irodori_not_ready` はモデル未読込、`codex_realtime_error` は実験 API の会話接続が
終了した状態です。まず `uv run moco doctor` を再実行し、Irodori サービスと
ChatGPT.app の状態を確認してください。

## プライバシーと観測

文字起こしは現在のブラウザ表示とプロセス内にだけ存在し、ファイルへ保存しません。
音声、文字起こし、プロンプト、アカウント識別子、capability、将来の記憶内容は
ログや OpenTelemetry 属性へ出しません。コンソールと任意の OTLP 出力が扱うのは、
状態、所要時間、境界名、安定したエラーコード、trace ID です。

操作サーバーは loopback にしか bind できません。WebSocket は同一 loopback origin と
プロセスごとの capability を要求し、同時に一つの操作クライアントだけを受け入れます。
詳細は [SECURITY.md](SECURITY.md) を参照してください。

## 開発

```bash
just format
just test
just check
```

`just check` は Ruff、mypy strict、vulture、deptry、ast-grep、Biome、Python/ブラウザ
テスト、branch coverage、secretlint、配布物ビルドをまとめて実行します。通常の CI は
実アカウント、マイク、Tailscale、GPU を必要としません。遅いステップを後から比較
できるよう、pytest の duration 上位も記録します。

ライセンスは [MIT](LICENSE) です。
