# Frameless Bidi音声アーキテクチャ復旧設計

## 位置づけ

本設計は、`2026-07-30-moco-first-usable-release-design.md`にある「GPT-Liveが
会話を所有し、必要な作業をCodexへ委譲する」という構成を、Codex Realtime v3の
公開Frameless Bidi契約に沿って復旧する。

`2026-08-07-codex-rich-agent-client-design.md`で導入したVoice Threadから別Agent
Threadへのclient-managed handoffは置き換える。同設計で追加した双方向RPC、生成schema
検証、能力発見、承認broker、Reviewer、進捗表示、profile、再接続、Irodori v4との契約は
維持する。長期記憶は引き続き対象外とする。

## 確認したsource of truth

対象macOSのChatGPT.appにはCodex `0.148.0-alpha.15`があり、その実行ファイルが生成する
schemaはRealtime v3と次のfieldを公開している。

- `clientManagedHandoffs`
- `delegationAckFiller`
- `codexResponsesAsItems`
- `codexResponseHandoffMode`
- `realtimeStartInstructions` / `realtimeEndInstructions`
- `initialItems`

同じ環境のPATH上にあるCodex `0.144.1`は必要なv3契約をすべて備えていない。mocoは
version番号をallowlist化せず、実際に選択したbinaryが生成するschemaを検査する。必要契約を
満たさないbinaryへv2 fallbackしない。

公式実装では、`clientManagedHandoffs`の既定値はfalseであり、v3のdelegation結果は自動的に
同じRealtime会話へ返る。`codexResponseHandoffMode: "bemTags"`ではCodexのcommentaryが
commentary channel、finalがspeakable channelへ送られる。`delegationAckFiller: true`は
delegation acknowledgementを有効にする。

実機probeでは、WebRTC音声から一つの`delegation.created`が発生し、同じitemへCodexの
commentaryとfinalが追加され、同一Threadの一つのturnとして完了した。別Agent Threadも
`appendText`も不要である。

## 目的

- Realtimeを会話の唯一の所有者にする。
- Realtimeが必要な作業をCodexへ自動delegationし、acknowledgement、speakable progress、
  finalを同じ会話へ受け取る。
- Realtimeの標準音声は再生せず、speakable transcriptを既存SpeechQueueからIrodoriへ一度だけ
  渡す。
- user speech開始時のbarge-in、動的caption、voice catalog、generation、readiness、Reviewer、
  profileを維持する。
- transcript、音声、credentialを永続化しない。

## 採用する構成

一つのapp-server接続と一つのephemeral Realtime Threadを会話leaseの所有単位にする。

```text
Browser microphone
  -> Realtime v3 WebRTC / VAD
  -> delegation.created
  -> Codex work on the same Thread
  -> automatic commentary / speakable append
  -> assistant transcript
  -> SpeechQueue
  -> Irodori v4 WAV
  -> Browser playback
```

`clientManagedHandoffs`はfalse、`codexResponsesAsItems`はfalseと明示する。mocoはuser finalを
`turn/start`へ再送せず、`appendText`または`appendSpeech`でCodex応答を再注入しない。これにより、
実行所有者と読み上げ所有者をそれぞれ一つに限定し、二重実行と二重読み上げを構造的に防ぐ。

別Agent Threadを製品経路から外すが、通常turn用adapterの既存単体テストは削除しない。将来別の
非音声入口から利用する可能性と、app-server契約の回帰検出に用いる。

### 不採用案

- 二つのThreadを維持する案は、Framelessの自動deliveryを捨て、acknowledgementと中間応答を失い、
  同一依頼を二重に実行する危険があるため採用しない。
- `clientManagedHandoffs: true`で`appendSpeech`する案は、mocoがCodex応答の順序と重複排除を再実装
  することになるため採用しない。
- v2 fallbackは、WebRTCで未対応であり、能力不足を成功に見せるため採用しない。

## moco Realtimeプロンプト

mocoは`thread/realtime/start.prompt`へリポジトリ管理の専用プロンプト全文を渡す。これは公式既定
Realtimeプロンプトへの追記ではなく完全な置換である。mocoプロンプトは少なくとも次を一体の
契約として持つ。

- mocoの人格、自然で簡潔な日本語、音声会話としての応答規則
- Realtimeが統一された会話面を所有すること
- actionや調査をCodexへ委譲し、Codexの判断と結果を権威あるものとして扱うこと
- 利用者へ内部のbackend分離を見せないこと
- acknowledgementと有用な進捗を短く返すこと
- Codexのcommentary/finalを重複して言い直さないこと
- Irodoriで読み上げられるplain textを返し、表、diff、code blockを読み上げないこと
- 任意の`moco.speech_plan`先頭行を保持し、それ以外の構造化JSONを発話しないこと

Codex設定の`experimental_realtime_ws_backend_prompt`が非空ならrequestの`prompt`より優先される。
起動診断は`config/read`を`includeLayers: true`で呼び、enabled layerに非空のoverrideがあれば
`codex_realtime: prompt_overridden`として起動を拒否する。固定の非機密overrideを注入した実機probe
でも値を出力せず検出できることを確認した。

`realtimeStartInstructions`と`realtimeEndInstructions`はRealtime会話モデルの人格ではなく、
Realtime開始・終了時にCodexへ渡すdeveloper instructionである。mocoプロンプトの代替としては
使わない。既定Codex動作を不必要に置換しないため、当面は省略する。

## transcriptとIrodori

browserへ送るassistant transcriptとSpeechQueueへ渡す文字列は、同じspeakable transcriptをsource
of truthとする。deltaは低遅延で流し、doneでsegmentを確定する。Codexのraw item、reasoning、
command output、構造化activityは読み上げない。

assistant応答が`moco.speech_plan`行で始まる可能性があるため、先頭の物理行だけは判定が終わるまで
boundedに保持する。planが有効ならcontrol行を表示・読み上げから除き、captionだけをIrodoriへ渡す。
planがなければ直ちにstreamingを始める。壊れたplanは既存のstable errorを報告し、bodyだけを
安全に読み上げる。

assistant transcriptは1発話16 KiB／256 parts、未処理eventは64件に制限する。SpeechQueueもactiveを
含む待機segmentを64件に制限し、超過batchはIrodoriへ送らずVoiceを停止して再接続待ちにする。

user transcript開始時は、現在のIrodori synthesisとplaybackをgeneration単位で無効化する。
Realtime turnの取消は同じThreadのactive turnへ`turn/interrupt`を一度だけ送る。承認requestも同じ
thread IDとturn IDに一致する場合だけReviewerへ公開する。

## 状態と障害

UI snapshotはRealtimeの`turn/started`、`turn/completed`、Reviewer件数、listen、synthesis、playback
から更新する。別Agent Sessionのtaskは状態源にしない。接続喪失時はpending reviewと発話を無効化し、
実行結果不明を成功として発話しない。

Realtime会話leaseの途中でbinaryやschemaを切り替えない。Voice接続だけを張り直す場合も、同じ
app-server接続と検証済みcontractを利用する。

## 音声品質

固定の非機密文による現状測定では、Irodori responseはheaderと実データが一致する48 kHz、mono、
PCM16であり、clippingは検出されなかった。segment境界にもfull-scale jumpはなかった。ブラウザは
既定の44.1 kHz AudioContextで48 kHz WAVをdecode時に再標本化していた。

修正後の同一固定文では、baselineは7.04秒、peak -0.866 dBFS、RMS -16.180 dBFS、DC 2.731、
clipped sample 0だった。dynamic caption使用時は6.04秒、peak -0.635 dBFS、RMS -16.777 dBFS、
clipped sample 0だった。2 segment境界のsample差は90/32768（0.002747）である。RIFF size、
byte rate、block align、data lengthはいずれも実データと一致した。

mocoはIrodoriの出力に合わせて`AudioContext({sampleRate: 48000})`を要求する。実ブラウザでcontextと
decoded bufferが48 kHzになり、duration、playbackRate 1、detune 0が維持されることを検証する。
hardware側が要求を拒む場合は、ブラウザの実sample rateを観測値として扱い、速度やpitchを手動補正
しない。

実機E2Eでは一つの固定音声入力に対して`delegation.created`が1件、assistant doneが1件だった。
acknowledgement、progress、finalは3つのIrodori segmentとして一度ずつ配信され、全segmentが
48 kHzでdecodeされ、playbackRate 1、detune 0で再生開始し、browser errorは0件だった。

Irodori v4のvoice、generation、caption capability、conditioning、stepsは既存設定を維持する。
48 kHzという値だけを低品質の根拠にせず、RIFF size、format tag、channels、sample rate、byte rate、
block align、bit depth、data length、peak、clipping、DC、duration、segment境界を診断根拠とする。

## 完成条件

- 選択binaryのschemaが必要なv3 fieldとpayload値を受理する。
- 音声一回につきdelegationは高々一回、speakable text一片につきIrodori投入も一回である。
- acknowledgementまたは最初のspeakable progressがfinalを待たずにIrodoriへ流れる。
- dynamic caption、barge-in、Reviewer、profile、voice readiness、再接続が回帰しない。
- `just check`が成功する。
- 実機でWebRTC音声、Frameless delegation、Codex作業、Irodori synthesis、browser再生を確認する。
- 診断用一時音声を削除し、audio、transcript、credentialを保存しない。
