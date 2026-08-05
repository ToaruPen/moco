# moco

moco は、Mac に話しかけて Codex に作業を頼み、その返答を Irodori の声で聞くための
ローカル音声エージェントです。設定した開始キーを一度押すとマイク入力が続き、
停止キーを押すまで GPT-Live と自然に会話できます。

ただし、F1/F2 が機能そのものなのではありません。内部の契約は
`LISTEN_START` / `LISTEN_STOP` であり、キーは YAML で変更できます。F1/F2 は
初期設定と機能テストで使う既定値にすぎません。

> [!WARNING]
> moco は初期リリースです。Codex App Server の experimental Realtime API を使うため、
> ChatGPT/Codex の更新に合わせて追従が必要になる場合があります。

## 何が常駐するのか

常駐する本体は、`launchd` から起動できる Python ランタイムです。会話の状態、
Codex との接続、Irodori への音声合成要求、グローバルキー、設定、テレメトリは
このランタイムが管理します。

ブラウザは常駐本体ではありません。デスクトップまたはスマートフォンのページは、
マイク権限、WebRTC の音声入力、生成済み WAV の再生だけを担うメディア操作面です。使うときにページを開き、一度
「接続」を押します。ページを閉じても launchd サービスは残りますが、
マイクと再生先がなくなるため音声会話はできません。

この分割は完成形ではなく、まず動作実績のある WebRTC 経路を製品として使える形にした
ものです。将来、メニューバーアプリやネイティブ音声クライアントへ移る場合も、
会話と音声合成のランタイムはそのまま利用できます。

## 必要なもの

- macOS とデスクトップ版 Chrome
- スマートフォンから使う場合は iOS Safari または Android Chrome
- Python 3.13、[uv](https://docs.astral.sh/uv/)、Node.js、`just`
- ChatGPT.app に同梱された Codex と、利用可能な ChatGPT アカウント
- Irodori-TTS API。通常は Windows GPU ホストで起動し、Tailscale 経由で接続します
- 初回利用時の Chrome マイク許可
- グローバルキーを使う場合は、moco を起動するターミナルまたは実行ファイルへの
  macOS Input Monitoring 許可
- スマートフォンから使う場合は、固定ドメインを持つ Cloudflare Tunnel と
  本人だけを許可する Cloudflare Access application

StackChan と長期記憶は初期リリースの対象外です。長期記憶の取得、保持、減衰、統合、
削除は [Issue #1](https://github.com/ToaruPen/moco/issues/1) で追跡しています。
Realtime セッションを明示的に破棄して新しい会話を始める操作は、
[Issue #2](https://github.com/ToaruPen/moco/issues/2) で追跡しています。

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

ページで「接続」を押し、Chrome にマイクを許可してください。初期設定の
F1/F2を使う機能テストでは、次のように操作します。

1. F1 を一度押して音声入力を開始する
2. キーから手を離し、そのまま複数ターン会話する
3. Codex の返答を Irodori の声で聞く
4. マイク入力を止める時だけ F2 を押す

F2 はマイク入力だけを停止します。進行中の応答、Irodori の読み上げ、Realtime の
会話コンテキストは中止しません。

操作バーの `VOICE` は、Irodori が実行時に公開する話者カタログをそのまま表示します。
候補、表示順、既定話者は Irodori が所有し、moco は固定の話者やナレーターを追加しません。
会話中の変更は次の読み上げから反映されます。選択中の話者がカタログから消えた場合は、
別の話者へ自動で切り替えず音声を停止します。

操作画面の進捗帯とアクティビティ欄には、Codex のターン、コマンドやファイル操作などの
処理種別、音声生成、再生、マイク、接続状態を表示します。経過時間と最終更新から、返答後も
処理が続いているかを確認できます。App Server が reasoning summary を通知した場合はその
短い要約だけを表示し、raw reasoning、コマンド本文、パス、ツール引数や結果は表示しません。
アクティビティはメモリ内の最大200件で、再読込すると消えます。エラー帯を閉じても、安定した
エラーコードを含む履歴はアクティビティ欄に残ります。

配色は OS に追従する System に加え、Light 5種（Porcelain、Paper、Mist、Sage、Rose）、
Dark 5種（Midnight、Graphite、Ocean、Forest、Aubergine）、High Contrast 2種から選択
できます。どのプリセットでも背景や文字など8項目を個別に変更できます。コントラストが基準を
下回る場合は測定値を表示しますが、指定色は自動変更しません。配色だけをブラウザの
`localStorage` に保存し、capability、会話、音声、アクティビティは含めません。配色入力中は
ブラウザ側のフォールバックキーを抑止します。

常時入力中に新しいユーザー発話を検出すると、再生中または合成中の古い Irodori 音声を
無効化し、Realtime 側の自然な割り込みに任せます。入力停止後に設定したアイドル時間を
過ぎると会話だけが閉じられ、デーモンと操作ページは残ります。次の入力開始で新しい
会話が作られ、以前の文字起こしは自動投入されません。

## スマートフォンから使う

スマートフォン対応でも、moco 自身を LAN やインターネットへ直接 bind しません。
FastAPI は `127.0.0.1` で待ち受け、macOS 上の `cloudflared` が固定 HTTPS ドメインから
loopback origin へ接続します。スマートフォン側に Tailscale アプリは不要です。

まず Cloudflare dashboard で名前付き Tunnel の public hostname を作り、転送先を
`http://127.0.0.1:8765` にします。Quick Tunnel は URL が変わるため使用しません。
同じ hostname を self-hosted Access application として登録し、利用者本人だけを allow
してください。bypass policy や Access を通らない予備 hostname は作りません。

moco の設定には固定 hostname だけを追加します。Tunnel token、credentials file、Access
identity はこの YAML に書きません。

```yaml
server:
  host: 127.0.0.1
  port: 8765
  public_url: https://voice.example.com
```

`cloudflared` はリポジトリ外のユーザー LaunchAgent
`dev.toarupen.moco-cloudflared` として常駐させます。Tunnel ingress は上記 hostname だけを
loopback origin へ送り、最後を `http_status:404` の catch-all にしてください。moco と
Tunnel は独立したサービスです。片方が停止しても別経路へ切り替えず、`doctor` が部分失敗を
そのまま報告します。

設定後に moco を再起動し、Mac で `uv run moco open` を実行します。loopback の操作画面に
「スマホ接続」が現れるので、QR をスマートフォンで読み取ってください。QR は現在プロセスの
capability を URL fragment にだけ含み、ファイル、通常ログ、Cloudflare の request path
には残りません。daemon を再起動すると古い QR は無効になります。

スマートフォンでは「接続」を押してマイクを許可し、「入力開始」と「入力停止」で操作します。
入力開始は押し続ける PTT ではありません。指を離しても入力は続き、入力停止はマイクだけを
止めます。デスクトップが接続中なら、スマートフォンは接続を奪わず
`single_operator_only` を表示します。

## Irodori の接続先

Windows ホストでは Irodori infra を loopback で常駐させ、Tailscale Serve の
ポート番号を含まない HTTPS URL から接続します。

```yaml
irodori:
  base_url: https://windows-node.example.ts.net
  # macOS の MagicDNS が利用できない環境だけ指定します。
  # 接続先だけを上書きし、Host、SNI、証明書検証は base_url の名前を使います。
  connect_ip: null
  # 実行時カタログの preferred canonical ID。移行時は一意な alias も解決します。
  speaker: null
  caption_mode: "off"
```

`speaker` は起動時に優先する canonical voice ID です。旧名を移行するための alias は、
カタログ内で一意な場合だけ同じ ID へ解決します。`null` の場合は Irodori が示す default を
使い、default がなければ明示選択まで会話開始を拒否します。候補を列挙する旧 `speakers`
キーは削除してください。残っていると厳格な設定検証が失敗します。

初期 v4 移行では `caption_mode` は `off` だけです。moco は自由記述 caption や
`calm` / `cheerful` / `clear` のような独自プリセットを送らず、Irodori の neutral な既定条件と
本文中の emoji を使います。checkpoint、tokenizer、generation、alias、embedding パスは
Irodori 内に留まり、ブラウザへ公開しません。URL にユーザー名やパスワードを埋め込む設定は
拒否されます。

`timeout_seconds` は capability/readiness 確認だけに使われます。音声合成には期限を設けず、
新しいユーザー発話または会話終了時に古い結果を無効化します。各合成要求は取得済みの
voice ID と runtime generation を条件にするため、不一致時は別話者や v3 へ fallback しません。

疎通確認だけでなく合成まで試す場合は、音声を保存せず WAV バイト数だけを確認できます。

```bash
uv run moco doctor --synthesize "接続確認です。"
```

## キーを変更する

グローバル監視とブラウザのフォールバック操作は、同じ設定を使います。

```yaml
hotkeys:
  enabled: true
  start_listening: f1
  stop_listening: f2
```

二つのキーを同じ値にはできません。キーリピートによる重複 down は無視され、対応する
down がない up も無視されます。開始キーを離しても入力は継続し、key-up 自体は制御を
送りません。Input Monitoring を許可できない場合でも、操作ページのボタンは利用できます。

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
| `operator_public_url` | スマートフォン用固定 HTTPS hostname の設定状態。hostname 自体は表示しません |
| `cloudflared_binary` | `cloudflared` の実行可否 |
| `cloudflared_service` | moco 専用 LaunchAgent が running かどうか |
| `codex_binary` | ChatGPT.app 同梱 Codex の実行可否 |
| `codex_account` | 認証済みかどうか。メールアドレス等は表示しません |
| `codex_features` / `codex_voices` | experimental API と Realtime voice |
| `irodori_capabilities` / `irodori_synthesis` | runtime readiness、話者選択可否、任意の条件付き合成 |
| `irodori_route` | OS DNS または明示した接続先 override |
| `hotkeys` | グローバルキー監視。失敗時は Input Monitoring を確認します |

`model_loading` / `model_not_loaded` / `voice_bank_invalid` は Irodori の準備状態、
`configured_voice_unavailable` / `voice_not_found` は話者カタログの不一致、
`runtime_generation_mismatch` は取得後に runtime が更新された状態です。これらの場合は
音声を停止し、別話者や旧モデルへ自動で切り替えません。`codex_realtime_error` は実験 API の
会話接続が終了した状態です。まず `uv run moco doctor` を再実行し、Irodori サービスと
ChatGPT.app の状態を確認してください。

## プライバシーと観測

文字起こしは現在のブラウザ表示とプロセス内にだけ存在し、ファイルへ保存しません。
音声、文字起こし、プロンプト、アカウント識別子、capability、将来の記憶内容は
ログや OpenTelemetry 属性へ出しません。コンソールと任意の OTLP 出力が扱うのは、
状態、所要時間、境界名、安定したエラーコード、trace ID です。

操作サーバーは loopback にしか bind できません。WebSocket は同一 loopback origin、または
設定した公開 HTTPS origin と Host の完全一致を要求します。どちらの経路でもプロセスごとの
capability が必要で、同時に一つの操作クライアントだけを受け入れます。
詳細は [SECURITY.md](SECURITY.md) を参照してください。

## 開発

```bash
just format
just test
just check
```

`just check` は Ruff、mypy strict、vulture、deptry、ast-grep、Biome、Python/JavaScript
単体テスト、320/390/430px の Playwright Chromium/WebKit テスト、branch coverage、
secretlint、配布物ビルドをまとめて実行します。通常の CI は
実アカウント、マイク、Tailscale、GPU を必要としません。遅いステップを後から比較
できるよう、pytest の duration 上位も記録します。

ライセンスは [MIT](LICENSE) です。
