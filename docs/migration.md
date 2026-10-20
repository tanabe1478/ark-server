# Aquatica packageの移行・専用復旧

この機能は特定形式の移行package向けです。Discordへ公開せず、package内のインストーラーや起動scriptも実行しません。source package・退避・現在進捗は別々に保持します。

## 基本条件

- 元環境を停止して作成したpackageを、別のstaging領域へ置く
- ワールド、profile、tribe、MOD本体・順序、設定と認証の採用元をプレビューする
- entry数・byte数・時間・空き容量を制限し、symlink・traversal・unsafe fileを拒否する
- revision/hashがプレビューと一致する場合だけ適用する
- 置換前をprivate退避し、途中中断はpending markerで起動を遮断する
- 復旧は明示承認し、勝手に再起動しない

参加パスワードのないsourceでも、移行先の参加／管理者認証を保持する選択があります。認証を空へ戻さず、RCONを無効にし、`GameUserSettings.ini`の初期化Versionを検証してください。ActiveModsの宣言だけでなくLinux向けcontentの存在も必要です。

保存内容が移行元のhashと変わったことだけを理由に、新しい進捗を上書きしてはいけません。異常起動後は現在状態を独立して保全してから、復旧元・認証採用元・置換範囲をレビューします。

通常の移行はcleanな停止を要求します。旧Native SIGABRTの専用復旧条件を、Dockerの134や未知の失敗へ拡大しないでください。Dockerでの一般復元は[更新と復旧](update-recovery.md)に従い、対応する停止証拠と明示レビューを別に用意します。

ファイルhash・A2S・設定維持は、ゲーム内の全進捗／全MODの完全復元の証明ではありません。元packageの実名、ユーザー数、Steam ID、バックアップの個別ID、生ログをこの公開文書へ追記しないでください。
