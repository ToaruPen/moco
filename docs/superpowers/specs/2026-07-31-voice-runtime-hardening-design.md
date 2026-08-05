# moco 音声ランタイム安定化設計

## 位置づけ

本設計は、`2026-07-30-moco-first-usable-release-design.md` のブラウザ認証、
音声入力制御、Irodori 接続に関する記述を具体化し、矛盾する箇所を置き換える。
長期記憶は引き続き Issue #1 の対象であり、本設計には含めない。
Realtime セッションを明示的に破棄して新規開始する操作は Issue #2 の対象とする。

## 目的

次の実機不具合を、個別の応急処置ではなく責務境界の修正として解消する。

- capability を URL から消した後にページを再読込すると WebSocket 認証に失敗する。
- push-to-talk 操作では ChatGPT Voice の常時入力と自然なターンテイキングを活かせない。
- Irodori 障害がブラウザに通知されず、音声が出ない理由が分からない。
- 音声合成が設定時間を超えると、正常な GPU 推論でもクライアントが中断する。
- 実行中に Irodori の話者モデルを選び直せない。
- Windows 上の旧 Irodori サーバーと infra API が同じポートを所有できる。

## 採用する構成

### ブラウザ capability

capability は最初の URL fragment から読み取り、同じタブの `sessionStorage` にだけ保持する。
読み取り後は従来どおり URL から fragment を除去する。ページ再読込時は
`sessionStorage` から復元する。永続ストレージ、ログ、HTTP path、query string には
保存しない。

### 常時音声入力

内部制御を `LISTEN_START` と `LISTEN_STOP` にする。F1/F2 は設定例と機能テストの
既定キーにすぎず、機能そのものにはしない。

- `LISTEN_START`: Realtime セッションを維持したままマイクトラックを有効にする。
  キーを離しても有効のままにし、重複 key-down は無視する。
- `LISTEN_STOP`: マイクトラックだけを無効にする。現在の turn、会話コンテキスト、
  Irodori の読み上げは中止しない。
- マイク有効中のターン終端と割り込みは GPT-Live の VAD と自然なターンテイキングに
  任せ、停止指示文を会話へ追加しない。
- 新しいユーザー発話が通知された時は、再生中または合成中の古い Irodori 音声を
  generation 単位で無効化する。会話自体はキャンセルしない。

Realtime セッションを破棄して新しく始める明示操作は、入力停止と混同せず Issue #2
で別の型付き契約と UI として追加する。

### Irodori タイムアウト

Irodori クライアントを用途別に分ける。

- health client: `irodori.timeout_seconds` を使用し、起動確認を有限時間で失敗させる。
- synthesis client: HTTPX の `timeout=None` を使用し、音声生成の期限を設けない。

新しいユーザー発話または会話終了時はローカル await を中止し、generation を無効化する。
Windows GPU 上で推論が継続しても、古い WAV は再生しない。

### 音声エラーの表示

`SpeechQueue` は Irodori の安定したエラーコードを接続オーナーへ通知する。
ブラウザには既存の `{"type":"error","code":"..."}` だけを送り、例外文、入力文、
接続先、音声内容は送らない。

### 話者モデルの選択

Irodori の基盤モデルは Windows サービス起動時に一つだけロードする。moco から
実行中に切り替えられる「音声モデル」は、公開合成契約の `speaker`、すなわち
話者埋め込みとする。

- `irodori.speaker`: 起動時の選択。`null` はナレーター。
- `irodori.speakers`: ブラウザに表示する portable speaker 名。
- ブラウザは `select_voice` を送り、サーバーは設定候補にない値を拒否する。
- 選択は次の合成から反映し、会話や WebSocket の再作成を要求しない。

### Windows Irodori の所有権

`irodori-tts-infra` を moco 用 API の唯一の正式所有者とする。

- 内部 listen: `127.0.0.1:8924`
- Windows Scheduled Task: `IrodoriTTSInfra`
- 起動スクリプト: infra の `.runtime-venv` と `.env` を使用する。
- プロセス終了時は上限付きバックオフで再起動する。
- Task state、listener owner、`/health`、moco の安定した境界コードで観測する。

旧 `Irodori-TTS/remote_server.py` のタスクは削除せず無効化して、復旧可能な状態で隔離する。
8923 番を必要とする旧用途が復活しても infra と競合しない。

### Tailscale の公開境界

単一 GPU ホストである現段階では、ノード固有の Tailscale Serve を採用する。

```text
https://<windows-node>.<tailnet>.ts.net/
  → Tailscale Serve
  → http://127.0.0.1:8924/
  → irodori-tts-infra
```

moco はポート番号を含まない HTTPS の MagicDNS URL を正規の識別子とする。Serve
設定は tailscaled が永続化し、外部から Windows の listen port へ直接接続させない。

macOS の OS resolver が MagicDNS を解決できない場合に限り、`connect_ip` で
tailnet 内の接続アドレスを明示できる。この場合も HTTP Host、TLS SNI、証明書検証は
`base_url` の FQDN を使う。TLS 検証の無効化や HTTP への降格は行わず、
`doctor` は `irodori_route: address_override_active` を表示する。

Tailscale Services の `svc:irodori` は複数 GPU ホストやホスト交換が必要になった時に
導入する。現時点で導入すると Windows ノードの tag 化、service-host 承認、ACL 更新が
必要になり、単一ホスト構成には過剰である。

## 起動と readiness

正常起動は次の全条件で判定する。

1. Scheduled Task が実行中である。
2. `127.0.0.1:8924` の所有プロセスが infra の uvicorn である。
3. Tailscale Serve が HTTPS 443 をその origin へ転送している。
4. `/health` が Irodori の JSON 契約を返す。
5. `model_loaded=true` である。
6. 実合成が完全な RIFF/WAVE を返す。

TCP 接続成功や任意の HTTP 200 だけでは ready とみなさない。

## テスト

- JavaScript: capability の初回取得、URL 消去、同一タブ再読込での復元。
- Hotkey/Web: key-down の `LISTEN_START` と `LISTEN_STOP` だけを送り、key-up では
  入力を止めない。
- Web: `LISTEN_STOP` は入力だけを止め、turn、読み上げ、会話セッションを維持する。
- Web: 常時入力中の新しいユーザー発話で、古い Irodori 音声だけを無効化する。
- Irodori: health client は有限 timeout、synthesis client は `None`。
- Irodori transport: connect address を上書きしても Host と SNI は FQDN を維持する。
- Web: 設定候補だけを選択でき、会話中の変更が次の合成へ反映される。
- Speech queue: 合成エラーコードが callback へ通知される。
- 通常ゲート: `just check`。
- 実機: Tailscale HTTPS 経由の health、合成、ブラウザ WAV 再生、F1/F2 の
  常時入力開始・停止機能テスト。

## 対象外

- Cloudflare Tunnel
- 複数 GPU ホスト間の負荷分散
- ネイティブ macOS 音声クライアント
- 長期記憶
- 旧 Irodori サーバーの削除
- Realtime セッションの明示的な破棄と新規開始（Issue #2）
