# xangi-dots

[xangi](https://github.com/karaage0703/xangi)と[Dots](https://learn.chatgpt.com/docs/dots)の間で、依頼・回答・ファイルをやり取りする拡張です。

```text
xangi → Dots → 回答と成果物 → 元の会話
Dots → xangi → 回答と成果物 → Dots
```

双方向の依頼、最大512MiBのファイル転送、進捗確認、許可待ち・更新途絶の通知、キャンセル、期限切れに対応します。単一ユーザー向けで、接続設定が必要です。Dotsへの新着依頼はMCP Eventsで通知します。

- [セットアップ](docs/setup.md) / [使い方](docs/usage.md)
- [Museとの連携・注意事項](docs/muse.md)
- [スキルZIPのダウンロード](https://github.com/karaage0703/xangi-dots/releases)
- [API](docs/api.md) / [設計・開発](docs/design.md)

MIT License — [LICENSE](LICENSE)
