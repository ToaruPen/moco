# Danger Full Access No Approval Design

## 目的

moco の音声 Agent が、利用者の承認操作を待たずに、各ホストで moco を実行するユーザーアカウントの権限範囲でファイル変更、コマンド実行、ネットワークアクセスを行えるようにする。

この設定は OpenAI Docs が「sandbox なし・approval なし」と説明する高リスク構成である。利用者がローカル設定で明示的に選択した場合だけ有効にする。

参考: [OpenAI Docs — Agent approvals & security](https://learn.chatgpt.com/docs/agent-approvals-security)

## 決定

`agent.profile` に `danger_full_access_no_approval` を追加する。この profile は Codex の `thread/start` に次を明示する。

- `sandbox: danger-full-access`
- `approvalPolicy: never`

Realtime 会話用 session と delegated Agent session の両方が同じ組み合わせを送る。既定 profile は `read_only` のまま変更しない。

## 選択肢と判断理由

### 採用: 専用の明示 profile

設定名だけで sandbox と approval の両方が無効だと判別できる。global Codex 設定の変更に追従せず、moco に全アクセスを与えた事実が owner-private な設定ファイルへ残る。

### 不採用: `inherit_codex` の安全拒否を解除

global Codex 設定が後から変更されたとき、moco の権限も暗黙に変わる。現在の `danger-full-access + never` を継承可能にすると、既存の admission safety ceiling の意味も弱くなるため不採用とする。

### 不採用: `workspace_write` の意味を変更

profile 名と実際の権限が一致せず、既存利用者の境界を黙って変更するため不採用とする。

## profile 契約

| profile | sandbox | approval policy |
| --- | --- | --- |
| `read_only` | `read-only` | `never` |
| `workspace_write` | `workspace-write` | `on-request` |
| `danger_full_access_no_approval` | `danger-full-access` | `never` |
| `inherit_codex` | 明示しない | 明示しない |

`danger_full_access_no_approval` はローカル設定で権限を明示した explicit profile として扱う。global effective policy を admission 条件には使わない。

`inherit_codex` は従来どおり global effective policy を検査し、policy が不明な場合と `danger-full-access + never` の場合に turn 開始を拒否する。専用 profile の追加によって、この安全拒否を解除しない。

## 設定境界

profile の変更は owner-private な moco 設定ファイルからだけ受け付ける。公開画面、音声、hotkey、通常の operator WebSocket message からは変更できない。

ローカル設定は次の値を使う。

```yaml
agent:
  profile: danger_full_access_no_approval
```

環境変数へ profile や権限値を固定しない。設定変更は実行中の moco プロセスを終了して再起動した後に有効になる。

## 実行時の権限

この profile の Codex command/file 操作には sandbox と approval prompt がない。各ホストで moco を実行するユーザーアカウントがアクセスできる範囲で、次の操作が可能になる。

- moco の作業ディレクトリ外を含むファイルの読取り、作成、変更、移動、削除
- ローカル資格情報、設定、会話データ、ソースコードの読取り
- 任意コマンド、子プロセス、ネットワーククライアントの実行
- 公開・ローカルネットワークへの送信
- launchd、Windows の自動起動設定、shell 設定など、実行ユーザーが変更できる永続設定の変更

管理者・root 権限、macOS TCC と SIP、Windows UAC、別ユーザーの権限、外部サービスの認可は別の境界であり、この profile だけでは迂回できない。ただし、moco プロセスへ既に与えられた権限と資格情報は利用可能になる。

## 信頼境界とリスク

公開 operator endpoint は Cloudflare Access identity だけで利用者を認証する。persistent operator capability は loopback operator endpoint だけを保護する。これらは Codex の実行 sandbox の代わりにはならない。

認証済み iPhone からの音声・テキスト、Codex が読むローカルファイル、web 検索結果、plugin/app/MCP の応答は、Agent の判断へ影響する入力である。誤認識、利用者の指示ミス、prompt injection、operator capability の漏えい、端末またはアカウント侵害が起きると、moco 実行ユーザーのデータ破壊、資格情報窃取、外部送信、永続化につながり得る。

この profile をローカル設定で選ぶ行為を、Codex command/file 操作についての継続的な事前許可として扱う。個別操作の Reviewer 承認は発生しない。

app/MCP/provider/workspace が持つ独自の認可、破壊的操作の安全規則、API 側の拒否は別契約である。moco はそれらを迂回せず、承認なしで必ず成功するとは保証しない。

## データフロー

1. iPhone は Cloudflare Access identity の検証を通過して operator WebSocket へ接続する。
2. Realtime 会話が delegated Agent task を生成する。
3. moco はローカル設定から `danger_full_access_no_approval` を読み取る。
4. moco は `thread/start` へ `danger-full-access` と `never` を明示する。
5. Codex は Reviewer を待たずに command/file 操作を実行する。
6. commentary と final answer は既存経路で iPhone へ返る。

profile の値を WebSocket payload、音声内容、モデル出力から受け取る経路は追加しない。

## 失敗時の動作

- Codex app-server schema が `danger-full-access + never` を受理すると証明できない場合、contract/admission error として turn を開始しない。
- moco 設定に未知の profile がある場合、起動時の strict validation で拒否する。
- OS、TCC、外部サービス、app/MCP policy が操作を拒否した場合、その拒否を表示し、別経路で迂回しない。
- Reviewer が未接続でも、この profile の command/file 操作は approval を要求しない。
- profile の値や資格情報をログ、telemetry、公開 UI へ新たに出力しない。Doctor は選択された profile 名だけを表示する。

## ロールバック

ローカル設定を `workspace_write` または `read_only` へ戻し、moco を再起動する。環境変数や global Codex 設定は変更しないため、ロールバックは moco 設定一か所で完結する。

## テスト

Red-Green-Refactor で次を固定する。

1. 設定 parser が `danger_full_access_no_approval` を受理し、未知値を拒否する。
2. schema discovery が `danger-full-access + never` の exact pair を証明できない app-server を拒否する。
3. Realtime session が `danger-full-access` と `never` を `thread/start` へ送る。
4. delegated Agent session が同じ組み合わせを送る。
5. Doctor は専用 profile を explicit profile として admission し、profile 名を表示する。
6. `inherit_codex` の `danger-full-access + never` 拒否が退行しない。
7. README と example config が高リスク、全アクセス、承認なし、ローカル限定選択、ロールバックを説明する。
8. ローカル設定の切替後、実行中 app-server に対する contract discovery と Doctor が成功する。
9. `just check` が成功する。

## 既存仕様との関係

この設計は `2026-08-07-codex-rich-agent-client-design.md` と `2026-08-17-profile-aware-agent-admission-design.md` の profile 数と explicit profile mapping を更新する。

ローカル設定だけが profile を所有すること、既定 `read_only`、`inherit_codex` の global policy 検査、公開画面から profile を変更できないこと、unknown contract の fail-closed 動作は維持する。
