# ブラウザ音声デバイス切り替え設計

## 位置づけ

mocoのブラウザ companion は現在、接続時にブラウザ既定マイクを一つ取得し、Irodori音声を
既定の`AudioContext`出力へ再生する。利用者はmacOS側の既定デバイスを変更できるが、moco画面
から入力元と出力先を明示的に選べず、入力変更を現在のWebRTC接続へ安全に反映する契約もない。

本設計は、Codex Realtimeの会話、Frameless Bidi delegation、SpeechQueue、Irodori generationを
維持したまま、ブラウザ側の音声経路だけを切り替えられるようにする。旧Realtime方式や別Agent
Threadへの下位互換経路は追加しない。

## 目的

- moco画面から入力マイクと出力スピーカーを選択できる。
- 会話中でもCodex Realtime Threadを作り直さず即時に切り替えられる。
- 選択をこのブラウザに保存し、次回接続時に利用可能なら復元する。
- 使用中デバイスが切断された場合は、会話を維持しながらシステム既定へ自動復帰する。
- ブラウザの能力不足や個別デバイスの失敗を、他方の音声経路や会話全体の障害へ拡大しない。
- 音声、transcript、credential、デバイス名称を永続化しない。

## 非目的

- 複数マイクのミキシング、入力ゲイン、ノイズ抑制設定のUIは追加しない。
- アプリ独自の仮想デバイスやmacOSのシステム音声ルーティングは実装しない。
- Irodoriのvoice選択と物理的な出力デバイス選択を統合しない。
- 出力先を選べないブラウザ向けに、別の再生エンジンや互換APIを追加しない。

## 採用方式

ブラウザ内に一つの音声デバイス管理単位を置き、既存のメディア所有者と次の境界で連携する。

```text
INPUT select
  -> getUserMedia(exact deviceId or default)
  -> RTCRtpSender.replaceTrack(new microphone track)
  -> existing Realtime v3 WebRTC conversation

Irodori WAV -> existing AudioPlaybackQueue -> AudioContext
                                           -> setSinkId(selected output or default)
                                           -> OUTPUT select
```

入力変更では新しいマイクtrackを先に取得し、現在のaudio senderへ`replaceTrack()`する。交換成功後に
のみ旧trackを停止する。これにより、変更失敗時は切り替え直前まで正常だった現在のマイクを維持する。
これは下位互換fallbackではなく、部分更新を原子的に扱うための失敗処理である。

出力変更では既存の48 kHz `AudioContext`に`setSinkId()`を適用する。AudioPlaybackQueue、decode、
playbackRate、detuneは変更しない。`setSinkId()`非対応環境ではOUTPUTをシステム既定に固定し、
セレクトを無効化する。非標準または旧式の代替再生経路は設けない。

## UIとデバイス一覧

上部のVOICE選択と同じ操作領域に、INPUTとOUTPUTのセレクトを追加する。未接続時は両方を無効にし、
接続操作によるマイク権限取得後に`enumerateDevices()`を呼ぶ。権限取得前の空ラベルや推測名を表示しない。

各一覧の先頭は「システム既定」とする。同一kindの候補だけを表示し、空のdevice ID、利用不能な項目、
重複IDは候補にしない。デバイスの追加・切断を検知したら一覧を再取得する。

選択中のデバイスIDだけをブラウザorigin内の`localStorage`へ保存する。名称や一覧全体は保存しない。
保存済みIDが一覧になければシステム既定を選び、保存値も既定へ戻す。ストレージが使用不能でも接続や
切り替えは継続し、その接続中だけ選択を保持する。

## 入力切り替え

INPUT変更時は次の順序を守る。

1. INPUTセレクトを一時的に無効化する。
2. 選択IDがあれば`deviceId.exact`、システム既定なら通常のaudio constraintで新trackを取得する。
3. 現在のMIC ON/OFF状態を新trackへ設定する。
4. 現在のWebRTC audio senderへ`replaceTrack()`する。
5. controllerとcleanupが所有するstreamを新trackへ更新する。
6. 旧trackを停止し、選択を保存してセレクトを再有効化する。

track取得または交換が失敗した場合は新trackだけを停止し、現在のtrack、選択表示、保存値を維持する。
利用者には`microphone_switch_failed`を表示する。デバイス名や例外本文はtelemetryへ送らない。

使用中マイクが`devicechange`後の一覧から消えた場合は、同じ手順でシステム既定trackへ交換する。
既定trackも取得できなければ現在のtrackの実状態に従い、成功を装わずエラーを表示する。

## 出力切り替え

OUTPUT変更時はOUTPUTセレクトを一時的に無効化し、選択IDまたは既定を現在の`AudioContext`へ
`setSinkId()`する。成功後に選択を保存する。失敗時は現在のsink、選択表示、保存値を維持し、
`audio_output_switch_failed`を表示する。

使用中出力が一覧から消えた場合はシステム既定sinkへ戻す。既定への復帰も失敗した場合は現在の
AudioContextを破棄せず、ブラウザが報告する実状態を維持してエラーを表示する。

## ライフサイクルと競合

デバイス変更はkindごとに直列化し、処理中は該当セレクトだけを無効にする。入力変更と出力変更は互いを
停止させない。接続終了時は`devicechange` listenerを外し、現在streamが所有するtrackを既存cleanup
経路で停止し、AudioContextを閉じる。

Realtimeの再接続でpeerだけを交換する場合、現在選択中の入力trackを新しいpeerへ追加する。デバイス
切り替えはThread、delegation、assistant transcript、SpeechQueue、Irodori generationを変更しない。
したがって一つの依頼や一つのspeakable textを再送せず、二重実行・二重読み上げを生じさせない。

## エラー表示と能力差

新しい安定エラーコードは次の二つに限定する。

- `microphone_switch_failed`: 新しい入力trackの取得またはWebRTC senderへの交換に失敗した。
- `audio_output_switch_failed`: `AudioContext`の出力先変更または既定復帰に失敗した。

`enumerateDevices()`の一時失敗は現在の経路を停止させない。選択UIを現在確認できる候補へ保ち、明示的な
切り替えが失敗した場合だけ上記コードを表示する。`setSinkId()`が存在しないことは実行時障害ではなく
能力差として扱い、OUTPUTを「システム既定」に固定する。

## テスト

ブラウザ単体テストをREDから追加し、次を確認する。

- 権限取得後の入力・出力カタログと「システム既定」の表示。
- device kind、空ID、重複IDの除外。
- 保存済み選択の復元と、未検出時の既定復帰。
- storage使用不能時も接続中の選択が機能すること。
- 会話中の入力track交換とMIC ON/OFF状態の維持。
- 入力変更失敗時に現在trackを維持し、新trackだけを停止すること。
- `setSinkId()`による出力変更と、失敗時に現在sinkを維持すること。
- `devicechange`による一覧更新と、切断された選択から既定への自動復帰。
- `setSinkId()`非対応時にOUTPUTだけを無効化すること。
- disconnect時にlistener、track、AudioContextを一度だけcleanupすること。
- Realtime peer交換時に現在の入力trackを使うこと。

HTMLのcontract testでINPUTとOUTPUTのlabel、select、初期disabled状態を確認する。既存の48 kHz decode、
playbackRate 1、detune 0、barge-in、Realtime再接続、Irodori generation、voice選択テストは維持する。

## 実機確認

`just check`成功後、実ブラウザで次を確認する。

- 権限取得後に実デバイス名がINPUTとOUTPUTへ表示される。
- MIC ONとMIC OFFの両方で入力を変更し、会話Threadを維持したまま次の発話へ反映される。
- Irodori再生前と再生間で出力を変更し、再接続や二重再生なしに選択先へ流れる。
- 使用中デバイスを切断したとき、一覧更新とシステム既定への復帰が行われる。
- 再読み込み後、利用可能な保存済み選択が復元される。

実機に複数の物理または仮想デバイスがない項目は、確認できた範囲と未確認点を報告する。診断のために
音声、transcript、credentialを保存しない。

## 完成条件

- 利用者がmoco画面から入力と出力を選べる。
- 会話中の切り替えでRealtime Thread、delegation、Irodori queueを作り直さない。
- 切り替え失敗は現在正常なデバイスを破壊しない。
- 切断された選択はシステム既定へ自動復帰する。
- 選択ID以外のデバイス情報を永続化しない。
- 対応能力のない出力APIへ下位互換経路を追加しない。
- `just check`が成功し、実機確認結果を報告できる。
