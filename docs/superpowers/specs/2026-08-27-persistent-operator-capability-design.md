# moco オペレーター接続キー永続化設計

## 位置づけ

本設計は、`2026-08-01-mobile-operator-access-design.md` にある「capability は daemon
再起動で更新される」「browser storage は `sessionStorage` に限定する」というライフサイクルを
置き換える。Cloudflare Access、Tunnel、loopback bind、Origin / Host 検証、単一オペレーター制約は
変更しない。

## 目的

本人が一度 QR コードでスマートフォンを登録した後は、次の操作だけでオペレーター接続キーを
失わないようにする。

- moco daemon の再起動
- macOS または Windows の再起動
- ブラウザの終了と再起動
- 同じブラウザプロファイルでの新しいタブ

Cloudflare Access のセッション期間は引き続き Cloudflare が管理する。moco の接続キーは
Cloudflare 認証を置き換えず、公開 hostname に対する独立した defense-in-depth の bearer
credential として維持する。

## 成功条件

- 接続キーを環境変数、YAML 設定、ソースコード、テスト固定値、リポジトリへ保存しない。
- daemon は所有者限定の永続ファイルから同じ接続キーを再利用する。
- QR から登録したブラウザは、同一 origin・同一ブラウザプロファイルで接続キーを再利用する。
- 既存の `sessionStorage` 値を一度だけ永続領域へ移行できる。
- runtime state は引き続きプロセス終了時に削除し、Reviewer control secret は永続化しない。
- 永続ファイルの破損、未知 field、symlink、所有者不一致、緩い権限を黙って修復せず fail closed にする。
- 明示的なオフライン操作で接続キーを失効できる。
- 接続キーを stdout、通常ログ、telemetry、例外本文、Cloudflare の request URL に出さない。

## 検討した方式

### 採用: owner-private ファイルと browser local storage

Mac / Windows では既存の private-state 境界を使う独立ファイルへランダムキーを保存し、
ブラウザでは URL fragment から受け取った値を origin-scoped な `localStorage` へ保存する。
既存の WebSocket subprotocol 検証を変えず、最小の変更で再起動と新規タブを扱える。

### 不採用: daemon 側だけ永続化

サーバー再起動には対応できるが、新しいタブやブラウザ再起動で QR が再度必要になり、今回の
成功条件を満たさない。

### 不採用: Cloudflare Access JWT の直接検証

moco 独自キーをなくせる一方、Cloudflare の署名鍵取得、audience、clock skew、鍵更新、障害時の
扱いを新たな runtime 境界として実装する必要がある。今回必要な UX に対して変更範囲が大きく、
Cloudflare への結合も強くなる。

## daemon 側の保存契約

永続ファイル名は `operator-capability.json` とし、`runtime.json` と同じ private directory に置く。

- macOS: `~/Library/Application Support/moco/operator-capability.json`
- Windows: `%LOCALAPPDATA%/moco/runtime-private/operator-capability.json`

ファイルは versioned JSON とする。

```json
{
  "version": 1,
  "capability": "<43-character base64url token>"
}
```

parser は object shape、既知 field、version、文字種、長さを strict に検証する。値は
`secrets.token_urlsafe(32)` で生成する。書き込みは既存の `write_private_state` を使い、POSIX では
directory `0700` / file `0600`、Windows では現在ユーザーだけを許可する protected ACL、同一
directory の一時ファイル、atomic replace、fsync の契約を再利用する。

起動時の処理は次の順序とする。

1. runtime lease を取得する。
2. 永続ファイルが存在すれば安全性と内容を検証してキーを読む。
3. 存在しなければ新しいキーを生成して永続ファイルへ原子的に保存する。
4. 読み込んだキーで operator app と `runtime.json` の URL を生成する。
5. 終了時は `runtime.json` だけを identity-checked cleanup で削除する。

永続ファイルが存在するのに読めない場合、新しいキーで上書きしてはならない。daemon は安定した
エラーコードで起動を拒否し、秘密値を含まない修復手順を表示する。

Reviewer の `control_secret` は従来どおりプロセスごとに生成し、`runtime.json` とともに削除する。

## ブラウザ側の保存契約

接続キーの優先順位は次のとおりとする。

1. 現在 URL の fragment
2. `localStorage` の `moco.capability`
3. 旧 `sessionStorage` の `moco.capability`
4. 空文字列

URL fragment があれば `localStorage` へ保存し、既存どおり直ちに history から fragment を除去する。
旧 `sessionStorage` だけに値がある場合は `localStorage` へコピーし、同じ値を返す。接続キーは query、
cookie、DOM、console、telemetry へ移さない。ブラウザ保存を消した場合や別プロファイルでは、再度 QR
登録が必要になる。

新しい QR の fragment は保存済み値より常に優先し、キー更新後の再登録で古い値を置き換えられる。
WebSocket は従来どおり `moco.capability.<value>` subprotocol を送り、サーバーは constant-time 比較を
維持する。

## 明示的な失効

`moco operator rotate` を追加する。この操作は runtime lease を非破壊的に取得できる、つまり daemon
停止中の場合だけ実行できる。実行時は owner-private 検証と identity check を通して永続ファイルを
削除する。daemon 稼働中はファイルもプロセス内キーも変更せず、停止後に再実行するよう安定した
エラーを返す。

次回 daemon 起動時に新しいキーを生成する。登録済みブラウザは `capability_mismatch` になり、新しい
QR を読むと `localStorage` が置き換わる。rotate コマンドは旧値、新値、QR URL を出力しない。

## 移行

初回更新時、永続ファイルがなければ新しいキーを生成するため、原則として最後に一度だけ新しい QR
登録が必要になる。ただし同じキーが安全に事前移行された環境では、ブラウザの旧
`sessionStorage` から `localStorage` への移行により再登録なしで継続できる。

既存の `runtime.json` を永続ファイルへ改名してはならない。`runtime.json` にはプロセス限定の
Reviewer control secret と死んだ endpoint 情報が含まれうるため、引き続き ephemeral state として
扱う。

## セキュリティと失敗時の扱い

- Cloudflare Access を通過しても正しい moco capability がなければ WebSocket を拒否する。
- 永続化によりブラウザプロファイル侵害時の capability 生存期間は長くなるが、攻撃者は別途
  Cloudflare Access も通過する必要がある。
- same-origin script は `localStorage` を読めるため、既存 CSP と外部 script 非依存を維持し、今回の
  変更で third-party script を追加しない。
- capability 欠落と不一致は値を記録せず、bounded な安定コードで区別する。
- 不正な永続ファイルを自動削除または自動再生成しない。意図しない認証状態の変更を避ける。
- rotate は daemon と競合した状態で成功扱いにしない。

## テスト

### Python

- 永続ファイルがない初回起動でキーを生成し、次回起動で同じキーを再利用する。
- runtime cleanup 後も永続ファイルだけが残る。
- control secret は起動ごとに変わり、永続ファイルへ入らない。
- version、unknown field、文字種、長さ、JSON、権限、owner、symlink の不正を fail closed にする。
- 書き込み失敗時に既存の有効なファイルを失わない。
- macOS と Windows の default path が既存 private directory に揃う。
- `moco operator rotate` は停止中だけ削除し、稼働中や identity 競合では変更しない。
- CLI output、例外、ログに capability が含まれない。
- 環境変数を実値へ固定せず、`tmp_path` と注入した environment mapping で検証する。

### JavaScript

- URL fragment が `localStorage` へ保存され、history から消える。
- reload と新規タブ相当で `localStorage` の値を再利用する。
- 旧 `sessionStorage` の値を `localStorage` へ移行する。
- fragment が既存値を置き換える。
- 保存値がなければ空 capability を送り、秘密情報を別の永続面へ複製しない。

### 統合と回帰

- daemon を再起動しても同じ QR capability を受け入れる。
- rotate 後は旧 capability を拒否し、新しい QR capability を受け入れる。
- loopback bind、公開 Origin / Host、QR endpoint、単一オペレーター、Reviewer secret の既存テストを
  維持する。
- 完了前に `just check` を通す。

## 対象外

- Cloudflare Access の session duration や policy の変更
- Access JWT の moco 内検証
- 複数ブラウザプロファイル間の同期
- ブラウザ保存を消した端末の自動再登録
- capability の時刻ベース自動更新
- third-party password manager や OS keychain への保存
