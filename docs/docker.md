# ホストsystemd方式：Docker構築と初回切替

> 新規の配布用構成は [Compose](compose.md)／[README](../README.md) を使用します。以下は既存のホストsystemd方式と初回Native→Docker切替の互換手順です。`activate-game` をCompose更新／既存Dockerの更新として使わないでください。

対象はLinux x86_64、systemd user manager、cgroup v2、Landlock ABI 3以上です。実ゲームは別途Valveから取得します。固定観測profileに一致しないゲームでは停止検証を解禁しません。

## 初期準備

通常ユーザーでリポジトリを配置します。初期のOS依存導入だけは管理者が自分の端末で行います。BotにsudoやDockerグループ権限を追加しません。

```bash
sudo apt-get install --no-install-recommends docker.io docker-compose-v2 rootlesskit slirp4netns uidmap
python3 -m scripts.docker_setup install-infrastructure --confirm
systemctl --user start ark-docker.service
python3 -m scripts.docker_setup check
```

既存のrootful Dockerを停止・変更せず、専用rootless socketを使います。Dockerのグローバルcontextや環境の`DOCKER_HOST`も変更しません。Ubuntuでは配布元のrootlesskit AppArmor profileを使用し、user namespace制限をシステム全体で無効化しません。

`install-infrastructure`はunitを配置しますが、enable・起動・ゲーム停止をしません。既存の異なるunitは勝手に上書きしません。サービスの継続にはログインセッション、または所有者が別途選択するlingerが必要です。PCの電源・スリープ設定は変更しません。

## ゲームと設定

ゲーム資産・認証INI・MODはローカルの`server/`に置きます。既存のNative/Ansible構成からの初回切替に対応します。[Ansible/bootstrap](../deploy/README.md)、[Discord設定](discord.md)を参照してください。新規データを作る場合も、互いに異なる参加／管理者パスワード、初期化Version、RCON無効を先に設定します。

Docker内でゲームをダウンロード・更新する起動処理はありません。`server/`をread-only、`server/ShooterGame/Saved/`を明示的なread-writeで重ねます。MOD資産もread-only側です。保存証拠とlifetime lockだけを別の限定mountで共有します。Bot設定・トークンはゲームへ渡しません。

## イメージ作成

```bash
python3 -m scripts.docker_setup build-runtime
python3 -m scripts.docker_setup build-bot
```

allowlistの`.dockerignore`がビルドコンテキストから実データ・秘密・履歴を除外します。LTSベースはdigest固定、BotのPython依存はversion/hash固定です。実際の配置はtagでなくimage IDに固定します。OSパッケージ一覧もイメージに記録します。aptリポジトリが可変なので、将来の再ビルドがbit単位で同一になるとは主張しません。

## 隔離検証→切替

**動いているサービスのソースを直接更新しないでください。** 現releaseで停止・退避してから変更するか、別release／別データコピーで候補を検証します。観測コードの変更はpolicyを失効させます。

1. 別データコピー・別container名・loopbackの別UDPポートで起動／保存／停止／再読込を検証する。
2. 実データのコピーでも同じ検証をする。コピーを本番へ書き戻さない。
3. 本番の人数を確認し、利用者へ保守を案内する。Botをdrainして停止する。
4. 共通CLIで保存確認付き停止を行う。未知の失敗ならここで中止。
5. `ark-discord.service`と`ark-control.service`が停止済みの状態で実施する。

```bash
python3 -m scripts.docker_setup activate-game --image sha256:REVIEWED_IMAGE_ID --confirm
```

コマンドは停止証拠を再検証し、Saved・unit・設定を私的に退避してから初回切替を承認します。**ゲームは起動せず、再起動／自動停止スイッチはfalseへ戻します。** 途中中断のmarkerが残れば起動を拒否し、記録を削除して迂回しません。既存Docker構成の上書き更新はこのコマンドでは行いません。

```bash
python3 scripts/arkctl.py start
python3 scripts/arkctl.py status
```

参加可能性・設定維持を確認後、0人の状態で確認付き再起動を試します。実ゲームのsignal 6、コンテナ134、systemd exit-code/134は別々の実際の記録です。0へ変換しません。

runtimeのlocaleは`C.UTF-8`です。元環境のlocaleと異なる場合、ゲーム自身がcrateの`ItemEntryName`などの表示名を翻訳してINIへ保存することがあります。byte一致と設定内容の一致を区別し、重複するUnreal配列も含めて前後を比較します。アイテム種類・数量・品質・認証・MOD順序などの差を、表示言語の変化として無条件に許可しないでください。停止receiptは引き続き全INIの実際のhashへ結び付きます。

## Botコンテナ

秘密を設定後、Botを止めた状態で配置します。

```bash
python3 -m scripts.docker_setup install-bot --image sha256:REVIEWED_BOT_IMAGE_ID --confirm
systemctl --user start ark-control.service
systemctl --user start ark-discord.service
```

Botにはconfig/token、同一受信器を防ぐlock、owner-only IPC directoryしかmountしません。Docker socket、systemd bus、Saved、ゲーム認証はmountしません。ホストIPCは通常ユーザーで共通Controllerを呼び、起動・確認済みrestart・status・間隔制限したidle checkだけを受け付けます。

新環境で検証が完了してから所有者が安全スイッチを変更し、BotとIPCを適切に再読込します。依存するゲームを暗黙に再起動しません。enableはその後の明示作業です。ゲームは通常disabledのまま、Discordからオンデマンドで起動できます。
