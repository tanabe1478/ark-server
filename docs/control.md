# 共通Controllerと通常運転

```bash
python3 scripts/arkctl.py status
python3 scripts/arkctl.py start
python3 scripts/arkctl.py stop --confirm
python3 scripts/arkctl.py restart --confirm
```

通常操作は共通Controllerを経由します。Docker構成ではrootless Docker transportを選び、Native構成では従来systemd backendを使います。`docker restart`、`systemctl restart`、直接のゲーム起動、`reset-failed`は検証済み操作の代わりではありません。

## 安全境界

- request IDのdurableな重複排除、actorの範囲、確認、入力検査
- control/backup/server/automationの排他とゲームのlifetime lock
- 起動前の認証・Version・ActiveMods・pending recovery検査
- 停止と次の起動を分離し、未知の停止を次の起動で隠さない
- 実行前の意図、監査、終了結果、保存退避をowner-onlyで保持
- 不確定なストレージ書込・中断・未知の操作結果は遮断し、自動再送しない

Botへ公開する操作はstatus/start/restartだけです。設定編集、移行、復旧、更新、証拠レビューはローカル所有者の別作業です。IPCも任意のshell・unit名・container名・パラメータを受け付けません。

## 参加可能性

activeは準備完了ではありません。認証・初期化・MOD内容、実プロセス、A2Sのマップ・定員・パスワード必須、Dockerでは観測器のattachも確認します。初期化途中のA2Sメタデータは期限内で再照会します。期限切れでゲームを強制停止／再起動しません。

人数未取得は0ではありません。メモリ表示はゲームとPC全体を分けます。メモリ利用率を理由にゲームを自動再起動しません。

## 停止と記録

Nativeのclean exit 0と、証拠付き警告停止は区別します。DockerのGame SIGABRT/6→Tini/container 134→host unit exit-code/134も保全します。終了134だけでは成功になりません。[観測契約](evidence-qualified-shutdown.md)

停止・保存を検証したSaved全体は`backups/shutdown-<id>/`または`backups/docker-shutdown-<id>/`へ停止前後で退避します。稼働中のコピーと停止後の保存、ゲーム内の整合性は別の主張です。空き容量不足・コピー中変更・unsafe entryは拒否し、古いバックアップを自動削除しません。

操作・証拠は`logs/control/`、`logs/automation/`、`logs/shutdown/`、`logs/docker/`へ保持します。公開issueへ添付しません。失敗latchを削除して起動を通さず、[更新と復旧](update-recovery.md)に従って明示レビューしてください。

通常の設定編集は停止時だけです。停止receiptが設定・Savedに結び付くため、変更すると次の起動条件も変わります。検証後のファイルを直接書き換えて、そのまま古いreceiptを使わないでください。
