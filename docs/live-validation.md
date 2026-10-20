# 実機検証チェックリスト（公開用テンプレート）

実値・実機履歴はignoredのprivate記録へ保存し、このファイルへIP・guild ID・ユーザーID・receipt全文・生ログを記入しないでください。オフライン試験の成功と、実ゲーム・実Discord・ゲーム内確認を分けて報告します。

## 非本番

- 合成の新規worldで起動→A2S準備→観測attach→保存確認付き停止→再読込
- 実データの独立コピーでも同じcycle。認証・初期化・MOD順序維持、profile/tribe保存
- 既に動いているstartでPIDが変わらない
- restartがstopとstartに分かれ、unknown停止でstartしない
- 起動／依頼後のgrace、人数不明、人数変化、同時操作、二重送信、保存失敗、ストレージ障害
- idle条件は時間加速の試験と、本当の1時間経過を分ける
- 実データの内容を本番へ書き戻さない。read-only資産共有と書込データの分離を検証する

## Bot／IPC

- Botにtoken/config/lock/IPCだけがmountされ、Docker socket・bus・Savedがない
- IPCはowner UIDだけ、許可actionだけ、未知／大きすぎる入力を拒否
- DM・別guild拒否、同guildの各メンバー許可、本人だけの確認、期限・キャンセル・人数再確認
- 通知失敗・切断でゲーム操作を再実行しない
- Botのdrain・再起動でゲームPIDが変わらない
- 実際のslash commandと実際の確認ボタン操作を、fake interaction試験と区別する

## 本番切替

- 現在人数と保守案内、受信器のdrain、停止前後退避、protected設定の維持
- 切替中断marker、失敗latch、古いreceipt再利用の拒否
- 本番の起動／保存／再起動／準備完了。ゲームsignal・container exit・unit exitを別々に確認
- 本当のhourly tick、start依頼の猶予、通知
- 別回線から参加・認証、ゲーム内のキャラクター／トライブ／進捗／建築／恐竜／MODアイテム
- 使用者がいる間は破壊的試験をしない。未知の失敗は繰り返さず保全する
