# 現行 Codex の command approval kind 対応

## 確認した契約

2026-09-05、ChatGPT.app 同梱の `codex-cli 0.153.1` が生成する experimental schema で、
`CommandExecutionRequestApprovalParams.kind` の追加を確認した。この version は検証記録であり、
実装の許可リストや executable 固定には使わない。

| 項目 | 生成 schema の意味 | moco の扱い |
| --- | --- | --- |
| `kind: command` | コマンド実行の承認 | 実際の command、cwd、reason を既存 Reviewer に提示 |
| `kind: writeStdin` | 既存端末への入力の承認 | 対象端末と入力の効果を説明できないため、公開前に拒否 |
| `kind` の省略 | default は `command` | コマンド承認として扱う |
| 未知の kind・型・default | 既知の契約ではない | profile 生成または request 適応で拒否 |

`kind` は optional、型は `command` と `writeStdin` の string enum で、現行生成物では
`allOf` の単一参照と `default: command` を伴う。残る command approval のトップレベル項目は
既存の family 定義内だった。旧実装は `kind` を未知のメンバーと判断し、command approval
profile を作らず、account 初期化前の broker handler 登録で停止していた。

## 境界での判定

- 既知メンバーの集合へ `kind` を追加するのは modern command approval family だけとする。
- enum 全体を型付き語彙 `CommandApprovalKind` と照合する。任意の string、未知の enum 値、
  nullable、別型、説明できない合成 schema は受け入れない。
- `allOf` の単一参照は、他の制約を持たない wrapper の場合だけ同じ enum として扱う。
- `kind` が schema にある場合、明記された default が `command` と異なれば profile を作らない。
  `$ref` の併記項目と途中の参照先、受け入れる単一 `allOf` 内も解決前に検査し、default が
  参照解決で捨てられて通常コマンドへ誤分類されることを防ぐ。
- payload 全体の既存検証後、`writeStdin` は scope error にする。コマンド風のテキストや cwd が
  あっても通常コマンドの承認へ変換しない。Reviewer の pending 件数も増やさない。
- command・cwd の説明、thread/turn/item の相関、Reviewer による一回限りの
  accept/decline/cancel は既存契約を維持する。自動承認は加えない。
- additional permissions、別 environment、network context、policy amendment は従来どおり
  absent/null のみ許可する。file approval の path・操作説明にも変更はない。

## 検証

隔離 worktree の専用環境 `/tmp/moco-current-codex-venv` を使い、managed Python 3.13 と
`just` の既存コマンドで検証する。

- 修正前の schema/approval suite: 1017 passed、2 skipped。
- RED: 現行 CLI の contract suite で既存 adaptability 検査と追加した production broker 登録検査が
  失敗し、`this Codex build advertises an approval moco cannot read` を再現した。
- RED: 直接 enum と `allOf` 参照、required/optional の4 fixture が profile 欠如で失敗した。
  省略/command の review、および non-command を公開しない broker 検査も profile 欠如で失敗した。
- GREEN: 現行 CLI の生成物を変更せず、`just contract-codex` の7件が成功した。production の
  `InteractionBroker` と `CodexConnectionSupervisor` 間の handler 登録も含む。
- default の検証は inline、参照の併記項目、`allOf` 内の参照・inline、参照先 enum、途中の
  参照の6箇所を対象とする。`command` は受理し、`writeStdin`、未知値、null は拒否する24件で、
  wrapper の default を見逃す12件の RED → GREEN を確認した。
- 最終 `just check` は formatting、lint、mypy、dead-code、dependencies、AST 検査、
  Python suite、frontend 220件、browser 15件、secret scan、sdist/wheel build まで成功した。
  Python は2794 passed、既存の4件を skip、8件を deselect、総合 coverage は90.34%だった。
- default wrapper 修正後の schema/approval suite は1064 passed、2 skippedだった。
- 最終変更後の `just contract-codex` も7件成功し、現行 CLI の approval family と broker 登録を
  再確認した。

承認境界の確認には [OWASP Top 10:2025](https://owasp.org/Top10/) を参照した（2026-09-05確認）。
新しい入力値は schema → adapter → broker の順で検査し、未知値や説明不能な action が
Reviewer 公開、許可応答、ログへの payload 出力へ進まないことを確認する。

この変更のローカル契約検証は account、通話、稼働サービスを操作しない。実通話からの作業委譲、
切断、callback は moco-mobile 側との統合検証で確認する。

## 2026-09-06 引き継ぎ検証

- `codex/current-codex-api` を `main` の `4d79bbf` までfast-forwardした。
  既存の未コミット差分は取り込み前後のSHA-256一致で保持を確認した。
- RED: 修正を含まない同じ `main` では `just contract-codex` が5件成功・1件失敗となり、
  command approval familyの不適合を再現した。`just doctor` でもAgent admission、
  local review、server requestsが `approval_family_unadaptable` になった。
- GREEN: 修正側の `just contract-codex` は7件成功し、`just doctor` は全18項目成功した。
  doctorでは既存アカウントの認証状態とIrodoriのcapabilityを確認した。音声合成は行っていない。
- 最新UIを含む修正側の `just check` はexit 0。Pythonは2794 passed、4 skipped、8 deselected、
  coverage 90.34%。JavaScriptは229件、browserは36件成功し、静的検査・secret scan・buildも成功した。
  既存mainのPython環境を `UV_PROJECT_ENVIRONMENT` で指定し、`UV_NO_SYNC=1` と
  修正側 `src` の `PYTHONPATH` を使って、依存の再インストールを避けて検証した。
- コードと承認境界をローカルでレビューし、追加の修正事項は認めなかった。
  schemaの語彙・default検証、adapterでの非command拒否、Reviewer公開前の検証順序を確認した。
  [OWASP Top 10:2025](https://owasp.org/Top10/) が現行版であることを同日再確認した。
- この引き継ぎではcommit・公開・稼働サービスへの反映を行っていない。
  iPhone 16eの接続は確認したが、この修正版を使う実通話・委譲・折り返しは未検証。

## 2026-09-06 PR前確認

引き継ぎ後、利用者から続行の指示を受け、commit・push・ドラフトPR作成へ進めた。
別インスタンスのコードレビューはAPPROVE、セキュリティレビューはACCEPTABLEだった。
コードレビュアーはschema/approval suiteを独立実行し、1064 passed・2 skippedを確認した。
両レビュアーは全品質チェック・契約テスト・doctorを再実行せず、上記の親タスクの確認結果を
証跡として扱った。前ターンの一時ログはPR前確認時には残っていなかった。

レビュー後のsimplifyでは、対象差分の分岐・重複・default検証の責務・テスト境界を確認し、
挙動を維持したまま明確に改善できる整理は不要と判断した。追加のソース変更はない。
依存変更はなく、依存脆弱性監査、実通話、稼働反映はこのレビューの確認範囲に含めない。
