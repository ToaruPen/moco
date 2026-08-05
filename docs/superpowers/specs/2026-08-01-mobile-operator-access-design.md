# moco スマートフォン対応と公開境界の設計

## 位置づけ

本設計は、`2026-07-30-moco-first-usable-release-design.md` の「モバイル UI を
対象外とする」という記述と、`2026-07-31-voice-runtime-hardening-design.md` の
「Cloudflare Tunnel を対象外とする」という記述を、オペレーター画面に限って
置き換える。

Irodori の GPU ホストへの接続境界は変更しない。moco から Irodori への経路は既存の
Tailscale HTTPS を維持し、スマートフォンや Cloudflare から Irodori を直接公開しない。
長期記憶と Realtime セッションの明示的な破棄・再生成も引き続き対象外とする。

## 目的

macOS 上で動く moco を、同じ LAN や特定 VPN クライアントの有無に依存せず、
認証済みのスマートフォンから安全に操作できるようにする。スマートフォンでも次の
既存機能を失わないことを重視する。

- Realtime セッションへの接続と継続的な音声入力
- 音声入力の明示的な開始と停止
- Irodori 話者モデルの選択
- 会話、処理状況、reasoning summary、エラーの観測
- Irodori 音声の再生
- エラーを成功状態で覆い隠さない振る舞い

## 成功条件

- moco の HTTP サーバーは引き続き loopback だけに bind する。
- 固定 HTTPS ホスト名を通じ、iOS と Android の一般的なブラウザから接続できる。
- Cloudflare Access の対話認証を通過しなければオペレーター画面へ到達できない。
- WebSocket は公開 HTTPS と同じホストの WSS を使い、未知 Origin を拒否する。
- capability は URL fragment 以外の公開 URL、ログ、永続ストレージへ出ない。
- スマートフォンへ capability 付き URL を手入力せず、安全な QR で引き渡せる。
- 320px 以上の表示幅で横スクロールせず、主要操作は片手で到達できる。
- iOS のユーザー操作・音声再生制約下でも、接続後のマイク入力と読み上げが機能する。
- デスクトップとスマートフォンの同時操作を暗黙に許可せず、既存の単一オペレーター制約を維持する。

## 検討した方式

### 採用: Cloudflare Tunnel と Cloudflare Access

名前付き Tunnel の固定ホスト名を Cloudflare Access の self-hosted application として
保護し、`cloudflared` から loopback の moco へ転送する。スマートフォン側に VPN
クライアントを要求せず、HTTPS 証明書と WSS を同じ公開ホスト名で扱える。

### 不採用: Tailscale をスマートフォンにも導入

private network としては単純だが、スマートフォンごとのアプリ導入、ログイン、端末登録、
tailnet 管理が必要になる。利用環境へ依存しないという今回の要件に合わない。

### 不採用: LAN 内 HTTPS 公開

Cloudflare への依存は減るが、外出先から使えず、ローカル証明書とホスト名解決を端末ごとに
管理する必要がある。moco 自身を LAN interface へ bind する必要も生じ、公開面が広がる。

## 採用するネットワーク構成

```text
スマートフォンのブラウザ
  → HTTPS / WSS
  → Cloudflare Access
  → 名前付き Cloudflare Tunnel
  → cloudflared on macOS
  → http://127.0.0.1:8765
  → moco operator server

moco
  → 既存の Tailscale HTTPS
  → Irodori GPU host
```

### loopback 所有権

moco の `server.host` は loopback 制約を維持する。Cloudflare 用に `0.0.0.0`、LAN IP、
Unix socket 以外の新しい listen endpoint は追加しない。`cloudflared` だけが
`http://127.0.0.1:<server.port>` を origin として参照する。

### 固定公開 URL

strict 設定へ任意の `server.public_url` を追加する。受け入れる値は次の全条件を満たす
URL に限定する。

- scheme は `https`
- host は DNS 名であり、IP address や wildcard ではない
- username、password、明示 port、path、query、fragment を含まない
- 正規化後は `https://<fqdn>` の形になる

設定されていない場合は従来どおり loopback 専用で動作し、Cloudflare を自動推測しない。
設定されている場合、ランタイムのスマートフォン用 URL は
`<public_url>/#<capability>` とする。capability はターミナルへ表示しない。

### Origin と Host の検証

WebSocket upgrade は以下の組み合わせだけを許可する。

- loopback Origin と同一の loopback Host
- `server.public_url` と完全一致する Origin と Host

Origin の欠落、scheme の降格、別 port、別 subdomain、forwarded header による推測、
Origin と Host の交差は拒否する。`Access-Control-Allow-Origin: *` や suffix match は使わない。
HTTP ページ自体に capability を要求する方式へ変更せず、WebSocket の subprotocol で
既存 capability を検証する。

### Cloudflare Access と Tunnel

本番経路には Quick Tunnel ではなく名前付き Tunnel を使う。Cloudflare dashboard で
固定 hostname の self-hosted application を作成し、利用者本人を許可する Access policy
を必須にする。Tunnel ingress の末尾は catch-all の 404 とし、moco 用 hostname だけを
loopback origin へ転送する。

Tunnel token、証明書、Access policy の秘密情報はリポジトリや moco 設定へ保存しない。
`cloudflared` 自身のユーザー設定と macOS service が所有する。moco は Access を迂回する
fallback hostname や、認証失敗を loopback 成功へ置き換える経路を提供しない。

## スマートフォンへの接続 URL 引き渡し

`moco open` で開く loopback 画面に、`server.public_url` が設定されている場合だけ
「スマホ接続」を表示する。操作すると、公開 URL と現在プロセスの capability を含む
QR をその場で表示する。

QR はサーバー側で SVG としてメモリ生成し、loopback 画面の JavaScript が同一 origin の
fetch で取得する。fetch には既存 capability を専用 header として付け、URL、query、cookie
には含めない。次を必須とする。

- QR endpoint は loopback Host と正しい capability header の組み合わせだけを許可する。
- `Sec-Fetch-Site` が送られた場合は `same-origin` だけを許可し、CORS は有効化しない。
- response に `Cache-Control: no-store` と `Pragma: no-cache` を付ける。
- QR の URL、capability、SVG 内容を telemetry や通常ログへ出さない。
- QR を DOM やファイルへ永続化しない。
- 公開ホストから同 endpoint へ到達した場合は QR の代わりに明示的な拒否を返す。
- capability は daemon 再起動で従来どおり更新され、古い QR は無効になる。

QR 生成には、ネットワークへ送信しない小さなローカルライブラリを使う。CDN の JavaScript、
外部 QR API、画像解析サービスは使わない。QR を閉じると URL を含む DOM node を削除する。

## モバイル UI

既存のオペレーターコンソールを別画面へ複製せず、同じ DOM とイベント契約を responsive
layout で使う。

### 表示幅と配置

- viewport に `viewport-fit=cover` を指定する。
- 820px 以下では既存どおり会話とアクティビティを縦積みにする。
- 520px 以下では、音声入力の開始・停止を画面下部の操作領域へ固定する。
- 下部操作領域には `env(safe-area-inset-bottom)` の余白を加える。
- 開始・停止の tap target は少なくとも 44 × 44px とする。
- F1/F2 など物理キー名は狭い画面では表示せず、機能名の「入力開始」「入力停止」を使う。
- 会話、アクティビティ、現在のエラー、処理継続時間をボタンより上で常に確認できる。
- 320px、390px、430px で横スクロールを発生させない。

下部操作領域は状態表示を兼ねた巨大ボタンにはしない。現在状態は上部の compact status に
残し、開始・停止は独立した操作とする。テーマと話者選択は全幅 sheet または native select
として開き、背後のログを不必要に覆わない。

### 接続と単一オペレーター

Realtime 接続は引き続き明示ボタンで開始する。デスクトップが接続中にスマートフォンが
接続した場合、暗黙に既存接続を奪わず `single_operator_only` を表示する。どちらを優先するかを
自動判定しない。

スマートフォンでは物理ホットキーを前提にせず、touch の開始・停止を既存の
`listen_start` / `listen_stop` 契約へ直接対応させる。停止はマイク track だけを無効化し、
Realtime session、実行中 turn、合成、再生を止めない。

## iOS / Android の音声制約

接続ボタンの同期的な user activation 内で、AudioContext の作成または resume を
microphone permission や SDP 交換より先に開始する。最初の `await` 後に初めて音声出力を
有効化する構成を避ける。

接続処理は次の順序を守る。

1. user activation 内で AudioContext を作成または resume する。
2. microphone permission を要求する。
3. Realtime peer connection と WebSocket を確立する。
4. 入力開始操作で microphone track を enable にする。
5. Irodori WAV は有効化済み AudioContext から再生する。

AudioContext の resume、microphone permission、WSS、Realtime のどこで失敗したかを安定した
error code として表示し、接続済みや再生成功へ fallback しない。音声合成には既存方針どおり
timeout を設けない。画面消灯防止の Wake Lock、PWA install、Service Worker、background audio
は初回対応に含めない。

## プライバシーと失敗時の扱い

- Cloudflare の request log に capability が現れないよう、capability は fragment にだけ置く。
- capability、transcript、reasoning summary、音声、QR をサーバーの通常ログへ含めない。
- Cloudflare Access や Tunnel の失敗を、ローカル接続成功で隠さない。
- Tunnel 未接続、Access 拒否、WSS handshake 失敗、origin 不一致を別の診断結果として扱う。
- `doctor` は設定の構文、loopback origin、公開 URL、ローカル cloudflared service の状態を
  検査するが、Access policy の内容を推測しない。
- 公開 hostname への live probe は明示した診断でのみ実行し、capability を送らない。

## 設定と運用

設定例には `server.public_url` と、Cloudflare dashboard / `cloudflared` に必要な非秘密の
手順だけを記載する。Tunnel credentials やアカウント固有 ID の記入欄は作らない。

ポート番号は moco の設定を唯一の source of truth とする。Cloudflare ingress は同じ
`server.port` を origin に使い、moco が空きポートへ自動移動する仕組みは追加しない。
固定 port の所有者衝突は service install / doctor で明示し、別番号への黙った fallback を
行わない。

macOS 起動時は moco と名前付き `cloudflared` tunnel の各 user service が独立して起動する。
Tunnel の LaunchAgent label は `dev.toarupen.moco-cloudflared` に固定し、doctor は別用途の
cloudflared process を moco 用 Tunnel の成功として扱わない。片方だけが ready な場合、
doctor と画面は部分成功をそのまま表示する。

## テスト

### Python

- `server.public_url` が正しい HTTPS FQDN だけを受け入れる。
- credentials、IP、wildcard、port、path、query、fragment、HTTP を拒否する。
- loopback と設定済み公開 URL の正しい Origin / Host 組み合わせだけを許可する。
- Origin 欠落、cross-origin、似た subdomain、forwarded header の偽装を拒否する。
- runtime state のローカル URL とスマートフォン用公開 URLを capability を出力せず扱う。
- QR endpoint は loopback Host、capability header、Fetch Metadata を検証し、no-store header を持つ。
- QR endpoint は公開 Host、capability 欠落、cross-site fetch では拒否する。
- QR と capability が telemetry、例外、CLI output に含まれない。
- `doctor` が public URL、loopback origin、cloudflared の不在・停止・ready を区別する。

### JavaScript / DOM

- touch の開始・停止が物理 hotkey と同じ typed message を送る。
- AudioContext の resume が最初の permission await より前に開始される。
- resume、permission、WSS、Realtime の失敗が個別に表示される。
- QR dialog を閉じると capability を含む node が DOM から除去される。
- 狭い画面では物理キー名を非表示にし、機能名と accessible name を維持する。
- theme、voice、conversation、activity、current error が mobile layout でも利用できる。

### 実機相当と実機

- Playwright の 320 × 568、390 × 844、430 × 932 viewport で横 overflow と重なりを検査する。
- touch 操作で接続、入力開始、入力停止、話者変更、テーマ変更、ログ確認を行う。
- iOS Safari と Android Chrome で microphone permission、WSS、常時入力、Irodori WAV 再生を確認する。
- Access 未認証、Tunnel 停止、moco 停止、単一オペレーター競合をそれぞれ再現する。
- 通常ゲートとして `just check` を通す。

## 対象外

- Irodori GPU host の Cloudflare 公開
- Cloudflare Access の代替となる moco 独自アカウント・パスワード認証
- 複数オペレーターの同時接続や接続奪取
- PWA install、Service Worker、offline cache
- Wake Lock と background audio の保証
- native iOS / Android application
- 長期記憶
- Realtime セッションの明示的な破棄と新規開始
