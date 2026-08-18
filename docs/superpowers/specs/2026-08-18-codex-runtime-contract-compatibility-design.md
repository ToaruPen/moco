# Codex runtime contract compatibility design

## 背景

moco の launchd サービスは、シェルの `PATH` にある Codex ではなく ChatGPT.app 同梱の
Codex を自動検出する場合がある。2026-08-18 時点で、この PC には次の二つの契約が存在する。

- PATH 版 `codex-cli 0.144.1` は Realtime protocol `v1` と `v2` だけを受理する。
- ChatGPT.app 同梱版 `codex-cli 0.148.0-alpha.9` は `v3` を受理する一方、legacy approval
  response に `approved_mcp_policy_amendment` を追加している。

現行 moco は Realtime start payload の `version: "v3"` を生成 schema で検証せず、承認応答の
追加 variant も既知の未送信値として扱わない。このため PATH 版では Realtime start 後に RPC
error となり、同梱版では接続開始前の approval handler 登録で拒否される。

## 目的

実行時に選ばれた Codex の生成 schema を唯一のプロトコル根拠とし、次を満たす。

- moco が実際に送る `thread/realtime/start` の v3/audio/WebRTC payload を開始前に検証する。
- schema が検証した raw RPC method 名を Realtime start に使う。
- `approved_mcp_policy_amendment` を Codex が持つ未送信 variant として認識し、legacy command
  と file approval を安全に読み書きできる状態へ戻す。
- 非互換な Codex は thread や WebRTC を開始する前に capability mismatch として拒否する。
- doctor とブラウザ会話開始は同じ生成契約と readiness 判定を使う。

## 非目的

- Realtime v2 への自動 fallback は追加しない。moco の会話と Agent handoff は v3 を製品契約
  としており、v2 の機能同等性は確認されていない。
- Codex executable の設定を特定パスへ固定しない。各プロセスが既存の解決規則で選んだ
  executable を、その executable 自身の schema に対して評価する。
- 新しい approval decision、永続 MCP policy 承認、設定項目、UI は追加しない。
- schema 全体の汎用 negotiation layer や、RPC failure 後の retry mechanism は追加しない。

## 設計

### Realtime start 契約

`CodexProtocolContract` の既存 client method contract に Realtime start semantic を加える。
schema probe は、moco が production で送る次の完全な invocation witness を生成 schema に照合
する。

- dynamic `threadId`
- `outputModality: "audio"`
- `includeStartupContext: false`
- dynamic prompt
- `transport: {"type": "webrtc", "sdp": <dynamic string>}`
- `version: "v3"`

全 envelope が schema に確実に受理される場合だけ semantic method contract を生成する。
method の存在や `version` enum だけを個別に見るのではなく、実際の送信形を一つの境界として
検証する。これにより、将来別の必須 member や transport 制約が加わった場合も半端な readiness
を主張しない。

Realtime start semantic は Voice readiness の必須 method に含める。`CapabilityDiscovery` は
voice catalog と feature flag に加え、この method contract が有効な場合だけ
`realtime=available` を返す。非互換時の detail は既存の `method_unavailable` を使い、新しい
状態 vocabulary は増やさない。

`CodexRealtimeSession` は生成済み contract を受け取り、検証済み Realtime start method 名を
request に使う。payload の v3/audio/WebRTC product contract は変えない。thread start も既存の
semantic contract が検証した raw method 名を使い、Voice session 内の二つの start request を
同じ contract source に揃える。

### Approval variant

`approved_mcp_policy_amendment` は legacy approval response schema が列挙するが、moco が提供
する一回限りの accept/decline/cancel には対応しない。この値を legacy family の
`unsent_variants` に加える。

この分類は値を無視して任意の response を許すものではない。probe は従来どおり response の
decision schema 全体を読み、既知の三 decision に対応する wire value が schema に受理される
ことを確認する。追加 variant は「Codex 側には存在するが moco は送らない値」としてのみ許可
するため、moco が永続 MCP policy を承認することはない。

### 起動と失敗経路

起動順は次のとおりとする。

1. 既存規則で Codex executable を選ぶ。
2. その executable から experimental schema と version を取得する。
3. client methods、approval profiles、Agent event profiles を一つの contract にコンパイルする。
4. approval broker が全必須 approval alias を処理可能か確認する。
5. capability discovery が account、policy、feature、voice、Realtime v3 start、Agent readiness を
   判定する。
6. 必須 readiness が揃った後だけ Voice thread と WebRTC Realtime session を開始する。

Realtime v3 witness を受理しない build は `realtime=version_mismatch/method_unavailable` となる。
ブラウザ側の会話 owner は既存の required capability error で停止し、doctor は同じ snapshot を
`codex_realtime` error として投影する。v3 request を試してから v2 へ下げる経路は持たない。

doctor と launchd が異なる環境で異なる executable を自動検出した場合、それぞれは選択した
実体の正しい互換性を報告する。二つの結果を同一 executable の結果と見せかける補正は行わない。

## テスト

TDD で次の回帰を先に固定する。

### Schema contract

- v3/audio/WebRTC witness を受理する generated schema は Realtime start semantic を生成する。
- `v1` と `v2` だけの schema は Realtime start semantic を生成しない。
- method alias が schema に現れた場合、contract がその raw name を保持する。
- `approved_mcp_policy_amendment` を含む legacy command/file response から approval profile を生成
  できる。
- profile の accept/decline/cancel wire value に追加 variant が選ばれない。

### Capability and session

- Realtime start semantic がない snapshot は `version_mismatch/method_unavailable` になる。
- 非互換時は `thread/start`、`thread/realtime/start` のどちらも送らない。
- 互換時は schema が証明した thread start と Realtime start の method 名を使い、v3/audio/WebRTC
  payload を一度だけ送る。
- doctor は Realtime 非互換を error として表示する。

### 実環境

- PATH 版 `0.144.1` は v3 非互換として schema/doctor contract check が失敗する。
- ChatGPT.app 同梱版 `0.148.0-alpha.9` は必須 approval family と Realtime v3 contract を通る。
- 全 unit、integration、static checks を通す。
- PR merge 後に launchd を再インストールまたは再起動し、browser WebRTC の会話開始まで確認
  する。
- Irodori capability/readiness と synthesis probe も再実行し、既存音声経路に回帰がないことを
  確認する。

## 完了条件

- ChatGPT.app 同梱 Codex を使う launchd 環境でブラウザ会話を開始できる。
- 非対応 Codex は conversation start 後の RPC error ではなく開始前の readiness error になる。
- command/file approval の一回限りの decision contract を維持し、追加 policy grant を送らない。
- polishment と AI slop cleaner のレビューを順に通し、PR の CI と review feedback を収束させる。
- PR merge、worktree と branch の清掃、再デプロイ、実接続確認まで完了する。
