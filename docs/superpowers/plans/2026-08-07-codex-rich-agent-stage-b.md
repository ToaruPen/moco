# Codex rich-agent 段階B 実装計画

## 目的

段階Aで完成した双方向app-server接続、生成schema契約、capability discovery、macOS/Windows基盤の上に、Voice Threadから通常Codex Agent turnへのhandoffと、command/file changeだけを扱う信頼済みローカルReviewerを追加する。

本計画は段階Bのfirst usable sliceだけを対象とする。Browser/Computer Use、MCP/app/permission approval、遠隔承認、Cloudflare経由リモート画面、長期記憶は作らない。Irodori v4、12 steps、`sway`は変更しない。

## 監督方針

- 実装担当は `claude-opus-5`、effort `xhigh`。Codexは各TDD単位のスコープ、差分、レビュー、検証を監督する。
- rate limit、API失敗、permission denialが発生した場合は別モデルへ自動fallbackせず停止して報告する。
- コミット、デプロイ、サービス再起動、Codex設定変更は明示許可なしに行わない。
- 各挙動変更は Red → Green → Refactor。既存テストを削除・disableしない。
- 生成schemaで確認できないmethod、field、decisionは推測せずfail-closedにする。

## 最終責務

- `RpcPeer`: wire分類、pending response、server request exactly-once応答、notification fan-out。
- `CodexConnectionSupervisor`: app-server process、initialize、stderr、shutdown。
- `CodexProtocolContract`: 実行時のmethod・params・server request semantic。
- `CapabilityDiscovery`: account、effective policy、admission、Voice/Agent readiness。
- `AgentSession`: Agent Thread、turn、interrupt、final answer抽出。
- `InteractionBroker`: command/file approvalのtyped requestとone-shot decision。
- `InteractionCoordinator`: Connection、Voice、Task、Speechの正規snapshotとhandoff状態遷移。
- `ReviewGate`: owner-private control secret、短命bootstrap nonce、loopback-only reviewer接続。

段階的な実装中はTask軸からreducerへ移すが、段階B完了時に既存`LifecycleController`や`_BrowserConnection`との二重所有を残さない。

## Task 0: 実Codex contractの観測

**目的:** 通常Agent turnと承認のwire shapeを実装前に確定する。

- macOSのinstalled Codexから生成schemaを一時directoryへ出力する。
- Windows実機でも同じsemanticを観測する。
- `thread/start`、`turn/start`、`turn/interrupt`、final answer notification、command approval、file change approval、decision responseを抽出する。
- version固有method名や全field集合をproduction testへ固定しない。
- 観測結果から既存`CodexProtocolContract`で表現できない最小のsemanticだけを追加する。

**受入:** 未知required fieldや未知decisionをcompatibleと判定せず、実装が送信可能なsubsetをstrictに検証できる。

## Task 1: profile modeと共通境界

**RED:** `tests/test_config.py`に`read_only`既定、`workspace_write`、`inherit_codex`、未知値・未知key拒否を追加する。

**GREEN:** `src/moco/config.py`、`config/moco.example.yaml`、`src/moco/errors.py`へ最小境界を追加する。

**受入:** profileは設定fileまたは後続のtrusted local controlだけで変更でき、voice・通常hotkey・remote Operatorから変更できない。

## Task 2: version-aware Agent method contract

**RED:** Task 0で観測したsemantic alias、required field、params omission、未知variant、旧/新version fixtureのcontract testsを追加する。

**GREEN:** `src/moco/codex/schema.py`へ必要なsemanticだけを追加する。

**受入:** method名やpayloadをhard-codeしてavailableと主張せず、構築不能なrequestは`VERSION_MISMATCH`になる。

## Task 3: 単一接続の共有化

**RED:** VoiceSessionがconnectionをstart/closeしないこと、1会話につきSupervisorが1つだけであること、close/cancel時に両threadが回収されることを固定する。

**GREEN:** `src/moco/codex/session.py`と`src/moco/web/app.py`のcompositionを更新する。

**受入:** Voice Realtime payload、SDP、transcript、audio playbackの既存契約を維持する。

## Task 4: command/file approval adapter

**RED:** `tests/test_codex_approval.py`で生成schema由来params、許可decision、未知field・scope・decision、privacy-safe `repr`を固定する。

**GREEN:** `src/moco/codex/approval.py`に2種類だけのtyped adapterを追加する。

**受入:** moco独自allowlistを作らず、説明不能なpayloadではReviewerを表示せずturnをfail-closedにする。

## Task 5: InteractionBroker

**RED:** opaque `reviewHandle`、single-use、二重click、遅延response、別接続、handler cancellation、reviewer disconnect、connection lossをテストする。

**GREEN:** brokerをReviewer境界に追加する。

**受入:** 同一server Request IDへsuccess/errorを二重送信しない。voiceや通常hotkeyをdecisionにしない。

## Task 6: AgentSession

**RED:** profile別`thread/start`、`inherit_codex`でのpolicy field省略、turn start、interrupt、thread continuity、notification分離、final answerだけの抽出を固定する。

**GREEN:** `src/moco/codex/agent.py`を追加する。

**受入:** `agent_admission != AVAILABLE`ではhandoffしない。Codexがpromptなしで許可した操作へmocoが追加承認を出さない。

## Task 7: local ReviewGate

**RED:** media tokenと別のcontrol secret、30秒・single-use・process-generation-bound nonce、loopback Host/Origin、single reviewer slotを固定する。

**GREEN:** `src/moco/web/review.py`、`src/moco/cli.py`、owner-private runtime state payloadを更新する。

**受入:** `moco review`がsecretやfragmentをstdoutへ出さず、remote/public connectionはreviewerへ到達できない。

## Task 8: Reviewer WebSocketとUI

**RED:** strict `{reviewHandle, decision}`、token cross-use拒否、fragment即時除去、HTML escape、accept既定focusなし、Enter/Space/hotkey即acceptなし、decision後の詳細破棄を固定する。

**GREEN:** Reviewer endpointと最小UIを既存web surfaceへ追加する。

**受入:** remote Operatorは`waiting_for_local_review`とturn全体cancelだけを見られ、操作詳細や個別decisionを受け取れない。

## Task 9: InteractionCoordinator

**詳細実装計画:** `docs/superpowers/plans/2026-08-09-interaction-coordinator.md`

**RED:** exactly-once handoff、中間ack音声を発さないこと、steer/queue/busy、`waiting_review`中の音声非decision、interrupt、connection-lost unknown outcomeを固定する。

**GREEN:** immutable snapshotと純粋transitionを追加し、Task軸から既存state ownerを移行する。

**Refactor:** Connection、Voice、Speechのauthoritative stateもCoordinator snapshotへ統合し、二重所有を除く。

**受入:** active privileged turnを接続回復後に自動再送しない。

## Task 10: progressとfinal-only speech

**RED:** Voice assistant transcriptをtask回答として読まないこと、Agent final answerだけをSpeechQueueへ渡すこと、stable failure summary、secret-free progressを固定する。

**GREEN:** `src/moco/web/app.py`のhandoffとspeech compositionを更新する。

**受入:** command、path、patch、reasoning、approval payloadを通常activity・telemetry・speechへ出さない。新utteranceは古いspeech generationを停止する。

## Task 11: doctor、README、contract、全gate

- profile、Agent readiness、local review readinessをstable codeで表示する。
- generated contract testをinstalled macOS/Windows Codexで実行する。
- `just test-python`、`just contract-codex`、`just check`を実行する。
- Windows CIは既存matrixへPython testsを載せ、frontend/e2eは既存Ubuntu full gateに保つ。
- macOS/Windows実機でread-only task、approval、voice非approval、interrupt、final speechを確認する。

## Review gate

Task 3、6、8、10、11の後に独立code reviewを行う。Task 5、7、8、10の後にsecurity reviewを行う。最終収束時にpolishmentとAI由来の不要重複確認を行う。

## 実装しないもの

- Browser/Computer Use adapter・registry
- MCP/app/permission approval
- 遠隔Reviewer・遠隔承認
- Cloudflare/Tailscale remote-screen automation
- runtime JSON Schema UI generator
- transcript/audio/approval payloadの永続化
- moco独自のtool/command/path allowlist
- 危険profileへの暗黙fallback
