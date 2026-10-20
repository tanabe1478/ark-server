# 保存証拠付き警告停止

これは終了値6／134を正常扱いする設定ではありません。対応するASE Linux固定ビルドに対し、既知の保存・終了経路を観測して、異常終了記録を残したまま限定的に次の起動を許可します。

## 条件

- ゲームバイナリのSHA-256、観測profile、観測ソース、Dockerではruntime image IDとコード契約が一致
- boot ID、systemd invocation、run ID、game/debugger/supervisor、container IDが整合
- 一意の停止nonceをdurableに保存してから停止要求
- 同一nonce内でsave begin→save complete→固定abort位置／caller→実ゲームSIGABRT/6の順序を確認
- worldの更新・サイズと保存完了時メタデータが一致し、profile/tribeが失われず空でない
- 認証を含む保護設定が変わらず、Saved全体の停止前後コピーとhashがある
- 実際に終了しlifetime lockを取得可能。OOM、timeout、未知のsignal、観測／lease faultを拒否
- 次のstart時にも現在の全Saved・設定・policyを再検証

成立時も`clean_exit=false`、`expected_native_abort`、`in_game_content_verified=false`です。起動を繰り返して成功に見せたり、`SuccessExitStatus`、`reset-failed`、abort抑止、binary patch、exit 0への変換は使用しません。

## NativeとDockerの結果

Native bridgeは実ゲームのsignalをそのまま再現し、systemdのfailed/core-dump/6を保持します。DockerではTiniがsignalによる子の終了を134で表現し、host runnerも134を返すため、systemdはfailed/exit-code/134です。private receiptには実ゲームのsignal 6と、コンテナ・ホストの134を別々に保存します。

コンテナ134だけのunknown crashは許可しません。起動中の観測と停止意図・native保存経路がそろわない限り、次の起動を遮断します。unitがreboot後にfactoryのinactive/success/0へ戻っても、過去の不確定なgame/launch recordは消えません。

## コンテナでの観測開始

headlessコンテナで、Steamライブラリの初期化を最初からGDB下で実行するとSIGTRAPを観測する比較ケースがありました。そのsignalは握りつぶしていません。

Docker用GDBはゲームの実親として、まずtracerなしで起動し、設定に合うA2S応答後に自分の子へattachします。SIGTRAPのソース上の理由は未確定です。attach後に同じsave/abort観測を設定し、それが完了するまで参加可能とは判定しません。未知のstartup終了はunknownとして記録し、保存証拠にしません。

GDB喪失時はPDEATHSIGで孤立writerを防ぎます。host runner喪失時もlease faultを残して資格を失効させます。通常のreadiness timeoutを理由に強制停止／再起動する設計ではありません。

## 制約

観測はソース関数名を完全に確定したものではなく、固定ビルド上のnative位置・呼出経路に依存します。別のゲーム・バージョン・Steam構成へ流用しません。対応バイナリ／定数は`scripts/ark_shutdown.py`にあります。

native保存関数完了、A2S、ファイルhash、再読込の成功だけで、全建築物・恐竜・アイテム・MOD内容の復元を保証しません。所有者のゲーム内確認が別に必要です。個別の実機記録・receipt・生ログ・Savedは公開せず、ローカルのignored領域へ保持します。
