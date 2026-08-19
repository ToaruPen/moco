# Frameless Bidi音声アーキテクチャ復旧実装計画

> 対応設計: `docs/superpowers/specs/2026-08-19-frameless-bidi-voice-restoration-design.md`

## Task 1: v3 contractとbinary選択を厳格化する

対象:

- `src/moco/codex/schema.py`
- `src/moco/platform.py`
- `src/moco/codex/session.py`
- `tests/test_codex_contract.py`
- `tests/test_codex_session.py`
- `tests/test_platform.py`

1. 現行v3 start payloadと必要fieldを表す失敗テストを追加する。
2. macOSでChatGPT.appの対応binaryをPATH上の非対応binaryより選択する失敗テストを追加する。
3. schema witnessとbinary選択を最小変更で実装する。
4. 対象テストをGREENにし、semantic fieldの出現箇所を`rg`で確認する。

## Task 2: moco専用Realtimeプロンプトを確立する

対象:

- `config/moco.prompt.example.md`
- `src/moco/codex/session.py`
- `tests/test_codex_session.py`
- `tests/test_config.py`

1. mocoの人格、Frameless delegation、Codex結果の扱い、Irodori向け発話を検証するテストを追加する。
2. prompt fileが未作成の場合もリポジトリ管理のmoco promptを使うよう実装する。
3. `prompt`がstart requestへ完全な文字列として一度だけ渡ることを確認する。
4. Codex側config overrideとの競合をdoctorで安全に観測できる範囲を確認する。

## Task 3: 同一Threadの自動delegationへ切り替える

対象:

- `src/moco/codex/session.py`
- `src/moco/web/app.py`
- `src/moco/runtime/coordinator.py`
- `tests/test_codex_session.py`
- `tests/test_web.py`
- `tests/test_integration.py`

1. user finalが別Agent turnを開始しない失敗テストを追加する。
2. Realtime start payloadがautomatic handoff、ack filler、BEM channel routingを指定する失敗テストを追加する。
3. 同一Realtime Threadのactive turnだけがinterruptとapproval ownershipを持つ失敗テストを追加する。
4. 製品compositionから別Agent Session handoffを外し、Realtime activityからsnapshotを更新する。
5. cancellation、connection loss、Voice再接続の既存テストをGREENへ戻す。

## Task 4: assistant speakable transcriptを一度だけIrodoriへ流す

対象:

- `src/moco/web/app.py`
- `src/moco/speech/plan.py`
- `tests/test_speech_plan.py`
- `tests/test_web.py`

1. acknowledgement deltaがAgent finalより前に表示・SpeechQueueへ届く失敗テストを追加する。
2. assistant transcriptを捨てず、delta/doneを同一generationで一度だけ配信する。
3. 先頭のspeech-plan行をboundedに判定し、captionとbodyを既存契約へ渡す。
4. user barge-in、stale generation、duplicate done、壊れたplanのテストをGREENにする。

## Task 5: 不要なbrowser再標本化を除く

対象:

- `src/moco/web/static/app.js`
- `tests/js/app.test.js`

1. `AudioContext`へ48 kHzを要求する失敗テストを追加する。
2. activation時だけ`{sampleRate: 48000}`を渡す。
3. decode後sample rate、duration、playbackRate、detuneを実browserで再測定する。

## Task 6: 文書、設定例、診断を同期する

対象:

- `README.md`
- `config/moco.example.yaml`
- `docs/superpowers/specs/2026-08-07-codex-rich-agent-client-design.md`
- `docs/superpowers/specs/2026-08-19-frameless-bidi-voice-restoration-design.md`

1. 二Thread/manual handoffの記述を検索し、置換範囲を確認する。
2. binary選択、moco prompt、automatic delegation、音声品質の診断結果を記す。
3. v2 fallback、audio/transcript保存、未実装の長期記憶が入っていないことを確認する。

## Task 7: verificationとlive E2Eを行う

1. Python/JSの対象テストを実行する。
2. `just format`後に`just check`を実行する。
3. 固定非機密文でWAV header/data、peak、clipping、segment境界を再測定する。
4. 実際にmocoを起動し、browser音声から一つのdelegation、Codex commentary/final、Irodori synthesis、
   48 kHz decode、再生まで確認する。
5. 診断用一時fileが残っていないことを確認する。

## Task 8: 指定レビューとdeliveryを完遂する

1. `polishment`をサブエージェントへ依頼し、指摘を検証して反映する。
2. 続けて`ai-slop-cleaner`を別サブエージェントへ依頼し、挙動を変えない整理だけを反映する。
3. verificationを再実行してcommit、push、PRを作成する。
4. CIとreview feedbackを収束し、mergeする。
5. merged branch/worktreeを安全にcleanupし、mainから再deploy・再起動する。
6. 最終live確認を行う。

