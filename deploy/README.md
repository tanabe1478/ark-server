# デプロイの責務

推奨の運転方式は[rootless Docker + Bot](../docs/docker.md)です。このディレクトリの既存Ansibleは、通常ユーザー向けNative bootstrap／設定・unit配置の互換経路として残しています。DockerとBotの完全な一括プロビジョニングをAnsibleだけで行うものではありません。

## 初期bootstrap（互換経路）

`inventory.example.ini`と`secrets.example.yml`を参考に、実inventory・秘密をローカルだけで用意します。実ファイルはGit対象外です。接続先・ユーザー・map・人数・portsを確認し、コピー元のPCへ誤適用しないでください。

Ansibleはユーザーサービスを配置し、通常はサーバーを起動せず、既存ゲームを暗黙更新しません。参加／管理者パスワードは別の長い値を要求し、認証INIを保護します。linger、自動起動、backup timer、firewall変更は安全側の初期値で無効です。sudoが必要なbootstrapを、Botの権限追加へ置き換えないでください。

Dockerへの切替は、既存ゲーム・設定・MODと検証済み停止が用意できてから、`scripts/docker_setup.py`で行います。rootless Docker、イメージ、private config、Token、namespace検証を省略しません。

## ファイルの分離

- `docker/`：公開可能なDockerfile・user unit雛形
- `discord/`：旧Native Bot unit雛形（Docker切替後は混用しない）
- `templates/`：Native互換unit
- `group_vars/all.yml`：一般化した既定値
- `inventory.example.ini`、`secrets.example.yml`：ダミー値だけ

runtime/SteamCMD/ゲーム/MOD/保存データ、秘密、実inventory、privateなplan・receipt・運用履歴は公開・image化しません。コンテナ運転で使うトークンはfile mountだけです。

## 通常運転と更新

通常操作は共通CLI／Discordへ集約します。ゲームに`Restart=always`や`unless-stopped`を付けず、保存・終了が不明なら停止したままにします。Dockerによるhealthcheckは参加可能性の補助であり、保存の証明や勝手な再起動を行いません。

起動時updateはありません。別候補の作成、別Savedコピーでの検証、保存・復旧レビューを分けます。[更新と復旧](../docs/update-recovery.md)を参照してください。ホスト固有の回線・IP・サービス状態の記録は、ignoredなprivate領域へ保持します。
