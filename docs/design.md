# 設計と障害時の扱い

## 境界

共通のStoreはジョブ状態を扱い、DotsのWebhook配送はEvents、xangi通知はNotifierへ分離しています。外部から利用できるのは既存ジョブの参照・claim・結果提出と購読です。ジョブ作成・通知先指定・キャンセルは親の管理APIに限定します。

Managed Extensionの管理ポートはOS割当の127.0.0.1です。Dotsポートもloopbackですが、公開proxy用に固定できます。公開proxyが間違ってもworker listenerには管理APIがありません。worker tokenと親tokenは別物です。

## 保存と実行

SQLite WALとprocess内lockで変更を直列化します。同じstateを2プロセスで開かないようOSのfile lockも保持します。workspaceと親instance IDでstateを分離し、ファイル作成時はumask 077です。

状態は`queued → running → completed / failed`。queued/runningから`cancelled / expired`にも遷移します。期限切れでclaimを自動解放しないので、外部操作の結果が不明なまま別のエージェントが二重実行することを避けます。自動再実行が必要なら、先に元の実行の副作用を確認して別のrequest_keyで登録します。

期限は登録時から計測し、再起動で延長しません。結果を保存する同じDB transactionで通知outboxを作成します。結果の正誤検証は依頼者が行います。

## 添付ファイル

inputは管理側で取込・SHA-256検証を済ませ、依頼登録時に原子的に紐付けます。outputはclaimしたworkerがrunning中に作成し、全添付のfinish後にジョブをcompletedにします。failedなら未完了uploadを残せますが、親はreadyだけを成果物として扱います。

標準転送のファイル本文は`inbox.sqlite.files/`へmode 600で保存し、SQLiteには情報と保存名だけを記録します。受信・取込・配信・書出しは逐次処理し、全体をメモリに保持しません。一時ファイルへ受信してサイズ・SHA-256を確定し、保存ファイルとディレクトリをfsyncした後にDBの状態をreadyにします。中断時は未完成ファイルをreadyとして公開しません。異常終了がDB更新前に起きると孤立ファイルが残る場合があります。バックアップは停止後にDBとファイルディレクトリを一緒に取得します。既存BLOBは互換読出しに対応し、自動変換はしません。ユーザー指定の名前は表示用で、サーバー上のパスに使いません。パス区切り・制御文字を含む名前を拒否します。MIMEは自己申告のメタデータで、内容の安全性は保証しません。ダウンロードhelperは受取先を親が明示し、ハッシュ検証後に新規ファイルへmode 600で保存します。

標準経路はファイル情報だけをツールで渡し、本体はHTTP転送します。親からは管理APIによるローカルファイル取込・書出し、Dotsへは10分有効のHMAC署名URL、Dotsからは標準fileParamsのOpenAIファイルCDN URLを使います。受信はHTTPS・public IPのみ、IP固定・TLS hostname検証・redirect拒否・512MiB上限・同時取得2件で制限します。CDNの一時URLは保存しません。ローカルパスはworkspaceまたは`/tmp`に限定します。

完成済み添付は上書き不可。標準経路は1ファイル512MiB、1ジョブ8件、全体20GiB（既定値、`max_storage_bytes`で変更可能）を申告サイズで予約し、件数上限も設けます。並行受信中の一時ファイルや異常終了時の孤立ファイルは別にディスクを使用します。受信は最大10分（個別socket timeout 30秒）、同時CDN取得2件です。削除は未添付か終了済みジョブだけ、管理口から行います。古いデータや孤立ファイルの自動削除・完全消去は提供しません。APIの分割転送は小容量用途として残しますが、通常の利用手順は標準方式に統一します。

別ジョブのclaim tokenによる読み書き、inputへのworker追記、未完成添付の読み出し、キャンセル・期限切れ後のworker操作は拒否します。worker共通token自体は依頼箱全体の権限なので、claimは複数ユーザーの認証分離ではありません。ファイル内容を通知文へ埋め込まず、親がjob IDから取得して通常のプラットフォーム添付へ渡します。

## イベント配送

通知payloadはjob IDとqueue IDだけです。本文はtoolで取得します。callbackはHTTPS 443のOpenAI/ChatGPTドメインに限定し、全DNS結果がpublic IPであることを確認、接続先IPを固定しTLS hostname検証を保持します。redirectには追従しません。

購読時のchallengeとイベントはStandard Webhooks形式のHMAC SHA-256署名付きです。有効購読は最大3、期間は最大24時間。配送は同じevent IDで最大5回、指数バックオフ。410/413は再送せず、410は購読を無効化します。2xxはイベント受付の確認であり、ジョブ完了ではありません。終了済みジョブの未送信通知は止めます。

配送の最終失敗、期限切れ購読、キャンセルはhealthやDBで確認できます。配送失敗の自動無限再送はしません。購読開始時はDotsにもlist_jobsを実行させて、既存の未処理依頼を確認します。購読解除と通信中のcallbackの競合では、既に送信中の1件が届く可能性があります。実行前のclaimで終了済みの依頼を拒否します。

## xangiへの完了通知

成功・失敗・キャンセル・期限切れを固定のoriginへ通知します。Dotsが提出した本文はtrigger指示に入れず、job IDで保存結果を読み直すようxangiに依頼します。読み直した本文も外部エージェントの出力として扱い、権限や実行指示として採用しないことを指示します。

通知状態:

- `pending`: 未送信。CLI未設定の場合もここに留まる。
- `sending`: 呼出し前に保存。クラッシュ後は`needs_review`へ移す。
- `accepted / running / completed`: xangiのreceiptに追従。まだ配信成功と断言しない。
- `delivered`: xangiがプラットフォーム配信を確認。
- `failed / interrupted`: xangi側の失敗・中断。
- `needs_review`: CLIエラー、timeout、receipt不明など。既に送信された可能性があるため再送しない。

receipt照会だけは30秒間隔で再試行します。通知送信のexactly-onceは保証しません。曖昧な失敗を自動再送しないことで重複リスクを抑えます。復旧時はxangiのreceiptと実会話を確認し、必要なら保存結果を手動で報告してください。初期版に通知再送APIはありません。

## 運用上の範囲

この初期版はLinuxの単一ユーザー用です。OAuth、タスクごとのworker認証、監査用の詳細操作履歴、古いデータの自動削除は未実装。DBサイズを監視し、機密扱いでバックアップします。健康状態readyはAPI稼働を示すだけで、Dots購読・公開HTTPS・xangi通知の設定成功は別途確認が必要です。

## 開発

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run python -m unittest discover -s tests -v
node scripts/host-smoke.mjs /absolute/path/to/xangi/dist
```

最後のテストは実際のxangi拡張ローダーを隔離プロセス・一時registryで使います。稼働中の設定・Dots・Discordに接続せず、終了時に一時状態を削除します。テスト結果はPRへ記載し、配備時の確認は[使い方](usage.md)に沿って行います。

## 参考元

xangi / xangi-search（Copyright (c) 2026 karaage、MIT）の拡張形式を参考にしています。著作権表示とMIT本文は[LICENSE](../LICENSE)に保持しています。Webhook transportと購読処理は、本開発に先立つ本人依頼のDots接続試験コードを整理したものです。

プロトコル資料: [MCP Events](https://developers.openai.com/plugins/build/mcp-events) / [MCP server](https://developers.openai.com/plugins/build/mcp-server) / [ChatGPT接続](https://developers.openai.com/plugins/deploy/connect-chatgpt) / [OAuth](https://developers.openai.com/plugins/build/auth)。実行時はPython標準ライブラリのみ、開発用ruffはMITでuv.lockに版を固定しています。

標準ファイル入力の仕様: [OpenAI File APIs](https://developers.openai.com/plugins/reference#file-apis)。fileスキーマにはdownload_url/file_id/mime_type/file_nameを宣言し、先頭2項目だけを必須とします。実Dotsで約5MiBのZIP往復を確認しています。

## Dots起点のxangi依頼

`parent_requests`テーブルに依頼・固定受付先・回答・排他的claim・配送状態を保存します。MCPは新規依頼・状態取得・入力アップロード・成果物リンク発行を公開し、claim・回答保存・取消は親の管理口に限定します。設定`request_destination`がない場合はMCPツール自体を公開せず、新規受付と配送を停止します。設定変更は既存依頼の送信先を変更しません。

送信前にsendingを永続化し、再起動時はneeds_reviewへ移します。trigger受付IDを保存し、receiptを30秒間隔で確認します。triggerの終了と依頼の完了を混同せず、親が回答APIへ保存したときだけcompleted/failedにします。回答保存前にtriggerが終わるとneeds_reviewになり、親が調査します。期限切れ・取消は回答保存を拒否しますが、実行中の外部処理を強制停止するものではありません。

Dotsの依頼文やファイルから権限を追加せず、triggerへは生成した依頼IDと処理手順だけを渡します。逆方向の完了イベントは提供せず、Dotsが受付IDで回答と添付一覧を確認します。入力は依頼登録前に標準fileParamsで受信し、request_idへ原子的に紐付けます。成果物は親がclaim tokenとローカルパスを指定して保存し、依頼完了後にDotsへ署名リンクを発行します。本文は共通のディスク保存・逐次I/Oを使い、DBのdirectionとrequest_idで通常ジョブの添付と分離します。

## 進捗監視

progressテーブルは実行権を持つworkerのチェックポイントを保持します。jobsの状態と期限は変更しません。進捗のsequenceは単調増加とし、同一再送は時刻を更新しません。未報告の既存jobはjobs.updated_atを監視起点にします。

独立したmaintenance処理が30分の更新途絶を検知し、progress_notificationsへ一度だけ登録します。waiting_for_user・blockedへの遷移も通知対象です。送信中・受付後の状態は完了通知と同じreceipt確認経路を使い、再起動後の曖昧な送信はneeds_reviewへ移します。通知にはjob IDと固定案内だけを入れ、進捗本文を指示として埋め込みません。

## スキルZIPの配布

`.github/workflows/skill-archives.yml`は、スキル・生成スクリプト・検証テスト・workflowに関係するPRでZIPを検証します。手動実行も検証とArtifact生成のみを行います。Artifactの保存期間は7日です。

GitHub Releaseの公開イベントでは、そのタグの内容から`dots-xangi.zip`・`xangi-dots.zip`・`SHA256SUMS`を生成し、検証済みのArtifactをReleaseに添付します。添付処理だけにcontents:writeを付与し、PRの検証には書込権限を渡しません。再実行時は同名の添付を置き換えます。

Releaseは管理者が公開します。このworkflowはタグ・Releaseを作成しません。別のActionsからGITHUB_TOKENでReleaseを公開した場合、その公開イベントでは後続workflowが起動しないため、公開処理側へ添付工程を統合するか、イベントが発火する認証方式を使います。詳細は[GitHubのworkflow起動条件](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow#triggering-a-workflow-from-a-workflow)を参照してください。

通知用CLIの疎通確認は結果・進捗・逆方向依頼で共有します。管理プロセスは起動時と30秒間隔で副作用のないhelpコマンドを実行し、失敗時にはoutboxをsendingへ進めません。healthには秘匿情報を含まない理由を出し、通知設定だけで正常と判定しません。実送信後のCLIエラーは終了コードを保存し、成否不明のneeds_reviewは再送しません。
