# Codex rich-agent音声クライアント設計

> **Frameless Bidiによる置換:** 2026-08-19に現行Codex app-serverの生成schema、
> 公式実装、実機WebRTCを再検証した結果、Realtime v3は`delegation.*`から通常Codex
> 作業を自動実行し、そのcommentaryとfinalを同じRealtime会話へ返すことが確認された。
> この文書の「一つのapp-server接続と二つのthread」「VoiceからAgentへのhandoff」
> 「Voice modelのassistant transcriptを捨てる」「Agent finalだけを読み上げる」という
> 設計は、`2026-08-19-frameless-bidi-voice-restoration-design.md`に置き換えられる。
> 双方向RPC、schema検証、能力発見、承認Reviewer、profile、進捗表示に関する設計は
> 引き続き有効であり、一つのRealtime Threadへ適用する。

## 位置づけ

mocoは現在、Codex Realtimeとの低遅延会話を中心に構成されている。しかし、音声で
「このリポジトリを調べて直して」のような依頼を受けたとき、会話が成立することと、
Codexの通常turnが安全に仕事を完遂できることは同じではない。通常turnではコマンド、
ファイル変更、web search、MCP、apps、skills、subagentなどが動き、その途中でクライアントへ
承認や追加入力を求めることがある。現行mocoのRPCクライアントは、この双方向契約を扱えない。

本設計は、音声入力をCodex app-serverの通常Agent作業へ引き渡し、進捗と承認要求を安全に
提示し、最終結果をIrodoriで読み上げるrich clientへmocoを拡張する。macOSを基準環境とする
一方、Windows 11実機で同じ基本体験が成立することも完成条件に含める。

本設計は、次の既存方針を限定的に置き換える。

- `2026-07-30-moco-first-usable-release-design.md`の「Codex Realtimeが会話と作業をともに所有する」
  という構成を、Voice Threadと通常Agent Threadの分離へ変更する。
- 同仕様の「Windowsクライアントを対象外とする」という境界を、Windows実機対応へ変更する。
- `2026-08-01-mobile-operator-access-design.md`の公開オペレーター画面は観測とturn取消に利用できるが、
  新設する承認Reviewerには利用しない。遠隔承認は別仕様とする。

Irodori v4、12 steps、`sway`、既存の音声分割と再生最適化は変更しない。長期記憶も導入しない。

## 目的

利用者が音声で依頼すると、mocoは確定した文字起こしを画面に表示してCodex app-serverの通常turnへ
仕事を渡す。汎用の受付音声は発しない。Codexが有効な設定の範囲内で処理を進め、承認が必要な場合だけローカルの信頼済み
画面へ正確な操作内容を提示する。処理中は安全な進捗を表示し、割り込みと取消を受け付ける。
完了後は生ログではなく、Codexの最終回答を発話向けに分割してIrodoriで読み上げる。

この体験を実現しても、mocoがCodexの第二の権限設定系になってはならない。Codexの有効設定が
許したapp-server内蔵能力は、moco独自のtool allowlistを通さず利用できる。反対に、Browserや
Computer Useのようにホスト側実装を要する能力は、Codex Desktopに存在するという理由だけで
利用可能と表示しない。

## 成功条件

- app-server接続が、request、response、notification、server-initiated requestを区別する
  双方向JSON-RPC peerになる。
- mocoは起動したapp-serverのバージョンと生成スキーマから契約を判定し、未知のserver requestを
  fail-closedで処理する。
- Voice Threadは音声入力とtranscript確定だけを担い、Coordinatorは汎用の受付音声を返さずAgentへ渡す。shell、
  filesystem、web、MCP、apps、skills、subagentなどの実作業は通常Agent Threadだけが担う。
- Agent Threadは一つの会話中で継続し、「それを直して」のような後続依頼の文脈を保持する。
- Codexの有効設定が承認なしで許可した操作に、mocoが追加承認を要求しない。
- app-serverが承認を要求した操作は、request ID、操作、引数、対象範囲に結び付いたReviewer UIで
  一度だけ決定できる。
- 発話、周辺音、Voice Threadの返答、公開オペレーター画面は承認操作として扱われない。
- 通常ログ、telemetry、公開画面、音声出力へコマンド引数、ファイル内容、approval tokenを出さない。
- macOSとWindowsの両方で、foreground実行、音声入力、Agent作業、ローカル承認、取消、最終発話が
  成立する。
- 利用不能な能力、認証不足、モデル不一致、プロトコル不一致を明示し、成功したように見せる
  fallbackを行わない。

## 対象外

- CloudflareまたはTailscale経由の遠隔承認
- 遠隔画面を使ったComputer Use
- Codex Desktopの非公開実装、バンドル内部契約、private IPCの複製
- 長期記憶、会話をまたぐAgent Thread永続化、transcript保存
- Windows ServiceまたはScheduled Taskによる自動起動
- 音声だけによる破壊的操作の承認
- `danger-full-access`と`approvalPolicy=never`を組み合わせた音声起動turn
- app-serverの内部tool catalogをmoco設定へ複製すること

BrowserとComputer Useのホストアダプターは後続段階で扱う。本設計には境界と導入条件を記すが、
最初の実装計画には含めない。

## 検討した方式

### 採用: 一つのapp-server接続と二つのthread

一つのapp-serverプロセスと双方向RPC接続を共有し、その上にVoice ThreadとAgent Threadを分ける。
Voice Threadは実験的Realtime経路を低遅延の音声入力とtranscript確定に利用するが、taskの回答や
tool実行を所有しない。Agent Threadは公開された通常の`turn/start`経路を使い、Codexの
rich-agent機能を所有する。

この方式なら、Realtimeが将来どのtool catalogを公開するかに依存せず、通常Codex turnと同じ
設定・承認・進捗契約を利用できる。接続監視と能力発見を共有できるため、threadごとにapp-serverを
起動する必要もない。

### 不採用: Realtime Threadだけで全作業を行う

現在の体験に最も近いが、実験的Realtime会話が通常turnと同じtool catalog、承認要求、MCP、apps、
skillsを常に公開する保証がない。能力差を推測すると、利用できない処理を利用可能と表示するか、
Realtime固有の制約をmocoへ固定化することになる。

### 不採用: Codex Desktopの内部機能をproxyする

DesktopのBrowserやComputer Useをそのまま継承できるように見えるが、公開app-server契約の外側へ
依存する。macOSとWindowsでprivate実装も異なり、更新で破損する。Windows Storeパッケージ内の
実行ファイルが外部プロセスから直接実行できない実機結果も、この方式を支持しない。

## 全体構成

```text
Browser Media / Hotkey
        │ audio, control
        ▼
InteractionCoordinator ──────────────── EventProjector ── Operator UI
        │ handoff / cancel / speech             ▲
        │                                        │ safe events
        ▼                                        │
Codex integration package                        │
  ├─ VoiceSession ─── Voice Thread               │
  ├─ AgentSession ─── Agent Thread ──────────────┘
  ├─ CapabilityDiscovery
  └─ InteractionBroker ── Local Reviewer UI
        │
        ▼
RpcPeer + CodexConnectionSupervisor
        │ bidirectional stdio
        ▼
codex app-server
        │
        ├─ shell / filesystem / web / MCP / apps / skills / subagents
        └─ future explicit host tool calls

Agent final text ── SpeechQueue ── Irodori ── Browser playback
```

これは七つの責務単位を示す。実装時に七つの新規top-level packageや抽象基底クラスを作るという
意味ではない。既存moduleへ収まる責務はそのまま置き、状態所有者とセキュリティ境界だけを分離する。

1. `RpcPeer`と`CodexConnectionSupervisor`
2. VoiceSession、AgentSession、能力発見を含むCodex integration package
3. `InteractionCoordinator`
4. `InteractionBroker`
5. 純粋な`EventProjector`
6. `OperatorGateway`と既存Browser Media
7. パス、プロセス起動、保護状態を扱う小さなOS別helper

Browser/Computer Use用の空のregistry、遠隔承認framework、実行時JSON Schema UI generatorは先に
作らない。必要な公開契約が確認できた段階で追加する。

## 双方向RPC契約

### message分類

app-serverのwire messageは`jsonrpc`fieldを要求しない。mocoは判定順に依存せず、次の排他的な条件で
messageを分類する。

1. server requestは文字列`method`と有効な`id`を持ち、`result`も`error`も持たない。
2. notificationは文字列`method`を持ち、`id`、`result`、`error`を持たない。
3. client requestへのresponseは`method`を持たず、有効な`id`と`result`または`error`のちょうど
   一方を持つ。
4. 複数条件の重なり、必要fieldの欠損、`result`と`error`の同居はmalformed messageである。

Request IDは整数または文字列であり、受信から応答まで型と値を変えない。client request用IDと
server request用IDは別方向の名前空間として扱い、現在のように「IDがあればresponse」とは
判定しない。JSON booleanはPython上で整数のsubtypeであってもRequest IDとして拒否する。

`RpcPeer`はclient requestのpending map、server request dispatcher、notification streamを所有する。
個別sessionはstdio readerを直接消費しない。server request handlerが例外を起こした場合は、
app-serverが定めるerror responseを一度だけ返し、接続全体の成功へ置き換えない。

client requestの期限はresponse待ちだけでなく、共有write lockの待機とstdinの`drain`を含む。
notificationとserver request responseも同じbounded writeを使い、期限超過は接続全体を
terminalizeする。故障したapp-serverがstdinを読まない場合も、無期限にleaseを保持しない。

同じserver Request IDが未解決のまま再受信された場合は、先行handlerを取消し、そのIDへ一度だけ
protocol errorを返して接続を閉じる。`method`を含むmalformed requestから有効なIDを一意に取り出せる
場合もhandlerを呼ばず一度だけerror responseを返す。`method`のないmalformed responseはserver request
responseを捏造せず、対応するclient pending requestをprotocol errorで失敗させて接続を閉じる。IDを一意に
決められないmessageも応答せず接続を閉じる。いずれの場合も同じserver Request IDへsuccessとerrorの
両方を送らない。

### バージョンとschema

起動時に`initialize`結果と実行ファイルのversionを記録する。`codex app-server
generate-json-schema`が利用できる場合は、その実行ファイル自身からstableまたはexperimental
schemaを生成し、semantic capabilityを判定する。生成物は一時領域だけに置き、認証情報や
thread内容を含めない。

schema probeのstdout/stderrは一時fileへ受けて各256 KiBまで読み、生成bundleは512 files、
1 document 1 MiB、合計16 MiBまでとする。子孫processがstdout/stderrを継承してもpipeのEOFを待たず、
超過、symlink、読取中のsize変化はfail-closedにする。

テストや実装は特定versionの全field集合を固定しない。notification、進捗event、認可に影響しない
metadataは、required fieldを検証した上で未知fieldを保持または無視できる。command、file、permission、
MCP/app、host actionなどのauthorization requestは別扱いとし、生成schemaに対応する認識済みtyped
adapterだけを受理する。adapterが意味を説明できないfield、decision、scopeがあればReviewerを出さず、
schema mismatchとしてturnを停止する。認可requestでforward compatibilityを推測しない。

method名がversion間で変わる場合は、生成schemaに存在する候補から一つを選ぶ。たとえば実機ではapps
列挙契約に差があったため、単一のmethod名を全versionへ送らない。

生成schemaをReviewer UIの自動生成には使わない。UIが受け付けるdecisionは、typedなserver request、
そのrequestが提示する選択肢、app-server response契約、mocoの信頼境界の積集合で決める。
`threadId`と`turnId`を持つmodern approvalは、AgentSessionが所有する現在のactive turnと全一致する場合
だけ公開する。直近のterminal通知はbounded tombstoneでも拒否し、別turn完了後に古いapprovalを
再公開しない。turn identityを持たないlegacy familyは生成schemaの専用adapter契約を維持する。

### 接続監視

`CodexConnectionSupervisor`はapp-server process、stderr drain、reader task、起動handshake、終了を
一つの所有者へ集約する。未実行時の接続失敗はbounded backoffで再試行できるが、実行中のprivileged
turnは再接続後に自動再送しない。どこまで実行されたか判定できないためである。

接続が切れた場合は次を行う。

- pending client requestを明示的なconnection-lost errorで完了する。
- pending server requestを取消し、Reviewer UIを閉じる。
- 実行中Agent turnを結果不明として扱い、成功発話を行わない。
- Agent notification購読だけが先に終端した場合もownerへ一度だけ通知し、idleを含むlease全体を閉じる。
- browserへ安全なfailure codeを表示する。
- 新しい接続では能力snapshotを再取得する。

未知notificationは内容を転送せず、genericな`codex_activity`として観測できる。未知server requestは
応答が必要なため、generic成功ではなくfail-closedとする。

## 能力発見とCodex設定

### policyの所有者

個々の操作を認可するpolicy engineはCodexの有効設定とapp-serverだけである。mocoはtool、command、
path、MCP/appを独自allowlistで再判定しない。一方、音声からAgent turnを開始してよいかを判断する
admission safety ceilingはmocoが所有する。この上限は操作を許可せず、危険な組み合わせのturn開始を
拒否するだけである。

mocoのAgent profile modeは次の三つに限定する。

- `read_only`: 新規設定の既定値。生成schemaに対応するread-only semantic profileをapp-serverへ渡す。
- `workspace_write`: 利用者がローカルOperator UIで明示選択した場合だけ、対応するsemantic profileを渡す。
- `inherit_codex`: sandbox、approval policy、permission profileを上書きせず、Codexの有効設定をそのまま使う。

profile modeの変更はローカルOperator UIの信頼済みcontrolと設定fileだけから受け付ける。音声、通常hotkey、
公開画面からは変更できない。mode未設定時は`read_only`であり、Codex設定のprovenanceを推測して自動的に
`inherit_codex`へ切り替えない。利用者が「Codex側で許可した能力をmocoでもそのまま使う」場合の正式な
選択肢は`inherit_codex`である。

選択modeの範囲でCodexがpromptなしに許可した操作はmocoでもpromptなしで進む。Codexがpromptを返せば
mocoはReviewerへ運び、Codexが拒否すればmocoも拒否を表示する。設定を読み直して独自に同じ判断を
再実装しない。

Capability discovery は global effective policy を観測結果として保持するが、明示 profile の
admission 条件には使わない。`read_only` と `workspace_write` は thread 作成時に各 profile の
policy を明示し、`inherit_codex` だけが global effective policy を継承する。`inherit_codex` で
effective policy を正規化できない場合、または `danger-full-access` かつ `approvalPolicy=never`
の場合は admission safety ceiling で拒否する。

### snapshot

CapabilityDiscoveryは、実行中versionが提供するmethodを使って次のsemantic categoryを段階的に
snapshot化する。

- accountとauthentication readiness
- effective configとmanaged requirements
- model catalogと選択model
- permission profileまたは同等のsandbox/approval情報
- experimental feature
- MCP server status
- appsまたはpluginの導入・認証状態
- skills
- Realtime conversation
- steer、interrupt、server request category

ここで列挙するのはmocoが必要とする意味上の分類であり、wire method名の固定listではない。shellや
web searchなどapp-server内部で完結するtoolの全名前も複製しない。

各categoryは`available`、`disabled`、`authentication_required`、`host_adapter_required`、
`version_mismatch`、`error`のいずれかになる。必須categoryが満たされなければAgent handoffを開始しない。
任意categoryの失敗はdegraded readinessとして表示する。選択model、profile、MCP、app、Realtimeが
見つからない場合は、別のものへ黙って変更しない。

段階Aで必須なのはinitialize/version、account readiness、Realtime、interruptと、その段階の
server request categoryだけである。effective policyは`inherit_codex`のadmissionに限り必須であり、
明示profileではprobe失敗をdegraded readinessとして表示してもAgent handoffは止めない。model catalog、
MCP、apps、skillsの完全snapshotは、Agent作業または該当interactionを公開する段階B/Cで追加する。

設定、MCP、app、skill、account、modelの変更notificationを受けた場合はsnapshotをinvalidにし、
次の会話または安全なidle境界で再取得する。実行中turnのpolicyを途中で推測し直さない。

### Codex実行ファイル

明示設定された実行ファイルまたはcommandを最優先し、未指定時はPATH上の公開Codex CLIを探索する。
macOSでは公開CLIまたは読み取り・実行可能な公式bundle resourceを利用できる。WindowsではPATH上の
独立CLIを利用し、Microsoft Store package内部のprivate pathを直接実行しない。

実行ファイルが見つかっても、それだけでreadyにはしない。initialize、schema生成または既知の
version query、account readiness、必須semantic capabilityまで確認する。CLI update、login、OS権限変更は
mocoが自動実行しない。

## VoiceからAgentへのhandoff

### threadの所有権

app-server接続ごとに、ephemeralなVoice ThreadとephemeralなAgent Threadを作る。Voice Threadは
microphone音声、VAD、user transcript確定だけを扱う。Voice modelのassistant transcriptは
task結果として表示または発話せず、作業中の中間音声も発しない。
Agent Threadは通常turnと会話文脈を所有する。

利用者のutteranceが確定すると、Coordinatorはそのtextをhandoff requestとしてAgent Threadへ渡す。
Voice ThreadへAgentの最終回答を注入しない。二つのthreadが互いのassistant発話を再解釈して文脈を
二重化することを防ぐためである。

Agent Threadは同じmoco会話中で継続する。新しいVoice接続を作り直しても、利用者が会話を明示終了
していなければAgentのthread IDを維持する。daemon再起動や会話終了を越えて保存しない。

### 状態model

Coordinatorは一つのimmutable snapshotに四つの直交軸を持つ。

- Connection: `starting`、`ready`、`degraded`、`disconnected`
- Voice: `idle`、`listening`、`transcribing`
- Task: `none`、`queued`、`running`、`waiting_review`、`completed`、`failed`、`interrupted`
- Speech: `silent`、`synthesizing`、`playing`

UI用の状態とidle判定はこのsnapshotから導出する。既存LifecycleControllerとCoordinatorが同じ状態を
別々に所有する構成にはしない。idleはlistening、実行中Task、pending interaction、合成、再生の
いずれも存在しない場合だけ成立する。

### 進捗と発話

進捗はUIへ非同期表示し、raw logやgeneric acknowledgementを読み上げない。完了時は
Agentの最終回答だけを同一の文字列でUIと既存SpeechQueueへ渡す。失敗時もstable
error categoryに対応する同一の短い説明をUIと発話に使い、実行していない処理を
完了したとは言わない。対応するuser transcriptのpresentationをFIFO barrierとして待ち、fastな
Agent finalが依頼文より先に画面へ出る順序逆転を防ぐ。

新しいutteranceは現在の合成・再生generationを先にinvalidateする。Task自体を取消すか、追加指示に
するかは現在状態で決める。

### steer、queue、interrupt

Agentが`running`中に新しいutteranceが確定した場合、能力snapshotがsteerを提供すれば現在turnへ
追加する。提供しなければ一件だけqueueする。二件目は古いqueued utteranceを黙って上書きせず、
busyとして拒否する。

`waiting_review`中は承認対象の意味を音声で変更しない。新しいutteranceは一件だけqueueできるが、
承認decisionにはならない。UIまたは明示controlからの取消は`turn/interrupt`へ対応する。

## InteractionBrokerとReviewer

### 信頼境界

Browser MediaとReviewerは同じローカルHTMLを共有できるが、WebSocketと認証materialを分ける。
processはmedia slotを一つ、local reviewer slotを一つだけ持つ。reviewer接続はloopback Host、loopback
Origin、一回限りのreview bootstrapが一致した場合だけ受理する。

公開Cloudflare画面やtailnet経由画面へreview control secretやbootstrap nonceを渡さない。公開画面は
`waiting_for_local_review`と
turn全体のcancelだけを表示し、操作詳細や個別decisionを受け取らない。

Reviewerは通常のmedia URLから自動的に有効化しない。利用者がローカル端末で`moco review`を実行すると、
CLIはowner-private stateにあるcontrol secretでloopback daemonへ認証し、30秒だけ有効な一回限りの
bootstrap nonceを一つ取得する。CLIはnonceをURL fragmentに入れて固定のlocal review URLを開く。
local HTMLはnonceをmemoryへ読み取ると直ちに`history.replaceState`でfragmentを除去し、review用
WebSocketの最初のmessageでredeemする。nonceをquery、cookie、local storage、session storageへ移さない。

daemonはnonceを発行時のprocess generationへ結び付け、最初のredeemで消費する。期限切れ、再利用、
別Origin、別Host、非loopback接続は拒否する。redeem後のreview capabilityはそのWebSocket接続だけに
束縛し、切断時に破棄する。再接続には新しい`moco review`が必要であり、長寿命のreview tokenをbrowserへ
渡さない。control secretと発行済みnonceはmedia認証に使えず、media tokenから発行もできない。

### request lifecycle

Brokerはserver requestをmethod別のtyped immutable valueへ変換し、元のRequest IDをそのまま保持する。
UIには一回限りのopaqueな`reviewHandle`を発行する。UIから受理するresponseは原則として次の形だけに
する。

```json
{"reviewHandle":"opaque-value","decision":"accept"}
```

UIはRequest ID、method、任意JSON responseを送らない。BrokerがreviewHandleから元requestと
許可decision集合を引き当て、version固有のapp-server responseへ変換する。payloadのcanonical hashや
fingerprintは作らない。methodごとに意味が異なるapproval IDやsecret fieldを誤って同一視するためである。

requestは`pending`から`resolved`、`cancelled`、`connection_lost`のいずれか一つへだけ遷移する。
二重click、遅延response、別接続のhandle、期限後responseは拒否する。app-serverがtimeoutを指定しない
requestへmoco独自の短いtimeoutを加えない。

Reviewer接続が失われたままdecision不能になった場合はfail-closedでserver requestを終了し、必要に
応じてAgent turnをinterruptする。Voice Threadや自動音声認識結果へ代替承認を求めない。

### method別decision

共通Broker lifecycleの上に、method別adapterを置く。最初のsliceはcommand executionとfile changeに
対するone-shotの`accept`、`decline`、`cancel`を扱う。次のsliceでpermission request、MCP/app
elicitation、user inputを追加する。

session継続承認、permission amendment、network amendmentなど、後続操作にも効くdecisionはone-shotと
同じボタンにしない。影響範囲の確認を一段追加し、app-server response契約が提供する場合だけ表示する。

### 表示内容

Reviewer UIだけは、承認判断に必要なコマンド、引数、対象path、MCP/app名、requested scopeを表示できる。
表示値はtextとしてescapeし、HTMLとして解釈しない。通常activity、telemetry、音声、公開画面には
同じ詳細を投影しない。

decision buttonはrequestから導出した許可集合だけを表示する。既定focusを`accept`へ置かず、Enter、
Space、音声、通常hotkeyによる即時acceptを許可しない。cancelとdeclineの意味がapp-server contractで
異なる場合は区別して返す。

## EventProjectorと進捗表示

EventProjectorはapp-server eventを副作用なしで安全なOperator eventへ変換する。既存のcommand、
file change、MCP、dynamic tool、subagent、web search、image、compaction mappingを利用するが、event名が
存在するだけでclient tool bridgeが実装済みとは判定しない。

通常activityは次の情報に限定する。

- category
- started、completed、failed、waitingなどのphase
- bounded elapsed time
- thread/turnと内部相関する非秘密ID
- stable error code

コマンド本文、filesystem path、patch、MCP arguments、model reasoning本文は通常activityへ含めない。
reasoning summaryはapp-serverが明示的に公開した要約だけを既存方針の範囲で扱う。

Realtime adapterとOperatorへの進捗送信はそれぞれbounded queueで直列化する。Realtime eventの
overflowはVoiceをfail-closedにし、Operator送信のoverflowはtask結果、transcript、speechと分離して
進捗だけを省略する。いずれもeventごとの無制限task生成やpayloadを含む警告ログを許可しない。

未知notificationはgeneric activityへ縮退できるが、未知の成功や能力名を作らない。能力snapshotとeventが
矛盾した場合はruntime mismatchとして表示し、利用可能表示を更新する。

## macOSとWindows

### 実行model

各ホストでmocoとCodex app-serverをローカル実行する。macOS上の音声依頼はmacOS上のCodex設定と
workspaceを使い、Windows上の音声依頼はWindows上のCodex設定とworkspaceを使う。Tailscale越しに
MacのmocoからWindowsのstdio app-serverを暗黙proxyしない。

この分離により、shell、path、sandbox、MCP command、認証、OS権限はCodexが動くホストと一致する。
クロスホスト実行が必要になった場合は、remote executorとして別の公開契約を設計する。

### OS差分

大きなPlatform frameworkは作らず、実際に差がある次の関数群だけを分離する。

- config、prompt、runtime stateの既定path
- Codex executableまたはcommandの探索とprocess spawn
- owner-private runtime directoryの作成と検証
- global hotkeyのreadiness説明
- service commandの公開可否
- browser open

macOSは現在のApplication Support、launchd、Input Monitoringを維持する。Windowsは`APPDATA`へconfig、
`LOCALAPPDATA`へruntime stateを置き、foreground `moco run`を正式経路とする。`moco service`はWindowsで
launchdを呼ばず、明示的なunsupported codeを返す。

global hotkeyは既存pynput経路をWindowsでも試し、利用不能時はbrowser内hotkeyを明示的に提示する。
Windows固有の失敗をmacOS Input Monitoring不足と表示しない。

### Windowsの保護状態

Windowsでは`os.fchmod(0o600)`をowner-only ACLとして扱えない。実機でもfile modeは`0666`のままで、
現行のPOSIX mode検査は正常なstate fileを拒否した。また通常の`LOCALAPPDATA`にはCodex sandbox用accountの
変更権限が継承されていた。

`moco.yaml`の`codex.command`も次回起動時の実行境界である。`moco config init`はWindowsのconfig
directoryとfileをcurrent user、SYSTEM、BUILTIN\Administratorsだけのprotected DACLで新規作成する。
読み込み時もdirectoryとfileのowner、DACL、reparse point、読み込み中のpath identityを検証する。
既存pathが不適合なら自動修復せず、明示config pathを含めて作成・読み込みを拒否する。

control secretを通常directoryへ保存してはならない。固定path
`%LOCALAPPDATA%\moco\runtime-private`が存在しない場合だけ、Python 3.13がWindowsで特別扱いする
`mode=0o700`を指定して新規作成する。secretを含むstate fileはそのdirectory内へ置く。

起動時にruntime-privateがreparse pointでないこと、owner SIDがcurrent userであること、DACLが適合する
ことを検証する。許可ACEはcurrent user、SYSTEM、BUILTIN\Administratorsだけに限定し、Codex sandbox
account、一般user/group、継承された他主体の許可ACEがあれば起動を拒否する。既存directoryのownerや
ACLは自動修復せず、random fallback directoryも作らない。AdministratorとSYSTEMによる侵害はこの
秘密保護の脅威model外とする。

### browser media

WindowsでもEdgeまたはChromeからloopback Operator UIを開く。`localhost`はbrowserのsecure contextとして
microphoneを要求できるが、初回のsite permissionは利用者が明示許可する。mocoはbrowserやWindowsの
microphone permissionを自動変更しない。

### 実機調査で確認した前提

対象Windows 11 Pro x64ではPython 3.13、uv、Git、Node、Edge、Chrome、音声deviceが利用でき、現在の
lockfileはWindows向け依存解決に成功した。PATH上のCodex CLIはstdio app-server、Realtime指定、
schema生成、主要server request categoryを提供した。ただし調査時点ではCLI accountがreadyではなく、
method集合もmacOSの別versionと完全一致しなかった。

したがってWindows ready条件はOS名ではなく、選択CLIのinitialize、account、schema、semantic capability
から判定する。調査時のversionやmethod名をproduction constantにはしない。

## Tailscale境界

TailscaleはWindows実機の到達性、Irodori HTTPS、開発中のOpenSSH接続に利用できる。Tailscaleがonlineで
あることは、loopback Operator UIやstdio app-serverが遠隔公開されたことを意味しない。

調査時点でWindows nodeのTailscale Serve `:443 /`は既存Irodori serviceへ転送されている。この設定を
moco導入のために変更、reset、上書きしない。moco UIを将来tailnetへ公開する場合は、別pathまたはport、
HTTPS/WSS、Origin/Host、media認証、review認証の分離を新しい仕様で定める。

Windowsへの調査接続はtraditional Windows OpenSSHをtailnet上で使用する。Tailscale SSH serverは
Windowsをsupportしないため、実機test手順で両者を同一視しない。

## BrowserとComputer Use

BrowserとComputer Useはapp-server内部toolではなく、結果を実行・観測するhost surfaceを要する。
Codex Desktopの設定に能力が見えても、moco側adapterがなければ`host_adapter_required`と表示する。

後続adapterは、公開app-serverのdynamic toolまたは同等の公開request契約を利用できることを先に
実証する。Desktop private IPCやbundle解析を採用しない。adapterは次の責務を持つ。

- tool inputのtyped validation
- per-appまたはper-site approval
- screenshot、DOM、操作結果のbounded rendering
- cancellationとtimeout
- macOS Screen Recording/AccessibilityまたはWindows相当権限のreadiness
- Agent Threadとhost actionの相関

Computer Useの操作承認は通常command approvalへ混ぜない。画面状態が変化し、表示した内容と実行時の
対象がずれるため、操作直前のhost stateとscopeを結び付ける必要がある。

## failureとreadiness

Operator UIは少なくとも次を区別する。

- Codex process unavailable
- initializeまたはschema mismatch
- authentication required
- required capability unavailable
- selected model/profile unavailable
- MCP/app/skill degraded
- Voice unavailable、Agent available
- Agent unavailable、Voice available
- local review required
- local reviewer disconnected
- turn interrupted
- connection lost with outcome unknown
- Irodori unavailable

Voiceだけがreadyな状態でAgent作業を受け付けてはならない。音声経路の接続確認はできてもtaskを
実行できないことを明示する。Agentだけがreadyでmicrophoneが使えない場合は、Operator UIにtext入力を
新設するのではなく、現行の音声要件を満たさない状態として表示する。text composerは別要件である。

errorはstable codeと安全な説明を持つ。外部error本文、command、path、tokenをbrowserやtelemetryへ
直接転送しない。retry可能性が分かる場合だけ再接続actionを提示する。

## 段階的delivery

### 段階A: protocol基盤

- `RpcPeer`を双方向化する。
- connection supervisorと、initialize/version、account、effective policy、Realtime、interrupt、必要な
  server request categoryに限定したschema-based capability discoveryを追加する。
- VoiceSessionを新しいpeer上へ移し、既存音声体験を維持する。
- macOS/WindowsのCodex起動、path、protected runtime stateを整える。
- Windows CIとfake app-server contract testを追加する。

この段階では新しいprivileged Agent UIを公開しない。

### 段階B: 最初に使えるAgent作業

- AgentSessionとhandoff reducerを追加する。
- 現行の固定`read-only`/`approvalPolicy=never`指定を、三つのprofile modeとeffective policy表示へ
  置き換える。
- commandとfile changeのone-shot approvalをBrokerとlocal Reviewerで扱う。
- safe progress、interrupt、final-only speechを接続する。
- macOSとWindowsの両方でread-orientedな通常作業を実機確認する。

shell、filesystem、web search、設定済みMCPなど、app-server内で完結しCodexが許可した能力は
moco固有allowlistなしで利用できる。未対応server requestが必要になったturnはfail-closedにする。

### 段階C: rich-client interaction完成

- permission request
- MCP/app elicitation
- request user input
- dynamic host tool requestの明示的unavailable responseまたは対応済みadapter
- session継続decisionとscope確認
- config、account、MCP、app、skill refresh

この段階で、明示的host adapterを要するBrowser/Computer Useを除き、active Codex configで許可された
通常Agent能力の中継を完成扱いにする。

### 段階D: optional host adapter

公開integration pathとOS permissionを実証してから、Browser、Computer Use、遠隔画面、遠隔承認を
それぞれ独立した仕様と実装計画で追加する。

## テスト戦略

すべてのbehavior changeはRed、Green、Refactorで進める。対象testを先に失敗させ、最小実装で通し、
重複を整理した後も同じtestを維持する。

### unit test

- request、response、notification、server requestの分類
- malformed overlap、boolean ID、重複server Request IDのfail-closed処理
- integer/string Request IDの保持
- pending requestとincoming requestの独立
- Coordinator reducerの直交状態とidle導出
- 中間音声を発さず、final resultだけを同一文でUI/TTSへ投影すること
- steer利用可否、一件queue、busy拒否
- Brokerのexactly-once、二重click、遅延handle、別接続handle
- authorization requestの未知field/decisionによるschema mismatch
- review bootstrap nonceの期限、single use、process generation、再接続
- method別decision集合とresponse変換
- EventProjectorの秘密情報除去
- capability snapshotとrequired/optional判定
- OS別path、Codex command探索、Windows protected directory
- Windows runtime-privateのreparse point、許可ACE、unsafe既存path拒否

### contract test

fake app-serverを子processとして起動し、server requestをmocoへ送り、正しいresponseが同じID型で一度だけ
返ることを確認する。approval待機中のinterrupt、reviewer切断、app-server終了、malformed message、
unknown request、late responseも含める。

実Codex contract testは、対象端末の`generate-json-schema`出力からsemantic capabilityを導出する。
productionのversion、method件数、tool名、field順序を固定しない。required field欠損と互換alias選択は
検証する。authorization requestの未知fieldまたは未知decisionではReviewerを出さず、schema mismatchで
turnが停止することも確認する。

### browser test

- media認証とreview control/bootstrapの相互利用拒否
- `moco review`の一回限りbootstrapとfragment即時除去
- loopback reviewer Origin/Host制約
- remote connectionに詳細やdecision controlが出ないこと
- approval textのescape
- acceptへの既定focusやkeyboard即時承認がないこと
- cancellationとdisconnect表示
- progressとfinal speechが競合しないこと

### cross-platform CI

Ubuntuの`just check`を全品質gateの基準として維持する。既存のmacOS単一test jobはmacOS/Windowsの
Python matrixへ置き換える。ただし全OSで完全gateを重複実行しない。採用する最小構成は次の三jobである。

1. `Quality / Ubuntu`は唯一の完全gateとして`just check`を実行する。Ruff、mypy、coverage、Node unit、
   Playwright、secretlint、buildはここで一度だけ実行する。
2. `Python platform / macOS`は新しい`just test-python`で`live`と`slow`を除くPython test全件を実行する。
3. `Python platform / Windows`も同じ`just test-python`を実行し、Windows filesystem、ACL、path、
   subprocess、loopback、pynput fallback、fake app-server transportを実OSで検証する。

macOSとWindowsは一つのmatrix jobとして定義できる。現行Python suiteは十分短いため、OS別test fileの
手動列挙や細かなmarkerを増やさない。現在のmacOS jobが実行するmocked integrationはUbuntuでも実行されて
いるため、単一fileの重複jobではなくPython全件matrixへ置き換える。

fake app-server fixtureはUnix shebangを直接実行せず、`sys.executable`とscript pathから起動する契約へ
変更する。これにより同じ双方向contract testをmacOSとWindowsで使う。

実Codex schema検証は`just contract-codex`として分離し、各段階のMac実機とTailscale越しWindows実機で
実行する。GitHub hosted CIがPRごとに最新Codexを取得する構成は、上流更新だけで無関係な変更を壊すため
初期scopeに含めない。必要性が実証された場合は、blocking PR gateではなくscheduled compatibility監視を
別途検討する。

既存のuv/npm cacheを維持し、matrixへNodeやPlaywrightを導入しない。追加のcache、sharding、path gateは、
測定されたcritical pathまたはfailure isolationに効果がある場合だけ扱う。CI最適化はWindows対応に伴う
実時間悪化や重複が確認された範囲で本featureのscopeに含め、一般的なCI刷新へ広げない。

### 実機acceptance

macOSとWindowsで次を確認する。

1. `moco doctor`が選択Codex binary、account、schema、profile、Realtime、Irodori、microphone、hotkeyを
   正しく報告する。
2. local browserで音声依頼を開始できる。
3. fast taskとlong-running taskのどちらも中間音声を発さない。
4. read-only taskが通常Agent turnで完了する。
5. command/file approvalがlocal Reviewerだけへ出る。
6. voiceの「はい」や公開画面から承認できない。
7. `workspace_write`または`inherit_codex`をローカルで明示選択し、Codexが許可した場合だけ変更taskが進む。
8. interruptでturnと古いspeech generationが止まる。
9. app-server切断後に実行中turnを再送しない。
10. final answerだけがIrodoriで読み上げられる。

Windows実機への自動調査はtraditional OpenSSH over Tailscaleを利用できるが、login、OS permission、
service、Tailscale Serve設定をtestが変更しない。microphone、hotkey、Reviewerの最終確認はinteractive
desktopで行う。

各段階の完了前にfocused testを通し、最後に`just check`を実行する。完了判断の前には独立code review、
approval境界のsecurity review、polishment、AI由来の不要重複確認を行う。

## privacyとtelemetry

mocoはtranscript、audio、生成speech、command全文、patch、approval payload、MCP argumentsを永続化しない。
telemetryにはcategory、phase、bounded duration、stable error code、非秘密相関IDだけを送る。

Reviewerが一時表示する詳細はprocess memoryだけに保持し、decision後、cancel後、disconnect後に破棄する。
browser history、query、local storage、session storageへreview capabilityやrequest詳細を保存しない。
一回限りのbootstrap nonceだけはreview URLのfragmentで受け渡し、document初期化時に即時除去する。

app-server stderrはdrainするが、通常ログへそのまま転送しない。必要なerror classificationだけを抽出し、
生本文はdebug modeを含め保存しない。

## 受け入れ基準

- 音声依頼がVoice Threadから通常Agent Threadへ一度だけhandoffされる。
- Agent Threadが同一会話中の後続依頼を解決できる。
- active Codex configで許可されたapp-server内蔵能力にmoco独自allowlistが介在しない。
- profile mode未設定時は`read_only`になり、`inherit_codex`をローカルで選ぶとCodex設定を上書きしない。
- `read_only`と`workspace_write`はglobal effective policyをadmission条件にせず、`inherit_codex`で
  effective policyを正規化できない場合、または`danger-full-access`かつ`approvalPolicy=never`の場合に
  音声からAgent turnを開始しない。
- app-serverがpromptを要求しない操作に追加Reviewerを出さない。
- app-serverがpromptを要求した操作はlocal Reviewer以外から承認できない。
- authorization requestに未対応field、decision、scopeがあればReviewerを出さずfail-closedになる。
- `moco review`のbootstrap nonceは一回限りで、長寿命のreview capabilityがbrowserへ渡らない。
- string/int Request IDを変えず、server requestへexactly onceで応答する。
- 未知server request、reviewer disconnect、connection lossがfail-closedになる。
- remote Operator、voice、telemetryへapproval詳細が漏れない。
- cancellationがAgent turnと古いspeechを停止する。
- capability、authentication、model、MCP/app、host adapter不足を明示する。
- macOSとWindowsで同じ基本workflowが実機合格する。
- Windowsのcontrol secretがCodex sandbox accountから読めるdirectoryへ置かれない。
- Windowsのunsafeな既存runtime-private pathを修復・迂回せず、起動を拒否する。
- Browser/Computer Useをadapter未実装時に利用可能と表示しない。
- runtime-derived testがproduction version、tool list、speaker list、payload field集合を固定しない。
- repositoryの`just check`が成功する。

## 実装計画への分割

本設計承認後も、段階AからDを一つの巨大planへまとめない。最初のimplementation planは段階Aだけを
対象とし、双方向RPC、能力発見、OS基盤、既存Voice回帰を完了させる。段階B以降は前段の実測結果と
生成schemaを確認して別planにする。

この分割により、最初のusable sliceへ不要なBrowser/Computer Use abstractionや遠隔承認frameworkが
入り込むことを防ぎ、Windowsを後付けにせず各段階のcontractへ含められる。
