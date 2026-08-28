# Cloudflare Access 単独で使える公開オペレーター認証設計

## 位置づけ

本設計は、`2026-08-01-mobile-operator-access-design.md` と
`2026-07-31-voice-runtime-hardening-design.md` の公開オペレーター認証を更新する。
公開ホストでは Cloudflare Access と moco capability の二重認証をやめ、検証済みの
Cloudflare Access identity を唯一のオペレーター認証とする。loopback 接続では既存の
moco capability を維持する。

Codex の実行権限、単一オペレーター制約、Irodori 接続、音声処理、Cloudflare Access
policy、Cloudflare Access session duration は変更しない。

## 背景

現在の公開画面は、Cloudflare Access の認証を通過した後も、URL fragment から取得した
moco capability を WebSocket subprotocol に含める必要がある。iPhone が通常の
`https://moco.toarupen.org` を開いた場合、Cloudflare Access session が有効でも
capability がないため `capability_missing` で拒否される。

capability を `localStorage` に保存する既存対応は、QR から一度正しく開いた同じ
browser origin では再利用できる。しかし、QR、URL fragment、保存状態という
Cloudflare Access とは別の認証状態が残り、「Access 認証済みなら公開 URL をいつでも
開いて使える」という期待を満たさない。

## 目的

- Cloudflare Access session が有効な iPhone では、固定公開 URL を直接開いて接続できる。
- 公開 WebSocket は、moco が暗号学的に検証した Cloudflare Access identity だけを許可する。
- Cloudflare Access policy が意図せず広がっても、設定した本人の email 以外を拒否する。
- loopback の管理・オペレーター経路は既存 capability で保護し続ける。
- 公開 URL、QR、公開 origin の browser storage に moco capability を渡さない。
- JWT、email、capability をログ、telemetry、例外 detail へ出さない。

## 対象外

- Cloudflare Access policy や identity provider の自動作成・変更
- Cloudflare Access session duration の変更
- moco 独自アカウント、password、refresh token、session store の追加
- iOS の user activation、microphone permission、audio playback 制約の回避
- 複数オペレーター接続や既存接続の自動奪取
- loopback capability の廃止

## 検討した方式

### 採用: moco が Cloudflare Access JWT を検証する

Cloudflare が origin request に付ける `Cf-Access-Jwt-Assertion` を moco が検証する。
署名だけでなく issuer、audience、有効期限、許可 email を必須にする。公開 request は
capability の有無ではなく、この検証結果で認可する。

Cloudflare edge や `cloudflared` の設定だけに依存せず、moco 自身が公開 request と
認証済み request を区別できる。loopback へ接続できる別 process が Access header を
偽装しても、有効な署名を作れない。

### 不採用: Cloudflare Access と cloudflared だけを信頼する

構成は最も単純になるが、moco は tunnel から転送された request と、loopback で
header を偽装した request を独立に判定できない。moco は Codex の強い実行権限へ到達する
境界なので、origin 側でも fail closed に検証する。

### 不採用: Access 認証後に moco capability を自動配布する

公開 URL を直接開く要件は満たせるが、JWT 検証に加えて capability 発行、保存、更新、
失効が残る。単一利用者の公開オペレーター認証としては重複状態と失敗点を増やすだけで、
追加の認可境界を提供しない。

## 設定契約

`server.public_url` を設定する場合、次の strict configuration を必須にする。

```yaml
server:
  public_url: https://moco.toarupen.org
  cloudflare_access:
    team_domain: https://<team-name>.cloudflareaccess.com
    audience: <application-audience-tag>
    allowed_email: <operator-email-address>
```

- `team_domain` は HTTPS の `*.cloudflareaccess.com` origin だけを受け入れ、credentials、
  port、path、query、fragment を拒否する。
- `audience` は空文字、空白、制御文字、過大な値を拒否する。
- `allowed_email` は単一の email address とし、比較時だけ Unicode case folding する。
- これらは秘密値ではないが、doctor や通常ログへ値そのものを表示しない。
- 値は moco YAML から注入する。process 環境変数の固定値や test 専用 fallback を
  source of truth にしない。
- `server.public_url` なしで `cloudflare_access` がある構成と、`public_url` があるのに
  `cloudflare_access` がない構成は起動時に拒否する。
- unknown key は既存方針どおり拒否する。

`cloudflared` 側でも対象 public hostname に `access.required: true`、同じ team name、
同じ application audience を設定する。moco の検証はこの edge/origin-proxy 検証を
置き換えず、追加の origin authorization として働く。

## 認証フロー

### 公開 HTTP / WebSocket

1. iPhone Safari が固定公開 URL へ HTTPS request を送る。
2. Cloudflare Access が `CF_Authorization` cookie を検査する。session が失効していれば
   identity provider の login へ遷移する。
3. Cloudflare は認証済み request に `Cf-Access-Jwt-Assertion` を付ける。
4. `cloudflared` は Access JWT 必須設定を検査して loopback origin へ転送する。
5. moco は公開 Host / Origin を既存の完全一致規則で判定し、JWT を検証する。
6. JWT が有効で email が `allowed_email` と一致する場合だけ operator page と
   WebSocket upgrade を許可する。
7. Browser は moco capability なしで WebSocket を開始する。

公開 request では capability を認証 fallback に使わない。有効な capability が付いていても
Access JWT が欠落・不正なら拒否する。これにより古い QR や browser storage に残った
capability は公開認証を迂回できない。

### loopback HTTP / WebSocket

`127.0.0.1` / `::1` の既存 Host / Origin 規則と capability 検証を維持する。
Cloudflare Access JWT は要求せず、JWT header があっても loopback 認証の代わりにしない。
`moco open` が capability 付き loopback fragment を開く挙動も維持する。

## JWT 検証

moco は JWT 暗号処理を独自実装せず、検証実績のある library を使う。検証器は次をすべて
満たした場合だけ identity を返す。

- header が一つだけ存在し、bounded size 内の JWT である。
- JWT の署名が `team_domain` の `/cdn-cgi/access/certs` にある公開鍵で検証できる。
- `iss` が設定した `team_domain` と完全一致する。
- `aud` が設定した application audience を含む。
- `exp` が存在して現在時刻に対して有効であり、`iat` / `nbf` が存在する場合も
  現在時刻に対して有効である。
- `email` が bounded non-empty string で、`allowed_email` と一致する。
- 許可していない algorithm、未知 key、malformed claim、重複 header は拒否する。

JWKS は process memory だけに bounded TTL で cache する。未知の `kid` を受けた場合は一度だけ
bounded timeout で再取得し、それでも検証できなければ拒否する。JWKS の取得失敗や期限切れ
cache しかない状態では public access を fail closed にする。JWT と検証済み identity は
disk、browser storage、runtime state に保存しない。

## Browser と pairing の変更

- 公開ページは URL fragment や `localStorage` から moco capability を読み込まない。
- 公開 origin に残る旧 `moco.capability` storage は起動時に削除する。
- 公開 WebSocket は capability subprotocol を送らない。
- loopback ページだけが capability を読み、既存 subprotocol を送る。
- runtime state の `mobile_url` は capability fragment のない `server.public_url` とする。
- 「スマホ接続」QR を維持する場合、その内容は秘密を含まない固定公開 URL だけにする。
  QR は初回入力を省く便宜機能であり、認証手段ではない。

Cloudflare Access session duration が一か月なら、その期間中は通常 login を求められない。
session の失効、明示 logout、policy 変更、cookie 削除後は Cloudflare の login が再度必要になる。
moco は Access session を延長・更新しない。

iOS の制約により、operator 接続開始は引き続き user gesture を必要とする。microphone
permission が保持されていなければ Safari が再度確認する。公開 URL を開いただけで
microphone や speaker を自動起動することは成功条件に含めない。

## エラーと観測

外部表示と telemetry には bounded stable code だけを使う。

- `access_token_missing`: 公開 request に Access assertion がない。
- `access_token_invalid`: 署名、issuer、audience、時刻 claim、形式の検証に失敗した。
- `access_identity_mismatch`: 検証済み email が許可 identity と一致しない。
- `access_keys_unavailable`: JWKS を安全に取得・更新できない。
- 既存の `origin_rejected`、`single_operator_only`、音声系 error code は維持する。

ログには request path、JWT、claim、email、audience、team domain、capability を含めない。
認証失敗を `websocket_failed` だけに潰さず、browser へ公開可能な stable code を返せる場合は
その code を表示する。WebSocket upgrade 前に HTTP 拒否となり browser API が detail を
取得できない場合は、別の同一 origin 認証状態 endpoint から bounded code を取得するか、
`access_auth_failed` に正規化する。

## 移行

1. moco の設定へ Cloudflare Access team domain、application audience、allowed email を追加する。
2. `cloudflared` の対象 ingress に Access JWT 必須検証を設定する。
3. moco を更新して JWT 検証を有効にする。
4. iPhone の公開 origin に残る旧 capability を browser code が削除する。
5. capability fragment なしの固定公開 URL で HTTP、WebSocket、音声接続を実機確認する。

設定不足のまま公開認証を capability-less に切り替える中間状態は作らない。新設定と検証器が
ready になるまで、現在の公開 WebSocket capability gate を維持する。rollback 時は moco と
`cloudflared` の設定を同時に旧二重認証へ戻す。

## テスト

### Configuration / unit

- `public_url` と `cloudflare_access` の組み合わせを strict に検証する。
- team domain、audience、email の正常値と malformed / oversized 値を検査する。
- test は設定 object と fake clock / fake JWKS client を注入し、固定 process 環境変数に依存しない。
- 正しい署名、issuer、audience、時刻、email の JWT だけを受理する。
- missing header、誤署名、未知 key、別 issuer、別 audience、expired / future-issued /
  not-yet-valid token、
  email 不一致、malformed claim、JWKS failure を拒否する。
- JWKS cache、未知 `kid` の一回 refresh、timeout、fail-closed を検査する。
- token と identity が log、exception、telemetry に出ないことを検査する。

### Web / integration

- 公開 HTTP と WebSocket は有効な Access JWT だけで接続でき、capability を要求しない。
- 公開 request は capability が正しくても JWT がなければ拒否する。
- loopback WebSocket は capability を引き続き要求し、JWT だけでは接続できない。
- 公開・loopback の Host / Origin 交差、forwarded header 偽装を拒否する。
- 公開 browser は capability subprotocol を送らず、旧 local storage を削除する。
- loopback browser は capability を保持して送る。
- mobile URL と QR に capability、JWT、email が含まれない。
- 単一オペレーター制約、接続、入力開始・停止、Irodori 再生の既存 test を維持する。

### Live

- iPhone Safari で Cloudflare Access に login 後、固定公開 URL を直接開いて接続する。
- page reload、新しい tab、Safari 再起動後も Access session 有効中は QR なしで接続する。
- Access logout 後は拒否または login redirect になり、moco WebSocket へ到達しない。
- 別 identity、期限切れ session、Tunnel 停止、moco 停止を区別して確認する。
- 実機 iPhone で microphone permission、音声入力、Irodori 音声再生を確認する。
- 通常 gate として `just check` を通す。

## 成功条件

- 認証済みの本人は `https://moco.toarupen.org` を直接開き、QR なしで operator 接続できる。
- 公開 WebSocket に `capability_missing` が発生しない。
- 本人以外、invalid Access JWT、Access JWT のない公開 request は接続できない。
- loopback capability、Origin / Host 検証、単一オペレーター制約は弱まらない。
- moco capability、Access JWT、email が公開 URL、公開 origin の browser storage、log、
  telemetry に残らない。
