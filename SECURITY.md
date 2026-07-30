# Security

## 対象

moco はローカルの音声エージェントです。操作サーバーを外部公開する構成、秘密情報を
含む URL、文字起こしや音声の永続化はサポートしません。

## 保護境界

- HTTP サーバーは loopback だけに bind します。
- WebSocket は Host と Origin が同じ loopback authority であることを要求します。
- プロセスごとの capability は URL fragment と WebSocket subprotocol で渡し、
  HTTP path、query、通常ログへ出しません。
- runtime state と launchd plist はユーザーだけが読める権限で原子的に書き込みます。
- Irodori と OTLP の URL に埋め込まれた認証情報は設定検証で拒否します。
- 音声、文字起こし、プロンプト、アカウント識別子、capability、記憶内容は
  テレメトリ属性として許可しません。
- Irodori 応答は上限を設け、完全な RIFF/WAVE framing を検証してから再生します。

## 利用者が守ること

設定ファイル、portable speaker、Tailscale の接続情報を公開しないでください。
共有 Mac では `~/Library/Application Support/moco` と `~/Library/Logs/moco` の
所有者と権限を確認してください。`moco open` が runtime state の権限違反を報告した
場合、そのファイルを信用せず、実行中の moco を停止してから再起動してください。

Codex Realtime は experimental API です。ChatGPT.app を更新した後は、
`moco doctor` と前景起動で接続を再確認してください。

## 脆弱性の報告

公開 Issue に秘密情報や再現用 credential を投稿しないでください。GitHub の
Security Advisories から非公開で報告してください。影響範囲、再現条件、確認した
バージョンを含め、トークン、音声、文字起こし、個人情報は削除してください。
