# API

## xangi側: capability `dots.jobs`

親が生成したBearer tokenを要求します。`extension_request`を使えば認証はxangi側が付けます。

- `GET /health`: ready、状態別の件数、有効な購読数、notification_transport（configured・ready・last_error）。通知設定済みで疎通失敗ならready=false。
- `GET /connection`: MCPポート、秘密path、state保存場所、通知有効フラグ。秘密情報として扱う。
- `POST /jobs`: `request_key`（1〜200字）、`prompt`（1〜20,000字）、`origin`（platform/channel）、任意の`timeout_seconds`（60〜604800、既定3600）と`attachments`（保存済みinput file IDの配列）。originと添付は登録後に変更不可。
- `GET /jobs`: 最新100件。特定の古い依頼はIDで取得。
- `GET /jobs/<id>`: 結果と通知状態、progress、attention_notifications。progressは未報告ならnull、報告済みならsequence/state/summary/next_action/updated_at（Unix秒）。attention_notificationsは進捗通知の最新10件で、送信状態・trigger_idを含む管理口専用情報です。
- `POST /jobs/<id>/cancel`: bodyは`{}`。終了済みなら現状を返す。

同じrequest_keyで内容を変えると拒否します。通信失敗時は同じキー・同じ内容で再試行してください。同時にqueued/runningで保持する依頼は最大100件です。会話IDをモデルや外部の依頼本文から推測せず、xangiの依頼元情報から設定してください。

### ファイル

標準経路（推奨）:

- `POST /files/import-local {path,upload_key,mime_type}`: workspaceまたは`/tmp`内の通常ファイルを直接読み込み、サイズ・SHA-256を計算してready inputを作る。最大512MiB。管理口だけで利用可。
- `POST /files/<id>/export-local {path}`: readyファイルを検証後、親が選んだworkspaceまたは`/tmp`内へ新規保存。mode 600、既存ファイル・symlinkを上書きしない。
- workerの`GET /download/<id>?expires=...&signature=...`: get_file_linkが発行した10分有効の署名付きURL。バイナリを直接返す。ジョブ取消・期限切れ・worker鍵の変更で無効。Origin付き要求は拒否。URLをログへ記録しない。

小容量の分割転送API（通常は標準方式を使用）:

- `POST /files`: `{upload_key,name,mime_type,size,sha256}`でinput uploadを作成。sizeはバイト数、sha256は小文字64桁。応答の`id`と`received`で続行。同じキー・メタデータの再送は同じfile IDを返す。
- `POST /files/<id>/chunks`: `{offset,data,encoding}`。offsetはバイト単位、encodingは`base64`か`utf-8`。復号後1〜49,152バイト。既存位置への同一内容の再送は許容、上書き・飛び越しは拒否。
- `POST /files/<id>/finish`: `{}`。全バイトとSHA-256を検証して`ready`にする。空ファイルも可。
- `GET /files/<id>?offset=0`: 最大49,152バイトを`data_base64`で返す。`next_offset`で続行、`eof=true`で終了。ダウンロード後にsizeとsha256を照合する。
- `DELETE /files/<id>`: 未添付、または終了済みジョブのファイルを削除。実行中ジョブの添付は削除不可。workerには公開しない。

`/jobs/<id>`の`files`には`id,job_id,direction,name,mime_type,size,sha256,status,received`を返し、本文は含めません。directionは`input`か`output`。アップロード中は`uploading`、検証済みは`ready`。管理側でinputをfinishしてから`attachments:["file_..."]`で依頼へ紐付けます。添付の束縛と依頼登録・イベント作成は同じtransactionで行い、不完全な入力をDotsへ通知しません。ファイルは1つのジョブだけに紐付きます。

標準経路は1ファイル最大512MiB、分割転送APIは1MiB、1ジョブは入出力合計8件（作成中含む）。全体は申告サイズ合計20GiB（既定値、`max_storage_bytes`で変更可能）・4096件、未添付または作成中は128件。使わなくなったアップロードや終了済み添付は管理APIで削除できます。上限は論理データ量で、一時ファイル・孤立ファイル・SQLite/WALの実ディスク容量とは異なります。標準転送本体はDB隣の`inbox.sqlite.files/`へ保存します。削除後もDBバックアップや空きページからの完全消去は保証しません。

## Dots側: `/mcp/<worker_token>`

管理APIとは別のlistenerです。POSTのJSON-RPCのみ対応、SSEは未対応です。通知は202、GET/DELETEは405。Origin付きリクエストは拒否します。

- `server/discover`: MCP 2026-07-28、tools/events capability。
- `initialize`: 互換入口（2025-06-18、2025-11-25、2026-07-28）。
- `tools/list` / `tools/call`: 下記ツール。
- `events/list` / `events/subscribe` / `events/unsubscribe`: `job.created`、引数`{"queue_id":"dots"}`。
- `ping`。

ツール:

- `list_jobs {}`: queued/runningの依頼を最大100件取得。
- `get_job {job_id}`: 終了済みも含めて取得。
- `claim_job {job_id, claim_key}`: 実行権とclaim_tokenを取得。同じclaim_keyの再試行は同じtoken、別のclaimは拒否。
- `report_progress {job_id,claim_token,sequence,state,summary,next_action}`: runningかつ正しいclaim_tokenで進捗を保存。sequenceは1〜1,000,000の増加番号。同じ番号・内容の再送は更新時刻を変えず、古い番号や内容違いは拒否。stateはworking/waiting_for_user/blocked、summaryは1〜4,000字、next_actionは1〜2,000字。権限や期限は変更しません。
- `submit_result {job_id, claim_token, result, outcome}`: outcomeは`completed`か`failed`、resultは1〜50,000字。終了状態と結果が同じ再送のみ許容。
- `get_file_link {job_id,claim_token,file_id}`: ready添付の10分有効ダウンロードURLを発行。`public_base_url`設定が必要。
- `receive_file {job_id,claim_token,upload_key,name,mime_type,file}`: 標準`_meta["openai/fileParams"]`を使って実ファイルを受信。fileは必須のdownload_url/file_idと任意のmime_type/file_name。512MiBまで、HTTPSの`*.oaiusercontent.com`からのみ取得。非public IPとredirectを拒否し、TLS検証・接続先IP固定・取得上限を適用。入力URLを保存・ログ出力しない。
- `read_file {job_id,claim_token,file_id,offset}`: 自分がclaimしたジョブのreadyファイルを分割取得。完了・失敗後も参照可、キャンセル・期限切れ後は拒否。
- `create_file {job_id,claim_token,upload_key,name,mime_type,size,sha256}`: runningジョブへoutput uploadを作成。
- `write_file {job_id,claim_token,file_id,offset,data,encoding}`: outputの分割保存。
- `finish_file {job_id,claim_token,file_id}`: outputのサイズ・ハッシュ検証。
- `save_text_file {job_id,claim_token,upload_key,name,mime_type,text}`: 最大50,000字のテキストをUTF-8ファイルとして保存。サイズとSHA-256はサーバーが計算。

ファイルの作成・追記・finishはrunning中だけです。添付の保存を終えてからsubmit_resultを呼びます。未完了のuploadがある場合のcompletedは拒否、failedは許容します。中断・失敗時の保存済みファイルは親が確認・回収・削除できます。既存ファイルは上書きできず、入力を加工した結果は新しいoutputとして保存します。

Dotsにはorigin・request_key・他のclaim_token・通知先設定を返しません。worker tokenは単一依頼箱全体の権限です。複数ユーザーのアクセス制御ではありません。

HTTP bodyは最大256KiB、各listenerは最大16同時接続、socket timeoutは15秒です。リクエスト・秘密URL・本文のaccess logは出しません。reverse proxy側の制限とログ設定は別途必要です。

## Dots起点の依頼

有効化時のMCPツール:

- `request_xangi(request_key, prompt, timeout_seconds, attachments?)`: 固定受付先へ非同期依頼。promptは20,000文字まで、期限60〜86400秒。同じkey・内容は同じIDを返し、内容違いは拒否します。
- `get_xangi_request(request_id)`: status・result・dispatchを参照。通知先・trigger ID・claim秘密は返しません。

親の管理API:

- `GET /requests/<id>`: 依頼・回答・通知先・trigger IDの取得。
- `POST /requests/<id>/claim`: `{claim_key}`で排他的に実行を取得。`claim_token`を返します。同じkeyで再試行可能。
- `POST /requests/<id>/result`: `{claim_token,result,outcome}`で回答保存。outcomeはcompleted/failed。同一回答の再送可、終端状態や内容の上書き不可。
- `POST /requests/<id>/cancel`: 空オブジェクトで未完了依頼をcancelledへ変更。

statusはqueued/running/completed/failed/expired/cancelled。dispatchはpending/sending/accepted/running/needs_review。回答保存後のdispatchは最後に確認した配送状態を保持します。結果保存が仕事の完了判定です。逆方向の依頼一覧・依頼削除・自動再送APIは提供していません。添付は既存の親ファイル削除APIで削除できます。単一worker共有の依頼箱で、複数ユーザー間の隔離機能ではありません。

逆方向のファイル:

- MCP `upload_xangi_file(upload_key,name,mime_type,file)`: 標準fileParamsで入力を保存。返却file IDをrequest_xangiのattachmentsへ指定します。通常のxangi→Dots入力との混在、同じ入力の複数依頼への添付を拒否します。
- 管理 `POST /requests/<id>/files/import-local`: `{claim_token,path,upload_key,mime_type}`で実行中依頼の成果物を登録。pathはworkspaceまたは/tmp、通常ファイルのみ。標準転送と同じ512MiB/件・20GiB（既定値、`max_storage_bytes`で変更可能）総量を適用します。
- MCP `get_xangi_file_link(request_id,file_id)`: completed/failedの依頼のready成果物に限り10分有効の署名URLを返します。
- `get_xangi_request`の`files`: 入力はdirection=reverse_input、成果物はreverse_output。request_id・size・sha256・statusを含みます。親は既存`/files/<id>/export-local`で入力を取得できます。

入出力合計8件。upload_keyは逆方向入力・依頼別出力ごとに独立し、再送で重複作成しません。未完成成果物があればcompleted保存を拒否します。active依頼の添付は削除できません。取消・期限切れ後の成果物登録、別依頼のリンク発行を拒否します。

## Parent follow-ups

`POST /jobs/<job_id>/followups` (admin authentication) accepts `request_key` and `message` (max 4000 characters). Active jobs only; at most 50 messages per job. Identical retries return the original message; conflicting key reuse is rejected. The job claim, deadline, origin and work remain unchanged.

`GET /jobs/<id>` exposes `followups` with `id`, `message`, `created_at`, and `acknowledged_at`. The existing `job.created` inbox event also signals follow-ups to queued/running jobs; consumers must inspect get_job and must not reclaim running jobs. Late subscriptions receive unacknowledged follow-ups. The original claimant calls MCP `acknowledge_followup(job_id, claim_token, message_id)` after reading, then reports progress separately. Acknowledgment is not approval or completion. Terminal jobs cancel pending event delivery. Browser automation is not used.
