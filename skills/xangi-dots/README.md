# xangi Dots連携スキル

「Dotsに頼んで」で依頼を登録し、MCP Events経由の処理と結果通知を確認します。接続済みのxangi-dots拡張とDotsプラグイン、イベント購読が必要です。操作手順は[SKILL.md](SKILL.md)を参照してください。

## 起動と未受領時の対応

イベント配送は非同期です。配送成功だけで処理完了とは判断せず、jobの状態と結果、通知を確認します。購読なし・未受領の場合は既存jobを保持し、本人がDots画面からそのjobの処理を指示します。Workと本人のdotは別の表示先です。

## 参考

- [MCP Events](https://developers.openai.com/plugins/build/mcp-events)
- [一般利用規約](https://openai.com/policies/row-terms-of-use/)
- [Service Terms §7・§14](https://openai.com/policies/service-terms/)
