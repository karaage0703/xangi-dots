# xangi Dots連携スキル

「Dotsに頼んで」で依頼を登録し、MCP Events経由の処理と結果通知を確認します。接続済みのxangi-dots拡張とDotsプラグイン、イベント購読が必要です。操作手順は[SKILL.md](SKILL.md)を参照してください。

## 起動と未受領時の対応

イベント配送は非同期です。配送成功だけで処理完了とは判断せず、jobの状態と結果、通知を確認します。購読なし・未受領の場合は既存jobを保持し、本人がDots画面からそのjobの処理を指示します。Workと本人のdotは別の表示先です。

## 参考

- [MCP Events](https://developers.openai.com/plugins/build/mcp-events)
- [一般利用規約](https://openai.com/policies/row-terms-of-use/)
- [Service Terms §7・§14](https://openai.com/policies/service-terms/)

## 保存容量と転送効率

設定対応版は合計20 GiBが既定値です。インスタンス別config.jsonの`max_storage_bytes`（正の整数バイト数）で変更でき、対象拡張の再起動後、管理healthの`storage.max_bytes`・`reserved_bytes`・`available_bytes`で確認します。既存設定の他項目は保持してください。旧版や別接続にも同じ上限が適用済みとは限りません。

上限拡大は整理や容量不足による待ち時間を減らしますが、通信速度や1件512 MiB・1ジョブ8件の制限は変わりません。一括入力・baseのSHAを指定した差分成果を使い、空きが十分なら毎回の整理を省きます。

汎用HTTP400は容量不足以外でも発生します。容量・件数・キーの競合・入力条件を切り分け、整理が必要なら許可された終了済みファイルを退避・サイズ/SHA照合してから転送コピーを削除します。設定保存と実効値の反映は区別します。具体的な操作は同梱SKILL.mdを参照してください。
