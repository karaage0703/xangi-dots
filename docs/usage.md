# 使い方

初めて利用する場合は[セットアップ](setup.md)を完了してください。

## 自然文で依頼する

- Dotsにこのファイルを分析してもらって。
- Dotsの返事を確認して。
- Dotsで「xangiに17×23の計算を頼んで、回答を確認して」。

## イベント起動と未受領時の対応

依頼登録後はMCP EventsでDotsへ通知し、APIで受領・結果保存・元会話への通知を確認します。

購読がない・依頼が受領されない場合は同じjobを保持し、本人がDots画面から既存job IDの処理を指示します。スキルの手動送信例を使い、新規jobを重複登録しません。

イベント購読・配送・処理開始・回答保存は別の状態です。購読期限は最大24時間で、更新が止まると新着通知も止まります。

## 依頼の登録・確認・キャンセル

以下の会話IDを実際の依頼元に置き換えます。登録する側が本人の依頼範囲を確認し、秘密や不要な会話履歴を含めないでください。

```bash
xangi tool extension_request --id xangi-dots --capability dots.jobs \
  --path /jobs --method POST \
  --body-json '{"request_key":"example-001","prompt":"17×23を計算してください。他のアプリは使わないでください。","origin":{"platform":"discord","channel":"YOUR_THREAD_ID"},"timeout_seconds":3600}'

xangi tool extension_request --id xangi-dots --capability dots.jobs \
  --path /jobs/JOB_ID

xangi tool extension_request --id xangi-dots --capability dots.jobs \
  --path /jobs/JOB_ID/cancel --method POST --body-json '{}'
```

`completed`はDotsが結果を提出した状態です。回答の正しさを保証するものではありません。通知の`accepted`も配信完了ではなく、`delivered`が元の会話への配信確認です。

## 長時間の依頼

依頼には期限、成果物、許可した作業範囲を明記します。Dotsはその範囲で作業を続け、15分以内ごとと停止前にreport_progressで進捗・次の作業を保存します。必要な本人承認はDots上で行います。

拡張は次の状況を元の会話へ通知します。

- waiting_for_user：本人の入力や承認が必要。
- blocked：リソース不足など、その他の理由で作業を続けられない。
- 30分間未受領、または作業開始・最新進捗から30分間更新なし：進捗未確認。

更新途絶だけでは停止と断定できません。runningと表示されていても、progressの時刻と次の作業を併せて確認します。同じ停止状態の繰り返し報告や同じ更新途絶は通知を重複させません。進捗報告が再開すると次の更新途絶を監視します。

自動通知にはxangi_cliの設定が必要です。進捗通知は結果通知と別に保存し、attention_notificationsで送信状況を確認できます。再起動時も監視を続けますが、送信結果が不明な通知はneeds_reviewとして扱います。

通知は停止を知らせる仕組みで、Dotsの本人承認や実行環境の停止を自動解除する機能ではありません。再開時は既存jobと保存した成果物を使います。

## ファイルを送る・受け取る

Markdown・CSV・コードだけでなく、画像・PDF・ZIPなどのバイト列を送受信できます。**標準経路は1ファイル512MiB、入出力合計8ファイル/依頼**までです。転送できることと、Dotsがその形式の内容を解釈・生成できることは別です。Dotsへは期限付きHTTPSリンクで渡し、Dotsからは標準の`fileParams`で実ファイルを受け取ります。ChatGPTが発行するファイルCDNの一時URLだけをサーバー側で取得し、ファイル本体を会話のテキストに流しません。任意の外部URLやチャットに貼られたリンクの自動取り込みはしません。

リポジトリ内で、対象instanceのCLIを指定して実行します。helperはファイルのパスとメタデータだけを`extension_request`へ渡し、同じホストで動く拡張が直接ファイルを読み書きします。保存元・保存先はworkspaceまたは`/tmp`内の絶対パスです。CLI引数長や応答の切り詰めを避けられます。対象xangiから起動された環境が必要です。ファイル本文をツール出力へ表示しません。

```bash
uv run python -m xangi_dots.file_client --xangi-cli /absolute/path/to/xangi/bin/xangi \
  upload /absolute/path/to/input.csv --upload-key example-001-input
```

表示された`id`を依頼の`attachments`に加えます。同じupload-key・同じ内容での再実行は同じfile IDを返します。内容を変更する場合は新しいキーを使います。

```bash
xangi tool extension_request --id xangi-dots --capability dots.jobs \
  --path /jobs --method POST \
  --body-json '{"request_key":"example-file-001","prompt":"添付CSVを集計し、summary.mdを保存してください。","origin":{"platform":"discord","channel":"YOUR_THREAD_ID"},"attachments":["FILE_ID_FROM_UPLOAD"]}'
```

Dotsへの追加指示:

> get_jobのfilesを確認し、claim後にget_file_linkで取得先を発行し、実行環境でHTTPダウンロードしてください。サイズとSHA-256を照合します。生成したファイルはreceive_fileの標準fileパラメータへ実ファイルとして渡してください。URLやfile IDを推測せず、ファイル本体を本文に流さないでください。小さなテキストだけならsave_text_fileも使えます。保存成功後にsubmit_resultで短い説明を返してください。

結果の`/jobs/JOB_ID`から`direction=output,status=ready`のfile IDを選び、保存先を明示して取得します。

```bash
uv run python -m xangi_dots.file_client --xangi-cli /absolute/path/to/xangi/bin/xangi \
  download FILE_ID --out /absolute/path/to/new-summary.md
```

helperは全体のサイズ・SHA-256を検証してからmode 600で新規保存します。既存ファイル・symlinkには上書きしません。ファイル名や本文をコマンドとして実行せず、ZIPの自動展開もしません。保存後はプラットフォームの通常の添付手順で返します。Discordではxangi側の返答に`MEDIA:/absolute/path/to/new-summary.md`を含めます。通知先は元の依頼の会話です。

拡張側の上限は1ファイル512MiB、保存容量の合計2GiBです。ファイル本体はディスクへ逐次読み書きします。Dots側や公開proxyの上限・タイムアウトは別に適用されます。拡張の容量上限は、Dots側で処理できる容量を保証する値ではありません。

管理APIで未使用ファイルを削除できます。アクティブなジョブの添付は削除できません。全体の保持上限は2GiBなので、保存期間に合わせて整理してください。

```bash
xangi tool extension_request --id xangi-dots --capability dots.jobs \
  --path /files/FILE_ID --method DELETE
```

## 停止・撤去

Dotsの購読を解除して監視を停止し、専用の公開proxy設定を撤去後、`xangi extension stop xangi-dots`を実行します。不要なら`unlink`とChatGPTのプラグイン削除も行います。SQLiteには依頼と結果、購読秘密が含まれます。標準転送のファイル本体は同じstate内の`inbox.sqlite.files/`に保存します。停止後、DBとファイルディレクトリを一緒にバックアップしてください。バックアップ・削除は利用者が保存方針を決めて実行します。

## 現在の制限

管理口はxangiが渡すBearer token、Dots口はランダムな秘密URLで制限します。OAuthは未実装です。公開するのはDots口だけです。秘密URLの所持者はその依頼箱の全タスクを読めるため、共有アカウント・複数ユーザー・機密業務には使わないでください。

複数ユーザーの権限分離、他エージェント用アダプター、停止したDotsの強制終了、期限切れ後の自動再割当、公開トンネルの自動設定は対象外です。キャンセル・期限切れは依頼箱の状態を止め、遅い結果を拒否します。Dots側で始まった外部操作を取り消す機能ではありません。

導入時は[セットアップの成功確認](setup.md#6-成功を確認)を行ってください。

## Dotsの利用条件

- **利用者の責任と監督**: Dotsへの権限・指示・制限を決め、その実行と送信内容を確認する責任は利用者にあります。自作Appにも開発者規約が適用されます。[Service Terms §7・§14](https://openai.com/policies/service-terms/)
- **操作の承認**: DotsのAuto-reviewや本人確認が必要な操作は、xangiから依頼しても省略できません。外部データである依頼文自体は権限を追加しません。[Dots FAQ](https://help.openai.com/en/articles/20001529-dots-privacy-security-and-safety-faqs)
- **利用枠**: Dotsが開始・管理するWork/Codexのタスクは、それぞれの利用枠に計上されます。プラン・提供地域・管理者設定の条件は公式案内で確認してください。[Dotsの提供条件](https://learn.chatgpt.com/docs/dots#access)
- **保存データ**: App側の安全管理、必要な範囲だけの収集・保存、個人データを扱う場合の同意・プライバシー通知などの条件があります。依頼・結果はローカルSQLiteへ保存されるため、保存期間と削除方法を運用者が決めます。共有・公開提供へ進む前には認証とデータ管理を見直してください。[App Developer Terms §1・§2](https://openai.com/policies/developer-apps-terms/)

## Dotsからxangiへ依頼する

[セットアップ](setup.md#2-通知用cliとmcpポートを設定)で`request_destination`と`xangi_cli`を設定し、[ツール一覧を更新](setup.md#ツールを更新したとき)してください。`/connection`の`parent_requests_enabled: true`で設定を確認できます。

Dotsには次のように依頼できます。

> request_xangiでxangiへ「17×23を計算してください」と依頼してください。request_keyはこの依頼用の一意な文字列、timeout_secondsは3600にしてください。受付IDを保存し、get_xangi_requestで30秒以上間隔を空けて結果を確認してください。通信を再試行するときは同じrequest_keyと内容を使い、新しい依頼を重複登録しないでください。completedならresultを報告、failed/expired/cancelledならその状態を報告してください。dispatchがneeds_reviewなら再送せず親へ確認を求めてください。

xangiは管理APIから依頼本文を取得し、`/requests/<id>/claim`へ`{"claim_key":"実行ごとの一意な値"}`を送ってから処理します。同じ実行の再試行では同じclaim_keyを使います。終わったら`/requests/<id>/result`へ`claim_token`、`result`、`outcome`（completedまたはfailed）を保存します。Dotsは受付IDで結果を取得します。単に会話へ返信しただけではDots側の依頼は完了しません。これらの手順は起動時のtriggerにも含めています。

逆方向も依頼・回答にファイルを添付できます。期限は登録から60秒〜24時間、同時未完了100件、保存総数4096件。結果の説明文は最大50,000文字です。ファイルは入出力合計8件、1件512MiB、拡張全体2GiBです。完了通知イベントはなく、Dotsが結果を確認します。依頼文・回答は同じSQLiteへ保存し、元のDots向け依頼とは別に管理します。

通知先は登録時に固定され、Dotsから変更できません。依頼文はtrigger本文へ直接埋め込まず、外部からの依頼としてユーザーの許可範囲で扱います。許可が必要な操作は勝手に実行せず、必要事項をfailedの回答として返します。xangiから受けた仕事をDotsがxangiへ丸投げする往復ループは行わないでください。

`dispatch`は起動通知の状態で、仕事の`status`とは別です。起動済みでも回答が保存されなければcompletedにはなりません。送信中の停止・受付結果不明・回答なしのtrigger終了ではneeds_reviewとなり、自動再送しません。管理側は`GET /requests/<id>`でtrigger_idを取得して実行状況を調べます。`POST /requests/<id>/cancel`（空オブジェクト）でキャンセルできますが、既に開始した外部処理を取り消す機能ではありません。期限切れも実行中の処理を強制停止しません。

既存のxangi→Dots依頼への`submit_result`による結果通知も引き続き利用できます。

### Dotsから入力ファイルを送り、成果物を受け取る

1. Dotsは`upload_xangi_file`の`file`へ実ファイルを渡します。`upload_key`・`name`・`mime_type`も指定します。サーバーが標準ファイルURLからダウンロードし、サイズとSHA-256を返します。再試行では同じupload_keyを使います。
2. `request_xangi`の任意の`attachments`配列へ、返されたfile IDを指定します。全ファイルのアップロード成功後に依頼してください。添付は登録時に固定され、別依頼へ使い回せません。
3. xangiはclaim後、`get`応答の`files`から`reverse_input`を確認し、既存のファイルhelperのdownload、または`POST /files/<id>/export-local`で新しいローカルパスへ保存します。
4. 成果物は`POST /requests/<id>/files/import-local`へ`claim_token`・`path`・`upload_key`・`mime_type`を送って登録します。例: `{"claim_token":"CLAIM_TOKEN","path":"/tmp/result.zip","upload_key":"result-1","mime_type":"application/zip"}`。全成果物の保存後に`/requests/<id>/result`で完了します。
5. Dotsは`get_xangi_request`の`files`にある`reverse_output`を確認し、`get_xangi_file_link(request_id,file_id)`で10分有効のURLを取得します。実行環境でダウンロードし、サイズとSHA-256を照合してください。URLの発行には`public_base_url`の設定が必要です。

成果物リンクはcompleted/failedの依頼だけに発行します。入力ファイルからリンクを発行することや、別依頼・未完了・取消・期限切れの成果物を取得することは拒否します。アップロードだけして依頼しなかった入力は容量を消費するため、不要なら親のファイル削除APIで削除します。Dotsから任意のホストパスを読み書きするAPIは公開しません。

### 通知経路の障害

`/health` の `notification_transport` は `configured`、`ready`、`last_error` を返します。設定済みの通知経路が使えない場合、全体の `ready` もfalseになりますが、依頼・結果の保存APIは継続します。`missing_tool_server` は起動元の接続先環境の欠落、`cli_exit_N` はCLI終了コード、`cli_timeout` / `cli_unavailable` はタイムアウト / 実行不能です。秘密を含み得るCLI出力は保存しません。

疎通失敗中は結果・進捗・逆方向依頼の未送信通知を保持し、30秒ごとの再確認が成功すると送信を再開します。環境変数の修正には拡張の再起動が必要です。復旧時は最新の進捗を確認し、解消済みの未送信停止通知は取り消します。既に `needs_review` の通知は送達成否が不明なので自動再送しません。triggerの受付・実会話での配信を確認して個別対応してください。
