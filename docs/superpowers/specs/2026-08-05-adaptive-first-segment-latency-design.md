# moco 適応的初回文節レイテンシ設計

**Status:** ユーザー承認済み
**Date:** 2026-08-05

## 目的

Irodori-TTS v4-Small の音質と現在の単一GPU実行境界を維持したまま、Codex の
assistant transcript が届いてから最初の音声再生が始まるまでの体感遅延を短縮する。

本変更は真の音声ストリーミングを実装しない。v4-Small は全時間長の latent を
非因果 RF-DiT で更新した後に一括 decode するため、現行 checkpoint の推論途中から
確定 PCM を返す変更は高リスクである。代わりに、最初の自然な文節だけを既存より早く
Irodori へ渡し、前文節の再生中に後続文節を推論する現在の重なりを活用する。

## 現状と根拠

- Codex transcript は逐次受信できる。
- moco は `。！？!?`、80文字到達、または assistant turn 完了まで本文を蓄積する。
- Irodori は一つの文節について完成 WAV を返すまで応答しない。
- browser は完成 WAV を `decodeAudioData()` した後に再生する。
- 文節を browser へ渡した後、SpeechQueue はその音声の再生完了を待たずに次文節の
  Irodori 推論へ進める。
- 実環境20件の Irodori 合成は中央値 1,034.4 ms、p95 1,117.3 ms だった。
- upstream の sampler は全長 latent を各 Euler step で同時更新し、途中 prefix を
  確定しない。

参考:

- [Irodori inference runtime](https://github.com/Aratako/Irodori-TTS/blob/8ca3acb58ab4e19ad6d594aaed6bafe3e88f7f71/irodori_tts/inference_runtime.py#L1036-L1462)
- [Irodori RF sampler](https://github.com/Aratako/Irodori-TTS/blob/8ca3acb58ab4e19ad6d594aaed6bafe3e88f7f71/irodori_tts/rf.py#L459-L584)
- [Irodori non-causal attention](https://github.com/Aratako/Irodori-TTS/blob/8ca3acb58ab4e19ad6d594aaed6bafe3e88f7f71/irodori_tts/model.py#L312-L415)

## スコープ

### 含むもの

- assistant turn の最初の文節だけを対象にした soft break
- strict な一つの設定値と無効化による rollback
- user interruption 時の browser invalidation 先行
- 内容非保持の文節準備時間と分割理由の計測
- 固定非機密テキストによる baseline/candidate 比較
- unit、integration、browser playback 回帰テスト

### 含まないもの

- Irodori model、codec、checkpoint、voice bank の変更
- PCM、codec token、WAV chunk の逐次配信
- AudioWorklet、MediaSource、WebCodecs の追加
- 並列GPU推論または Irodori capacity の増加
- caption、style preset、`cfg_scale_caption` の追加
- 話者ID、話者数、表示順、既定話者を固定するテスト
- transcript、caption、音声の保存または telemetry 出力
- assistant 応答内容を意味解析する分割器

## 検討した方式

### A. 固定最大文字数を80から32前後へ下げる

実装は最小だが、句読点のない本文を機械的に分割し、Irodori request 数、文間の継ぎ目、
韻律断絶を増やす。後続文節まで常に短くなるため採用しない。

### B. 最初の自然な文節だけを先行する

最初の文節に限り、一定文字数以降の `、，,；;：:` を安全な切れ目として使う。
sentence end と後続文節は現行挙動を維持する。初声だけを短縮し、request 増加を1 turn
あたり原則1件以内に抑えられるため採用する。

### C. 再生バッファ量に応じて分割位置を動的制御する

理論上の最適化余地は大きいが、browser playback 状態を server segmenter へ戻す新しい
制御ループ、backpressure、遅延した ACK の扱いが必要になる。現段階では採用せず、B の
実測後に必要性を再評価する。

## 設定契約

`speech` に次の一項目だけを追加する。

```yaml
speech:
  segment_max_chars: 80
  first_segment_soft_break_min_chars: null
```

型は `PositiveInt | None` とする。

- 既定値は `null` とし、採用gateの承認前に通常serviceへ自動反映しない。
- `null` は適応的soft breakを無効化し、現行挙動を維持する。
- 整数値は `segment_max_chars` 以下でなければならない。
- `bool`、float、数値文字列、0、負数、上限超過を拒否する。
- unknown key は従来どおり拒否する。
- browser UI には公開しない。運用・rollback用のserver設定である。

値18は最終的な品質定数でも既定値でもなく、隔離検証で明示的に有効化するcandidateの初期値
である。後述のgateを満たさない場合は値を小さくするのではなく、`null` のまま通常serviceへ
反映しない。

## 分割アルゴリズム

`TranscriptSegmenter` は assistant turn ごとに `first_segment_emitted` を保持する。

優先順位は次のとおりとする。

1. buffer に `。！？!?` があれば、最初のsentence endと連続する終端記号、直後の
   `」』）】》”’` までを一文節として返す。
2. まだ最初の文節を返しておらず、設定が有効で、sentence end がなく、設定された
   最小文字数以降に `、，,；;：:` が到着した場合、最初の該当soft breakまでを返す。
3. buffer が `segment_max_chars` に達した場合、現行どおり上限内の最後のsoft break、
   なければ上限位置で切る。
4. assistant turn 完了時は残りの発話可能な本文をflushする。

soft break の判定は受信した Unicode code point の位置に対して決定的に行う。control emoji
はIrodoriへの原文では保持し、画面表示だけから除く現在の境界を変更しない。

次の状態遷移を守る。

- sentence end、soft break、最大長のいずれであっても、最初の文節を返した時点で
  `first_segment_emitted=true` とする。
- 2文節目以降のsoft breakは最大長へ到達するまで先行分割に使わない。
- assistant done のflush後、user invalidation、`clear()` でturn状態を初期化する。
- user transcript 中は現在どおりassistant音声を抑止する。
- speakable textのない記号・control emojiだけの文節をenqueueしない。

## データフロー

```text
Codex assistant delta
  -> authoritative display text 更新
  -> TranscriptSegmenter.push(raw speech delta)
  -> first natural segment ready
  -> audio_id / playback generation 予約
  -> Irodori full-segment synthesis
  -> completed WAVをbrowserへ送信
  -> browser decode/playback

同時に:
  browserが前文節を再生
  -> SpeechQueueが次文節をIrodoriで合成
```

SpeechQueue は一つの Irodori worker を維持する。GPU request を並列化せず、FIFO、
`audio_id`、playback generation、現在の cancellation 意味論を保持する。

## 割り込み順序

現在は user transcript 開始時に active synthesis/delivery の取消完了を待ってから
`audio_invalidate` をbrowserへ送っている。remote cleanupが遅い場合でも古い再生を即座に
止めるため、順序を次へ変更する。

1. `_BrowserConnection` の playback generation を1増やす。
2. 新generationの `audio_invalidate` をbrowserへ送る。
3. `finally` でSpeechQueueを `reason="user_transcript"` としてinvalidateする。
4. active Irodori/delivery taskの取消完了を待つ。

browser送信に失敗してもSpeechQueueの取消を必ず行う。invalidation後に旧generationのaudioが
到着しても、browserはmetadata照合で破棄する。

## 観測性

既存の `audio_id` と整数playback generationを相関キーとして使う。新しいイベントは
`speech_segment_ready` 一つに限定する。

許可する属性:

- `audio_id`: strict non-negative integer
- `generation`: strict non-negative integer
- `segment_index`: strict positive integer。turn内では1始まり
- `text_chars`: strict non-negative integer
- `queue_depth`: strict non-negative integer
- `duration_ms`: 最初のassistant deltaから文節確定までの非負時間
- `segment_reason`: `sentence_end | first_soft_break | max_chars | turn_flush`

本文、音声、voice ID/label/alias、Irodori runtime generation、caption、promptは記録しない。
既存の `synthesis_*`、`audio_delivery_*`、`browser_playback` と同じ相関値を使い、外部計測で
各段階の時間を比較できるようにする。

`_BrowserConnection` は本文を保持せず、現在turnの最初のassistant deltaを受けたmonotonic
timestamp、最初に予約された `audio_id`、そのplayback generationだけを保持する。対応する
`browser_playback phase=started` を受けた時点で、最初のdeltaから再生開始までの
`duration_ms` を同イベントへ付与して状態を破棄する。IDまたはgenerationが一致しない通知は
この時間計測に使わない。user invalidation、接続終了、次turn開始でも古い計測状態を破棄する。
これによりserverとbrowserの時計を混在させず、採用gateのend-to-end p95を直接集計できる。

browserからserverへの再生ACKは次のstrict payloadだけを受理する。

```json
{
  "type": "playback",
  "phase": "started | completed | failed",
  "audio_id": 1,
  "generation": 0,
  "context_state": "running | suspended | closed | interrupted"
}
```

client申告のaggregate `active` と無相関 `stopped` は契約に含めない。serverは送信完了した
`(audio_id, generation)` を `delivered` として登録し、`delivered -> started -> completed` または
`delivered -> failed` だけを受理する。busy状態はserver側の `started` 集合から導出する。
未発行ID、現generation以外、replay、順序違反は `invalid_message` とboundedな
`browser_playback_rejected` telemetryにし、ライフサイクル、first-playback計測、通常の
`browser_playback` telemetryを変更しない。invalidationとconnection closeでは登録状態を
破棄する。WAV送信中だけ内部状態を`delivering`とし、その間に正常なACKが先着した場合は
send lockの解放を待ってから検証する。送信失敗時は登録を破棄し、送信完了前のACKを正式な
再生状態に昇格させない。

browserは `AudioBufferSourceNode.start(startAt)` の呼び出し直後には `started` を送らない。
`AudioContext` がrunningで、`currentTime >= startAt` になった時点で初めて通知する。decodeまたは
start失敗は `failed` として計測状態を破棄するが、再生開始時間には数えない。invalidationでは
待機中の開始通知を取り消し、Promise chainも新generation用に切り替えるため、旧generationの
未解決decodeが新しい音声を塞がない。

## 検証計画

### Unit tests

- 18文字未満のsoft breakを先行分割に使わない。
- 18文字目以降の最初のsoft breakを最初の文節だけに使う。
- 同じbufferにsentence endがある場合はsentence endを優先する。
- 2文節目以降はsoft breakだけで先行分割しない。
- `null` で現行挙動へ戻る。
- sentence closer、複数終端記号、control emoji、最大長、done flushを保持する。
- `clear()`、user invalidation、次assistant turnで最初の文節状態をresetする。
- strict config境界と `first_segment_soft_break_min_chars <= segment_max_chars` を検査する。

### Queue / web integration tests

- 最初のsoft break文節と後続文節を一度ずつ、FIFOで合成する。
- 各文節の `audio_id` とgenerationが synthesis、delivery、playbackまで一致する。
- user transcriptでは `audio_invalidate` がcancel cleanup完了より先に送られる。
- send failureでもSpeechQueueが取消される。
- invalidation後の旧generation WAVをbrowserが再生しない。
- 分割理由と文字数だけがtelemetryへ入り、本文は入らない。
- 話者fixtureはruntime capabilityから生成し、名前、件数、順序を固定しない。

### Browser tests

- invalidation受信時にactive sourceとqueued decodeを即時停止する。
- old generationのbinaryを破棄する。
- 複数文節のschedule、decode failure後の回復、AudioContext resumeを維持する。
- 新しいUIまたはbrowser設定は追加しない。

### Live comparison

個人データを含まない固定日本語サンプルを使い、同じruntime capabilityから選んだ既定voiceで
baselineとcandidateを比較する。話者名、voice数、順序はテストへ固定しない。

最低限、短文、長い一文、読点を含む説明、引用符、疑問文、control emojiを含む文を扱う。
生成音声と本文は保存せず、run-local memoryで集計する。人手AB確認に必要な一時音声は明示した
一時directoryにだけ置き、判定後に削除する。

## 採用ゲート

candidateを既定値18で採用するには、同じ入力集合で次をすべて満たす必要がある。

- first assistant deltaから最初のbrowser playback startedまでのp95を15%以上短縮する。
- synthesis failure、delivery failure、stale generation playbackが0件。
- turn全体の完了時間の悪化がbaseline比10%未満。
- 一つのturnあたりの文節数がbaseline平均の1.5倍以内。
- playback gapのp95がbaselineより30 msを超えて悪化しない。
- 新しいclick、無音、語中分割が0件。
- blind AB確認でcandidateの明確な韻律・感情・話者品質悪化が0件。
- caption modeは`off`、runtime voice catalogとmodel readinessが正常。

一つでも満たさない場合、自動昇格しない。閾値を場当たり的に下げず、失敗した指標と音声境界を
分析して設計を再承認する。

## Rolloutとrollback

1. 現行80文字方式でbaselineを採取する。
2. unit/integration/browser testsをTDDで追加する。
3. candidateをforegroundまたは隔離test appで測定する。
4. 固定サンプルのblind ABと全品質gateを実行する。
5. gate通過後だけ通常moco serviceへ反映する。
6. 反映後、最初の実会話で新telemetryと再生を確認する。

rollbackは `speech.first_segment_soft_break_min_chars: null` としてmocoだけを再起動する。
Irodori、voice bank、checkpoint、tokenizer、cloudflaredを変更または再起動しない。

## リスク

### 文間品質

読点で分割すると、Irodoriが一文全体から得ていた韻律・感情の連続性が弱くなる可能性がある。
最初の文節だけに限定し、blind ABをblocking gateにする。

### request overheadと再生gap

短い文節はIrodori requestの固定費を増やす。GPU並列化で隠さず、前文節再生中の直列先読みで
吸収する。文節数、turn全体時間、playback gapを同時にgateする。

### 早すぎる分割

短い挿入句や引用内の読点が不自然な切れ目になる可能性がある。18文字未満を無視し、
意味解析や日本語固有の助詞heuristicは追加しない。

### cancellation race

browser invalidationを先行すると、server側の旧task cleanupと短時間重なる。generation照合、
SpeechQueue取消、send failureの`finally`をテストし、旧音声の再生だけをfail-closedにする。

### 計測によるprivacy漏洩

文字数と時間だけでもturn形状は推定できるため、既存の明示的telemetry設定にだけ出力する。
本文、音声、話者、caption、runtime generationは引き続き禁止する。

## 完了条件

- 設定、分割、割り込み、telemetry契約がstrictなテストで固定されている。
- `just check` が通る。
- baseline/candidateの採用ゲート結果が提示される。
- polishmentとai-slop-cleanerの独立レビューが完了する。
- 通常serviceへの反映前に、gate結果についてユーザー承認を得る。
- 未コミットの既存変更と別作業のuntracked artifactsを保持する。

## 実測結果（2026-08-05）

active configと同一voiceを使うread-only live probeを、固定非機密サンプル6件、warmup 1件、
baseline/candidateの先行順を交互にして実行した。音声はメモリ内だけで検証し、本文、voice情報、
runtime generation、WAV bytesは結果へ保持していない。p95はnearest-rankで算出した。
n=6のnearest-rank p95は各条件の最大値であり、ここでの結果は探索的な小標本に限られる。
安定したtail latency推定やend-to-end 15%採用gateの根拠には使わない。

| 指標 | baseline | candidate | 差分・比率 |
| --- | ---: | ---: | ---: |
| first-ready chars p95 | 80.000 | 80.000 | — |
| first synthesis p95 | 1,151.052 ms | 1,141.770 ms | model-only first audio 0.806%改善 |
| total synthesis p95 | 2,197.999 ms | 2,083.803 ms | -5.195% |
| estimated turn completion p95 | 16,271.052 ms | 16,261.770 ms | -0.057% |
| estimated playback gap p95 | 0.000 ms | 0.000 ms | +0.000 ms |
| segment count | — | — | baseline比1.143倍 |

capabilities取得は32.843 ms、warmup合成は1,077.843 msだった。全6サンプルで合成とWAV検証が
成功し、`failures=0`、`runtime_ready=true`だった。first audioの0.806%はtranscript到着時間と
browserのdecode/playback開始を含まないmodel-only推定であり、end-to-end採用gateの代用には
しない。

同日、再生ACK境界のhardening後に同じprobeを再実行した。全6サンプル成功、
`runtime_ready=true`、model-only first audioは1.764%改善、total synthesisは4.143%増、
estimated turn completionは0.151%短縮、segment count比は1.143だった。このばらつきも小標本の
探索値であり、blocking gateの判定を変更しない。

| 採用gate | 判定 | 根拠 |
| --- | --- | --- |
| first assistant deltaからbrowser playback startedまでp95を15%以上短縮 | UNVERIFIED | browser実再生とtranscript到着時間を測定していない |
| synthesis failure 0件 | PASS | live probeの全合成とWAV検証が成功 |
| delivery failure 0件 | UNVERIFIED | browser deliveryを経由していない |
| stale generation playback 0件 | UNVERIFIED | browser playbackとgeneration競合を実測していない |
| turn全体の完了時間悪化が10%未満 | UNVERIFIED | model-only推定は-0.057%だが、WebSocket delivery、browser decode、AudioContext schedulingを含まない |
| 文節数がbaseline平均の1.5倍以内 | PASS | 1.143倍 |
| playback gap p95の悪化が30 ms以内 | UNVERIFIED | model-only推定は+0.000 msだが、WebSocket delivery、browser decode、AudioContext schedulingを含まない |
| click、無音、語中分割が0件 | UNVERIFIED | 音響・聴感確認を実施していない |
| blind A/Bで明確な品質悪化が0件 | UNVERIFIED | blind A/Bを実施していない |
| caption off、voice catalogとmodel readinessが正常 | PASS | active configはcaption off、capabilityはready |
| stale状態とVRAM影響 | UNVERIFIED | このread-only probeでは観測していない |

未検証のblocking gateが残るためcandidateを自動昇格しない。通常moco service、Irodori、
voice bank、active configは変更していない。

### 後続のsampling変更

同日の独立したv4推論高速化評価で、`12 steps / sway / neutral`は客観metric上の最良候補になった。
2026-08-06のblind ABでは12組すべてが同等と回答され、明示承認後にmocoの現在の運用設定へ
採用した。これは通信契約の固定値ではなく、sampling設定から変更可能であり、testも選択中profileを
固定しない。このsampling変更はfirst-segment candidateの採否を変えず、
`speech.first_segment_soft_break_min_chars`は`null`のままとする。
