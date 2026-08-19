# moco

moco は、macOS または Windows 11 の手元のマシンに話しかけて Codex に作業を頼み、
その応答を Irodori の声で聞くための macOS-first ローカル音声エージェントです。
Codex Realtime v3が会話を所有し、Frameless Bidiの`delegation.*`で同じThreadのCodexへ
必要な作業を委譲します。acknowledgement、進捗、最終結果は同じRealtime会話へ戻ります。

ただし、F1/F2 が機能そのものなのではありません。内部の契約は
`LISTEN_START` / `LISTEN_STOP` であり、キーは YAML で変更できます。F1/F2 は
初期設定と機能テストで使う既定値にすぎません。

> [!WARNING]
> moco は初期リリースです。Codex App Server の experimental Realtime API を使うため、
> ChatGPT/Codex の更新に合わせて追従が必要になる場合があります。

## 何が常駐するのか

本体は foreground で起動する Python ランタイムです。macOS では確認後に `launchd` からも
起動できます。会話の状態、
Codex との接続、Irodori への音声合成要求、グローバルキー、設定、テレメトリは
このランタイムが管理します。

ブラウザは常駐本体ではありません。デスクトップまたはスマートフォンのページは、
マイク権限、WebRTC の音声入力、生成済み WAV の再生に加え、現在の文字起こし、safe progress、
状態の観測、turn全体の取消を担う操作面です。Agent の実行や個別 approval decision は所有しません。
Reviewer はこの画面と認証を共有しない別の loopback-only surface です。使うときにページを開き、
一度「接続」を押します。ページを閉じても foreground プロセスまたは macOS の `launchd`
サービスは残りますが、マイクと再生先がなくなるため音声会話はできません。

この分割は完成形ではなく、まず動作実績のある WebRTC 経路を製品として使える形にした
ものです。将来、メニューバーアプリやネイティブ音声クライアントへ移る場合も、
会話と音声合成のランタイムはそのまま利用できます。

## 必要なもの

- macOS-first。macOS、または Windows 11 の対話デスクトップと Chrome / Edge
- スマートフォンから使う場合は iOS Safari または Android Chrome
- Python 3.13、[uv](https://docs.astral.sh/uv/)、Node.js、`just`
- `PATH` 上、または `codex.command` に設定した公開 Codex CLI と、利用可能な ChatGPT
  アカウント。macOSで`codex.command`が未指定なら、対応するChatGPT.app bundleを
  `PATH`上のCLIより先に利用します
- Irodori-TTS API。通常は Windows GPU ホストで起動し、Tailscale 経由で接続します
- 初回利用時の Chrome または Edge のマイク許可
- グローバルキーを使う場合、macOS では moco を起動するターミナルまたは実行ファイルへの
  macOS Input Monitoring 許可。Windows ではブラウザのフォールバック操作を利用でき、
  ネイティブホットキーの可否は対話デスクトップ上で確認します
- スマートフォンから使う場合は、固定ドメインを持つ Cloudflare Tunnel と
  本人だけを許可する Cloudflare Access application

StackChan と長期記憶は初期リリースの対象外です。長期記憶の取得、保持、減衰、統合、
削除は [Issue #1](https://github.com/ToaruPen/moco/issues/1) で追跡しています。
Realtime セッションを明示的に破棄して新しい会話を始める操作は、
[Issue #2](https://github.com/ToaruPen/moco/issues/2) で追跡しています。

### macOS / Windows Stage B

moco は macOS-first ですが、Frameless delegation とローカル approval は macOS と
Windows 11 の各ホストで foreground 実行する同じ基本契約を対象にします。各ホストの moco は
そのホストの Codex CLI、設定、workspace を使い、別ホストの app-server を暗黙に proxy
しません。`codex.command: null` の場合、Windows は `PATH` 上の公開 Codex CLI だけを使い、
Windows Store の非公開インストールパスは探索しません。設定は `APPDATA`、実行中だけ使う
owner-private な状態は `LOCALAPPDATA` に保存します。

Windows で `moco service` を実行すると `unsupported_platform` になります。正式な起動方法は
`uv run moco run` による foreground 実行です。ブラウザのマイク許可とグローバルホットキーの
利用可否は、対話デスクトップ上で利用者が確認してください。

Agent profile は設定ファイルの `agent.profile` で選びます。既定の `read_only`、明示的な
`workspace_write`、Codex の有効設定を上書きしない `inherit_codex` の3種類です。音声や
公開画面から profile は変更できません。`read_only` と `workspace_write` は global Codex policy を admission 条件にしません。
`read_only` と `workspace_write` は sandbox と approval policy を thread 作成時に明示します。`inherit_codex` だけが global Codex policy を継承します。
この profile で有効 policy を確認できない場合、または `danger-full-access` と approval policy
`never` の組み合わせになる場合は、音声からCodex作業を開始しません。承認が発生し得る依頼を始める前に、
同じホストの別ターミナルで `uv run moco review` を実行してローカル Reviewer を接続します。
Reviewer が未接続のまま承認要求を受けると fail-closed になります。公開画面は待機状態と
turn 全体の取消だけを扱い、操作詳細の閲覧や decision はできません。音声の「はい」も承認に
はなりません。

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

設定はmacOSでは `~/Library/Application Support/moco/moco.yaml`、Windowsでは
`%APPDATA%\moco\moco.yaml` に作成されます。Windowsでは設定の`codex.command`が実行境界になるため、
新規directoryとfileをcurrent user、SYSTEM、Administratorsだけのprotected DACLで作成します。既存pathの
owner、DACL、reparse pointが安全でなければ、自動修復せず設定の作成・読み込みを拒否します。
別のターミナルで次を実行すると、実行中
プロセスだけが知る capability を使って操作ページが開きます。capability はターミナルへ
表示されません。

```bash
uv run moco open
```

以前の設定key `codex.binary` は削除されました。後方互換のaliasはなく、残っている設定は厳格な
unknown-key validationで拒否されます。明示的なCLIを選ぶ場合は次のように置き換えてください。

```yaml
codex:
  command: ["/absolute/path/to/codex"]
```

macOSの公式bundle、または他のOSでホストの`PATH`から自動解決する場合は次を使います。

```yaml
codex:
  command: null
```

ページで「接続」を押し、Chrome にマイクを許可してください。初期設定の
F1/F2を使う機能テストでは、次のように操作します。

1. F1 を一度押して音声入力を開始する
2. 依頼を話す。RealtimeのVADが発話を確定し、必要な作業を同じThreadのCodexへ自動委譲する
3. マイクをONのまま、続けて複数ターン会話する
4. 事前に接続したローカル Reviewer へ要求が届いた場合だけ、内容を確認して決定する
5. acknowledgement、speakable progress、finalをIrodoriの声で聞く
6. マイク入力を止めるときだけ F2 を押す

F2 はマイク入力だけを停止します。文字起こし確定やdelegationの開始条件ではなく、進行中の
Codex作業、Irodoriの読み上げ、Realtimeの会話コンテキストも取り消しません。F1を押すと
同じ会話で直ちにライブ入力を再開します。停止処理の遅い状態通知が後から届いても、その後の
`listening` 状態を正としてマイクと表示を ON に戻します。

### Codex作業、取消、再接続

確定したuser transcriptはapp-serverの別Threadへ再送しません。Realtime v3が一つの
`delegation.created`を作り、Codexのcommentaryとfinalを同じ会話へ自動返送します。
`clientManagedHandoffs`と`codexResponsesAsItems`はfalse、`delegationAckFiller`はtrue、
`codexResponseHandoffMode`は`bemTags`です。mocoは`appendText`や`appendSpeech`で応答を
再注入しないため、同じ依頼の二重実行と同じ応答の二重読み上げを行いません。

Realtimeのspeakable assistant transcriptを表示とSpeechQueueの共通source of truthにします。
acknowledgementやspeakable progressはfinalを待たずIrodoriへ流れます。reasoning、command output、
raw Codex itemは読み上げません。任意の`moco.speech_plan`先頭行は表示・読み上げから除き、
検証済みdelivery captionだけをIrodoriへ渡します。

Realtime event とuser/assistant transcriptの未処理queueはそれぞれ64件です。1発話あたりuserは
64 KiB／256 parts、assistantは16 KiB／256 parts、Irodori待機segmentは64件に制限します。上限を
超える不正・異常なstreamは黙って欠落させず、合成へ渡す前にVoiceを停止して明示的な再接続待ちに
します。app-server notificationの購読終了は待機中でも会話lease全体へ伝播し、次の依頼を壊れた
接続へ受け付けません。

進行中に新しい発話を始めると、Frameless会話へそのままsteering contextとして入り、同時に
再生中・合成中の古いspeech generationを停止します。操作画面の明示的な取消はpending local
reviewを撤回して、同じRealtime Threadのactive turnへ`turn/interrupt`を一度だけ送ります。音声で話した
「キャンセル」は通常の依頼であり、取消や Reviewer decision にはなりません。

Voice接続だけを失った場合は同じRealtime Threadを維持し、画面から明示的に再接続します。自動retryは
しません。app-server接続を失った場合はactive turnを成功扱いにせず、旧依頼を新しい接続へ
自動再送しません。

### GPTの応答スタイルを変更する

GPTへ渡すプロンプトの既定pathは、macOSでは `~/.moco/prompt.md`、Windowsでは
`%APPDATA%\moco\prompt.md` です。macOSのUnix shellでは、現在の内蔵プロンプトを次のように
コピーしてから、口調やキャラクターを編集できます。

```bash
mkdir -p ~/.moco
cp config/moco.prompt.example.md ~/.moco/prompt.md
```

Windows PowerShellでは次を実行します。

```powershell
New-Item -ItemType Directory -Force "$env:APPDATA\moco"
Copy-Item config\moco.prompt.example.md "$env:APPDATA\moco\prompt.md"
```

このファイルがなければ内蔵のmoco専用プロンプトを使います。`prompt`は公式Codex Realtime
プロンプトへの追記ではなく全文置換です。編集時も、mocoの人格だけでなく、Frameless delegation、
Codex結果の権威性、二重実行防止、Irodoriのplain speakable text契約を維持してください。
Codex側の`experimental_realtime_ws_backend_prompt`が非空の場合はそちらが優先されます。
別のファイルを使う場合は、
`moco.yaml` の `codex.prompt_file` へ対象ホストで有効な絶対pathを指定してください。
macOSでは `~` から始まる現在userのpathも使用できます。内容は会話開始ごとに読み直すため、
編集後のmoco再起動は不要で、次の会話
から反映されます。空、非UTF-8、64 KiB超、または読めない明示設定ファイルは会話開始時に
拒否されます。プロンプト本文はログやtelemetryへ出力しません。

操作バーの `VOICE` は、Irodori が実行時に公開する話者カタログをそのまま表示します。
候補、表示順、既定話者は Irodori が所有し、moco は固定の話者やナレーターを追加しません。
会話中の変更は次の読み上げから反映されます。選択中の話者がカタログから消えた場合は、
別の話者へ自動で切り替えず音声を停止します。

IrodoriのWAVはheaderと実データを検証してから配信します。現在のv4出力は48 kHz、mono、
PCM16です。ブラウザのAudioContextも48 kHzを要求し、decode段階の不要な48→44.1 kHz
再標本化を避けます。再生速度は1、detuneは0のままで、sample rateだけを理由に音質不良とは
判定しません。

操作画面の進捗帯とアクティビティ欄には、Codex turn と作業種別の固定ラベル、phase、
音声生成、再生、マイク、接続状態だけを表示します。コマンド本文、ファイルパス、patch、
reasoning、ツール引数や結果は表示しません。アクティビティはメモリ内の最大200件で、再読込
すると消えます。サーバー側の進捗送信queueは64件に制限し、操作画面が停止している間の
上限超過分は task の結果やtranscriptへ影響させず進捗だけを省略します。エラー帯を閉じても、
安定したエラーコードを含む履歴はアクティビティ欄に残ります。

配色は OS に追従する System に加え、Light 5種（Porcelain、Paper、Mist、Sage、Rose）、
Dark 5種（Midnight、Graphite、Ocean、Forest、Aubergine）、High Contrast 2種から選択
できます。どのプリセットでも背景や文字など8項目を個別に変更できます。コントラストが基準を
下回る場合は測定値を表示しますが、指定色は自動変更しません。配色だけをブラウザの
`localStorage` に保存し、capability、会話、音声、アクティビティは含めません。配色入力中は
ブラウザ側のフォールバックキーを抑止します。

新しいuser transcriptを開始すると、再生中または合成中の古いIrodori音声を無効化します。
Codex turn、Voice、speechがidleのまま設定時間を過ぎると会話だけが閉じられ、デーモンと
操作ページは残ります。次の入力開始では新しいRealtime Threadが作られ、
以前の文字起こしや未完了依頼は自動投入されません。

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
media capability を URL fragment に含み、Cloudflare の request path には載せません。同じ値の
唯一のファイル保存先はowner-privateな `runtime.json` で、ローカルCLIが操作画面を開くために
process lifetime中だけ使用し、終了時に削除します。daemon を再起動すると古い QR は無効になります。

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
  caption_mode: "auto"
```

`speaker` は起動時に優先する canonical voice ID です。旧名を移行するための alias は、
カタログ内で一意な場合だけ同じ ID へ解決します。`null` の場合は Irodori が示す default を
使い、default がなければ明示選択まで会話開始を拒否します。候補を列挙する旧 `speakers`
キーは削除してください。残っていると厳格な設定検証が失敗します。

`caption_mode` の既定値は `off` です。`auto` にすると、Irodori が delivery caption 対応と
正の `max_chars` を広告している場合だけ会話を開始します。非対応時は
`caption_unsupported` で停止し、別の条件へ自動で切り替えません。caption の上限は実行時の
`max_chars` に従い、読み上げ本文の文字数には適用されません。

`auto` では、Codex の確定回答の先頭の非空行に次の一行 JSON を置けます。二行目以降だけが
画面表示と読み上げ本文になり、検証済みの caption は同じ回答から分割された全音声へ送られます。

```json
{"type":"moco.speech_plan","version":1,"delivery_caption":"落ち着いて、親しみを込めて話す。"}
```

標準表現を明示する場合は `delivery_caption` を `null` にします。不正な plan は制御行だけを
除去し、本文を caption なしで継続して `speech_caption_invalid` を通知します。caption や本文は
通常ログと telemetry へ記録しません。いつこの一行を出すか、どのような話し方を指定するかは、
必要になった時点でリポジトリの `AGENTS.md` などに指示を追加できます。

checkpoint、tokenizer、generation、alias、embedding パスは Irodori 内に留まり、ブラウザへ
公開しません。URL にユーザー名やパスワードを埋め込む設定は拒否されます。

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
| `codex_profile` | 選択した `read_only` / `workspace_write` / `inherit_codex` |
| `codex_command` | 設定または自動解決した公開 Codex CLI の実行可否 |
| `codex_schema` | 実行中の CLI から生成したprotocol schemaとの互換性 |
| `codex_account` | 認証済みかどうか。メールアドレス等は表示しません |
| `codex_policy` | Codexが返すeffective sandboxとapproval policyの妥当性 |
| `codex_agent_admission` | policyとserver request categoryに基づくAgent利用可否 |
| `codex_local_review` | command/file approvalのschemaをローカルReviewer adapterで扱えるか。接続中かどうかとは別です |
| `codex_realtime` | Realtime voiceとmoco promptの利用可否。Codex側のprompt overrideは`prompt_overridden` |
| `codex_interrupt` | turn interrupt semanticの利用可否 |
| `codex_server_requests` | approval用server request categoryの互換性 |
| `irodori_capabilities` / `irodori_synthesis` | runtime readiness、話者選択可否、任意の条件付き合成 |
| `irodori_route` | OS DNS または明示した接続先 override |
| `hotkeys` | グローバルキー監視。macOS Input Monitoring / Windows browser fallbackを確認します |

`model_loading` / `model_not_loaded` / `voice_bank_invalid` は Irodori の準備状態、
`configured_voice_unavailable` / `voice_not_found` は話者カタログの不一致、
`runtime_generation_mismatch` は取得後に runtime が更新された状態です。これらの場合は
音声を停止し、別話者や旧モデルへ自動で切り替えません。`codex_realtime_error` は実験 API の
会話接続が終了した状態です。まず `uv run moco doctor` を再実行し、Irodori サービスと
Codex CLI のreadinessを確認してください。

## プライバシーと観測

文字起こし、音声、生成 speech、プロンプト、コマンド本文、ファイルパスと内容、patch本文、
MCP arguments、approval payload、reasoning、アカウント識別子はファイルへ保存しません。
通常の操作画面、音声、ログ、OpenTelemetry にもこれらの本文を出しません。App Server の
`ReasoningSummary` を受信しても、その本文は表示しません。通常アクティビティが扱うのは固定した
category／phase／label と時刻だけです。コンソールと任意の OTLP 出力は状態、所要時間、境界名、
安定したエラーコード、trace ID に限定します。

media capability とReviewerのcontrol secretだけは、process lifetime中にowner-privateな
`runtime.json` へ保存し、プロセス終了時に削除します。media capability は同じタブのreload用に
`sessionStorage`にも保持しますが、cookie、URL、`localStorage`などの永続領域には保存しません。
Reviewerのcontrol secret、bootstrap、review capabilityはbrowser storageへ保存しません。
いずれのcredentialもstdout、通常ログ、telemetryへ出しません。

ローカル Reviewer は承認判断に必要なコマンド、cwd、path、change kind、move targetをboundedに
一時表示しますが、patch本文は表示しません。patch本文はmetadataへの変換時に破棄します。
詳細と one-shot handle はprocess memoryとReviewer DOMだけに置き、decision、取消、切断時に
破棄します。review capability や操作詳細をbrowser history、query、`localStorage`、
`sessionStorage`へ保存しません。

操作サーバーは loopback にしか bind できません。WebSocket は同一 loopback origin、または
設定した公開 HTTPS origin と Host の完全一致を要求します。どちらの経路でもプロセスごとの
capability が必要で、同時に一つの操作クライアントだけを受け入れます。
Reviewer は別のcontrol secretと短命bootstrapを使い、loopbackだけから接続できます。
詳細は [SECURITY.md](SECURITY.md) を参照してください。

## 開発

```bash
just format
just test-python
just contract-codex
just check
```

`just test-python` は live／slow／installed contract を除くPython testを実行します。CIでは
macOSとWindowsのmatrixがこのrecipeを実行し、NodeとPlaywrightはUbuntuの完全gateだけに
残します。`just contract-codex` は現在のホストで選択されるinstalled public Codex CLIから
一時directoryへschemaを生成し、Stage Bに必要なsemanticを検証します。Codexの全field集合、
version、method件数は固定せず、login、account、設定、permissionを変更しません。macOSと
Windowsの各実機で明示的に実行してください。

`just check` は Ruff、mypy strict、vulture、deptry、ast-grep、Biome、Python/JavaScript
単体テスト、320/390/430px の Playwright Chromium/WebKit テスト、branch coverage、
secretlint、配布物ビルドをまとめて実行します。通常の CI は
実アカウント、マイク、Tailscale、GPU を必要としません。遅いステップを後から比較
できるよう、pytest の duration 上位も記録します。

これらの自動gateは実機acceptanceの代替にはなりません。macOSとWindowsで foreground 起動、
read-only task、local approval、音声から承認できないこと、取消／interrupt、final speechを
対話デスクトップ上で確認します。テストはlogin、OS permission、service、Tailscale Serve設定を
自動変更しません。

ライセンスは [MIT](LICENSE) です。
