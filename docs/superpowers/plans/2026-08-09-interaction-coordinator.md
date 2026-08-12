# Interaction Coordinator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Voice の確定 user transcript を通常 Codex Agent turn へ exactly-once で handoff し、steer、1件 queue、local review 待ち、明示 interrupt、final-only speech、idle を一つの immutable snapshot から制御する。

**Architecture:** `InteractionCoordinator` を1会話 lease の唯一の interaction state owner にする。`turn/steer` は生成 schema が証明した場合だけ使う任意能力とし、未提供時は1件 queue へ落とす。file-change notification は JSON-RPC inbound 順序内の同期 observer で approval より先に相関し、Broker の private bounded map だけに保持する。Coordinator の effect sink は同期・非 blocking とし、SpeechQueue への接続は Stage B Task 10 に残す。

**Tech Stack:** Python 3.13、asyncio、dataclass/StrEnum、Pydantic、FastAPI/WebSocket、生成 JSON Schema、pytest/pytest-asyncio、Node test runner、uv、just

---

## 実行規則

- 各挙動は RED → GREEN → Refactor。既存 test は削除・disable しない。
- stage、commit、push、service restart、Codex 設定変更は利用者の明示許可なしに行わない。
- Stage B Task 10 の final-only speech/progress と Task 11 の doctor/README/release は先取りしない。
- `.codex/`、`.superpowers/`、`prompt.md` はローカル証拠・作業物のまま保持し、Stage B Task 9 の追跡対象にしない。
- 汎用 FSM、event bus、observer registry、複数 turn、2件以上の queue、retry/replay、永続化、daemon-global manager は作らない。

## 固定契約

`InteractionSnapshot` が外部へ公開するのはaccepted designどおり次の四軸だけとする。

```python
@dataclass(frozen=True, slots=True)
class InteractionSnapshot:
    connection: ConnectionState
    voice: VoiceState
    task: TaskState
    speech: SpeechState
```

四軸の値は accepted design と同じである。

- Connection: `starting`、`ready`、`degraded`、`disconnected`
- Voice: `idle`、`listening`、`transcribing`
- Task: `none`、`queued`、`running`、`waiting_review`、`completed`、`failed`、`interrupted`
- Speech: `silent`、`synthesizing`、`playing`

listen/turn generation、consumed generation、queued text、pending review count、turn task、steer submission task、cancel/terminal claim は Coordinator private state に置く。active turn背後のqueue中もTask軸はRUNNING/WAITING_REVIEWを維持し、pending count 1以上はWAITING_REVIEWへ投影する。active完了時のqueue昇格はterminal snapshotを公開する前の同一claimで行うため、private queue/pendingとpublic Task軸が矛盾しない。UI と idle は四軸snapshotから導出し、旧 `LifecycleController` や `_BrowserConnection` に同じ state/busy flagを残さない。

effect sink は次の同期 callable に限定する。callback を await せず、generation の state claim を完了してから呼ぶ。callback failure は payload-free に処理し、同じ effect を再発行しない。

- `on_snapshot_changed(snapshot: InteractionSnapshot) -> None`
- `on_turn_terminal_claimed() -> None`
- `on_turn_finished(result: TurnResult) -> None`
- `on_submission_error(code: str) -> None`

`on_turn_terminal_claimed` はowner-bound `Broker.cancel_pending()`だけを同期実行する。Coordinatorはterminal generationを先にclaimし、このhookでpending countを0へ戻す。private queueがあれば同一claimで次turnへ昇格してterminal snapshotを公開せず、なければterminal snapshotを公開する。そのstate claim後に旧turnの`on_turn_finished`をexactly onceで呼び、UI/TTS taskはこの順序の後にだけscheduleする。

Coordinatorは中間のacknowledgement音声を発行しない。Task 10はfinal/error resultだけを
同一の文字列でOperator UIとSpeechQueueへ接続する。

`TurnResult` のterminal mappingは次に限定し、raw `CodexAgentError` textを渡さない。started Coordinator turnごとに一つだけ発行し、busy/admission拒否とrunningを維持する既知steer rejectionは`on_submission_error`だけにする。

| Outcome | `TurnResult` |
| --- | --- |
| final answer確定 | `final_answer=<text>`, `error_code=None` |
| reusable session上の既知turn failureまたはfinal unavailable | `final_answer=None`, `error_code="agent_turn_failed"` |
| 明示cancel成功またはserverのinterrupted terminal | `final_answer=None`, `error_code="agent_turn_interrupted"` |
| non-reusable cancellation settlement、connection loss、受理/結果不明 | `final_answer=None`, `error_code="agent_outcome_unknown"` |

## 実装単位 1: steer schema、optional capability、AgentSession

**Files:**

- Modify: `src/moco/codex/schema.py`
- Modify: `src/moco/codex/capabilities.py`
- Modify: `src/moco/codex/agent.py`
- Modify: `src/moco/errors.py`
- Modify: `tests/test_codex_schema.py`
- Modify: `tests/test_codex_capabilities.py`
- Modify: `tests/test_codex_agent.py`
- Modify: `tests/test_web.py`（snapshot helper の追随だけ）
- Modify: `justfile`
- Modify: `tests/test_repository_contract.py`

- [ ] RED: repository contract test で単一optional `pattern=""` を取る `test-frontend` recipe を要求する。recipe は `node --test {{ if pattern == "" { "" } else { "--test-name-pattern=" + quote(pattern) } }} tests/js/*.test.js` とし、Node optionをtest file globより前へ置き、空白/`|`を含むfocused patternの引数境界が保たれることを固定する。
- [ ] RED を `just test-python tests/test_repository_contract.py -q -k "test_frontend"` で確認し、上記1 recipeだけを `justfile` へ追加して GREEN にする。既存 `test` / `test-cov` recipe は変更しない。

- [ ] RED: synthetic schema で `TurnSteerParams` / `Turn/steerRequest` を観測し、`SemanticMethod.TURN_STEER`、object params、required `expectedTurnId` / `input` / `threadId`、1件の text input witness を固定する。title 衝突、required 欠落、text item 拒否は unavailable にする。
- [ ] RED: `CapabilitySnapshot.steer` は valid method で `AVAILABLE`、absent で `VERSION_MISMATCH/method_unavailable`、malformed で `VERSION_MISMATCH/invalid_response`。steer unavailable でも `agent_admission` は `AVAILABLE` のままにする。
- [ ] RED: `AgentSession.steer(text)` は active thread/turn へ次の exact payload を送り、response の required `turnId: string` が snapshot 済み active turn と一致した場合だけ成功する。

```json
{
  "expectedTurnId": "active-turn-id",
  "input": [{"type": "text", "text": "追加指示"}],
  "threadId": "active-thread-id"
}
```

- [ ] RED: blank/oversize/no active turn/unavailable は送信前拒否。ordinary JSON-RPC error response は既知 rejection として元 turn を running のまま保つ。timeout、connection/protocol loss、caller cancellation 後の受理不明、malformed/mismatched response は session/active turn を unknown terminal にする。payload-free read-only `AgentSession.reusable` はopenかつunknown terminalでない時だけtrueとし、cancellation settlement後にCoordinatorがinterrupt成功とunknownを区別できるようにする。raw error/turn detailは公開しない。
- [ ] RED: `AgentSession.start_turn()` のterminal exceptionはtyped `AgentTurnErrorCode`だけを持つ。server `interrupted`は`agent_turn_interrupted`、server `failed`とfinal unavailableは`agent_turn_failed`、connection/protocol/受理・結果不明は`agent_outcome_unknown`。explicit cancel成功はCoordinatorが同じinterrupted codeへ写し、admission/busy/known steer rejectionはterminal outcome型にしない。
- [ ] RED を確認する。

```bash
just test-python tests/test_codex_schema.py -q -k "turn_steer"
just test-python tests/test_codex_capabilities.py -q -k "steer"
just test-python tests/test_codex_agent.py -q -k "steer"
just test-python tests/test_codex_agent.py -q -k "reusable"
just test-python tests/test_codex_agent.py -q -k "turn_outcome"
```

- [ ] GREEN: `SemanticMethod.TURN_STEER`、`_CLIENT_SIGNALS`、`_CLIENT_INVOCATIONS` を追加する。`AGENT_READINESS_METHODS` は変更しない。response registry は作らず、既存 thread/turn parser と同じ専用 parser で `turnId` を検証する。
- [ ] GREEN: `_EXPECTED_METHODS`、全 `CapabilitySnapshot` constructor、`_steer_state()` を更新する。
- [ ] GREEN: AgentSession に単一 steer claim/task を追加し、start/interrupt と同じ shield/settlement pattern で cancellation と close を回収する。既知 rejection は stable `agent_steer_rejected`、受理不明は既存 unknown terminal pathへ分ける。`reusable` は同一loopでsettlement完了後に読むbool propertyだけとする。`AgentTurnErrorCode`は上記3値の`StrEnum`、terminal例外はcode以外のserver payloadを持たず、Coordinatorはmessage文字列を解析しない。
- [ ] 全回帰と配置を確認する。

```bash
just test-python tests/test_repository_contract.py tests/test_codex_schema.py tests/test_codex_capabilities.py tests/test_codex_agent.py -q
rg -n "TURN_STEER|turn/steer|expectedTurnId|_steer_task|steer=|AgentTurnErrorCode|reusable" src/moco/codex src/moco/errors.py tests
```

## 実装単位 2: ordered file-change correlation

**Files:**

- Modify: `src/moco/codex/rpc.py`
- Modify: `src/moco/codex/connection.py`
- Modify: `src/moco/codex/schema.py`
- Modify: `src/moco/codex/approval.py`
- Modify: `src/moco/codex/broker.py`
- Modify: `tests/test_codex_rpc.py`
- Modify: `tests/test_codex_connection.py`
- Modify: `tests/test_codex_schema.py`
- Modify: `tests/test_codex_approval.py`

- [ ] RED: wire 上で `item/fileChange/patchUpdated` notification と `item/fileChange/requestApproval` request を yield なしで連続投入しても、approval handler 開始前に explanation が存在することを固定する。通常 subscriber fan-out と server request exactly-once response は維持する。
- [ ] RED: generated patch notification shape は required `changes` / `itemId` / `threadId` / `turnId`。各 change は required `diff` / `kind` / `path`、`kind` は object の required `type: add|delete|update`、update だけ optional `move_path: string | null` を持つ。absent/null は destination なし、non-string/non-null は fail-closed とする。`diff` は検証するが保存しない。
- [ ] RED: `(threadId, turnId, itemId)` 全一致、notificationごとのchangeは1〜64件、path/kind/destination exact shape、one-shot explain、turn terminal/connection close で clear、unknown member/unknown kind/invalid moveはfail-closedを固定する。private map全体は異なるcorrelation keyを最大64件まで保持し、既存keyのreplacementは件数を増やさず許可する。65個目の新規keyはpayload-free protocol failureとしてconnectionをterminalizeする。
- [ ] RED を確認する。

```bash
just test-python tests/test_codex_rpc.py -q -k "notification_observer"
just test-python tests/test_codex_rpc.py -q -k "notification_before_request"
just test-python tests/test_codex_connection.py -q -k "notification_observer"
just test-python tests/test_codex_schema.py tests/test_codex_approval.py -q -k "file_change_patch"
```

- [ ] GREEN: `RpcPeer` と `CodexConnectionSupervisor` に before-start registration だけを許す1個の synchronous notification observer slot を追加する。inbound notification は observer を完了してから subscriber queue へ fan-outし、次の inbound server request を処理する。observer は await不可、2個目の登録は拒否、例外は payload-free protocol failure として connection を terminalize する。
- [ ] GREEN: schema から patchUpdated method/shape を optional Agent event evidence として導出する。証拠がない build は Agent admission を壊さず、newer file approvalだけを説明不能として fail-closedにする。
- [ ] GREEN: Broker 内に64 correlation key上限のprivate bounded mapを置き、同期 observer で `FileChangeExplanation` だけを保存する。Broker の handler registration が observer を approval handler より先に supervisorへ登録する。既存 `explain_file_change` seam は test injectionに限定し、productionはprivate mapを使う。AgentSessionのexact `(threadId, turnId)` ownershipをReviewer公開前に同期callbackとしてbindし、modern approvalはcurrent active Agent turn以外を公開前にfail-closedにする。observerとAgent notification pumpの同一loop順序をまたぐ直近64 terminal turnだけはbounded tombstoneで補い、無制限の完了ID履歴を持たない。
- [ ] GREEN: explanation は approval adapterへ渡す時に popし、turn terminal/connection lost/close で残りを破棄する。独立 public tracker、patch body保存、notification event bus は追加しない。
- [ ] 全回帰を実行する。

```bash
just test-python tests/test_codex_rpc.py tests/test_codex_connection.py tests/test_codex_schema.py tests/test_codex_approval.py -q
rg -n "notification_observer|patchUpdated|FileChangeExplanation" src/moco/codex tests
```

## 実装単位 3: immutable Coordinator、exactly-once、queue、final-only result

**Files:**

- Create: `src/moco/runtime/coordinator.py`
- Create: `tests/test_coordinator.py`
- Modify: `src/moco/runtime/__init__.py`

- [ ] RED: snapshotの四軸とidle projectionを固定する。idleはconnection ready/degraded、voice idle、task terminal/none、speech silentの場合だけtrue。private queueはactive RUNNING/WAITING_REVIEWとだけ共存し、private pending count 1以上は必ずWAITING_REVIEWなので、追加public fieldなしでidleを判定できる。
- [ ] RED: `connection_changed`、`listen_started`、`listen_stopped`、`voice_lost()`、`consume_user_final`、`review_count_changed`、`speech_changed` の小さい pure transitionを固定する。文字列 dispatch/FSM frameworkは作らない。
- [ ] RED: Voice IDLEからの`listen_started`だけprivate generationを増やし、同じLISTENING中のstartはidempotentにする。Realtime VADのuser doneはLISTENING中にそのままhandoffし、VoiceをLISTENINGに保ったまま次のutteranceも受理する。`listen_stopped`はVoiceをIDLEへ戻してF1による即時再開を許可し、停止直後に届く現generationのfinalだけは一度受理して同じadapter eventの再配送を無視する。F2を文字起こし確定条件にしない。Realtime transcript schemaにremote item IDはないため本文や時間から推測せず、Browser adapterが観測したuser transcript partへ単調増加identityを付ける。新しいdeltaまたは新しいdone-only eventで始まる次partは、本文が同じでも別identityとして受理する。
- [ ] RED: ownerがmatching Voice resource generationと確認したterminalだけが引数なし`voice_lost()`を呼び、VoiceをIDLEへ戻してLISTENINGまたは停止直後の未完了listen generationをabandon/consumed扱いにする。CoordinatorはVoice resource generationを保持・比較しない。ownerはterminalだけでなく全Voice eventをcurrent resource generationと照合し、user transcript buffer更新とfinalのCoordinator state claimまでは同じ同期区間でawaitを挟まない。generation 2のfresh Voice開始後にgeneration 1のlate finalが到着してもhandoff 0件とし、current resource generationのdoneだけを受理する。transcript/activity等の外向きeffectも送信直前にowner identity/generationを再確認する。
- [ ] RED: user transcriptは同期claim後に単一FIFO workerでsource orderを保って公開し、slow steer settlementで通知受信を止めない。Realtime adapterとBrowser transcriptのpending queueを各64件、1発話64 KiB／256 partsに制限し、overflowはVoiceをfail-closedにして再接続待ちへ移す。中間deltaを黙って破棄したり、独立taskへ無制限fan-outしたりしない。Agent progressは結果・transcript・speechと分離した単一FIFO senderで64件まで保持し、遅いOperatorへの上限超過分だけをpayload-free metadataへ記録して省略する。
- [ ] RED: task none/terminalなら `start_turn`、running+steer availableなら `steer`、running+unavailableなら1件queue、waiting_reviewではsteer availableでも1件queue、2件目はbusy。active completion後はqueued textを同じAgentSession threadの次turnへ一度だけ昇格する。
- [ ] RED: 既知 steer rejection は `HandoffDisposition.REJECTED` と `on_submission_error("agent_steer_rejected")` を返し元turnはrunning、queue/retryなし。受理不明はturn taskのunknown failureでFAILED、queue discard、no replayにする。
- [ ] RED: in-flight steer中のexplicit cancelはprivate cancel claimで新規handoffを止め、steer submission taskを先にcancel/settleしてからactive wrapperをcancelする。steer success/known rejectionならactive interrupt exactly once、steer受理不明/non-reusableならFAILED/DISCONNECTED、追加interrupt 0回、queue discard/no replayとする。steer settlement待機中に同generationのfinal/failed/interrupted terminalがwrapperを先に完了した場合はterminal結果を唯一の勝者とし、追加interrupt/interrupted合成を行わない。
- [ ] RED: fast/slowとも中間音声を発さず、terminal generation claim後にfinal/error resultをexactly once発行する。
- [ ] RED: `TurnResult` は `final_answer: str | None` と`error_code: str | None`のexactly-oneだけを持ち、snapshot state/generationを重複させない。上の4行mappingをfast/slow/cancel/known failure/unknown/connection lossでexactly once固定する。
- [ ] RED: server completed/failed/interrupted terminalがwaiting reviewより先に確定した場合、terminal hookがpendingをwithdrawしlate decisionを拒否してcount 0へ戻してから、terminal snapshotまたはqueued turn昇格を行う。終了済みturnのreview detailを残さない。
- [ ] RED を確認する。

```bash
just test-python tests/test_coordinator.py -q
```

- [ ] GREEN: Coordinator mutationは同一 asyncio event loop上でawaitを挟まない短いclaimとして実装する。外部callbackはすべて同期・non-blockingとし、async UI/TTS workはowner側がtask化する。
- [ ] GREEN: private stateはlisten/turn generation、consumed generation、queued text、pending review count、wrapper turn task、steer submission task、cancel/terminal claimに限定する。snapshotへrace制御flagを出さない。
- [ ] GREEN: app-server connection terminalは `connection_lost()` で同期的にDISCONNECTEDへ移し、activeをunknown failure、queueを破棄し、pending review countを0へ収束させる。新leaseは旧utteranceを引き継がない。
- [ ] raceを反復確認する。

```bash
just test-python tests/test_coordinator.py -q
for run in {1..20}; do
  just test-python tests/test_coordinator.py -q -k "deadline" || exit 1
  just test-python tests/test_coordinator.py -q -k "duplicate" || exit 1
  just test-python tests/test_coordinator.py -q -k "connection_lost" || exit 1
done
rg -n "deque|retry|replay|event.?bus|registry" src/moco/runtime/coordinator.py
```

## 実装単位 4: pending review と explicit cancel

**Files:**

- Modify: `src/moco/codex/broker.py`
- Modify: `src/moco/web/messages.py`
- Modify: `src/moco/web/app.py`
- Modify: `src/moco/web/static/index.html`
- Modify: `src/moco/web/static/app.js`
- Modify: `tests/test_codex_approval.py`
- Modify: `tests/test_web_messages.py`
- Modify: `tests/test_web.py`
- Modify: `tests/js/app.test.js`
- Modify: `tests/test_coordinator.py`

- [ ] RED: Broker pending count は publish成功後だけ1、2件なら2、decision/disconnect/handler cancellation/connection loss/closeで0へ戻る。adaptation failure/queue fullはpendingを残さない。
- [ ] RED: bound count callbackはpure同期・no-raiseなCoordinator transitionとする。代表的なinjected callback exceptionでは共通helperがpendingをexactly onceでfailしBrokerをterminalizeし、owner境界がwhole leaseをcloseする。path別rollback matrixは作らず、payload/handle/request idをerror/logへ出さない。
- [ ] RED: pending 0→1と同じ同期区間で Coordinator はrunning→waiting_reviewへ移る。その直後のuser finalはsteer/decision/interruptではなく1件queueになる。1→0はactive turnのときだけrunningへ戻し、terminal taskをresurrectしない。
- [ ] RED: strict `ClientControl.TURN_CANCEL`、明示button、running/waiting_reviewからのcancel exactly-once、queue破棄、no-active stable errorを固定する。`thread/start`中または`turn/start` response待ちでもcancelでき、Coordinator所有wrapper taskを一度だけcancelする。wire送信前ならinterrupt 0回、送信後なら既存`AgentSession.start_turn()` cancellation settlementがturn idを回収してinterrupt 1回、通常activeなら同じcancellation pathでinterrupt 1回とし、`AgentSession.interrupt()`を並行発行しない。Voice transcriptの「キャンセル」は通常utteranceであり、Reviewerの`ApprovalDecision.CANCEL`とも共有しない。
- [ ] RED: waiting_review中のexplicit cancelはBrokerの現在pendingをfail-closedで全withdrawし、pending count 0、Reviewer DOMからdetail除去、late decision拒否にする。read済みreviewにもwithdrawalはexactly onceで、同じReviewer connectionとBrokerが次turnのapprovalをpublishできる。Brokerは1 Agent lease/一active turnのownerなのでturn correlation APIやreview registryは追加しない。
- [ ] RED: cancel buttonにglobal hotkeyを追加せず、Reviewer acceptとDOM/handlerを共有せず、server stateのprivacy-safe `canCancel` booleanがfalseの時はdisabledにする。`canCancel`はtask running/waiting_reviewからだけ導出し、task detailを含めない。
- [ ] RED を確認する。

```bash
just test-python tests/test_codex_approval.py -q -k "pending_count"
just test-python tests/test_codex_approval.py -q -k "waiting_review"
just test-python tests/test_web_messages.py tests/test_web.py tests/test_coordinator.py -q -k "turn_cancel"
just test-python tests/test_web_messages.py tests/test_web.py tests/test_coordinator.py -q -k "spoken_cancel"
just test-python tests/test_web_messages.py tests/test_web.py tests/test_coordinator.py -q -k "waiting_review"
just test-frontend "turn cancel"
```

- [ ] GREEN: Brokerへ one-shot `bind_pending_count_changed(callback)` と同期`cancel_pending()`を追加する。bindはCoordinator生成後、reviewer slot公開前、pending 0のopen brokerに一度だけ許可する。それ以前のunsolicited approvalはreviewer不在でpublish前にfail-closedにする。`cancel_pending()`は現在の全futureをstable cancel errorで一度だけfailし、unread envelopeをdrop/read済みhandleをwithdrawしてcountを0へ通知するが、Broker/Reviewer connectionは閉じない。
- [ ] GREEN: `_PendingReview`にprivate one-shot withdrawal claimを持たせ、`cancel_pending()`と既存`review().finally/_discard()`は同じ`_withdraw_once()`だけを使う。pending count mutationは単一helperからpure同期callbackへ通知し、unexpected exceptionは共通Broker terminal pathへ送る。`call_soon`、async queue、observer list、path別rollbackは使わない。
- [ ] GREEN: ownerは`on_turn_terminal_claimed`を同じBrokerの`cancel_pending()`へbindする。explicit `turn_cancel` はBrowser→Conversation ownerの同一event-loop claimで、waiting reviewならawait前に同operationを呼び、Coordinatorはprivate cancel claim後にin-flight steer submissionを先にsettleする。そのawait後・wrapper cancel前の同一同期区間で同generationのterminal claimと`wrapper.done()`を再確認し、terminal先行ならactual resultを一度だけ採用してcancelを終了する。未完了かつsession reusableの場合だけwrapperをcancelし、既存`AgentSession.start_turn()`/active waitのinterrupt cleanupへ委ねて、別の`AgentSession.interrupt()` taskは作らない。settlement後`AgentSession.reusable`ならINTERRUPTED/`agent_turn_interrupted`、falseならFAILED/`agent_outcome_unknown`、queue破棄/no replayかつConnection DISCONNECTEDとする。ownerの既存`on_snapshot_changed` sinkはDISCONNECTEDを見てwhole-lease closeを一度だけclaimする。listen_stop、Voice transcript、Reviewer decisionからcancel controlを呼ばない。
- [ ] 全回帰を実行する。

```bash
just test-python tests/test_codex_approval.py tests/test_web_messages.py tests/test_web.py tests/test_coordinator.py -q
just test-frontend
rg -n "bind_pending_count_changed|review_count_changed|turn_cancel|turn-cancel" src/moco tests
```

## 実装単位 5: production lease、Voice re-offer、idle/UI projection

**Files:**

- Modify: `src/moco/web/reviewer.py`
- Modify: `src/moco/web/app.py`
- Modify: `src/moco/web/messages.py`
- Modify: `src/moco/web/static/app.js`
- Modify: `src/moco/runtime/lifecycle.py`
- Modify: `src/moco/runtime/__init__.py`
- Modify: `src/moco/codex/__init__.py`
- Modify: `tests/test_codex_reviewer.py`
- Modify: `tests/test_web.py`
- Modify: `tests/test_web_messages.py`
- Modify: `tests/test_lifecycle.py`
- Modify: `tests/js/app.test.js`

- [ ] RED: production composition orderを次で固定する。

```text
CodexSchemaProbe.probe exactly once
→ InteractionBroker construction
→ notification observer / approval handler / owner terminal callback registration
→ CodexConnectionSupervisor.start
→ CapabilityDiscovery with the same contract
→ AgentSession with the same contract and snapshot
→ InteractionCoordinator
→ Broker pending callback bind
→ CodexRealtimeSession.start
→ private Reviewer slot bind
→ connection READY or DEGRADED transition / lease publish
```

- [ ] RED: handler/observerがSupervisor.start後、contract再probe、BrokerとAgentが別contractなら失敗する。SupervisorがCoordinator生成前に終端した場合はstartupを中止する。公開後のterminal callbackはCoordinatorへ同期`connection_lost()`を渡してactiveをunknown failure、queueを破棄し、ownerのwhole-lease closeを一度だけclaimする。Agent notification pump未開始のidle中lossもDISCONNECTEDになり、Reviewer slotをreleaseしてVoice/Agent/Broker/connectionを全回収する。
- [ ] RED: readiness mappingを固定する。`agent_admission`または`realtime`がAVAILABLEでなければ、private Reviewer slot / Voice / leaseを公開する前にstartupを失敗させる。両方AVAILABLEで、steerがAVAILABLE、かつpatch correlation evidenceが必要なcontractでは`patchUpdated` evidenceもAVAILABLEならREADY、それ以外の任意能力不足はDEGRADEDとする。changesを自身に持たないmodern file approval familyをadvertiseするcontractだけがpatch evidenceを必要とし、legacy/self-contained familyだけならpatch evidenceなしでもREADYになれる。DEGRADEDではsteer unavailableを1件queueへ落とし、説明不能なmodern file approvalはfail-closedにする。READY/DEGRADEDはいずれもidle判定のconnection eligible stateとする。
- [ ] RED: production Reviewerはinitial Voice start成功後のactive lease中だけcurrent Brokerへ接続でき、開始前/Voice start失敗/終了後は1008 unavailable。holderはcurrent broker1個、identity bind/releaseだけで、conversation registryを持たない。approval handler/notification observerはSupervisor.start前に登録するが、slot bindとREADY/DEGRADED公開はVoice成功後だけにする。
- [ ] RED: 同じownerでVoice固有terminal後に次のlisten-startから再offerし、AgentSession/thread id/Broker/connectionを維持する。Supervisor connection terminalはVoice固有terminalとして扱わずprivacy-safe `connection_lost`を表示してlease全体を閉じる。operator WebSocket close、StopMessage、idle expiry、daemon endもlease全体を閉じ、次の明示handshakeは新lease/new Agent threadを作り、旧active/queueを再送しない。
- [ ] Voice re-offer APIを次に固定する。
  - initial `start(sdp)` はlease resourcesを一度だけ構成してVoice generation 1を開始
  - `close_voice(expected_generation)` はownerがVoice resource generation一致を確認した時だけ引数なしCoordinator `voice_lost()`を適用して閉じ、Agent/Broker/connectionを残す
  - `replace_voice(sdp)` はlease openかつVoice inactiveの時だけgenerationを増やして開始
  - `notifications(expected_generation)` は同generationだけを返し、old streamのlate terminalはnew Voiceを閉じない
  - notification consumerはiterator取得後も各event dispatch直前にownerのcurrent generationを再確認し、user finalのCoordinator claimまでawaitなしで進め、stale transcript/terminal/activityを捨てる
  - `voice_active` と `voice_generation` はresource factでありsnapshotの追加fieldにしない
  - initial Voice start failureは未公開lease全体をcloseし、replacement failureはVoice inactiveのまま`voice_reconnect_required`を保つ。どちらも自動retryしない
- [ ] RED: Browserはactive Voice中のStartMessageだけ`already_started`。ownerがmatching resource generationと確認したVoice固有terminal/invalid eventではCoordinator `voice_lost()`を同期適用してからpartial speechをinvalidateし、Voice resourceとgeneration-bound notification taskだけを閉じ、presentation state `voice_reconnect_required`を送る。frontendは自動retryせず、`voice_reconnect_required`または`connection_lost`後の次の明示listen-startで`connectConversation()`を一度だけ呼ぶ。同じWebSocket上のStartMessageを受けたserverはownerが存在しVoiceだけinactiveなら`replace_voice`、ownerがdetach済みならnew leaseを選ぶ。connection terminal後またはold generationのlate Voice terminalはVoice-only経路へ入らず、dead lease上の`replace_voice`を試みない。
- [ ] RED: current `RTCPeerConnection` failureはstrict exact `VoiceLostMessage {"type":"voice_lost"}`（reason/generation/unknown fieldなし）を同じOperator WebSocketへ送り、current peer/mediaだけを閉じてsocket/owner/Agent thread/Broker/app-server connectionを維持する。old peer callbackはfrontend peer identityでno-opにする。replacement handshake failureもfailed peerだけを閉じ、`voice_reconnect_required`とmanual retryを維持する。initial unpublished Voice start failureとapp-server connection terminalだけはwhole leaseを閉じる。
- [ ] RED: `IdleLeaseTimer` はtimestamp/expiredだけを持ち、snapshot `is_idle` false中はexpireしない。ready/degradedで全軸idleになった時点からtimeoutを測り、lease全体を一度だけcloseする。
- [ ] RED: timeout直前の明示listen-startによるVoice re-offerは、ownerが`replace_voice` taskを最初のawait前にclaimしてIdleLeaseTimerをtouchする。replacement task中はexpiryがwhole-closeをclaimできず、success/failure settlementで再touchしてそこから新しいidle期間を測る。fake clockでreplace途中closeなし、settlement後timeoutだけを固定する。
- [ ] RED: UI projection優先順は `idle_expired`（lease外）→ `connection_lost`（lease外）→ `connecting` → `voice_reconnect_required` → `listening` → `transcribing` → `waiting_for_local_review` → `speaking` → `ready`。public state/activityにcommand/path/reason/patchを含めない。connection lossのwhole-closeとlate Voice terminalの競合でも`voice_reconnect_required`へ戻さない。
- [ ] RED: listen-stopはマイクだけを止めてauthoritative Voice stateをIDLEへ戻し、直後のlisten-startは同じVoice generationでライブ入力を再開する。listen-startを文字起こし待ちのbusyにしない。frontendは受信stateが`listening`以外ならaudio track、MIC表示、pressed stateをOFFへreconcileし、遅着したstop由来stateの後に`listening`を受けた場合はtrackと表示をONへ戻す。Python WebSocket testとNode testの両方で固定する。
- [ ] RED を確認する。

```bash
just test-python tests/test_web.py -q
just test-python tests/test_codex_reviewer.py tests/test_lifecycle.py -q
just test-frontend "voice reconnect required|waiting for local review|connection lost|transcribing|listen busy"
```

- [ ] GREEN: `_ReviewerBrokerSlot` をweb composition private classとして実装し、既存`ReviewerBroker` protocolのforwardingとidentity bind/releaseだけを持たせる。initial Voice start成功後にだけbindし、export/manager/registryは追加しない。
- [ ] GREEN: `_CodexConversationOwner`をapp-server/Agent/Broker/Coordinator lease ownerにし、Voiceだけgeneration付きで交換可能にする。Voice notification taskはuser finalのbuffer/Coordinator claimまでawaitなしのowner identity/generation照合区間で処理し、外向きeffect前にも再照合する。`replace_voice` taskはowner private resource factとしてclose/idle-expiry claimと直列化し、開始・settlementでtimerをtouchする。strict Voice-loss client messageはcurrent Voiceだけへ`close_voice(current_generation)`を適用し、Operator socketを閉じない。Supervisor terminal callbackはCoordinator terminalizationとowner identity付きwhole-close taskを同一loopで一度だけclaimする。Browserはprivacy-safe `connection_lost`を送ってcurrent ownerをdetachし、closeはCoordinator→AgentSession→Broker/slot→Voice→connectionのcleanupをshieldしてcaller cancellation後も同じtaskを回収する。次のStartMessageだけが新ownerを構成する。
- [ ] GREEN: `LifecycleController`/`BusyKind`をtimestampだけの`IdleLeaseTimer`へ縮小する。Browserの`_synthesis_busy`、`_delegated_busy`、mutable `_browser_state`を削除し、SpeechQueue/playbackのresource factsからCoordinator `SpeechState`を更新する。既存のfirst-user-transcript→`_invalidate_speech()`とprivate `_user_utterance_active` guardは挙動を変えずTask 10まで保持し、新しいCoordinator speech effect/cancel compositionは追加しない。
- [ ] GREEN: user doneのhandoffをBrowser transcript adapterへ接続する。frontendは`voice_reconnect_required`/`connection_lost`を単一private reconnect-required factへ投影し、次の明示listen-startだけでhandshakeする。Voice assistant transcriptの既存SpeechQueue経路はStage B Task 10まで変更しない。
- [ ] 全回帰を実行する。

```bash
just test-python tests/test_codex_reviewer.py tests/test_lifecycle.py tests/test_web.py -q
just test-frontend
rg -n "_ReviewerBrokerSlot|replace_voice|close_voice|voice_reconnect_required|IdleLeaseTimer" src/moco tests
rg -n "LifecycleController|BusyKind|_synthesis_busy|_delegated_busy|_browser_state" src/moco
```

後者の検索はproduction codeで0件を期待する。

## 実装単位 6: integration、no-replay、全gate

**Files:**

- Modify: `tests/fixtures/fake_codex.py`
- Modify: `tests/test_integration.py`
- Modify: `tests/test_codex_contract.py`
- Modify: `tests/test_web.py`
- Modify: `tests/test_coordinator.py`
- Verify: Stage B Task 9で変更した全file

- [ ] fake Codexへturn/steer、patchUpdated→approval連続送信、turn completion、connection lossをdeterministicに発生させるtest optionだけを追加する。production fault-injection/reconnect flagは追加しない。
- [ ] integration RED/GREENで次を固定する。
  - one final transcript→oneAgent turn、duplicate done→追加0
  - running+steer→same thread/expected turn id
  - steerなし→1件queued second turn、third utterance busy
  - known steer rejection→元turn継続、unknown steer outcome→FAILED/no replay
  - patch notification直後approval→説明済みlocal review
  - waiting review中utterance→decisionでなくqueue、explicit cancel→pending withdraw/late decision拒否/Broker次turn再利用
  - waiting review中server terminal→pending withdraw/count 0後にterminalまたはqueue昇格
  - thread/start中、turn/start response待ち、active中のexplicit cancel→interrupt 0/1/1回、成功はINTERRUPTED、cleanup unknownはFAILED/no replay/whole close
  - in-flight steer中cancel→steer known settlement後interrupt一回、steer unknown→追加interruptなし/whole close、待機中terminal先着→actual result一回
  - listen-stop→mic off/Voice IDLE、直後のlisten-start→同じ会話でlive input再開、旧partのduplicate done→追加handoff 0
  - fast/slow/no-intermediate-speech final-only
  - idle/active/start/steer/review中connection loss→unknown/failure、queue破棄、Reviewer release/whole close
  - Voice re-offer→same Agent thread、whole lease close/new lease→new thread
  - LISTENING中またはlisten-stop直後のVoice loss→未完了part abandon、re-offer後fresh listen、fresh start後もlate old event無視
  - timeout直前Voice re-offer→in-flight expiryなし、settlement後から新idle期間
  - current peer/replacement failure→socket/Agent thread維持とmanual retry、old peer callback no-op、app-server lossだけwhole close
  - connection loss後のlate Voice terminalはreconnect-requiredを出さず、明示handshake/new leaseでold active/queued utteranceを再送しない
- [ ] focused suiteを実行する。

```bash
just test-python \
  tests/test_codex_rpc.py \
  tests/test_codex_connection.py \
  tests/test_codex_schema.py \
  tests/test_codex_capabilities.py \
  tests/test_codex_agent.py \
  tests/test_codex_approval.py \
  tests/test_codex_reviewer.py \
  tests/test_coordinator.py \
  tests/test_lifecycle.py \
  tests/test_web_messages.py \
  tests/test_web.py -q
just test-integration
just contract-codex
just test-frontend
```

- [ ] full repository gateとscope scanを実行する。

```bash
just check
git diff --check
scan_pattern='TO''DO|TB''D|FIX''ME|implement lat''er|pass$|NotImplement''ed'
rg -n "$scan_pattern" src/moco/runtime/coordinator.py
rg -n "InteractionCoordinator|TURN_STEER|notification_observer|bind_pending_count_changed|cancel_pending|_ReviewerBrokerSlot|replace_voice|IdleLeaseTimer|turn_cancel" \
  src/moco tests justfile
git status --short
```

- [ ] Stage B Task 9のcorrectness reviewとsecurity reviewを、実装・修正に参加していない別々の新規インスタンスへ依頼する。修正後のre-reviewも前回とは異なる新規インスタンスにする。
- [ ] stage/commitは利用者が明示した場合だけ行う。完了報告にはfocused/full gate、残余risk、Stage B Task 10へ残したcompositionを明記する。

## Stage B Task 10へのhandoff

Task 9完了時にTask 10へ渡すのは、`TurnResult.final_answer`またはstable `TurnResult.error_code`、authoritative `InteractionSnapshot`だけである。Task 10はそれらをOperator UI、SpeechQueue、privacy-safe progressへ接続し、新utterance/cancelのspeech invalidation ownershipを既存Browser guardからCoordinator effectへ移す。Voice assistant transcriptはtask回答として公開しない。
