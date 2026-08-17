# Profile-aware Agent admission design

## 背景

moco は Codex の有効設定から sandbox と approval policy を読み取り、`danger-full-access`
と `never` の組み合わせを音声 Agent turn の開始前に拒否している。この判定は安全側だが、
`agent.profile: read_only` や `workspace_write` が thread 作成時に明示する policy まで、global
Codex policy によって止めてしまう。

明示 profile の実行条件は global policy では決まらない。`read_only` は `read-only` と
`never`、`workspace_write` は `workspace-write` と `on-request` を `thread/start` へ渡す。
global policy を継承するのは `inherit_codex` だけである。

## 決定

Agent admission を二つの責務に分ける。

1. Capability discovery は、account、protocol schema、Agent event、interrupt、server request
   category など、profile に依存しない実行準備を判定する。
2. Agent session と doctor は、選択された profile の境界で policy を判定する。

`read_only` と `workspace_write` は global Codex policy を admission 条件にしない。
`inherit_codex` は global policy をそのまま使うため、policy が取得できない場合と
`danger-full-access + never` の場合を引き続き拒否する。

## 実行時の流れ

Capability discovery は global policy を観測し、`effective_policy` と `policy_state` に保持する。
ただし、その値だけを理由に `agent_admission` を disabled にしない。他の契約が揃っていれば、
profile 非依存の admission は available になる。

Agent session は turn 開始前に profile を確認する。

- `read_only`: `sandbox: read-only`、`approvalPolicy: never` を送る。
- `workspace_write`: `sandbox: workspace-write`、`approvalPolicy: on-request` を送る。
- `inherit_codex`: sandbox と approval policy を送らず、観測済み global policy を検査する。

`inherit_codex` で policy が不明または危険な組み合わせなら、既存の安定した admission error
で fail-closed にする。明示 profile では、global policy が危険でも thread の明示 policy を
正として開始する。

## Doctor 表示

`codex_policy` は global Codex policy の観測結果として表示を残す。これは診断情報であり、
明示 profile の admission 結果を決めない。

`codex_agent_admission` は選択 profile を反映する。`read_only` と `workspace_write` では、
profile 非依存の準備が整っていれば available とする。`inherit_codex` では global policy を
加味し、`danger-full-access + never` を `unsafe_voice_policy` として報告する。

## 変更範囲

- Capability discovery の admission 判定から global unsafe policy の拒否を外す。
- Doctor の projection に選択 profile を渡し、`inherit_codex` だけ unsafe policy を拒否する。
- Agent session の既存 profile 境界を維持し、明示 profile が global policy を参照しないことを
  回帰テストで固定する。
- README の policy 説明を profile-aware な契約へ更新する。

Codex の global config、moco の `codex.command`、sandbox 名、approval policy 名は変更しない。
`danger-full-access` を moco の明示 profile として追加しない。

## 検証

テストは次の境界を確認する。

- Capability discovery は global policy が `danger-full-access + never` でも、他の準備が揃えば
  profile 非依存 admission を available とする。
- Doctor は `read_only` と `workspace_write` で global unsafe policy を情報として表示しつつ、
  Agent admission を available とする。
- Doctor は `inherit_codex` で同じ policy を `unsafe_voice_policy` として拒否する。
- Agent session は明示 profile の policy を `thread/start` に送り、`inherit_codex` だけ global
  policy を再検査する。
- repository 全体の `just check` が合格する。
