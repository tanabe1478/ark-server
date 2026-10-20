# Discord Bot

外向きGateway/WebSocketとHTTPSを使います。Interactions Endpoint URLや管理用HTTP listenerは不要です。guild intentだけを使い、member/presence/message-contentのprivileged intentは要求しません。

Discord Developer PortalでBotを作成し、一つのguildへ`bot`と`applications.commands` scopeで招待します。必要なチャンネル閲覧・送信とアプリケーションコマンド権限を設定してください。Administratorは不要です。アプリのトークンをチャットやGitへ貼らないでください。

## ローカル設定

```bash
python3 -m scripts.discord_setup --configure
python3 -m scripts.discord_setup --check
```

設定は自分のTTYで入力します。トークン入力は非表示で、echoへフォールバックしません。専用ファイル`config/discord-bot.token`にトークンだけを一行で保存します。`.env`、`DISCORD_TOKEN=`形式、引数、環境変数は使いません。権限は所有者のみ、symlinkや大きすぎる入力を拒否します。

`config/discord-bot.json`にはguild/channel IDと安全設定を保存します。実IDも公開せず、`examples/discord-bot.example.json`を説明用に使ってください。再設定は`shutdown_verified`と`auto_stop_enabled`をfalseへ戻します。

Docker配置は[構築手順](docker.md)に従います。旧Native用の`discord_setup --install`をDocker移行後に実行するとunitの方式が競合するため、Docker用`docker_setup install-bot`を使用してください。トークン変更も、受信器を止めてファイルを更新し、Botだけを再起動します。

## 利用範囲

一つのguildの全メンバーがstatus/start/restartを利用できます。DM・他guildは拒否します。個人・ロール・チャンネルの独自allowlistはありませんが、Discord自身の権限は適用されます。旧互換のPolicy例は、このBotの認可設定ではありません。

restartは本人・guild・channel・actionに結び付いた60秒、一回限りの確認です。現在人数を警告し、人数が変わったら再確認します。キャンセル・期限切れはゲームを操作しません。

完了通知に失敗しても操作は再実行しません。長時間の起動は別のチャンネルメッセージで完了を伝え、送信できない場合はephemeralへフォールバックします。生のSDK/HTTP例外やinteraction URLは出力しません。

## 無人判定

Bot起動後も最初の判定まで1時間待ちます。再接続でループを増やしません。接続断・通知先不明・別の操作中ならスキップします。IPC側も最短間隔を制限します。

起動／start依頼後の猶予、安定したPID、設定維持、期待するマップ、整数0人を確認し、5秒後と退避後に再確認します。取得失敗・不正値・人数不明は停止条件になりません。

ARKが停止中でもPC・rootless Docker・Bot・IPCを動かしておいてください。PCの電源投入、スリープ解除、停電復旧は対象外です。
